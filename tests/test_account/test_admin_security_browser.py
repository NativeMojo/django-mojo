"""Opt-in real-Chrome proof for the packaged Admin Security workspace.

Run with ``--extra slow`` and set ``MOJO_ADMIN_CHROME`` to an exact
Chrome/Chromium executable.  The harness never discovers or downloads a
browser implicitly.
"""

import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

from testit import helpers as th
import websocket


TESTIT_TIER = "admin"
ROOT = Path(__file__).resolve().parents[2]
DEADLINE_SECONDS = 30


def _port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _json(url, deadline):
    last = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.4) as response:
                return json.loads(response.read())
        except Exception as error:
            last = error
            time.sleep(0.05)
    raise AssertionError(f"browser harness deadline expired waiting for {url}: {type(last).__name__}")


class CDP:
    def __init__(self, url):
        self.socket = websocket.create_connection(
            url, timeout=2, origin="http://127.0.0.1",
            suppress_origin=True)
        self.sequence = 0
        self.failures = []

    def close(self):
        self.socket.close()

    def call(self, method, params=None):
        self.sequence += 1
        identity = self.sequence
        self.socket.send(json.dumps({"id": identity, "method": method,
                                     "params": params or {}}))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            message = json.loads(self.socket.recv())
            event = message.get("method")
            if event == "Runtime.exceptionThrown":
                self.failures.append(message["params"]["exceptionDetails"].get("text", "exception"))
            elif event == "Log.entryAdded" and message["params"]["entry"].get("level") == "error":
                self.failures.append(message["params"]["entry"].get("text", "console error"))
            if message.get("id") == identity:
                assert "error" not in message, f"CDP {method} failed: {message.get('error')}"
                return message.get("result", {})
        raise AssertionError(f"CDP command deadline expired: {method}")

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True,
            "awaitPromise": True, "userGesture": True,
        })
        assert "exceptionDetails" not in result, result.get("exceptionDetails")
        return result.get("result", {}).get("value")

    def wait(self, expression, label):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.evaluate(expression):
                return
            time.sleep(0.05)
        raise AssertionError(f"Chrome did not render {label} before the deadline")


