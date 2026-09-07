#!/usr/bin/env python3
"""SubShield — automatic subscription & free-trial canceler (local MVP CLI).

Chains three tools for one subscription:
  1. Parser      — extract renewal date / cost / vendor / account URL
                   from an invoice PDF or trial email (.pdf/.eml/.txt),
                   structured by Gemini (or entered manually).
  2. Scheduler   — add a "cancel before renewal" reminder to Google Calendar.
  3. Agent       — launch a Gemini-driven browser agent that attempts the
                   cancellation, screenshotting every step.

Examples:
  python main.py --input invoice.pdf
  python main.py --input trial_email.eml --dry-run
  python main.py --input invoice.pdf --skip-calendar --auto-confirm
  python main.py --manual
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

import llm_provider  # noqa: E402

from agent import run_cancellation_flow, summarize  # noqa: E402
from parser import (  # noqa: E402
    SubscriptionInfo,
    UnsupportedFileError,
    manual_entry,
    parse_document,
    pretty_print,
    validate_from_llm,
)
from scheduler import CalendarNotConfigured, create_calendar_event, create_reminder_event, reminder_date_for  # noqa: E402

BANNER = r"""
  SubShield — automatic subscription & free-trial canceler (local MVP)
"""

DISCLAIMER = (
    "  NOTE: personal-use tool. Automating logins/actions on third-party sites may\n"
    "  violate their Terms of Service. Credentials are entered live and never stored.\n"
)


def _hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(f"  {title}")
    print("=" * 72)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="subshield",
        description="Extract a subscription's renewal details and attempt to cancel it.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "stage toggles: --no-llm / --skip-calendar / --skip-agent\n"
            "safe mode:     --dry-run  (no calendar insert, agent never clicks cancel)"
        ),
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--input", metavar="FILE", help="invoice PDF or trial email (.pdf/.eml/.txt)")
    source.add_argument("--manual", action="store_true", help="skip file parsing; type the details by hand")
    source.add_argument("--event", action="store_true", help="enter a calendar event in the terminal")

    parser.add_argument("--no-llm", action="store_true", help="never call the LLM; use manual entry instead")
    parser.add_argument("--skip-calendar", action="store_true", help="don't schedule the Google Calendar reminder")
    parser.add_argument("--skip-agent", action="store_true", help="don't launch the browser cancellation agent")
    parser.add_argument("--dry-run", action="store_true", help="safe mode: print the calendar event, let the agent plan but never click cancel")
    parser.add_argument("--auto-confirm", action="store_true", help="skip the human approval before the final 'cancel' click (use with care)")
    parser.add_argument("--account-email", metavar="EMAIL", default=None, help="pre-fill the login email for the agent (password is still prompted live)")
    parser.add_argument("--reminder-days", type=int, default=None, metavar="N", help="days before renewal for the reminder (default: REMINDER_DAYS_BEFORE env / 3)")
    parser.add_argument("--version", action="version", version="SubShield MVP 0.1.0")
    return parser


# ── Extraction (Tool 1) ───────────────────────────────────────


def _llm_configured() -> bool:
    return bool(llm_provider.api_key())


def _extract_with_llm(text: str, source_file: str) -> tuple[SubscriptionInfo, list[str]]:
    from parser.llm_structurer import structure_text

    raw = structure_text(text, api_key=llm_provider.api_key(), model=llm_provider.model())
    info, issues = validate_from_llm(raw, source_file=source_file)
    return info, issues


def _confirm_fields(info: SubscriptionInfo, *, can_rerun: bool, rerun) -> SubscriptionInfo:
    """Show extracted fields; let the user accept, edit or re-extract."""
    threshold = _env_float("CONFIDENCE_THRESHOLD", 0.6)
    while True:
        pretty_print(info)
        low_confidence = info.confidence > 0 and info.confidence < threshold
        if low_confidence:
            print(f"\n  [info] Confidence is {info.confidence:.0%} (below {threshold:.0%}) — double-check the fields.")
        options = "[y] accept, [e] edit" + (", [r] re-run extraction" if can_rerun else "")
        choice = input(f"\n  Looks right? {options}: ").strip().lower()
        if choice in {"y", "yes", ""}:
            return info
        if choice in {"e", "edit"}:
            return manual_entry(info, source_file=info.source_file)
        if choice in {"r", "rerun"} and can_rerun:
            try:
                info, _issues = rerun()
            except Exception as exc:  # noqa: BLE001
                print(f"\n  [error] Re-extraction failed: {exc}")
            continue
        print("  Please answer y, e" + (", r" if can_rerun else "") + ".")


def _prompt_event() -> tuple[str, date, int] | None:
    """Collect and confirm a simple all-day calendar event from the terminal."""
    print("\nEnter calendar event details:")
    title = input("  Event name: ").strip()
    if not title:
        print("  [cancelled] Event name cannot be empty.")
        return None

    while True:
        raw_date = input("  Start date (YYYY-MM-DD): ").strip()
        try:
            start_date = date.fromisoformat(raw_date)
            break
        except ValueError:
            print("  [warn] Use a valid date in YYYY-MM-DD format.")

    while True:
        raw_duration = input("  Duration in days (e.g. 1): ").strip()
        try:
            duration_days = int(raw_duration)
            if duration_days < 1:
                raise ValueError
            break
        except ValueError:
            print("  [warn] Duration must be a whole number of at least 1 day.")

    end_date = start_date.fromordinal(start_date.toordinal() + duration_days - 1)
    print("\n  Review event:")
    print(f"    Name     : {title}")
    print(f"    Calendar date: {end_date.isoformat()} (all-day)")
    print(f"    Duration : {duration_days} day(s)")
    choice = input("\n  Accept and add to Google Calendar? [a]ccept/[r]eject: ").strip().lower()
    if choice not in {"a", "accept", "y", "yes"}:
        print("  [rejected] No calendar event was created.")
        return None
    return title, start_date, duration_days


def run_event_entry(*, dry_run: bool) -> int:
    """Run the standalone terminal-to-Google-Calendar event flow."""
    _hr("Terminal calendar event")
    event = _prompt_event()
    if event is None:
        return 0

    title, start_date, duration_days = event
    try:
        create_calendar_event(title, start_date, duration_days, dry_run=dry_run)
    except (CalendarNotConfigured, ValueError) as exc:
        print(f"\n  [error] {exc}")
        return 1
    return 0


def run_extraction(args) -> SubscriptionInfo:
    _hr("Tool 1 of 3 — Extract subscription details")

    if args.manual or not args.input:
        if args.input:
            print("  (--manual not set but no --input given — using manual entry)")
        info = manual_entry()
        return info

    source_file = args.input
    print(f"  Parsing: {source_file}")
    try:
        text = parse_document(source_file)
    except UnsupportedFileError as exc:
        print(f"  [error] {exc}")
        sys.exit(1)
    except (FileNotFoundError, ValueError) as exc:
        print(f"  [error] {exc}")
        sys.exit(1)
    print(f"  Extracted {len(text)} characters of document text.")

    can_use_llm = _llm_configured() and not args.no_llm
    if not can_use_llm:
        reason = "--no-llm given" if args.no_llm else f"no key for {llm_provider.provider_display()} in .env"
        print(f"\n  [info] Skipping LLM structuring ({reason}). Falling back to manual entry.")
        print(f"  [info] {llm_provider.setup_hint()}")
        return manual_entry(source_file=source_file)

    print(f"  Asking {llm_provider.provider_display()} ({llm_provider.model()}) to structure the document...")

    def _rerun() -> tuple[SubscriptionInfo, list[str]]:
        return _extract_with_llm(text, source_file=source_file)

    try:
        info, issues = _rerun()
    except Exception as exc:  # noqa: BLE001 — API errors shouldn't kill the session
        print(f"\n  [error] {llm_provider.provider_display()} extraction failed: {exc}")
        print("  Falling back to manual entry.")
        return manual_entry(source_file=source_file)

    for issue in issues:
        print(f"  [warn] {issue}")
    return _confirm_fields(info, can_rerun=True, rerun=lambda: _rerun())


# ── Scheduler (Tool 2) ────────────────────────────────────────


def run_scheduler(info: SubscriptionInfo, args, run_dir: Path) -> bool:
    """Schedule the reminder. Returns True on success, False if not configured."""
    _hr("Tool 2 of 3 — Google Calendar reminder")

    if args.skip_calendar:
        print("  Skipped (--skip-calendar).")
        return True
    if not info.renewal_date:
        print("  [warn] No renewal date available — cannot schedule a reminder.")
        return True

    days_before = args.reminder_days if args.reminder_days is not None else _env_int("REMINDER_DAYS_BEFORE", 3)
    reminder = reminder_date_for(info.renewal_date, days_before)
    print(f"  Renewal {info.renewal_date} -> reminder {reminder.isoformat()} ({days_before} day(s) before).")

    try:
        create_reminder_event(
            info,
            days_before=days_before,
            dry_run=args.dry_run,
            notes=f"Evidence/logs for this run: {run_dir}",
        )
    except CalendarNotConfigured as exc:
        print(f"\n  [warn] Calendar step skipped: {exc}")
        return False
    return True


# ── Agent (Tool 3) ────────────────────────────────────────────


def _prompt_credentials(prefill_email: str | None) -> dict[str, str] | None:
    import getpass

    print("\n  Agent login credentials — entered live, kept in memory, never stored.")
    print("  They are passed to the browser agent as redacted placeholders.")
    if prefill_email:
        email = input(f"  Email/username [{prefill_email}]: ").strip() or prefill_email
    else:
        email = input("  Email/username (Enter to skip — you will log in manually): ").strip()
    if not email:
        print("  (No credentials — the agent will pause for a manual login if needed.)")
        return None
    password = ""
    while not password:
        password = getpass.getpass("  Password: ")
    return {"email": email, "password": password}


async def _console_ask(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


def run_agent(info: SubscriptionInfo, args, run_dir: Path) -> tuple[bool, list]:
    """Run the browser cancellation flow. Returns (completed, outcomes)."""
    _hr("Tool 3 of 3 — Browser cancellation agent")

    api_key = llm_provider.api_key()
    if not api_key:
        print(f"\n  [error] No API key configured for {llm_provider.provider_display()} — the browser agent needs one to think.")
        print(f"  {llm_provider.setup_hint()}")
        return False, []

    if not info.account_url:
        print("\n  [warn] No account URL was extracted. The agent needs a URL to start from.")
        url = input("  Account/login URL (Enter to skip the agent): ").strip()
        if not url:
            print("  Skipping the agent.")
            return False, []
        info.account_url = url if "://" in url else f"https://{url}"

    creds = _prompt_credentials(args.account_email)

    if args.dry_run:
        print("\n  [dry-run] The agent will navigate and plan the cancellation but will NOT")
        print("  click the final cancel button. Watch the browser window.")
    elif args.auto_confirm:
        print("\n  [auto-confirm] The agent may click the final cancel button without asking.")
    else:
        print("\n  The agent will stop right before the final 'cancel' click and ask for your")
        print("  approval. A browser window will open — complete any 2FA there if prompted.")

    try:
        outcomes = asyncio.run(
            run_cancellation_flow(
                info,
                run_dir=run_dir,
                credentials=creds,
                dry_run=args.dry_run,
                auto_confirm=args.auto_confirm,
                ask=_console_ask,
                model=llm_provider.model(),
                api_key=api_key,
            )
        )
    except Exception as exc:  # noqa: BLE001 — e.g. Playwright not installed
        print(f"\n  [error] Browser agent failed to start: {exc}")
        print("  Hint: install the browser engine with:  python -m playwright install chromium")
        return False, []

    print("\n" + "-" * 72)
    print("Agent run summary")
    print("-" * 72)
    print(summarize(outcomes))
    return True, outcomes


# ── Orchestration ─────────────────────────────────────────────


def _make_run_dir(info: SubscriptionInfo) -> Path:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", (info.vendor or "subscription").lower()).strip("-")[:40] or "subscription"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = PROJECT_ROOT / "runs" / f"{stamp}-{slug}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if sys.version_info < (3, 11):
        print("SubShield requires Python 3.11+.")
        return 1

    load_dotenv(PROJECT_ROOT / ".env")
    print(BANNER)
    print(DISCLAIMER)

    if args.event:
        return run_event_entry(dry_run=args.dry_run)

    if not args.input and not args.manual:
        _build_parser().error("provide --input FILE, or use --manual to type the details")

    # Tool 1 — extraction ---------------------------------------------------
    info = run_extraction(args)

    # Persist the parsed record for later review.
    run_dir = _make_run_dir(info)
    (run_dir / "subscription.json").write_text(
        json.dumps(info.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Tool 2 — calendar reminder -------------------------------------------
    calendar_ok = run_scheduler(info, args, run_dir)

    # Tool 3 — browser cancellation agent -----------------------------------
    agent_ok: bool = True
    outcomes: list = []
    if args.skip_agent:
        print("\n  Tool 3 skipped (--skip-agent).")
    else:
        agent_ok, outcomes = run_agent(info, args, run_dir)

    # Wrap-up -----------------------------------------------------------------
    _hr("Run complete")
    print(f"  Artifacts saved under: {run_dir}")
    print(f"    subscription.json  — extracted record")
    print(f"    screenshots/       — per-step screenshots of the agent run")
    print(f"    phase_*_log.json   — agent step/action logs")
    print(f"    confirmation.png   — final page (when the cancel click ran)")
    if outcomes and outcomes[-1].status == "cancelled":
        print("\n  Cancellation confirmed on the site. Double-check the confirmation.png")
        print("  and, if you want, remove the calendar reminder now that it's moot.")
    elif outcomes and outcomes[-1].status == "reached_final_confirm" and not args.dry_run:
        print("\n  The agent stopped before the final 'confirm cancel' click — nothing was")
        print("  cancelled. Re-run and approve the confirm step when asked to finish.")
    elif args.dry_run:
        print("\n  Dry-run finished — nothing was cancelled and no event was created.")

    if not agent_ok or not calendar_ok and not args.skip_calendar:
        print("\n  Some stages were skipped because setup is incomplete — see README.")
    return 0 if agent_ok else 1


if __name__ == "__main__":
    sys.exit(main())
