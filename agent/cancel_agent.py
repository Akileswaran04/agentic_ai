"""Phase 3 — AI browser agent that attempts the cancellation.

Wraps ``browser-use`` + Playwright with Gemini (via browser-use's
``ChatGoogle``) as the model backend.

Design / safety model
---------------------
The run is split into two phases, each an :class:`Agent` run that reuses the
*same* headed ``BrowserSession`` (``keep_alive=True`` keeps the window open
between phases):

1. **Recon phase** — navigate, log in with the user's live credentials,
   decline retention offers, and stop *immediately before* the final
   irreversible "confirm cancel" click. In ``--dry-run`` mode this is the
   whole run: the agent reports the plan and never clicks confirm.
2. **Confirm phase** — started only after an explicit human approval (or
   ``--auto-confirm``): click the final confirmation, then verify that the
   cancellation actually went through.

Credentials are passed through browser-use's ``sensitive_data`` mechanism as
placeholders (``<secret>email</secret>``), so the raw values never appear in
prompts or on-disk logs.

Every agent step is screenshotted into ``runs/<timestamp>/screenshots/`` and
each phase writes ``phase_<n>_log.json`` plus a final-state screenshot.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from parser import SubscriptionInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CancellationFlowError(RuntimeError):
    """Raised for environment problems (e.g. browser engine missing)."""


# ── Small env helpers ─────────────────────────────────────────


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# ── Prompt building ───────────────────────────────────────────

SAFETY_RULES = """\
CRITICAL SAFETY RULES - non-negotiable:
1. Your ONLY goal is to cancel this subscription. Never enter or submit
   payment, billing or card details. Never make a purchase, upgrade or start
   a new plan/trial. Never download or install anything.
2. Always decline retention offers, discounts, surveys and upsells. Keep
   choosing the option that moves TOWARD cancellation ("Continue to cancel",
   "No thanks", "Cancel subscription", etc.).
3. Enter no personal information beyond the provided login credentials. If a
   form asks for anything else, stop and report.
4. If a CAPTCHA, 2FA/OTP code or security challenge appears that the human
   owner must solve, do NOT guess or brute-force: stop and report STATUS: blocked.
5. When in doubt, stop and report rather than click."""


def _login_instruction(has_credentials: bool) -> str:
    if has_credentials:
        return (
            "Login credentials are available to you as sensitive-data placeholders: "
            "type <secret>email</secret> for the username/email and "
            "<secret>password</secret> for the password wherever the login form asks."
        )
    return (
        "No credentials were supplied. If the account is already logged in, "
        "continue. Otherwise STOP and report STATUS: blocked (credentials required)."
    )


def _describe_subscription(info: SubscriptionInfo) -> str:
    amount = f"{info.currency} {info.amount}".strip()
    parts = [info.vendor or "the subscription"]
    if amount:
        parts.append(f"({amount} per renewal period")
        if info.renewal_date:
            parts.append(f", renewing {info.renewal_date}")
        parts.append(")")
    elif info.renewal_date:
        parts.append(f"(free trial, next renewal {info.renewal_date})")
    return " ".join(parts)


def _phase1_task(info: SubscriptionInfo, has_credentials: bool) -> str:
    return f"""\
Help cancel {_describe_subscription(info)}.

Account URL: {info.account_url or "<unknown — inspect current page>"}
{_login_instruction(has_credentials)}

Steps:
1. Go to the account URL. If a login screen appears, log in with the provided
   credentials.
2. Find the account/settings/billing area and locate the subscription or plan.
3. Start the cancellation flow. Decline every retention offer, survey and
   upsell you encounter.
4. Proceed until you are standing at the FINAL, IRREVERSIBLE confirmation
   step - the button or screen that permanently cancels the subscription.
   Do NOT click it. Do not go past it.
5. Stop there and write your report.

{SAFETY_RULES}

Your final report must be concise and include: the exact steps you took, what
the final confirmation screen/button says (quote its text), and the current
page URL.

