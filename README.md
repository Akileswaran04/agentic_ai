# SubShield

**Automatic subscription & free-trial canceler — local CLI, free-tier services only.**

SubShield takes an invoice PDF or a trial-confirmation email, extracts the renewal
date / cost / vendor / account URL, schedules a Google Calendar reminder before the
renewal, then launches a Gemini-driven browser agent that attempts to **cancel** the
subscription — screenshotting every step along the way.

```
invoice.pdf ─► 1. Parser (pdfplumber/mailparser + Gemini) ─► structured JSON
email.eml  ─►                                                   │
                                                                ▼
                                         2. Calendar scheduler ─► reminder event
                                              (3 days before renewal)
                                                                ▼
                                         3. Browser agent (browser-use + Playwright
                                            + Gemini) ─► screenshots + confirmation.png
```

> ⚠️ **Personal-use only.** Automating logins/actions on third-party sites may violate
> their Terms of Service. SubShield is designed for your own subscriptions. Don't
> operate it as a hosted service, and don't point it at sites you don't own.

---

## Requirements

- Python **3.11+** (tested on 3.12)
- A free **Gemini API key**
- (optional) Google Cloud **Calendar API** credentials for reminders
- ~150 MB for the Playwright Chromium browser engine

Everything here is on free tiers — no paid APIs, no cloud accounts (browser-use runs
fully local).

## 1. Install

```bash
# create a virtualenv (recommended)
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate

pip install -r requirements.txt
python -m playwright install chromium
```

> The PDF/email parser is named `mail-parser` on PyPI and imports as `mailparser`.
> A common typo (`pip install mailparser`) installs nothing.

## 2. Gemini API setup (free)

1. Go to **https://aistudio.google.com/apikey** and sign in with a Google account.
2. Click **Create API key** (pick a Cloud project, or let it create a default one).
3. Copy the key.

```bash
cp .env.example .env      # then edit .env
```

```dotenv
GEMINI_API_KEY=your_key_here
GOOGLE_API_KEY=your_key_here        # same key; browser-use reads this one
GEMINI_MODEL=gemini-3.6-flash       # current-gen flash — older names (2.x) are
                                    # retired for new accounts; see ai.google.dev models
```

> Free-tier rate limits for Gemini change over time. Verify current caps at
> ai.google.dev/gemini-api/docs/rate-limits. SubShield retries with backoff on
> `429`/`RESOURCE_EXHAUSTED` for extraction; the browser agent's `ChatGoogle`
> retries internally.

## 3. Google Calendar setup (free, optional — needed for Tool 2)

1. Create a project (or reuse the one from step 2): **console.cloud.google.com**.
2. **APIs & Services → Library** → search **Google Calendar API** → **Enable**.
3. **APIs & Services → OAuth consent screen**:
   - User type **External**, app name e.g. "SubShield", your email.
   - Under *Test users* add your own Google account (required while the app is
     unverified).
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Desktop app** → Create.
   - **Download JSON** and save it as **`credentials.json`** in the project root.
5. First run of Tool 2 opens a browser for consent and caches the token in
   `token.json` (gitignored) for future runs.

Skip this setup and SubShield still works — it just warns and skips the reminder.

## 4. Usage

```bash
# Full flow: extract → calendar reminder → browser agent tries to cancel
python main.py --input invoice.pdf

# Trial email, safe mode (agent plans but never clicks cancel; no event created)
python main.py --input trial_email.eml --dry-run

# Everything by hand (no file, no Gemini key needed)
python main.py --manual

# Enter a normal all-day event from the terminal
python main.py --event

# Stage toggles / safety
python main.py --input invoice.pdf --skip-calendar --skip-agent   # extract only
python main.py --input invoice.pdf --auto-confirm                  # skip the approval prompt
python main.py --input invoice.pdf --no-llm                        # force manual fields
```

With `--event`, enter the event name, start date (`YYYY-MM-DD`), and duration in
days. The details are shown for review; choose `accept` to add one all-day event
on the calculated end date, or `reject` to exit without creating anything. Add
`--dry-run` to preview the Calendar payload without inserting it.

### What happens on a normal run

1. **Extract** — the document is parsed and Gemini returns structured JSON
   (`vendor`, `amount`, `currency`, `renewal_date`, `account_url`, `confidence`).
   Below the confidence threshold you're asked to confirm or edit the fields.