def _stop(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


@th.django_unit_test("real Chrome proves Admin Security navigation and interaction")
@th.requires_extra("slow")
def test_admin_security_real_chrome(opts):
    executable = os.environ.get("MOJO_ADMIN_CHROME", "")
    assert executable and Path(executable).is_file(), (
        "Set MOJO_ADMIN_CHROME to an explicit Chrome/Chromium executable; "
        "the opt-in harness does not auto-discover or download browsers.")
    preview_port = _port()
    debug_port = _port()
    preview = malformed_preview = chrome = None
    cdp = None
    with tempfile.TemporaryDirectory(prefix="mojo-admin-security-chrome-") as profile:
        try:
            preview = subprocess.Popen([
                sys.executable, str(ROOT / "bin/admin_preview"),
                "--port", str(preview_port), "--security-state", "full",
            ], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True)
            _json(f"http://127.0.0.1:{preview_port}/api/account/admin/bootstrap",
                  time.monotonic() + DEADLINE_SECONDS)
            chrome = subprocess.Popen([
                executable, "--headless=new", "--no-first-run", "--no-default-browser-check",
                "--disable-background-networking", "--disable-component-update",
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={debug_port}", f"--user-data-dir={profile}",
                "--remote-allow-origins=*",
                f"http://127.0.0.1:{preview_port}/v2/#/security-operations",
            ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            targets = _json(f"http://127.0.0.1:{debug_port}/json/list",
                            time.monotonic() + DEADLINE_SECONDS)
            page = next((item for item in targets if item.get("type") == "page"
                         and f":{preview_port}/v2/" in item.get("url", "")), None)
            assert page and page.get("webSocketDebuggerUrl"), "Chrome exposed no inspectable page target"
            cdp = CDP(page["webSocketDebuggerUrl"])
            cdp.call("Runtime.enable")
            cdp.call("Log.enable")
            cdp.call("Page.enable")
            cdp.wait("document.querySelector('h1')?.textContent === 'Security'", "Security heading")

            tabs = cdp.evaluate("[...document.querySelectorAll('.section-tabs button')].map(x=>x.textContent.trim())")
            assert tabs == ["Overview", "Cases", "Incidents & events", "Rules",
                            "Firewall & IPSets", "Recommendations"], tabs
            assert cdp.evaluate("location.hash") == "#/security-operations", "initial hash drifted"

            cdp.evaluate("[...document.querySelectorAll('.section-tabs button')].find(x=>x.textContent.includes('Cases')).click()")
            cdp.wait("location.hash.includes('tab=cases') && !!document.querySelector('input[aria-label=\"Filter cases\"]')", "Cases route")
            cdp.evaluate("[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='Next').click()")
            cdp.wait("document.body.textContent.includes('page 2 of 2')", "case paging")
            cdp.evaluate("(()=>{const x=document.querySelector('input[aria-label=\"Filter cases\"]');x.value='no-such-case';x.dispatchEvent(new Event('input',{bubbles:true}));return true})()")
            cdp.wait("document.body.textContent.includes('No cases match this filter')", "case filtering")
            cdp.evaluate("(()=>{const x=document.querySelector('input[aria-label=\"Filter cases\"]');x.value='';x.dispatchEvent(new Event('input',{bubbles:true}));return true})()")
            cdp.evaluate("document.querySelector('tbody tr').click()")
            cdp.wait("document.body.textContent.includes('Bounded evidence samples')", "bounded case detail")
            cdp.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Escape", "code": "Escape"})
            cdp.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Escape", "code": "Escape"})

            cdp.call("Emulation.setDeviceMetricsOverride", {
                "width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": False})
            assert cdp.evaluate("document.documentElement.scrollWidth <= 390"), "narrow viewport overflows"
            for theme in ("light", "dark"):
                cdp.call("Emulation.setEmulatedMedia", {"features": [{
                    "name": "prefers-color-scheme", "value": theme}]})
                cdp.evaluate(f"document.documentElement.dataset.theme='{theme}'")
                assert cdp.evaluate("getComputedStyle(document.body).backgroundColor") != "rgba(0, 0, 0, 0)", f"{theme} theme lost its canvas"
                png = base64.b64decode(cdp.call("Page.captureScreenshot", {
                    "format": "png", "captureBeyondViewport": False})["data"])
                assert png.startswith(b"\x89PNG") and len(png) > 1000, f"{theme} screenshot was invalid"
                (Path(profile) / f"security-{theme}.png").write_bytes(png)
            cdp.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Tab", "code": "Tab"})
            cdp.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Tab", "code": "Tab"})
            assert cdp.evaluate("document.activeElement !== document.body"), "keyboard focus did not enter the workspace"
            malformed_port = _port()
            malformed_preview = subprocess.Popen([
                sys.executable, str(ROOT / "bin/admin_preview"),
                "--port", str(malformed_port), "--security-state", "malformed",
            ], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True)
            _json(f"http://127.0.0.1:{malformed_port}/api/account/admin/bootstrap",
                  time.monotonic() + DEADLINE_SECONDS)
            cdp.call("Page.navigate", {"url":
                f"http://127.0.0.1:{malformed_port}/v2/#/security-operations"})
            cdp.wait("document.body.textContent.includes('security section is malformed')",
                     "malformed contract")
            assert not cdp.evaluate(
                "!!document.querySelector('.security-body table, .security-row-actions')"), (
                    "malformed Security data enabled rows or governed actions")
            time.sleep(0.1)
            cdp.evaluate("true")  # Drain any trailing console/runtime events.
            assert not cdp.failures, f"Chrome reported console/runtime failures: {cdp.failures}"
        finally:
            try:
                if cdp:
                    cdp.close()
            finally:
                _stop(chrome)
                _stop(preview)
                _stop(malformed_preview)