End your final message with exactly one status line (last line of your reply):
- STATUS: reached_final_confirm   (you stopped right before the final irreversible cancel step)
- STATUS: blocked                 (a human is needed: 2FA/OTP, CAPTCHA, missing credentials, or a wall you cannot pass)
- STATUS: done                    (the task finished some other way)"""


def _phase2_task(info: SubscriptionInfo, has_credentials: bool) -> str:
    return f"""\
Continue the cancellation run for {_describe_subscription(info)}.

The human has EXPLICITLY approved clicking the final cancel button. Your job
is to finish and verify the cancellation:
1. The browser window from the previous phase is still open - continue from
   the current page/tab. If the session expired or a fresh page is needed,
   log in again using the provided credentials (
   {_login_instruction(has_credentials)} ).
2. If you are not yet at the final irreversible confirmation step, keep going
   through the cancellation flow (declining every retention offer) until you
   reach it.
3. Click the final confirmation / "cancel subscription" button. If an extra
   "are you sure?" step appears, confirm the cancellation.
4. Wait for the result and verify it: quote visible page text proving the
   subscription is cancelled (e.g. "subscription cancelled", "plan ends on",
   no active subscription).
5. Take no other actions afterwards.

{SAFETY_RULES}

Your final report: what you clicked, the visible confirmation text, and the
current page URL.

