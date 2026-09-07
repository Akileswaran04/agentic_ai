"""SubShield demo dashboard - one local web page for all 4 demo subscriptions.

For each service the page shows its renewal date and two actions:
  * Cancel - creates the calendar reminder (3 days before) and runs the browser
    agent to actually cancel the subscription on the service's local fake site.
  * Keep   - no agent; marks the renewal date itself in Google Calendar as a
    "Renew ... on <date>" event.

Everything runs locally: the fake sites are served from this same server, so
nothing real is ever touched. The agent opens a real browser window on this
machine while it works.

Run:  python demo/dashboard.py          (then open http://127.0.0.1:8756)
      python demo/dashboard.py --port 9000 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from demo.demo_manifest import DEMOS, ORDER  # noqa: E402
from parser import SubscriptionInfo  # noqa: E402
from scheduler import create_reminder_event, create_renewal_marker  # noqa: E402
from agent import run_cancellation_flow, summarize  # noqa: E402

ACCENTS = {
    "streamify": "#1db954",
    "cloudpress": "#2563eb",
    "fitbeam": "#f97316",
    "mailnest": "#7c3aed",
}

UI_FILE = PROJECT_ROOT / "demo" / "dashboard.html"
INVOICES_DIR = PROJECT_ROOT / "demo" / "invoices"
SITES_DIR = PROJECT_ROOT / "demo" / "sites"

_lock = threading.Lock()
STATUS: dict[str, dict] = {k: {"state": "idle"} for k in ORDER}


def _services_meta(port: int) -> list[dict]:
    base = f"http://127.0.0.1:{port}"
    out = []
    for key in ORDER:
        d = DEMOS[key]
        out.append(
            {
                "key": key,
                "vendor": d["vendor"],
                "plan": d["plan"],
                "amount": d["amount"],
                "currency": d["currency"],
                "renewal_date": d["renewal_date"],
                "renewal_text": d["renewal_text"],
                "accent": ACCENTS[key],
                "site_url": f"{base}/sites/{key}/index.html",
                "invoice_url": f"{base}/invoices/{key}.eml",
            }
        )
    return out


def _info_for(key: str, port: int) -> SubscriptionInfo:
    d = DEMOS[key]
    return SubscriptionInfo(
        vendor=d["vendor"],
        amount=d["amount"],
        currency=d["currency"],
        renewal_date=d["renewal_date"],
        account_url=f"http://127.0.0.1:{port}/sites/{key}/index.html",
        confidence=1.0,
        source_file=f"demo/invoices/{key}.eml",
    )


async def _scripted_ask(prompt: str) -> str:
    print(f"  [dashboard] Human gate -> {prompt}")
    print("  [dashboard] Approving (you clicked 'Cancel' on the dashboard).")
    return "YES"


def _run_cancel(key: str, port: int, *, dry_run: bool) -> None:
    d = DEMOS[key]
    info = _info_for(key, port)
    run_dir = PROJECT_ROOT / "runs" / f"demo-{key}"
    run_dir.mkdir(parents=True, exist_ok=True)

    def _note(msg: str) -> None:
        with _lock:
            STATUS[key].update({"state": "running", "step": msg})

    _note(f"Creating calendar reminder for {d['renewal_date']}...")
    try:
        create_reminder_event(info, dry_run=dry_run, notes=f"Demo run artifacts: {run_dir}")
        with _lock:
            STATUS[key]["calendar"] = "reminder scheduled"
    except Exception as exc:  # noqa: BLE001
        with _lock:
            STATUS[key]["calendar"] = f"warning: {exc}"

    _note("Launching the browser agent...")
    try:
        outcomes = asyncio.run(
            run_cancellation_flow(
                info,
                run_dir=run_dir,
                credentials=None,
                dry_run=dry_run,
                auto_confirm=False,
                ask=_scripted_ask,
                max_steps=25,
            )
        )
    except Exception as exc:  # noqa: BLE001
        with _lock:
            STATUS[key].update({"state": "error", "error": str(exc), "step": "agent failed to start"})
        return

    last = outcomes[-1]
    shots = sorted((run_dir / "screenshots").glob("*.png")) if (run_dir / "screenshots").exists() else []
    with _lock:
        STATUS[key].update(
            {
                "state": "done",
                "agent_status": last.status,
                "summary": summarize(outcomes),
                "screenshots": len(shots),
                "run_dir": str(run_dir),
                "proof": (run_dir / "confirmation.png").exists(),
                "step": "finished",
            }
        )


def _run_keep(key: str, *, dry_run: bool) -> dict:
    d = DEMOS[key]
    info = _info_for(key, 0)  # port unused by calendar path
    try:
        create_renewal_marker(info, dry_run=dry_run)
        return {"ok": True, "state": "kept", "message": f"Renewal marked on {d['renewal_date']}."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "state": "error", "message": str(exc)}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"))

    # -- helpers --------------------------------------------------------
    @property
    def _port(self) -> int:
        return self.server.server_address[1]

    def _subpath(self, prefix: str) -> list[str]:
        parsed = urlparse(self.path)
        parts = [unquote(p) for p in parsed.path.split("/") if p]
        if parts and parts[0] == prefix:
            return parts[1:]
        return []

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    # -- GET ------------------------------------------------------------
    def do_GET(self) -> None:
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            html = UI_FILE.read_text(encoding="utf-8")
            html = html.replace(
                "__SERVICES__",
                json.dumps(_services_meta(self._port)),
            )
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path == "/api/status":
            with _lock:
                self._json({"services": STATUS})
            return

        if path == "/api/events":
            self._json(self._calendar_events())
            return

        parts = self._subpath("sites")
        if len(parts) == 2 and parts[1] == "index.html" and parts[0] in ORDER:
            self._serve_file(SITES_DIR / parts[0] / "index.html", "text/html; charset=utf-8")
            return

        parts = self._subpath("invoices")
        if len(parts) == 1 and parts[0].endswith(".eml"):
            self._serve_file(INVOICES_DIR / parts[0], "text/plain; charset=utf-8")
            return

        self._send(404, b"not found")

    def _serve_file(self, path: Path, ctype: str) -> None:
        try:
            self._send(200, path.read_bytes(), ctype)
        except OSError:
            self._send(404, b"not found")

    def _calendar_events(self) -> dict:
        try:
            from scheduler.calendar_api import _service

            service = _service()
        except Exception as exc:  # noqa: BLE001
            return {"configured": False, "note": str(exc)[:200], "events": []}
        try:
            now = datetime.now(timezone.utc).isoformat()
            later = (datetime.now(timezone.utc) + timedelta(days=120)).isoformat()
            items = (
                service.events()
                .list(
                    calendarId="primary",
                    timeMin=now,
                    timeMax=later,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
                .get("items", [])
            )
        except Exception as exc:  # noqa: BLE001
            return {"configured": True, "note": str(exc)[:200], "events": []}
        events = []
        for e in items:
            summary = e.get("summary", "")
            if summary.startswith(("Cancel ", "Renew ")):
                events.append(
                    {
                        "date": e["start"].get("date", e["start"].get("dateTime", "")[:10]),
                        "summary": summary,
                        "link": e.get("htmlLink", ""),
                    }
                )
        return {"configured": True, "events": sorted(events, key=lambda x: x["date"])}

    # -- POST -----------------------------------------------------------
    def do_POST(self) -> None:
        path = urlparse(self.path).path
        body = self._read_body()

        if path.startswith("/api/cancel/"):
            key = path.rsplit("/", 1)[-1]
            if key not in ORDER:
                self._json({"ok": False, "message": "unknown service"}, 404)
                return
            with _lock:
                if STATUS[key].get("state") == "running":
                    self._json({"ok": False, "message": f"{key} is already running"}, 409)
                    return
                STATUS[key] = {"state": "running", "step": "queued"}
            dry_run = bool(body.get("dry_run")) or self._dry_run
            t = threading.Thread(target=_run_cancel, args=(key, self._port), kwargs={"dry_run": dry_run}, daemon=True)
            t.start()
            self._json({"ok": True, "message": f"cancellation started for {key}"})
            return

        if path.startswith("/api/keep/"):
            key = path.rsplit("/", 1)[-1]
            if key not in ORDER:
                self._json({"ok": False, "message": "unknown service"}, 404)
                return
            self._json(_run_keep(key, dry_run=self._dry_run))
            return

        self._json({"ok": False, "message": "unknown endpoint"}, 404)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SubShield demo dashboard")
    parser.add_argument("--port", type=int, default=8756)
    parser.add_argument("--dry-run", action="store_true", help="no calendar inserts + agent never clicks cancel")
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)

    handler = type("C", (_Handler,), {"_dry_run": args.dry_run})
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print("SubShield demo dashboard running:")
    print(f"  http://{args.host}:{args.port}")
    print("  4 demo subscriptions (fictional services, local fake sites only).")
    print("  Cancel = calendar reminder + AI agent cancels on the fake site (browser opens).")
    print("  Keep   = marks the renewal date in Google Calendar (no agent).")
    if args.dry_run:
        print("  DRY-RUN: no calendar inserts, agent plans but never clicks.")
    print("  Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
