"""Turn messy invoice/email text into structured JSON via an LLM.

Default backend is Google Gemini (official ``google-genai`` SDK); set
``LLM_PROVIDER=nvidia`` in .env to use NVIDIA's OpenAI-compatible NIM API
instead. The model is asked to return **only** JSON matching
:data:`EXTRACTION_SCHEMA`, with all dates resolved against today's date
(emails say things like "your trial renews in 7 days" and the model must
convert that to ``YYYY-MM-DD``).

Rate-limit handling: both free tiers return 429 when quota is hit, so calls
are retried with exponential backoff.
"""
from __future__ import annotations

import json
import logging
import random
import time
from datetime import date
from typing import Any

log = logging.getLogger(__name__)

EXTRACTION_SCHEMA: dict[str, Any] = {
    "vendor": "Service/company name, e.g. 'Spotify'",
    "amount": "Numeric renewal amount only, e.g. '9.99'. Use '' if free trial with no card.",
    "currency": "ISO 4217 currency code, e.g. 'USD'",
    "renewal_date": "Next renewal date as YYYY-MM-DD",
    "account_url": "URL where the user logs in to manage/cancel the account",
    "confidence": "Your confidence 0.0-1.0 that these fields are correct",
}

_SYSTEM_PROMPT = """You extract subscription details from raw invoice/trial emails.
Respond with ONLY a JSON object, no prose, no markdown fences. Schema:
{
  "vendor": string,
  "amount": string,
  "currency": string,
  "renewal_date": "YYYY-MM-DD",
  "account_url": string,
  "confidence": number between 0 and 1
}

Rules:
- renewal_date MUST be an actual ISO date (YYYY-MM-DD). Convert relative
  wording ("renews in 7 days", "trial ends Feb 3") using today's date given below.
- If the amount is only implied (e.g. "$0.00, then $X/month") report the
  amount that will be charged at the NEXT renewal, or "" when the trial is free.
- amount is the bare number without the currency symbol.
- account_url: the most likely login URL for this service (usually the
  homepage or an explicit link in the email). "" if truly unknown.
- If any field is missing or unknowable use "" and lower confidence.
- confidence < 0.5 if the renewal date or vendor cannot be determined."""


def _extract_json(text: str) -> dict[str, Any]:
    """Parse JSON out of a model reply, tolerating stray whitespace/fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Strip optional ```json ... ``` fences.
        cleaned = cleaned.split("```", 2)[1] if "```" in cleaned[3:] else cleaned
        cleaned = cleaned.strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # Last resort: find the outermost {...} block.
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        parsed = json.loads(cleaned[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError(f"Model did not return a JSON object: {text[:200]!r}")
    return parsed


def _is_transient(exc: Exception) -> bool:
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    message = str(exc).upper()
    if code in (429, 500, 502, 503, 504):
        return True
    return any(tag in message for tag in ("RESOURCE_EXHAUSTED", "RATE_LIMIT", "UNAVAILABLE", "HIGH DEMAND", "429"))


def _call_gemini_with_backoff(client, *, model: str, prompt: str, attempts: int = 4) -> str:
    """Gemini generate_content with retries on transient/rate-limit errors."""
    from google.genai import types  # lazy import keeps CLI importable pre-install

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )
            return (response.text or "").strip()
        except Exception as exc:  # noqa: BLE001 - retry policy below
            last_error = exc
            if not _is_transient(exc) or attempt == attempts:
                break
            delay = (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            log.warning("Gemini rate limited (attempt %s/%s); retrying in %.1fs", attempt, attempts, delay)
            time.sleep(delay)
    raise RuntimeError(
        f"Gemini request failed after {attempts} attempt(s): {last_error}"
    ) from last_error


def _call_nvidia_with_backoff(*, api_key: str, model: str, system: str, user: str, attempts: int = 4) -> str:
    """NVIDIA NIM (OpenAI-compatible) chat completion with retries."""
    from openai import OpenAI  # lazy import

    from llm_provider import NVIDIA_BASE_URL

    client = OpenAI(api_key=api_key, base_url=NVIDIA_BASE_URL)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.1,
            )
            return (response.choices[0].message.content or "").strip()
        except Exception as exc:  # noqa: BLE001 - retry policy below
            last_error = exc
            if not _is_transient(exc) or attempt == attempts:
                break
            delay = (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            log.warning("NVIDIA rate limited (attempt %s/%s); retrying in %.1fs", attempt, attempts, delay)
            time.sleep(delay)
    raise RuntimeError(
        f"NVIDIA request failed after {attempts} attempt(s): {last_error}"
    ) from last_error


def structure_text(text: str, *, api_key: str, model: str, today: date | None = None) -> dict[str, Any]:
    """Send *text* to Gemini and return the raw extracted JSON dict.

    Raises:
        RuntimeError: after retries, if the API call itself fails.
        ValueError: if the model returns unparseable or non-dict output.
    """
    if not text or not text.strip():
        raise ValueError("Cannot structure empty document text.")

    today = today or date.today()
    schema_preview = json.dumps(EXTRACTION_SCHEMA, indent=2)
    user_prompt = (
        f"Today's date: {today.isoformat()}\n\n"
        "Expected JSON keys and meaning:\n"
        f"{schema_preview}\n\n"
        "Document text to analyze:\n"
        "-------------------------------\n"
        f"{text[:40000]}\n"
        "-------------------------------\n"
        "Return ONLY the JSON object."
    )

    from llm_provider import is_nvidia

    if is_nvidia():
        reply = _call_nvidia_with_backoff(
            api_key=api_key,
            model=model,
            system=_SYSTEM_PROMPT,
            user=user_prompt,
        )
    else:
        from google import genai  # lazy import keeps CLI importable pre-install

        client = genai.Client(api_key=api_key)
        reply = _call_gemini_with_backoff(
            client, model=model, prompt=f"{_SYSTEM_PROMPT}\n\n{user_prompt}"
        )
    return _extract_json(reply)
