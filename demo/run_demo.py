"""Visual demo driver: run the AI agent against the local fake cancellation site.

The agent genuinely navigates and clicks through the demo flow (declining a
retention offer, stopping at the final confirm), then completes the
cancellation after a scripted human "YES". Every step is screenshotted, and
``confirmation.png`` captures the final cancelled state.

The fake site is served over http://127.0.0.1 on a free port (browser agents
cannot reliably navigate file:// URLs), then shut down when the run ends.

Run:  python demo/run_demo.py
"""
from __future__ import annotations

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

from parser import SubscriptionInfo  # noqa: E402
from agent import run_cancellation_flow, summarize  # noqa: E402

SITE_DIR = PROJECT_ROOT / "demo" / "site"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # silence per-request logging
        pass


def _serve_site() -> tuple[http.server.ThreadingHTTPServer, threading.Thread]:
    """Serve demo/site over HTTP on a free local port."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    handler = functools.partial(_QuietHandler, directory=str(SITE_DIR))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread


async def _ask(prompt: str) -> str:
    print(f"\n  [demo] Human-in-the-loop gate -> {prompt}")
    print("  [demo] Scripted answer: YES (on the real tool you would type this)")
    return "YES"


def main() -> int:
    httpd, server_thread = _serve_site()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/index.html"
    print(f"Demo site served at: {url}")

    info = SubscriptionInfo(
        vendor="Streamify Premium",
        amount="9.99",
        currency="USD",
        renewal_date="2026-10-15",
        account_url=url,
        confidence=1.0,
        source_file="demo/site/index.html",
    )

    run_dir = PROJECT_ROOT / "runs" / "visual-demo"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)

    print("A real browser window will open - watch the AI agent work through")
    print("the cancellation flow step by step.\n")

    try:
        outcomes = asyncio.run(
            run_cancellation_flow(
                info,
                run_dir=run_dir,
                credentials=None,
                dry_run=False,      # allow the full flow on the FAKE site only
                auto_confirm=False,
                ask=_ask,           # scripted human approval at the gate
                max_steps=25,
            )
        )
    finally:
        httpd.shutdown()
        server_thread.join(timeout=2)

    print("\n" + "-" * 60)
    print("Demo run summary")
    print("-" * 60)
    print(summarize(outcomes))

    last = outcomes[-1]
    shots = sorted((run_dir / "screenshots").glob("*.png")) if (run_dir / "screenshots").exists() else []
    print(f"\nRun folder  : {run_dir}")
    print(f"Screenshots : {len(shots)} step image(s)")
    print(f"Phase logs  : {[p.name for p in run_dir.glob('phase*_log.json')]}")
    conf = run_dir / "confirmation.png"
    print(f"Proof shot  : {'confirmation.png' if conf.exists() else '(no confirm phase ran)'}")
    return 0 if last.status == "cancelled" else 1


if __name__ == "__main__":
    sys.exit(main())
