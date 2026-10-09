"""Google Calendar integration: OAuth + "cancel before renewal" reminders.

First run opens a browser for the OAuth consent screen; the resulting
token is cached in ``token.json`` (gitignored) and refreshed silently on
later runs. Requires ``credentials.json`` (a Google Cloud "Desktop app"
OAuth client) next to the project root — see README for setup steps.

Design decision: events are all-day (``date``, not ``dateTime``) so they
appear at the top of the day *before* the renewal, with the amount and
account URL in the description.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from parser import SubscriptionInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = PROJECT_ROOT / "token.json"
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

SETUP_HINT = (
    "Google Calendar is not configured yet.\n"
    "  1. Create a free Google Cloud project and enable the Calendar API:\n"
    "     https://console.cloud.google.com/apis/library/calendar-googleapis.com\n"
    "  2. Create OAuth client credentials (type: Desktop app) and download the\n"
    "     JSON as 'credentials.json' into this project folder.\n"
    "  3. Re-run the command.\n"
    "Full walkthrough: README.md -> 'Google Calendar setup'."
)


class CalendarNotConfigured(RuntimeError):
    """Raised when OAuth credentials/token are missing or unusable."""


def _credentials_file() -> Path:
    raw = os.getenv("GOOGLE_CALENDAR_CREDENTIALS", "credentials.json")
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _calendar_id() -> str:
    return os.getenv("GOOGLE_CALENDAR_ID", "primary") or "primary"


def _authenticated_credentials():
    """Return valid credentials, running the OAuth flow when needed."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    if creds and creds.valid:
        return creds

    creds_file = _credentials_file()
    if not creds_file.exists():
        raise CalendarNotConfigured(
            f"No OAuth credentials found at {creds_file}.\n{SETUP_HINT}"
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES)
    print("\n  Opening your browser for Google Calendar authorization...")
    creds = flow.run_local_server(port=0, prompt="consent")
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    print(f"[OK] Authorization saved to {TOKEN_FILE.name}")
    return creds


def _service():
    from googleapiclient.discovery import build

    try:
        return build(
            "calendar", "v3", credentials=_authenticated_credentials(), cache_discovery=False
        )
    except CalendarNotConfigured:
        raise
    except Exception as exc:  # e.g. wrong client type, revoked consent
        raise CalendarNotConfigured(f"Calendar authentication failed: {exc}") from exc


def build_calendar_event(title: str, start_date: date, end_date: date) -> dict[str, Any]:
    """Build an all-day event spanning start_date..end_date (inclusive)."""
    return {
        "summary": title,
        "description": (
            "Created by SubShield terminal event entry.\n"
            f"Subscription: {start_date.isoformat()} to {end_date.isoformat()}"
        ),
        "start": {"date": start_date.isoformat()},
        # Google's all-day end date is exclusive.
        "end": {"date": (end_date + timedelta(days=1)).isoformat()},
        "transparency": "transparent",
        "reminders": {"useDefault": True},
    }


def create_calendar_event(
    title: str,
    start_date: date,
    end_date: date,
    *,
    dry_run: bool = False,
) -> dict[str, Any] | None:
    """Create a user-entered all-day event after the caller confirms it."""
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")

    event = build_calendar_event(title, start_date, end_date)
    calendar_id = _calendar_id()

    if dry_run:
        print("\n  [dry-run] Would create this Google Calendar event:")
        print(f"    Title      : {title}")
        print(f"    Dates      : {start_date.isoformat()} to {end_date.isoformat()} (all-day)")
        print(f"    Calendar   : {calendar_id}")
        return None

    try:
        created = _service().events().insert(calendarId=calendar_id, body=event).execute()
    except Exception as exc:
        raise CalendarNotConfigured(f"Could not create the calendar event: {exc}") from exc

    print(f"\n[OK] Event added to calendar '{calendar_id}'.")
    link = created.get("htmlLink")
    if link:
        print(f"  View: {link}")
    return created


def reminder_date_for(renewal_date: str, days_before: int) -> date:
    """Return the reminder date = renewal date minus *days_before* days."""
    renewal = date.fromisoformat(renewal_date)
    reminder = renewal - timedelta(days=max(days_before, 0))
    if reminder < date.today():
        print(f"  [warn] Reminder date {reminder.isoformat()} is in the past - it will still be created for the record.")
    return reminder


