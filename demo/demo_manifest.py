"""Shared data for the 4-demo-invoice pack (single source of truth).

Used by ``make_demo_pack.py`` (generates invoices + fake sites) and
``run_demos.py`` (calendar + agent per service).
"""
from __future__ import annotations

# key -> subscription details. Renewal dates are THIS month (September 2026),
# a few weeks out, so reminders (3 days before) land in the future.
DEMOS: dict[str, dict] = {
    "streamify": {
        "vendor": "Streamify Premium",
        "plan": "Streamify Premium (monthly)",
        "amount": "9.99",
        "currency": "USD",
        "renewal_date": "2026-09-20",
        "renewal_text": "September 20, 2026",
        "payment": "Visa ending in 4242",
        "trial": "14-day free trial",
    },
    "cloudpress": {
        "vendor": "CloudPress Pro",
        "plan": "CloudPress Pro (monthly)",
        "amount": "19.99",
        "currency": "USD",
        "renewal_date": "2026-09-22",
        "renewal_text": "September 22, 2026",
        "payment": "Mastercard ending in 8801",
        "trial": "30-day free trial",
    },
    "fitbeam": {
        "vendor": "FitBeam Plus",
        "plan": "FitBeam Plus (monthly)",
        "amount": "12.99",
        "currency": "USD",
        "renewal_date": "2026-09-24",
        "renewal_text": "September 24, 2026",
        "payment": "Visa ending in 1234",
        "trial": "7-day free trial",
    },
    "mailnest": {
        "vendor": "MailNest",
        "plan": "MailNest (monthly)",
        "amount": "4.99",
        "currency": "USD",
        "renewal_date": "2026-09-26",
        "renewal_text": "September 26, 2026",
        "payment": "PayPal",
        "trial": "free starter month",
    },
}

ORDER = ["streamify", "cloudpress", "fitbeam", "mailnest"]