2. **Reminder** — an all-day Google Calendar event `Cancel <vendor> before renewal`
   is created `REMINDER_DAYS_BEFORE` (default 3) days before the renewal, with the
   amount and account URL in the description.
3. **Browser agent** — a visible Chromium window opens. You enter your login
   credentials **live** (never stored, passed as redacted placeholders). The agent:
   - logs in and navigates to billing/subscription settings,
   - starts the cancellation flow, declining all retention offers/surveys,
   - **stops right before the final "confirm cancel" click** and asks for your
     approval (2FA codes are for you to type into the open window),
   - on approval, clicks through, verifies the cancellation, and screenshots it.

### Human-in-the-loop (configurable)

| Mode | Final "cancel" click |
|---|---|
| default | Agent stops; **you approve** first |
| `--auto-confirm` | Clicked without a prompt (riskier) |
| `--dry-run` | Never clicked — plan + screenshots only |

### Credentials

- Prompts via `input`/`getpass` at runtime — never written to disk.
- Passed to browser-use as **sensitive-data placeholders** (`<secret>email</secret>`),
  so the raw values don't appear in conversation logs.
- Leave the email blank to run without credentials: the agent will pause
  (`STATUS: blocked`) and you can log in manually in the open browser window,
  then approve the continuation.

## 5. Output — `runs/<timestamp>-<vendor>/`

| File | Contents |
|---|---|
| `subscription.json` | The extracted record used by all tools |
| `screenshots/phase1_step*.png` | Screenshot of every agent step |
| `phase_1_log.json` / `phase_2_log.json` | Step log: URL, action, screenshot file |
| `phase*_final.png` | State of the page when a phase ended |
| `confirmation.png` | Final page after the cancel click ran |
| `downloads/` | Any files the site made the agent download |

`runs/` is gitignored by design (it can contain account screenshots).

## 6. Project layout

```
├── main.py                    # CLI entrypoint — chains the three tools
├── parser/
│   ├── extract_pdf.py         # pdfplumber text extraction
│   ├── extract_email.py       # mail-parser (.eml/.txt)
│   └── llm_structurer.py      # Gemini → strict JSON + rate-limit backoff
├── scheduler/
│   └── calendar_api.py        # Google Calendar OAuth + reminder event
├── agent/
│   └── cancel_agent.py        # browser-use Agent (2-phase, screenshotting)
├── runs/                      # per-run artifacts (gitignored)
├── requirements.txt
├── .env.example
└── README.md
```

## 7. Safety & known limitations

- **Hard rules baked into the agent prompt:** never pay, never enter card details,
  never upgrade/start trials, always decline retention offers, stop on 2FA/CAPTCHA.
- **Domain lock-down (hardening):** set `LOCK_DOMAINS=1` in `.env` to restrict the
  agent's navigation to the account site (guards against prompt-injection on
  malicious pages). Leave off if login redirects to a different auth domain.
- **CAPTCHA / bot detection:** some sites (especially streaming) block automated
  browsers. SubShield uses a visible (non-headless) Chromium, but expect graceful
  failure sometimes — the run log shows exactly how far it got.
- **2FA:** the agent never guesses codes. It pauses, you type the code into the
  open browser window, and the flow continues.
- **Rate limits:** Gemini free tier caps requests/day. Extraction uses 1 call per
  document (plus retries); a browser run uses several per step.
- **Scanned PDFs** (image-only) aren't OCR'd — re-export as text or paste the email.

## 8. Troubleshooting

| Problem | Fix |
|---|---|
| `mailparser` install fails | It's `mail-parser` on PyPI — see requirements.txt |
| Agent says browser failed to start | `python -m playwright install chromium` |
| `No Gemini API key` | Fill `GEMINI_API_KEY` in `.env` |
| Calendar step skipped with setup hint | Do the Google Cloud steps in §3, save `credentials.json` at project root |
| Extraction stuck / low confidence | Run with `--no-llm` and enter fields manually |
| Agent hits an OTP screen | Type the code in the open browser window, then answer the approval prompt |

## 9. Ideas beyond the MVP

- Gmail API auto-detection of trial confirmation emails
- Streamlit dashboard: upcoming renewals + one-click "cancel now"
- Fallback LLM backends (e.g. local Ollama) when Gemini quota is exhausted
- OCR fallback for scanned PDFs
- Optional keyring-backed credential storage
