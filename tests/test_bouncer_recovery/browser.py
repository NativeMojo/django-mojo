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
    from mojo.apps.account.rest.bouncer.views import _serve_challenge, _serve_login
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
                path = self.path.split('?', 1)[0]
                asset = ROOT / 'mojo/apps/account/static/account' / path.rsplit('/', 1)[-1]
                body = asset.read_bytes()
                kind = 'text/css' if path.endswith('.css') else 'application/javascript'
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
            page.wait("!!document.querySelector('#mbg-continue:not([disabled])')", 'continue button')
            before = len([p for p in visits if p == '/auth'])
            page.evaluate("window.__verifiedSeen=false;new MutationObserver(()=>{if(document.body.textContent.includes('Verified'))window.__verifiedSeen=true}).observe(document.body,{subtree:true,childList:true,characterData:true});document.querySelector('#mbg-continue:not([disabled])').click();true")
            time.sleep(1.4)
            assert len([p for p in visits if p == '/auth']) == before, "a blocked assessment must not reload the challenge"
            assert not page.evaluate("window.__verifiedSeen || document.body.textContent.includes('Verified')"), "a blocked assessment must never display Verified"
    finally:
        if page:
            page.close()
        _stop(chrome)
        server.shutdown()
        server.server_close()


LONG_REF = '1234567890123-' + 'b' * 30  # 44 characters, the longest reference shape
GROUP_NAME = 'test-9132-browser-theme'

# Layout probe: every visible block of the challenge, no horizontal scroll,
# no overlapping blocks, and every visible control at least 44x44.
LAYOUT_PROBE = """(async()=>{
  // Entrance animations scale and shift elements; measure the settled layout.
  await Promise.all(document.getAnimations().filter(a=>a.effect&&a.effect.getTiming().iterations!==Infinity).map(a=>a.finished.catch(()=>null)));
  const vis=el=>{if(!el)return false;const r=el.getBoundingClientRect();const cs=getComputedStyle(el);return r.width>0&&r.height>0&&cs.display!=='none'&&cs.visibility!=='hidden'};
  const blocks=['.mbg-brand','.mbg-glyph','#mbg-live','.mbg-actions','#mbg-reference-row','#mbg-sent-restart','#mbg-review','.mbg-footer']
    .map(s=>document.querySelector(s)).filter(vis);
  const overlaps=[];
  for(let i=0;i<blocks.length;i++)for(let j=i+1;j<blocks.length;j++){
    const a=blocks[i].getBoundingClientRect(),b=blocks[j].getBoundingClientRect();
    if(a.left<b.right-0.5&&b.left<a.right-0.5&&a.top<b.bottom-0.5&&b.top<a.bottom-0.5)overlaps.push((blocks[i].id||blocks[i].className)+' x '+(blocks[j].id||blocks[j].className));
  }
  const small=[...document.querySelectorAll('.mbg button,.mbg a,.mbg input:not(#mbg-hp-field),.mbg textarea')].filter(vis)
    .filter(el=>el.offsetWidth<44||el.offsetHeight<44).map(el=>el.id||el.textContent.trim());
  const outside=[...document.querySelectorAll('.mbg *')].filter(vis).filter(el=>!el.closest('.mbg-hp'))
    .filter(el=>{const r=el.getBoundingClientRect();return r.left<-0.5||r.right>innerWidth+0.5}).map(el=>el.id||el.className||el.tagName);
  return {scroll:document.documentElement.scrollWidth<=innerWidth,overlaps,small,outside:outside.slice(0,5),state:document.getElementById('mbg-root').dataset.state};
})()"""


