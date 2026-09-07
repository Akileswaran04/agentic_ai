"""Parser package facade.

Pipeline: raw document text (PDF / EML / TXT) -> optional Gemini
structuring -> validated :class:`SubscriptionInfo`. If no API key is
configured, or confidence is low, the user can enter/edit fields
interactively.
"""
from __future__ import annotations

import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from parser.extract_email import extract_text as extract_email_text
from parser.extract_pdf import extract_text as extract_pdf_text

SUPPORTED_EXTENSIONS = (".pdf", ".eml", ".txt")

_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR", "kr": "SEK"}


class UnsupportedFileError(ValueError):
    """Raised when the input file type can't be parsed."""


@dataclass
class SubscriptionInfo:
    """Normalized details of a subscription found in a document."""

    vendor: str = ""
    amount: str = ""  # bare number, e.g. "9.99"; "" for free trials
    currency: str = ""  # ISO code, e.g. "USD"
    renewal_date: str = ""  # ISO YYYY-MM-DD
    account_url: str = ""
    confidence: float = 0.0  # 0.0 when manually entered
    source_file: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SubscriptionInfo":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


# ── Document text extraction ──────────────────────────────────


def parse_document(path: str | Path) -> str:
    """Return raw text from a .pdf / .eml / .txt file."""
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf_text(file_path)
    if suffix in (".eml", ".txt"):
        return extract_email_text(file_path)
    raise UnsupportedFileError(
        f"Unsupported file type {suffix!r}. Supported: {', '.join(SUPPORTED_EXTENSIONS)}"
    )


# ── Normalization / validation ────────────────────────────────


def _normalize_date(raw: Any) -> str:
    """Best-effort conversion of a model-provided date to YYYY-MM-DD."""
    if not raw:
        return ""
    value = str(raw).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value  # already ISO
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _normalize_amount_and_currency(amount: Any, currency: Any) -> tuple[str, str]:
    """Split a combined value like 'US$9.99' into ('9.99', 'USD')."""
    raw_amount = str(amount or "").strip()
    raw_currency = str(currency or "").strip().upper()

    # Pull symbol out of the amount string.
    for symbol, code in _CURRENCY_SYMBOLS.items():
        if symbol in raw_amount and not raw_currency:
            raw_currency = code
    # Keep only digits and one decimal separator.
    match = re.search(r"\d+(?:[.,]\d+)?", raw_amount)
    cleaned = match.group(0).replace(",", ".") if match else ""
    if cleaned and not raw_currency:
        raw_currency = "USD"  # safest default when only a number is present
    return cleaned, raw_currency


def validate_from_llm(data: dict[str, Any], *, source_file: str = "") -> tuple[SubscriptionInfo, list[str]]:
    """Map an LLM JSON dict onto a :class:`SubscriptionInfo`.

    Returns the normalized info plus a list of problems found. Problems
    lower confidence so the caller can require human confirmation.
    """
    issues: list[str] = []

    def _string(key: str) -> str:
        return str(data.get(key) or "").strip()

    vendor = _string("vendor")
    if not vendor:
        issues.append("No vendor/service name extracted.")

    raw_date = _normalize_date(data.get("renewal_date"))
    if raw_date:
        try:
            date.fromisoformat(raw_date)  # validate
        except ValueError:
            issues.append(f"Renewal date '{raw_date}' is not a valid calendar date.")
            raw_date = ""
    else:
        issues.append("No renewal date extracted.")

    amount, currency = _normalize_amount_and_currency(
        data.get("amount"), data.get("currency")
    )

    account_url = _string("account_url")
    if account_url and not account_url.startswith(("http://", "https://")):
        account_url = "https://" + account_url

    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
    if issues:
        confidence = min(confidence, 0.5)

    return (
        SubscriptionInfo(
            vendor=vendor,
            amount=amount,
            currency=currency,
            renewal_date=raw_date,
            account_url=account_url,
            confidence=round(confidence, 2),
            source_file=source_file,
        ),
        issues,
    )


# ── Interactive manual entry ──────────────────────────────────


def _ask(prompt_text: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"  {prompt_text}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(130)
    return answer or default


def manual_entry(defaults: SubscriptionInfo | None = None, *, source_file: str = "") -> SubscriptionInfo:
    """Interactive form letting the user type/confirm every field."""
    d = defaults or SubscriptionInfo(source_file=source_file)
    print("\nEnter subscription details (press Enter to keep the shown default):")
    vendor = _ask("Service/vendor", d.vendor)
    amount = _ask("Amount at next renewal (digits only, e.g. 9.99; blank if free)", d.amount)
    currency = _ask("Currency code (e.g. USD)", d.currency)
    renewal = _ask("Renewal date (YYYY-MM-DD)", d.renewal_date)
    url = _ask("Account/login URL", d.account_url)

    while True:
        try:
            date.fromisoformat(renewal)
            break
        except ValueError:
            print(f"  [warn] '{renewal}' is not a valid date - use YYYY-MM-DD.")
            renewal = _ask("Renewal date (YYYY-MM-DD)", renewal)

    return SubscriptionInfo(
        vendor=vendor,
        amount=amount,
        currency=currency,
        renewal_date=renewal,
        account_url=url,
        confidence=1.0,
        source_file=source_file,
    )


def pretty_print(info: SubscriptionInfo) -> None:
    """Render extracted info for the console."""
    print("  Vendor        :", info.vendor or "(unknown)")
    amount = f"{info.currency} {info.amount}".strip() if info.currency or info.amount else "(free trial - no amount)"
    print("  Amount        :", amount)
    print("  Renewal date  :", info.renewal_date or "(unknown)")
    print("  Account URL   :", info.account_url or "(unknown)")
    print("  Confidence    :", f"{info.confidence:.0%}" if info.confidence else "manual entry")