def build_event_body(info: SubscriptionInfo, reminder: date, *, notes: str = "") -> dict[str, Any]:
    """Assemble the Calendar event payload (all-day event)."""
    amount = f"{info.currency} {info.amount}".strip()
    description = "\n".join(
        line
        for line in (
            "SubShield automatic-cancellation reminder.",
            f"Service : {info.vendor or '(unknown)'}",
            f"Amount   : {amount}" if amount else "Amount   : free trial",
            f"Renews   : {info.renewal_date}",
            f"Account  : {info.account_url}" if info.account_url else None,
            notes or None,
        )
        if line
    )
    return {
        "summary": f"Cancel {info.vendor or 'subscription'} before renewal",
        "description": description,
        "start": {"date": reminder.isoformat()},
        "end": {"date": (reminder + timedelta(days=1)).isoformat()},
        "transparency": "transparent",  # don't mark the day as busy
        "reminders": {"useDefault": True},
    }


def create_renewal_marker(info: SubscriptionInfo, *, dry_run: bool = False, notes: str = "") -> dict[str, Any] | None:
    """'Keep' path: mark the renewal date itself as an all-day event.

    Used when the user decides NOT to cancel - the calendar then reminds them
    that the subscription renews on that date ("Renew <vendor>").
    """
    renewal = date.fromisoformat(info.renewal_date)
    amount = f"{info.currency} {info.amount}".strip()
    description = "\n".join(
        line
        for line in (
            "SubShield renewal reminder (subscription kept - not cancelled).",
            f"Service : {info.vendor or '(unknown)'}",
            f"Amount   : {amount}" if amount else None,
            f"Renews   : {info.renewal_date}",
            f"Account  : {info.account_url}" if info.account_url else None,
            notes or None,
        )
        if line
    )
    event = {
        "summary": f"Renew {info.vendor or 'subscription'} on {info.renewal_date}",
        "description": description,
        "start": {"date": info.renewal_date},
        "end": {"date": (renewal + timedelta(days=1)).isoformat()},
        "transparency": "transparent",
        "reminders": {"useDefault": True},
    }
    calendar_id = _calendar_id()
    if dry_run:
        print("\n  [dry-run] Would mark this renewal in Google Calendar:")
        print(f"    Title      : {event['summary']}")
        print(f"    Date       : {info.renewal_date} (all-day)")
        print(f"    Calendar   : {calendar_id}")
        return None

    service = _service()
    try:
        existing = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=f"{info.renewal_date}T00:00:00Z",
                timeMax=f"{(renewal + timedelta(days=1)).isoformat()}T00:00:00Z",
                singleEvents=True,
            )
            .execute()
            .get("items", [])
        )
        for item in existing:
            if item.get("summary") == event["summary"]:
                print(f"\n[OK] Renewal marker already on the calendar for {info.renewal_date} - skipped (no duplicate).")
                link = item.get("htmlLink")
                if link:
                    print(f"  View: {link}")
                return item
    except Exception:  # noqa: BLE001 - dedupe is best-effort
        pass

    created = service.events().insert(calendarId=calendar_id, body=event).execute()
    print(f"\n[OK] Renewal marked for {info.renewal_date} on calendar '{calendar_id}' (subscription kept).")
    link = created.get("htmlLink")
    if link:
        print(f"  View: {link}")
    return created


def create_reminder_event(info: SubscriptionInfo, *, days_before: int = 3, dry_run: bool = False, notes: str = "") -> dict[str, Any] | None:
    """Schedule the reminder. Returns the created event dict, or ``None`` on dry-run."""
    reminder = reminder_date_for(info.renewal_date, days_before)
    event = build_event_body(info, reminder, notes=notes)
    calendar_id = _calendar_id()

    if dry_run:
        print("\n  [dry-run] Would create this Google Calendar event:")
        print(f"    Title      : {event['summary']}")
        print(f"    Date       : {reminder.isoformat()} (all-day)")
        print(f"    Calendar   : {calendar_id}")
        print(f"    Description: {event['description']!r}")
        return None

    service = _service()

    # Idempotency: skip if an identical reminder already exists on that day.
    try:
        existing = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=f"{reminder.isoformat()}T00:00:00Z",
                timeMax=f"{(reminder + timedelta(days=1)).isoformat()}T00:00:00Z",
                singleEvents=True,
            )
            .execute()
            .get("items", [])
        )
        for item in existing:
            if item.get("summary") == event["summary"]:
                print(f"\n[OK] Reminder already on the calendar for {reminder.isoformat()} - skipped (no duplicate).")
                link = item.get("htmlLink")
                if link:
                    print(f"  View: {link}")
                return item
    except Exception:  # noqa: BLE001 - dedupe is best-effort; never block insertion
        pass

    try:
        created = service.events().insert(calendarId=calendar_id, body=event).execute()
    except Exception as exc:
        raise CalendarNotConfigured(f"Could not create the calendar event: {exc}") from exc

    print(f"\n[OK] Reminder scheduled for {reminder.isoformat()} on calendar '{calendar_id}'.")
    link = created.get("htmlLink")
    if link:
        print(f"  View: {link}")
    return created