@th.django_unit_test('rendered Continue supports mouse, touch, keyboard, honest failures and credential-free decoys')
def test_browser_interaction_matrix(opts):
    import base64
    import re
    import time
    import objict
    from django.test import RequestFactory
    from mojo.apps.account.models import Group
    from mojo.apps.account.rest.bouncer.views import _serve_challenge, _serve_login
    from test_account.test_admin_security_browser import _port, _json, _new_page, _stop
    executable = os.environ.get('MOJO_BOUNCER_CHROME', '')
    assert executable and Path(executable).is_file(), 'select installed Chrome with MOJO_BOUNCER_CHROME'
    state = {'mode': 'success', 'posts': [], 'visits': []}

    def render(query=None, group=None, reference=None):
        request = RequestFactory().get('/auth', query or {})
        request.DATA = objict.objict(query or {})
        request.muid = 'browser-matrix'
        request.ip = '127.0.0.1'
        request.user_agent = 'Mozilla/5.0'
        cfg = {'descriptor': 'x' * 32, 'next_action': 'check'}
        if reference:
            cfg.update(reference=reference, review_ticket='ticket-browser-9132')
        return _serve_challenge(request, group=group, hosted_config=cfg).content.decode().replace(
            '"redirect_url": "/auth', '"redirect_url": "/finished').encode()

    request = RequestFactory().get('/auth')
    request.DATA = objict.objict()
    request.muid = 'browser-matrix'
    request.ip = '127.0.0.1'
    request.user_agent = 'Mozilla/5.0'
    cfg = {'descriptor': 'x' * 32, 'next_action': 'check'}
    html = render()
    nonce = re.search(rb'<script nonce="([^"]+)"', html).group(1).decode()
    login_html = _serve_login(request, hosted_config=cfg).content
    Group.objects.filter(name=GROUP_NAME).delete()
    group = Group.objects.create(name=GROUP_NAME, kind='organization', metadata={'auth_config': {'theme': {
        'accent_color': '#0f9d8a',
        'app_title': 'A Considerably Long Tenant Application Title For Narrow Phones',
        'auth_provider_name': 'An Unusually Verbose Identity Provider Name Incorporated',
    }}})
    variants = {
        'light': render({'auth_appearance': 'light'}),
        'dark': render({'auth_appearance': 'dark'}),
        'system': render({'auth_appearance': 'system'}),
        'tenant': render(group=group, reference=LONG_REF),
    }
    Group.objects.filter(name=GROUP_NAME).delete()
    variants['noscript-src'] = html.replace(b'mojo-hosted-bouncer.js?v=', b'missing-hosted-bouncer.js?v=')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state['visits'].append(self.path)
            path, _, query = self.path.partition('?')
            status = 200
            if path.startswith('/api/account/static/'):
                asset = ROOT / 'mojo/apps/account/static/account' / path.rsplit('/', 1)[-1]
                if asset.is_file():
                    body = asset.read_bytes()
                else:
                    status, body = 404, b'missing'
                kind = 'text/css' if path.endswith('.css') else 'application/javascript'
            else:
                variant = re.search(r'(?:^|&)variant=([\w-]+)', query)
                body = b'<h1>Destination reached</h1>' if path == '/finished' else (
                    login_html if path.startswith('/real-login') else variants.get(variant.group(1), html) if variant else html)
                kind = 'text/html'
            self.send_response(status)
            self.send_header('Content-Type', kind)
            rendered_nonce = re.search(rb'<script nonce="([^"]+)"', body)
            active_nonce = rendered_nonce.group(1).decode() if rendered_nonce else nonce
            self.send_header('Content-Security-Policy', f"default-src 'self'; script-src 'self' 'nonce-{active_nonce}'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            state['posts'].append(body)
            if self.path == '/api/auth/bouncer/recovery':
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'status': True, 'data': {'reference': 'review-browser-fixture', 'review_state': 'pending', 'message': 'Your request is recorded for review.'}}).encode())
                return
            op = body['hosted_gate']['operation']
            mode = state['mode']
            data = {'decision': 'allow', 'next_action': 'allow' if op == 'confirm' else 'check_cookie'}
            status, content_type, headers = 200, 'application/json', {}
            if mode == 'cookies':
                data = {'decision': 'block', 'next_action': 'recovery', 'reason': 'cookies', 'reference': LONG_REF} if op == 'confirm' else data
            elif mode == 'limited':
                status = 429
                headers['Retry-After'] = '42'
                data = {}
            elif mode == 'decoy':
                data = {'decision': 'block', 'next_action': 'decoy'}
            elif mode == 'invalid':
                status = 400
                data = {'next_action': 'error', 'reason': 'invalid', 'reference': LONG_REF}
            elif mode == 'http':
                status = 503
            elif mode == 'html':
                content_type = 'text/html'
            elif mode == 'hold':
                time.sleep(4)
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(b'not json' if mode == 'json' else json.dumps({'status': status == 200, 'data': data}).encode())

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    chrome = page = None
    try:
        with tempfile.TemporaryDirectory(prefix='bouncer-matrix-') as profile:
            port = _port()
            chrome = subprocess.Popen([executable, '--headless=new', '--no-first-run', '--no-default-browser-check', '--disable-gpu',
                '--disable-background-networking', '--remote-debugging-address=127.0.0.1', f'--remote-debugging-port={port}', f'--user-data-dir={profile}', 'about:blank'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _json(f'http://127.0.0.1:{port}/json/version', time.monotonic() + 10)
            page = _new_page(port, 'about:blank')
            def fresh(mode='success', query=''):
                state.update(mode=mode, posts=[])
                page.call('Page.navigate', {'url': f'http://127.0.0.1:{server.server_port}/auth{query}'})
                page.wait("!!document.querySelector('#mbg-continue:not([disabled])')", 'enabled Continue')
            def rect(selector='#mbg-continue'):
                return page.evaluate("(()=>{let r=document.querySelector(%r).getBoundingClientRect();return {left:r.left,width:r.width,y:r.top+r.height/2}})()" % selector)
            def key(name, code):
                text = '\r' if name == 'Enter' else (' ' if name == ' ' else '')
                page.call('Input.dispatchKeyEvent', {'type': 'keyDown', 'key': name, 'code': 'Space' if name == ' ' else name, 'windowsVirtualKeyCode': code, 'text': text})
                page.call('Input.dispatchKeyEvent', {'type': 'keyUp', 'key': name, 'code': 'Space' if name == ' ' else name, 'windowsVirtualKeyCode': code})
            def click(selector):
                page.evaluate("document.querySelector(%r).scrollIntoView({block:'center'});true" % selector)
                box = rect(selector)
                for kind in ('mousePressed', 'mouseReleased'):
                    page.call('Input.dispatchMouseEvent', {'type': kind, 'x': box['left'] + box['width']/2, 'y': box['y'], 'button': 'left', 'clickCount': 1})
            def confirm():
                page.evaluate("document.getElementById('mbg-continue').click();true")
            def at(name):
                page.wait(f"document.getElementById('mbg-root').dataset.state==='{name}'", f'{name} state')
            def layout(tag):
                probe = page.evaluate(LAYOUT_PROBE)
                assert probe['scroll'], f'{tag}: no horizontal scroll ({probe})'
                assert not probe['overlaps'], f'{tag}: blocks overlap: {probe["overlaps"]}'
                assert not probe['small'], f'{tag}: controls under 44x44: {probe["small"]}'
                assert not probe['outside'], f'{tag}: content runs off screen: {probe["outside"]}'
            page.call('Emulation.setDeviceMetricsOverride', {'width': 1100, 'height': 820, 'deviceScaleFactor': 1, 'mobile': False})
            fresh()
            assert page.evaluate("!document.querySelector('input[type=range]') && !document.body.textContent.includes('slider')"), 'hosted page must expose a Continue button without a drag requirement'
            # Real keyboard: Tab reaches Continue first, Enter activates it.
            key('Tab', 9)
            assert page.evaluate("document.activeElement.id==='mbg-continue'"), 'a real Tab must land on Continue first'
            assert page.evaluate("(()=>{const l=document.getElementById('mbg-live');return l.getAttribute('role')==='status'&&l.getAttribute('aria-live')==='polite'&&l.contains(document.getElementById('mbg-status'))})()"), 'the live status region must wrap the status line'
            Path('/tmp/bouncer-no-slider-desktop.png').write_bytes(base64.b64decode(page.call('Page.captureScreenshot', {'format': 'png'})['data']))
            key('Enter', 13)
            page.wait("location.pathname==='/finished'", 'keyboard destination')
            assert [p['hosted_gate']['operation'] for p in state['posts']] == ['check', 'confirm'], 'Continue must confirm the cookie before navigation'
            fresh()
            click('#mbg-continue')
            page.wait("location.pathname==='/finished'", 'mouse destination')
            # A cookies failure reached with a real Enter: focus lands on the heading,
            # then a real Tab reaches Ask for a review and Space opens the review step.
            fresh('cookies')
            key('Tab', 9)
            key('Enter', 13)
            at('failed')
            assert page.evaluate("document.activeElement.id==='mbg-heading'"), 'a failure that hides Try again must move focus to the heading'
            key('Tab', 9)
            assert page.evaluate("document.activeElement.id==='mbg-restart' && document.activeElement.textContent==='Try again'"), 'a real Tab from the heading must reach the promoted Try again first'
            key('Tab', 9)
            assert page.evaluate("document.activeElement.id==='mbg-review-open'"), 'the next real Tab must reach Ask for a review'
            key(' ', 32)
            at('review')
            assert page.evaluate("document.activeElement.id==='mbg-review-email'"), 'Space on Ask for a review must open the step with focus in the email field'
            # Touch: EMULATED (CDP touch emulation at 360x640), not a physical device.
            page.call('Emulation.setDeviceMetricsOverride', {'width': 360, 'height': 640, 'deviceScaleFactor': 1, 'mobile': True})
            page.call('Emulation.setTouchEmulationEnabled', {'enabled': True})
            page.call('Emulation.setEmulatedMedia', {'features': [{'name': 'prefers-reduced-motion', 'value': 'reduce'}]})
            fresh()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), 'narrow layout must not overflow horizontally'
            Path('/tmp/bouncer-no-slider-mobile.png').write_bytes(base64.b64decode(page.call('Page.captureScreenshot', {'format': 'png'})['data']))
            box = rect()
            for kind in ('touchStart', 'touchEnd'):
                page.call('Input.dispatchTouchEvent', {'type': kind, 'touchPoints': [] if kind == 'touchEnd' else [{'x': box['left']+box['width']/2, 'y': box['y'], 'id': 1}]})
            page.wait("location.pathname==='/finished'", 'single emulated-touch tap destination')
            assert [p['hosted_gate']['operation'] for p in state['posts']] == ['check', 'confirm'], 'one emulated tap must confirm the cookie and reach the destination without dragging'
            tap = state['posts'][0]['signals']
            assert tap['behavior']['touch_event_count'] > 0, 'a stationary emulated tap must be measured without dragging'
            assert tap['gate_challenge'].get('had_activation') is True, 'Continue activation must be reported before the assessment'
            # Reduced motion: no animation on any element of the challenge, in any state.
            for mode in ('success', 'cookies'):
                fresh(mode)
                if mode == 'cookies':
                    confirm()
                    at('failed')
                moving = page.evaluate("[...document.querySelectorAll('.mbg,.mbg *')].filter(el=>getComputedStyle(el).animationName!=='none').map(el=>el.className||el.tagName)")
                assert not moving, f'reduced motion must stop every challenge animation ({mode}): {moving}'
            page.call('Emulation.setEmulatedMedia', {'features': [{'name': 'prefers-reduced-motion', 'value': 'no-preference'}]})
            fresh()
            page.evaluate("MojoHostedBouncer.mount(document, {descriptor:'x'.repeat(32),next_action:'slider',redirect_url:'/finished'});true")
            assert page.evaluate("document.getElementById('mbg-continue-label').textContent==='Continue' && !document.getElementById('mbg-continue').disabled"), 'legacy page configuration must still expose Continue'
            for mode in ('cookies', 'http', 'html', 'json', 'decoy', 'limited', 'invalid'):
                fresh(mode)
                confirm()
                time.sleep(0.25)
                assert page.evaluate("location.pathname==='/auth' && !document.body.textContent.includes('Verified')"), f'{mode} must never show success or navigate'
                assert page.evaluate("document.getElementById('mbg-root').dataset.state==='failed'"), f'{mode} must show the failed state'
                if mode == 'cookies':
                    assert page.evaluate("document.getElementById('mbg-status').textContent.includes('cookie')"), 'missing cookie must explain recovery'
                if mode == 'limited':
                    assert page.evaluate("document.getElementById('mbg-heading').textContent==='Too many tries' && /^Try again in \\d:\\d\\d$/.test(document.getElementById('mbg-continue-label').textContent) && document.getElementById('mbg-continue').disabled"), '429 must count down on a disabled Try again'
                    assert page.evaluate("getComputedStyle(document.getElementById('mbg-review-open')).display!=='none'"), '429 must keep review reachable'
                if mode == 'decoy':
                    assert page.evaluate("getComputedStyle(document.getElementById('mbg-review-open')).display!=='none' && !document.querySelector('input[type=password]')"), 'a legacy hosted decoy response must offer review without collecting credentials'
                if mode == 'invalid':
                    assert page.evaluate("(()=>{const r=document.getElementById('mbg-restart');return getComputedStyle(r).display!=='none'&&r.textContent==='Start over'})()"), 'a deterministic invalid response must offer Start over at once'
            fresh('cookies')
            confirm()
            at('failed')
            click('#mbg-review-open')
            at('review')
            page.evaluate("document.getElementById('mbg-review-email').value='mobile@example.test';document.getElementById('mbg-review-note').value='Mobile check failed';true")
            click('#mbg-review-submit')
            at('sent')
            page.wait("document.getElementById('mbg-status').textContent.includes('recorded')", 'review receipt')
            assert state['posts'][-1]['email'] == 'mobile@example.test' and 'hosted_gate' not in state['posts'][-1], 'review submission must use independent intake, not the failed assess path'
            Path('/tmp/bouncer-mobile-review.png').write_bytes(base64.b64decode(page.call('Page.captureScreenshot', {'format': 'png'})['data']))
            fresh('html')
            for _ in range(3):
                confirm()
                time.sleep(0.15)
            assert page.evaluate("document.getElementById('mbg-continue').hidden && !document.getElementById('mbg-restart').hidden && document.getElementById('mbg-restart').classList.contains('is-primary')"), 'three interrupted attempts must promote Restart instead of an endless Try again'
            # Theme: light, dark and system (with EMULATED dark/light) set the page background;
            # a tenant accent changes Continue.
            backgrounds = {}
            for variant, scheme in (('light', 'dark'), ('dark', 'light'), ('system', 'dark'), ('system', 'light')):
                page.call('Emulation.setEmulatedMedia', {'features': [{'name': 'prefers-color-scheme', 'value': scheme}]})
                fresh('success', f'?variant={variant}')
                backgrounds[(variant, scheme)] = page.evaluate("getComputedStyle(document.body).backgroundColor")
            assert backgrounds[('light', 'dark')] == 'rgb(242, 239, 232)', f'light appearance ignores an OS dark theme: {backgrounds}'
            assert backgrounds[('dark', 'light')] == 'rgb(9, 14, 22)', f'dark appearance ignores an OS light theme: {backgrounds}'
            assert backgrounds[('system', 'dark')] == 'rgb(9, 14, 22)' and backgrounds[('system', 'light')] == 'rgb(242, 239, 232)', f'system appearance follows the (emulated) OS theme: {backgrounds}'
            page.call('Emulation.setEmulatedMedia', {'features': []})
            fresh()
            default_button = page.evaluate("getComputedStyle(document.getElementById('mbg-continue')).backgroundColor")
            fresh('success', '?variant=tenant')
            tenant_button = page.evaluate("getComputedStyle(document.getElementById('mbg-continue')).backgroundColor")
            assert default_button != tenant_button and tenant_button not in ('rgba(0, 0, 0, 0)', 'transparent'), f'the tenant accent must colour Continue: {default_button} vs {tenant_button}'
            # Layout at phone widths with a 44-character reference and a long title/provider.
            for width, height in ((320, 568), (360, 640)):
                page.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': height, 'deviceScaleFactor': 1, 'mobile': True})
                tag = f'{width}x{height}'
                fresh('success', '?variant=tenant')
                layout(f'{tag} ready')
                fresh('hold', '?variant=tenant')
                confirm()
                at('checking')
                layout(f'{tag} checking')
                fresh('cookies', '?variant=tenant')
                confirm()
                at('failed')
                layout(f'{tag} failed (Restart promoted, 44-char reference)')
                click('#mbg-review-open')
                at('review')
                layout(f'{tag} review')
                page.evaluate("document.getElementById('mbg-review-email').value='narrow@example.test';true")
                click('#mbg-review-submit')
                at('sent')
                layout(f'{tag} sent')
                fresh('limited', '?variant=tenant')
                confirm()
                at('failed')
                layout(f'{tag} limited (Try again + Start over)')
                fresh('success', '?variant=tenant')
                page.evaluate("document.getElementById('mbg-root').dataset.state='verified';true")
                layout(f'{tag} verified (FORCED: a real pass navigates away at once)')
            page.call('Emulation.setDeviceMetricsOverride', {'width': 360, 'height': 640, 'deviceScaleFactor': 1, 'mobile': True})
            # The script never loads: Get help still reveals the review step (CSS :target).
            page.call('Page.navigate', {'url': f'http://127.0.0.1:{server.server_port}/auth?variant=noscript-src'})
            page.wait("document.readyState==='complete'", 'page without its script')
            assert page.evaluate("getComputedStyle(document.getElementById('mbg-review')).display==='none'"), 'the review step starts closed'
            click('#mbg-help')
            page.wait("getComputedStyle(document.getElementById('mbg-review')).display!=='none'", 'review revealed without script')
            # Storage denial still leaves controls and the original reset URL usable.
            script = page.call('Page.addScriptToEvaluateOnNewDocument', {'source': "Object.defineProperty(window,'sessionStorage',{get(){throw new Error('blocked')}});Object.defineProperty(window,'localStorage',{get(){throw new Error('blocked')}});"})
            fresh('success', '?token=pr%3Afixture-reset')
            before = state['visits'].count('/auth?token=pr%3Afixture-reset')
            confirm()
            time.sleep(0.4)
            assert state['visits'].count('/auth?token=pr%3Afixture-reset') == before + 1, 'storage-refused reset recovery must reload the original URL exactly once'
            fresh('success', '?token=iv%3Afixture-invite')
            before = state['visits'].count('/auth?token=iv%3Afixture-invite')
            confirm()
            time.sleep(0.4)
            assert state['visits'].count('/auth?token=iv%3Afixture-invite') == before + 1, 'storage-refused invite recovery must reload the original URL exactly once'
            page.call('Page.removeScriptToEvaluateOnNewDocument', {'identifier': script['identifier']})
            fresh()
            providers = page.evaluate("""(async()=>{
              await new Promise((resolve,reject)=>{let s=document.createElement('script');s.src='/api/account/static/mojo-auth.js';s.onload=resolve;s.onerror=reject;document.head.append(s)});
              localStorage.setItem('mojo_device_uid','bound-browser');
              const calls=[];let index=0;
              window.fetch=async(url,options)=>{
                const body=JSON.parse(options.body);calls.push({url,body});
                const data=body.hosted_gate ? {decision:'allow',next_action:'token',token:'fresh-'+(++index)} : {mfa_required:true,requires_verification:true};
                return {ok:true,headers:{get:()=> 'application/json'},json:async()=>({status:true,data})};
              };
              MojoAuth.init({baseURL:'https://auth.example.test',bouncerTokenProvider:MojoHostedBouncer.tokenProvider({descriptor:'x'.repeat(32)})});
              await MojoAuth.login('example','secret',{group_uuid:'group-fixture'});
              await MojoAuth.startPhoneRegister('+15550004309');
              await MojoAuth.register({email:'fixture@example.test',password:'secret'});
              const before=calls.length;
              MojoAuth.init({baseURL:location.origin,bouncerTokenProvider:()=>Promise.reject(new Error('verification unavailable'))});
              let refused=false;try{await MojoAuth.login('example','secret')}catch(e){refused=true}
              const stopped=refused && calls.length===before;
              MojoAuth.init({baseURL:location.origin});
              await MojoAuth.login('example','secret');
              return {calls,stopped};
            })()""")
            calls = providers['calls']
            assert providers['stopped'], 'provider failure must stop the protected submission'
            assert [c['url'] for c in calls[:6:2]] == ['/api/account/bouncer/assess'] * 3, 'hosted control stays same-origin even with a configured auth API origin'
            assert [c['body'].get('duid') for c in calls[:6:2]] == ['bound-browser'] * 3, 'only device context must flow to each hosted token request'
            assert [c['body'].get('bouncer_token') for c in calls[1:6:2]] == ['fresh-1', 'fresh-2', 'fresh-3'], 'login, phone-start and register require separate fresh tokens'
            assert all(c['url'].startswith('https://auth.example.test/') for c in calls[1:6:2]), 'actual authentication preserves the configured API origin'
            assert calls[1]['body']['group_uuid'] == 'group-fixture', 'async token acquisition must retain the login group'
            assert 'bouncer_token' not in calls[-1]['body'], 'without a provider the legacy no-token behavior remains'
            retry = page.evaluate("""(async()=>{
              let proofs=0, posts=0;
              MojoAuth.init({baseURL:location.origin,bouncerTokenProvider:async()=> 'proof-'+(++proofs)});
              window.fetch=async()=>{posts++;return {ok:false,json:async()=>({status:false,error:'Invalid bouncer token',code:403})}};
              try{await MojoAuth.login('example','secret')}catch(_){}
              const rejected={proofs,posts}; proofs=0;posts=0;
              window.fetch=async()=>{posts++;return {ok:false,json:async()=>({status:false,error:'Invalid username or password',code:401})}};
              try{await MojoAuth.login('example','secret')}catch(_){}
              return {rejected,credentials:{proofs,posts}};
            })()""")
            assert retry['rejected'] == {'proofs': 2, 'posts': 2}, 'an explicit pre-credential token rejection gets exactly one fresh-token retry'
            assert retry['credentials'] == {'proofs': 1, 'posts': 1}, 'wrong credentials must never be automatically retried'
            for blocked in (False, True):
                init = None
                if blocked:
                    init = page.call('Page.addScriptToEvaluateOnNewDocument', {'source': "Object.defineProperty(window,'sessionStorage',{get(){throw new Error('blocked')}})"})
                page.call('Page.navigate', {'url': f'http://127.0.0.1:{server.server_port}/real-login?token=pr%3Afirst-click'})
                page.wait("!!window._mat && document.getElementById('view-set-password').classList.contains('is-active')", 'first-click reset form')
                assert page.evaluate("!location.search.includes('token')"), 'first-click reset removes its token from the visible URL'
                reset = page.evaluate("""(async()=>{
                  const sent=[];let completed=false;
                  MojoAuth.resetWithToken=(token,password)=>{sent.push(token);return sent.length===1?Promise.reject({error:'weak password'}):Promise.resolve({})};
                  window._mat.onAuthSuccess=()=>{completed=true};
                  const form=document.getElementById('form-set-password');
                  document.getElementById('set-password-new').value='weak';
                  document.getElementById('set-password-confirm').value='weak';
                  form.dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}));
                  await new Promise(r=>setTimeout(r,20));
                  let clean=true;try{clean=sessionStorage.getItem('mat_reset_token')===null}catch(_){}
                  document.getElementById('set-password-new').value='Stronger-Password-4309!';
                  document.getElementById('set-password-confirm').value='Stronger-Password-4309!';
                  form.dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}));
                  await new Promise(r=>setTimeout(r,20));
                  return {sent,clean,completed};
                })()""")
                assert reset['sent'] == ['pr:first-click', 'pr:first-click'] and reset['clean'] and reset['completed'], f'weak-password retry must retain only page memory and finish even when storage is refused (blocked={blocked}): {reset}'
                # An invite (iv:) link opens the same view with invite copy, and a
                # dead invite lands on sign-in with a plain message (#1955).
                if blocked:
                    page.call('Page.navigate', {'url': f'http://127.0.0.1:{server.server_port}/real-login?token=iv%3Afirst-click'})
                else:
                    page.call('Page.navigate', {'url': f'http://127.0.0.1:{server.server_port}/real-login'})
                    page.wait("!!window._mat", 'login page before the stash')
                    page.evaluate("sessionStorage.setItem('mat_reset_token','iv:first-click');true")
                    page.call('Page.navigate', {'url': f'http://127.0.0.1:{server.server_port}/real-login'})
                page.wait("!!window._mat && document.getElementById('view-set-password').classList.contains('is-active')", 'first-click invite form')
                assert page.evaluate("document.getElementById('set-password-title').textContent==='Accept your invitation' && !location.search.includes('token')"), f'an invite link must open the invite copy with its token removed from the URL (blocked={blocked})'
                invite = page.evaluate("""(async()=>{
                  const sent=[];
                  MojoAuth.resetWithToken=(token,password)=>{sent.push(token);return Promise.reject({error:'Expired token'})};
                  document.getElementById('set-password-new').value='Stronger-Password-4309!';
                  document.getElementById('set-password-confirm').value='Stronger-Password-4309!';
                  document.getElementById('form-set-password').dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}));
                  await new Promise(r=>setTimeout(r,20));
                  let clean=true;try{clean=sessionStorage.getItem('mat_reset_token')===null}catch(_){}
                  return {sent,clean,signin:document.getElementById('view-signin').classList.contains('is-active'),
                          message:document.getElementById('mat-message').textContent};
                })()""")
                assert invite['sent'] == ['iv:first-click'] and invite['clean'] and invite['signin'] and 'invitation link has expired' in invite['message'], f'an expired invite must land on sign-in with the invite message (blocked={blocked}): {invite}'
                if init:
                    page.call('Page.removeScriptToEvaluateOnNewDocument', {'identifier': init['identifier']})
            page.call('Emulation.setScriptExecutionDisabled', {'value': True})
            fresh_url = f'http://127.0.0.1:{server.server_port}/auth'
            page.call('Page.navigate', {'url': fresh_url})
            time.sleep(0.25)
            assert page.evaluate("!!document.querySelector('#mbg-review-form[method=post] input[name=review_ticket]') && !document.querySelector('input[type=password]')"), 'without JavaScript visitors can still submit a review and no password form is present'
            assert page.evaluate("getComputedStyle(document.getElementById('mbg-review')).display!=='none' && getComputedStyle(document.getElementById('mbg-review-back')).display==='none'"), 'without JavaScript the review form shows under the check, with no Back button'
    finally:
        if page:
            page.close()
        _stop(chrome)
        server.shutdown()
        server.server_close()