End your final message with exactly one status line (last line of your reply):
- STATUS: cancelled   (the page confirms the cancellation went through)
- STATUS: blocked     (a human is needed: 2FA/OTP, CAPTCHA, or cancellation could not be completed)
- STATUS: done        (something else happened - explain)"""


_STATUS_RE = re.compile(r"\bSTATUS:\s*([a-z_]+)\s*$", re.IGNORECASE | re.MULTILINE)

_CANCELLED_HINTS = (
    "subscription has been cancelled",
    "subscription has been canceled",
    "successfully cancelled",
    "successfully canceled",
    "subscription is cancelled",
    "has been cancelled",
)


def _looks_cancelled(message: str) -> bool:
    lowered = message.lower()
    return any(hint in lowered for hint in _CANCELLED_HINTS)


def _parse_status(message: str) -> str:
    """Extract the trailing STATUS: marker the model was asked to emit."""
    if not message:
        return ""
    matches = _STATUS_RE.findall(message)
    return matches[-1].lower() if matches else ""


# ── Result / log models ───────────────────────────────────────


@dataclass
class PhaseOutcome:
    """Outcome of a single agent phase, ready to serialize to JSON."""

    phase: int
    status: str  # reached_final_confirm | blocked | done | cancelled | error
    message: str = ""
    url: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    final_screenshot: str = ""  # filename relative to the run dir
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "status": self.status,
            "message": self.message,
            "url": self.url,
            "step_count": len(self.steps),
            "final_screenshot": self.final_screenshot,
            "error": self.error,
            "steps": self.steps,
        }


def _serializable(value: Any) -> Any:
    """Best-effort JSON-safe conversion (drops non-serializable details)."""
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


# ── Runner ────────────────────────────────────────────────────


class _CancellationRunner:
    """Executes agent phases against one persistent BrowserSession."""

    def __init__(
        self,
        *,
        session: Any,  # browser_use BrowserSession (kept alive between phases)
        info: SubscriptionInfo,
        credentials: dict[str, str] | None,
        run_dir: Path,
        llm: Any,  # browser-use chat model (Gemini or NVIDIA), built by llm_provider
        max_steps: int,
    ) -> None:
        self._session = session
        self._info = info
        self._credentials = credentials or None
        self._run_dir = run_dir
        self._llm = llm
        self._max_steps = max_steps
        self._screenshots_dir = run_dir / "screenshots"
        self._screenshots_dir.mkdir(parents=True, exist_ok=True)
        self._phase_steps: list[dict[str, Any]] = []
        self._phase_number = 0

    async def _on_step(self, state_summary: Any, agent_output: Any, step_number: int) -> None:
        """Auto-capture a screenshot + summary of every agent step."""
        entry: dict[str, Any] = {"step": step_number}
        try:
            entry["url"] = getattr(state_summary, "url", "")
            entry["title"] = getattr(state_summary, "title", "")
            if agent_output is not None:
                actions = []
                for action in getattr(agent_output, "action", None) or []:
                    try:
                        actions.append(_serializable(action.model_dump(mode="json")))
                    except Exception:  # pragma: no cover - defensive
                        actions.append(str(action))
                entry["actions"] = actions
            shot_path = self._screenshots_dir / f"phase{self._phase_number}_step{step_number:03d}.png"
            await self._session.take_screenshot(path=str(shot_path))
            entry["screenshot"] = shot_path.name
        except Exception as exc:  # never let logging break the agent
            entry["error"] = f"screenshot failed: {exc}"
        self._phase_steps.append(entry)

    async def run_phase(self, phase_number: int, task: str) -> PhaseOutcome:
        """Run one agent phase; returns a serializable outcome."""
        self._phase_number = phase_number
        self._phase_steps = []

        from browser_use import Agent  # lazy: module imports without install

        agent_kwargs: dict[str, Any] = {}
        provider = getattr(self._llm, "provider", "")
        model_name = str(getattr(self._llm, "model", "") or "").lower()
        # OpenAI-compatible backends (e.g. NVIDIA): trim per-run overhead - no
        # judge pass and no planning structure means fewer model calls and
        # snappier runs on text models.
        if provider == "openai":
            agent_kwargs["enable_planning"] = False
            agent_kwargs["use_judge"] = False
            # Text-only NVIDIA models reject screenshot image parts (HTTP 400);
            # let the agent reason from the page's DOM instead. Vision-capable
            # models (name contains 'vision') and Gemini keep vision enabled.
            if "vision" not in model_name:
                agent_kwargs["use_vision"] = False
        agent = Agent(
            task=task,
            llm=self._llm,
            browser_session=self._session,
            sensitive_data=self._credentials,
            register_new_step_callback=self._on_step,
            **agent_kwargs,
        )

        history = None
        error: str | None = None
        try:
            history = await agent.run(max_steps=self._max_steps)
        except Exception as exc:  # noqa: BLE001 - surface gracefully
            error = str(exc)
        # NOTE: Agent.run() closes the agent itself; keep_alive=True on the
        # BrowserSession keeps the actual browser window open for the user.

        final_message = ""
        url = ""
        try:
            if history is not None:
                final_message = str(history.final_result() or "")
                urls = history.urls()
                url = urls[-1] if urls else ""
        except Exception:  # noqa: BLE001
            pass

        status = "error" if error else _parse_status(final_message) or ("done" if history else "")
        # Keep only human-meaningful statuses.
        if status not in {"reached_final_confirm", "blocked", "done", "cancelled", "error"}:
            status = "done"
        # Fallback: some models mangle the trailing STATUS: line even after a
        # successful cancellation - recognize obvious confirmation phrasing.
        if status in ("done", "") and final_message and _looks_cancelled(final_message):
            status = "cancelled"

        final_shot = self._run_dir / f"phase{phase_number}_final.png"
        try:
            await self._session.take_screenshot(path=str(final_shot))
        except Exception:  # noqa: BLE001
            final_shot = None

        if error and not status:
            status = "error"

        outcome = PhaseOutcome(
            phase=phase_number,
            status=status,
            message=final_message,
            url=url,
            steps=list(self._phase_steps),
            final_screenshot=final_shot.name if final_shot and final_shot.exists() else "",
            error=error or "",
        )
        self._save_phase_log(outcome)
        return outcome

    def _save_phase_log(self, outcome: PhaseOutcome) -> None:
        log_path = self._run_dir / f"phase{outcome.phase}_log.json"
        log_path.write_text(
            json.dumps(outcome.to_dict(), indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )


# ── Public async entrypoint ───────────────────────────────────


async def _default_ask(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


async def run_cancellation_flow(
    info: SubscriptionInfo,
    *,
    run_dir: Path,
    credentials: dict[str, str] | None = None,
    dry_run: bool = False,
    auto_confirm: bool = False,
    ask: Callable[[str], Awaitable[str]] | None = None,
    model: str | None = None,
    api_key: str | None = None,
    max_steps: int | None = None,
    headless: bool | None = None,
) -> list[PhaseOutcome]:
    """Run the full cancellation flow and return per-phase outcomes.

    - ``dry_run``: only the recon phase runs — nothing is ever clicked.
    - otherwise the recon phase runs first and, after human approval (or
      ``auto_confirm``), the confirm phase clicks the final button.
    """
    from browser_use import BrowserSession  # lazy import

    import llm_provider

    ask = ask or _default_ask
    model = model or llm_provider.model()
    api_key = api_key or llm_provider.api_key()
    if not api_key:
        raise CancellationFlowError(
            f"No API key configured for {llm_provider.provider_display()}.\n{llm_provider.setup_hint()}"
        )
    max_steps = max_steps or _env_int("AGENT_MAX_STEPS", 40)
    headless = _env_bool("BROWSER_HEADLESS", False) if headless is None else headless

    # Optional domain lock-down (hardening against prompt-injection sites).
    allowed_domains: list[str] | None = None
    if _env_bool("LOCK_DOMAINS", False):
        host = _host_of(info.account_url)
        if host:
            allowed_domains = [host, f"*.{host}"]

    run_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir = run_dir / "downloads"
    downloads_dir.mkdir(exist_ok=True)

    session = BrowserSession(
        headless=headless,
        keep_alive=True,
        window_size={"width": 1366, "height": 900},
        downloads_path=str(downloads_dir),
        allowed_domains=allowed_domains,
    )

    outcomes: list[PhaseOutcome] = []
    try:
        chat_llm = llm_provider.browser_chat(model_name=model, key=api_key)
        runner = _CancellationRunner(
            session=session,
            info=info,
            credentials=credentials,
            run_dir=run_dir,
            llm=chat_llm,
            max_steps=max_steps,
        )

        # Phase 1 — recon, stop before the final click.
        phase1 = await runner.run_phase(1, _phase1_task(info, bool(credentials)))
        outcomes.append(phase1)

        if dry_run:
            return outcomes  # never proceeds to the confirm phase

        # Decide whether the human wants to authorize the final click.
        should_confirm = bool(auto_confirm)
        if not should_confirm and phase1.status != "error":
            if phase1.status == "reached_final_confirm":
                guidance = "The agent says it is standing right before the final cancel button."
            elif phase1.status == "blocked":
                guidance = (
                    "The agent stopped because a human step is needed (2FA/OTP/CAPTCHA, "
                    "or it lacks credentials). If you just completed that step in the "
                    "open browser window, it can continue."
                )
            else:
                guidance = "The agent did not clearly reach the final cancel step — inspect the screenshots."
            print("\n" + "=" * 70)
            print(guidance)
            print("=" * 70)
            answer = (await ask("\nType YES to let the agent click the final cancel button, or NO to stop here: ")).strip()
            should_confirm = answer.upper() in {"YES", "Y"}

        if should_confirm:
            phase2 = await runner.run_phase(2, _phase2_task(info, bool(credentials)))
            outcomes.append(phase2)
            # Copy the last state as the canonical proof screenshot.
            if phase2.final_screenshot:
                src = run_dir / phase2.final_screenshot
                if src.exists():
                    try:
                        shutil.copyfile(src, run_dir / "confirmation.png")
                    except OSError:  # pragma: no cover - best effort
                        pass
    finally:
        try:
            await session.close()
        except Exception:  # pragma: no cover - cleanup only
            pass

    return outcomes


def _host_of(url: str) -> str:
    from urllib.parse import urlparse

    if not url:
        return ""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return (parsed.hostname or "").lower()


def summarize(outcomes: list[PhaseOutcome]) -> str:
    """Human-readable summary of the flow's outcomes."""
    if not outcomes:
        return "No agent phases ran."
    lines = []
    for outcome in outcomes:
        status_text = {
            "reached_final_confirm": "stopped before the final cancel click",
            "blocked": "needs human action (2FA / credentials / wall)",
            "cancelled": "cancellation confirmed",
            "done": "finished",
            "error": f"error: {outcome.error or 'unknown'}",
        }.get(outcome.status, outcome.status)
        lines.append(f"  Phase {outcome.phase}: {status_text}")
    last = outcomes[-1]
    if last.message:
        lines.append("")
        lines.append("  Agent report:")
        for line in (last.message or "").splitlines():
            lines.append(f"    {line}")
    return "\n".join(lines)
