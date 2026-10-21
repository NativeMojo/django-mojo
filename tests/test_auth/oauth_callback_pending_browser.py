"""Opt-in real-Chrome proof for the OAuth callback pending view (#8579).

The node harness (oauth_callback_pending.js) proves the state machine; this
proves what a person actually sees. A stub server serves the rendered callback
page with its real CSP and the real account static files, and holds the
`/complete` POST until the test releases it. A probe installed before any page
script samples every animation frame from the first one: the sign-in form must
never be displayed, and focus never inside it, until the person asks for it.

Set MOJO_BOUNCER_CHROME to an installed Chrome executable (as for
test_bouncer_recovery.browser); the harness never downloads a browser.
"""
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from testit import helpers as th

TESTIT_TIER = "extended"
ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / 'mojo/apps/account/static/account'
CALLBACK = '/auth?code=browser-code-8579&state=browser-state-8579&redirect=%2Fdashboard'

PROBE = r"""
(function () {
  if (location.search.indexOf('code=') !== -1) sessionStorage.setItem('oauth_provider', 'google');
  var p = window.__probe = {frames: 0, formShown: 0, focusInForm: 0, states: []};
  var watching = false;
  function record(state) {
    if (!window.__endedByUser && p.states[p.states.length - 1] !== state) p.states.push(state);
  }
  function tick() {
    var form = document.getElementById('mat-page');
    var view = document.getElementById('mat-pending');
    // States are recorded on every attribute change, not per frame: under load
    // the redirect state can be replaced by the navigation before a frame paints.
    if (view && !watching) {
      watching = true;
      record(view.getAttribute('data-state'));
      new MutationObserver(function () { record(view.getAttribute('data-state')); })
        .observe(view, {attributes: true, attributeFilter: ['data-state']});
    }
    if (form && !window.__endedByUser) {
      p.frames++;
      if (getComputedStyle(form).display !== 'none') p.formShown++;
      if (form.contains(document.activeElement)) p.focusInForm++;
    }
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
  // Keep the callback page's record across the navigation to the destination.
  addEventListener('pagehide', function () {
    if (document.getElementById('mat-pending')) sessionStorage.setItem('__probe', JSON.stringify(p));
  });
})();
"""


def _render(query):
    import objict
    from urllib.parse import parse_qsl, urlsplit
    from django.test import RequestFactory
    from mojo.apps.account.rest.bouncer.views import _serve_login
    params = dict(parse_qsl(urlsplit(query).query))
    request = RequestFactory().get('/auth', params, HTTP_HOST='127.0.0.1', HTTP_USER_AGENT='Mozilla/5.0')
    request.DATA = objict.objict(params)
    request.muid = 'oauth-pending-browser'
    request.msid = request.mtab = request.duid = ''
    request.ip = '127.0.0.1'
    request.user_agent = 'Mozilla/5.0'

    response = _serve_login(request)
    return response.content, response['Content-Security-Policy']


