"""Run the 4-demo pack (or a single demo) end to end.

For each selected service:
  1. creates a REAL Google Calendar reminder 3 days before the renewal, and
  2. runs the browser agent against the service's local fake site through to
     a completed cancellation (screenshots + confirmation.png per run).

Run one at a time (separate command = separate run):
    python demo/run_demos.py --only streamify
    python demo/run_demos.py --only cloudpress
    python demo/run_demos.py --only fitbeam
    python demo/run_demos.py --only mailnest

Run all four in one go:
    python demo/run_demos.py

Safety: the sites are fictional and served from 127.0.0.1 - nothing real is
ever touched. Add --dry-run to skip calendar inserts and never click cancel.
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import http.server
import shutil
import socket
import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from demo.demo_manifest import DEMOS, ORDER  # noqa: E402
from parser import SubscriptionInfo  # noqa: E402
from agent import run_cancellation_flow, summarize  # noqa: E402
from scheduler import create_reminder_event  # noqa: E402

SITES_DIR = PROJECT_ROOT / "demo" / "sites"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # silence per-request logging
        pass


def _serve_sites() -> tuple[http.server.ThreadingHTTPServer, threading.Thread, int]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    handler = functools.partial(_QuietHandler, directory=str(SITES_DIR))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, port


async def _ask(prompt: str) -> str:
    print(f"\n  [demo] Human-in-the-loop gate -> {prompt}")
    print("  [demo] Scripted answer: YES (on the real tool you would type this)")
    return "YES"


def _run_one(key: str, d: dict, *, port: int, do_calendar: bool, dry_run: bool) -> tuple[str, str]:
    url = f"http://127.0.0.1:{port}/{key}/index.html"
    info = SubscriptionInfo(
        vendor=d["vendor"],
        amount=d["amount"],
        currency=d["currency"],
        renewal_date=d["renewal_date"],
        account_url=url,
        confidence=1.0,
        source_file=f"demo/invoices/{key}.eml",
    )
    print("\n" + "#" * 72)
    print(f"# {d['vendor']}  |  {d['currency']} {d['amount']}/mo  |  renews {d['renewal_date']}")
    print(f"# fake account page: {url}")
    print("#" * 72)

    run_dir = PROJECT_ROOT / "runs" / f"demo-{key}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)

    if do_calendar:
        try:
            create_reminder_event(info, dry_run=dry_run, notes=f"Demo run artifacts: {run_dir}")
        except Exception as exc:  # noqa: BLE001 - calendar is not the main event
            print(f"\n  [warn] Calendar step skipped: {exc}")
    else:
        print("\n  [demo] Calendar step skipped (--no-calendar).")

    if dry_run:
        print("\n  [demo] DRY-RUN: agent plans only, no cancel click, no calendar insert.\n")

    outcomes = asyncio.run(
        run_cancellation_flow(
            info,
            run_dir=run_dir,
            credentials=None,
            dry_run=dry_run,
            auto_confirm=False,
            ask=_ask,
            max_steps=25,
        )
    )
    print("\n" + "-" * 60)
    print(f"{d['vendor']} - run summary")
    print("-" * 60)
    print(summarize(outcomes))
    last = outcomes[-1]
    conf = run_dir / "confirmation.png"
    print(f"  Artifacts: {run_dir} | proof: {'confirmation.png' if conf.exists() else '(no confirm phase)'}")
    return key, last.status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the SubShield 4-demo pack.")
    parser.add_argument("--only", choices=ORDER, help="run a single demo (default: all four)")
    parser.add_argument("--no-calendar", action="store_true", help="skip the calendar reminder step")
    parser.add_argument("--dry-run", action="store_true", help="no calendar insert + agent never clicks cancel")
    args = parser.parse_args(argv)

    keys = [args.only] if args.only else list(ORDER)

    httpd, server_thread, port = _serve_sites()
    results: list[tuple[str, str]] = []
    try:
        for key in keys:
            status = _run_one(
                key,
                DEMOS[key],
                port=port,
                do_calendar=not args.no_calendar,
                dry_run=args.dry_run,
            )
            results.append((key, status))
    finally:
        httpd.shutdown()
        server_thread.join(timeout=2)

    print("\n" + "=" * 60)
    print("Pack summary")
    print("=" * 60)
    for key, status in results:
        print(f"  {DEMOS[key]['vendor']:<22} renews {DEMOS[key]['renewal_date']}  ->  {status}")
    ok = args.dry_run or all(status == "cancelled" for _, status in results)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
