# SubShield — How to demo it to your teacher

**Total time:** ~5-8 minutes. One command runs the whole thing; the magic is
explaining *what each tool does* as it happens.

---

## Before the demo (do this once, before class)

1. Open a terminal in the project folder and run each demo command **once** so
   that everything is warm:
   - The Gemini key is already in `.env` (no action).
   - Google Calendar is already authorized (`token.json` exists).
   - Chromium is already installed.
   - A browser may briefly open during a full run - that is *expected*, it is
     the AI agent working. Say so in advance so it isn't a surprise.
2. Practice the full command below end-to-end at least once.
3. **Internet is required** (Gemini + Google). If the room has no Wi-Fi, skip
   straight to the "offline fallback" at the bottom and show the saved
   artifacts in `runs/` instead.

---

## The one-command demo (the "wow")

```bash
python main.py --input demo/sample_trial.eml
```

A browser window opens. The terminal shows, in order:

**Tool 1 - Parser.** "Asking Gemini to structure the document..." then a table:
`Streamify | USD 9.99 | renews 2026-10-15 | https://example.com/streamify/account`,
confidence 98%.

> Say: *"The tool reads a messy invoice/email, and an LLM turns it into clean,
> structured JSON - vendor, price, renewal date, account URL - with a
> confidence score. I can accept, edit, or re-run it."*

Press `y` to accept.

**Tool 2 - Calendar.** "Reminder scheduled for 2026-10-12" + a Google Calendar link.

> Say: *"It creates a real event on my Google Calendar 3 days before the
> renewal, so I get reminded to cancel before I'm charged."*

**Tool 3 - Agent.** The agent asks for login (press Enter to skip - no
credentials in a demo). Then Chromium opens and the agent navigates, reasons
about each page, and screenshots every step.

> Say: *"This is an AI browser agent using the same Gemini model as a 'brain'.
> It logs in, finds the billing section, and walks toward cancellation -
> declining every 'please stay' upsell - and screenshots each step as proof."*

Because the sample URL is `example.com` (a fake service), the agent honestly
reports it cannot log in / finds nothing to cancel (`STATUS: blocked`) and
**never clicks anything**. That is the safety design working.

---

## Staging the demo (optional, more structured)

If you prefer showing each tool separately (good for Q&A):

```bash
# Tool 1 only - extraction + your confirmation
python main.py --input demo/sample_invoice.pdf --skip-calendar --skip-agent

# Tool 2 only - real calendar event (no browser)
python main.py --input demo/sample_trial.eml --skip-agent

# Tool 3 only - the agent (browser opens)
python main.py --manual --skip-calendar
```

The `--manual` run lets you type a subscription by hand, skipping Gemini - nice
for showing the agent without depending on the network.

---

## What to point at

- **The safety rules printed at agent start** - "never enter payment details,
  never click the final cancel without approval" - show the human-in-the-loop
  design.
- **The `runs/` folder after a run** - `subscription.json` (the structured
  record), `phase1_log.json` (every step the agent took), and PNG screenshots
  as proof.
- **`--dry-run`** - add it to any command to show the *safe* mode: prints the
  calendar event instead of inserting it, and the agent only plans, never
  clicks:
  ```bash
  python main.py --input demo/sample_trial.eml --dry-run
  ```
- The real event that lands on the calendar (open calendar.google.com and show
  `Cancel Streamify before renewal` on Oct 12).

---

## If something fails mid-demo (don't panic)

| Symptom | Fix |
|---|---|
| `503` / "high demand" from Gemini | Wait 10-20 s and re-run - the code auto-retries 4x with backoff, then falls back to manual entry. Blame it on free-tier spikes. |
| No browser opens / agent errors | `python -m playwright install chromium`, then re-run. |
| Calendar step says "not configured" | Skip it with `--skip-calendar` and explain it needs a Google Cloud OAuth file (a one-time setup). |
| Everything is broken / no internet | Show the offline fallback below. |

## Offline fallback (no internet)

You already have a **previous complete run** saved in the project:

```text
runs/20260905-154353-peakfit-pro/
```

Open it and walk through `subscription.json`, `phase1_log.json`, and the PNG
screenshots - real output from a real run, no network needed.

---

## Recording it

- **Windows built-in:** press `Win + Alt + R` (Xbox Game Bar) to record your
  screen, or `Win + Shift + S` for screenshots.
- **OBS** (free) if you want a cleaner capture with a mic.
- Record once, practice-run, then present the recording as a fallback video.

---

## Files in this folder

| File | Purpose |
|---|---|
| `sample_trial.eml` | Fictional trial-confirmation email (Streamify) |
| `sample_invoice.pdf` | Fictional invoice PDF (Streamify) |
| `make_sample_pdf.py` | Regenerates the PDF (`python demo/make_sample_pdf.py`) |

Both samples point at `example.com`, so the agent can never touch a real
service during the demo.