@th.django_unit_test("real Chrome: the callback landing never shows the form while sign-in completes")
def test_callback_pending_in_chrome(opts):
    from test_account.test_admin_security_browser import _port, _json, _new_page, _stop

    executable = os.environ.get('MOJO_BOUNCER_CHROME', '')
    assert executable and Path(executable).is_file(), \
        "MOJO_BOUNCER_CHROME must select an installed Chrome executable"
    callback_html, callback_csp = _render(CALLBACK)
    # Shorten the slow threshold in this test-owned copy only, so the slow
    # state is reachable in a second rather than fifteen.
    fast_html = callback_html.replace(b'PENDING_SLOW_MS = 15000', b'PENDING_SLOW_MS = 1200')
    assert fast_html != callback_html, "the rendered page must carry PENDING_SLOW_MS = 15000"
    state = {'html': callback_html, 'posts': [], 'release': threading.Event(),
             'outcome': 'success', 'missing_script': False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, body, kind, csp=None):
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Cache-Control', 'no-store')
            if csp:
                self.send_header('Content-Security-Policy', csp)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split('?', 1)[0]
            if path.startswith('/api/account/static/'):
                name = path.rsplit('/', 1)[-1]
                asset = STATIC / name
                if not asset.is_file() or (state['missing_script'] and name == 'mojo-auth.js'):
                    return self._send(404, b'not found', 'text/plain')
                kind = 'text/css' if name.endswith('.css') else 'application/javascript'
                return self._send(200, asset.read_bytes(), kind)
            if path == '/auth':
                return self._send(200, state['html'], 'text/html; charset=utf-8', callback_csp)
            if path == '/dashboard':
                return self._send(200, b'<h1>Destination reached</h1>', 'text/html')
            return self._send(404, b'not found', 'text/plain')

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
            state['posts'].append((self.path, body))
            if self.path.endswith('/complete'):
                state['release'].wait(20)
                if state['outcome'] == 'success':
                    payload = {'status': True, 'data': {
                        'access_token': 'browser.access.token', 'refresh_token': 'browser-refresh',
                        'user': {'display_name': 'Ian Starnes'}}}
                    return self._send(200, json.dumps(payload).encode(), 'application/json')
                payload = {'status': False, 'code': 401,
                           'error': 'Your sign-in expired before it could be completed.'}
                return self._send(401, json.dumps(payload).encode(), 'application/json')
            return self._send(404, b'{}', 'application/json')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{server.server_port}'
    chrome = page = None

    def land(html=callback_html, outcome='success', missing_script=False):
        state.update(html=html, outcome=outcome, missing_script=missing_script, posts=[])
        state['release'] = threading.Event()
        page.failures.clear()
        page.call('Page.navigate', {'url': origin + CALLBACK})
        page.wait("!!document.getElementById('mat-pending') && !!window.__probe && __probe.frames > 3",
                  'the pending view')

    def probe():
        return page.evaluate("JSON.parse(JSON.stringify(window.__probe))")

    def left_behind():
        # The callback page's probe record, saved as it navigated away.
        return page.evaluate("JSON.parse(sessionStorage.getItem('__probe') || 'null')")

    def completes():
        return [p for p, _ in state['posts'] if p.endswith('/complete')]

    def page_errors():
        # Resource 404s (favicon, manifest) are the stub server's, not the page's.
        return [f for f in page.failures
                if 'Failed to load resource' not in f and 'Manifest fetch' not in f]

    try:
        with tempfile.TemporaryDirectory(prefix='oauth-pending-') as profile:
            port = _port()
            chrome = subprocess.Popen([
                executable, '--headless=new', '--no-first-run', '--no-default-browser-check',
                '--disable-gpu', '--disable-background-networking',
                '--remote-debugging-address=127.0.0.1', f'--remote-debugging-port={port}',
                f'--user-data-dir={profile}', 'about:blank',
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _json(f'http://127.0.0.1:{port}/json/version', time.monotonic() + 10)
            page = _new_page(port, 'about:blank')
            page.call('Page.addScriptToEvaluateOnNewDocument', {'source': PROBE})

            # 1. Held completion, then success: pending -> success -> redirect -> destination.
            land()
            assert page.evaluate("document.getElementById('mat-pending').getAttribute('data-state')==='pending'"), \
                "a held completion must show the pending state"
            assert page.evaluate("!location.search.includes('code=') && location.search.includes('redirect=')"), \
                "code/state must leave the visible URL while the destination stays"
            assert page.evaluate("getComputedStyle(document.getElementById('mat-pending')).position==='fixed'"), \
                "the theme stylesheet must lay the pending view out full screen"
            time.sleep(0.4)
            assert len(completes()) == 1, f"one completion POST while held, got {completes()}"
            state['release'].set()
            page.wait("location.pathname==='/dashboard'", 'the destination after success')
            assert len(completes()) == 1, "success must post the code exactly once"
            seen = left_behind()
            assert seen and seen['states'] == ['pending', 'success', 'redirect'], \
                f"the callback page must play pending -> success -> redirect: {seen}"
            assert seen['formShown'] == 0 and seen['focusInForm'] == 0, \
                f"the form must never paint on the way to the destination: {seen}"

            # 2. Held completion, then a refusal; the form only returns when asked for.
            land(outcome='refused')
            state['release'].set()
            page.wait("document.getElementById('mat-pending').getAttribute('data-state')==='failure'", 'the failure state')
            seen = probe()
            assert page.evaluate("document.activeElement.id==='mat-pending-retry'"), \
                "a refusal must focus Try again"
            assert page.evaluate("document.getElementById('mat-pending-sub').textContent.includes('expired')"), \
                "a refusal must show the server message"
            page.evaluate("window.__endedByUser=true;document.getElementById('mat-pending-other').click();true")
            assert page.evaluate("getComputedStyle(document.getElementById('mat-page')).display!=='none' && "
                                 "!document.getElementById('mat-page').inert && "
                                 "getComputedStyle(document.getElementById('mat-pending')).display==='none'"), \
                "Use a different sign-in method must restore a usable form"
            assert page.evaluate("document.getElementById('mat-page').contains(document.activeElement) && "
                                 "document.activeElement.matches('input, button')"), \
                "focus must move into the restored form"
            assert len(completes()) == 1, "a refusal must never be replayed"
            assert seen['formShown'] == 0 and seen['focusInForm'] == 0, \
                f"the form must stay hidden and unfocused until asked for: {seen}"
            assert not page_errors(), f"the refusal path must not raise: {page_errors()}"

            # 3. Slow, then a late success still signs in.
            land(html=fast_html)
            page.wait("document.getElementById('mat-pending').getAttribute('data-state')==='slow'", 'the slow state')
            assert page.evaluate("(()=>{const a=document.querySelector('.mat-pending-slow-actions');"
                                 "const s=getComputedStyle(a);return s.display!=='none'&&s.visibility==='visible'})()"), \
                "the slow state must show Start over"
            state['release'].set()
            page.wait("location.pathname==='/dashboard'", 'the destination after a late success')
            assert len(completes()) == 1, "a late success must still post once"
            seen = left_behind()
            assert seen and seen['states'] == ['pending', 'slow', 'success', 'redirect'], \
                f"a late result must go pending -> slow -> success -> redirect: {seen}"
            assert seen['formShown'] == 0 and seen['focusInForm'] == 0, \
                f"the form must never paint while slow: {seen}"

            # 4. mojo-auth.js missing: only the pending view ever renders.
            land(missing_script=True)
            time.sleep(1.0)
            seen = probe()
            assert seen['states'] == ['pending'], f"without the auth script the view must stay pending: {seen}"
            assert not completes(), "without the auth script nothing is posted"
            assert page.evaluate("(()=>{const s=getComputedStyle(document.querySelector('.mat-pending-slow-actions'));"
                                 "return s.visibility==='hidden' && s.animationDelay==='15s'})()"), \
                "the no-script Start over must wait for the 15s CSS reveal"
            assert page.evaluate("document.getElementById('mat-pending-restart').getAttribute('href').startsWith('/auth')"), \
                "Start over must stay a working link without script"

            seen = probe()
            assert seen['formShown'] == 0 and seen['focusInForm'] == 0, \
                f"the form must never paint while sign-in completes: {seen}"
    finally:
        if page:
            page.close()
        _stop(chrome)
        server.shutdown()
        server.server_close()
