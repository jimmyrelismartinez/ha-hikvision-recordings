#!/usr/bin/env python3
"""Run tests/frontend/test_fallback.html in a real browser and report the result.

The playback fallback logic lives in the browser, so asserting on it needs a
browser — a Python mock of app.js would only be testing the mock. This serves the
repo over HTTP (relative URLs matter: see the ingress rule at the top of app.js),
drives headless Chrome over CDP, and exits non-zero if any case failed.

Skips cleanly when no Chrome is installed, so `pytest -q` still passes on a box
without one.
"""
from __future__ import annotations

import http.server
import json
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
PAGE = "/tests/frontend/test_fallback.html"
CHROME_CANDIDATES = (
    "/opt/google/chrome/chrome",
    "google-chrome",
    "chromium",
    "chromium-browser",
)


def find_chrome() -> str | None:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    return None


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(REPO), **kwargs)

    def log_message(self, *args):  # keep the test output readable
        pass


def main() -> int:
    chrome_bin = find_chrome()
    if not chrome_bin:
        print("SKIP: no Chrome/Chromium found")
        return 0
    try:
        import websockets.sync.client as wsc
    except ImportError:
        print("SKIP: websockets not installed (pip install -r requirements-dev.txt)")
        return 0

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as httpd:
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        profile = REPO / ".pytest_cache" / "chrome-frontend"
        chrome = subprocess.Popen(
            [chrome_bin, "--headless=new", "--no-sandbox", "--disable-gpu",
             "--disable-dev-shm-usage", "--remote-debugging-port=9334",
             f"--user-data-dir={profile}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(60):
                try:
                    urllib.request.urlopen("http://127.0.0.1:9334/json/version", timeout=1).read()
                    break
                except Exception:
                    time.sleep(0.5)
            else:
                print("FAIL: chrome never came up")
                return 1

            url = f"http://127.0.0.1:{port}{PAGE}"
            tab = json.loads(urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:9334/json/new?{url}", method="PUT")).read())

            with wsc.connect(tab["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024) as ws:
                counter = [0]

                def send(method, params=None):
                    counter[0] += 1
                    mid = counter[0]
                    ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                    while True:
                        reply = json.loads(ws.recv())
                        if reply.get("id") == mid:
                            return reply

                send("Runtime.enable")
                deadline = time.time() + 120
                result = None
                while time.time() < deadline:
                    raw = send("Runtime.evaluate", {
                        "expression": "JSON.stringify(window.RESULT || {})",
                        "returnByValue": True,
                    })["result"]["result"].get("value")
                    if raw:
                        result = json.loads(raw)
                        if result.get("done"):
                            break
                    time.sleep(0.5)

                log = send("Runtime.evaluate", {
                    "expression": "document.getElementById('log').textContent",
                    "returnByValue": True,
                })["result"]["result"].get("value") or ""
                print(log.rstrip())

                if not result or not result.get("done"):
                    print("FAIL: harness never finished")
                    return 1
                print(f"\n{result['passed']} passed, {result['failed']} failed")
                return 1 if result["failed"] else 0
        finally:
            chrome.terminate()
            try:
                chrome.wait(timeout=10)
            except subprocess.TimeoutExpired:
                chrome.kill()


if __name__ == "__main__":
    sys.exit(main())
