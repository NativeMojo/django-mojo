"""Opt-in rendered-browser regression; uses an explicitly selected local Chrome."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from testit import helpers as th

TESTIT_TIER = "extended"
ROOT = Path(__file__).resolve().parents[2]


@th.django_unit_test("a rejected hosted check never claims Verified or reloads")
def test_rejected_check_stays_on_recovery(opts):
    import objict
    from django.test import RequestFactory
    from mojo.apps.account.rest.bouncer.views import _serve_challenge
    from test_account.test_admin_security_browser import _port, _json, _new_page, _stop
    import time

    executable = os.environ.get("MOJO_BOUNCER_CHROME", "")
    assert executable and Path(executable).is_file(), "MOJO_BOUNCER_CHROME must select an installed Chrome executable"
    request = RequestFactory().get('/auth', HTTP_HOST='127.0.0.1', HTTP_USER_AGENT='Mozilla/5.0')
    request.DATA = objict.objict()
    request.muid = 'test-bouncer-browser-rejection'
    request.msid = request.mtab = request.duid = ''
    request.ip = '127.0.0.1'
    request.user_agent = 'Mozilla/5.0'
    html = _serve_challenge(request).content
    visits = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path.startswith('/api/account/static/'):
                asset = ROOT / 'mojo/apps/account/static/account' / self.path.rsplit('/', 1)[-1]
                body = asset.read_bytes()
                kind = 'application/javascript'
            else:
                visits.append(self.path)
                body, kind = html, 'text/html'
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'status': True, 'data': {
                'decision': 'block', 'next_action': 'recovery', 'reason': 'operator',
            }}).encode())

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    chrome = page = None
    try:
        with tempfile.TemporaryDirectory(prefix='bouncer-browser-') as profile:
            port = _port()
            chrome = subprocess.Popen([
                executable, '--headless=new', '--no-first-run', '--no-default-browser-check',
                '--disable-gpu', '--disable-background-networking',
                '--remote-debugging-address=127.0.0.1', f'--remote-debugging-port={port}',
                f'--user-data-dir={profile}', 'about:blank',
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _json(f'http://127.0.0.1:{port}/json/version', time.monotonic() + 10)
            page = _new_page(port, f'http://127.0.0.1:{server.server_port}/auth')
            page.wait("!!document.querySelector('button:not([disabled])')", 'continue button')
            before = len([p for p in visits if p == '/auth'])
            page.evaluate("window.__verifiedSeen=false;new MutationObserver(()=>{if(document.body.textContent.includes('Verified'))window.__verifiedSeen=true}).observe(document.body,{subtree:true,childList:true,characterData:true});document.querySelector('button:not([disabled])').click();true")
            time.sleep(1.4)
            assert len([p for p in visits if p == '/auth']) == before, "a blocked assessment must not reload the challenge"
            assert not page.evaluate("window.__verifiedSeen || document.body.textContent.includes('Verified')"), "a blocked assessment must never display Verified"
    finally:
        if page:
            page.close()
        _stop(chrome)
        server.shutdown()
        server.server_close()
