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
                asset = ROOT / 'mojo/apps/account/static/account' / self.path.split('?', 1)[0].rsplit('/', 1)[-1]
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


@th.django_unit_test('rendered Continue supports mouse, touch, keyboard, honest failures and credential-free decoys')
def test_browser_interaction_matrix(opts):
    import base64
    import time
    import objict
    from django.test import RequestFactory
    from mojo.apps.account.rest.bouncer.views import _serve_challenge, _serve_login
    from test_account.test_admin_security_browser import _port, _json, _new_page, _stop
    executable = os.environ.get('MOJO_BOUNCER_CHROME', '')
    assert executable and Path(executable).is_file(), 'select installed Chrome with MOJO_BOUNCER_CHROME'
    state = {'mode': 'success', 'posts': [], 'visits': []}
    request = RequestFactory().get('/auth')
    request.DATA = objict.objict()
    request.muid = 'browser-matrix'
    request.ip = '127.0.0.1'
    request.user_agent = 'Mozilla/5.0'
    cfg = {'descriptor': 'x' * 32, 'next_action': 'check'}
    html = _serve_challenge(request, hosted_config=cfg).content.decode().replace('"redirect_url": "/auth"', '"redirect_url": "/finished"').encode()
    nonce = __import__('re').search(rb'<script nonce="([^"]+)"', html).group(1).decode()
    login_html = _serve_login(request, hosted_config=cfg).content

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state['visits'].append(self.path)
            if self.path.startswith('/api/account/static/'):
                body = (ROOT / 'mojo/apps/account/static/account' / self.path.split('?', 1)[0].rsplit('/', 1)[-1]).read_bytes()
                kind = 'text/css' if self.path.endswith('.css') else 'application/javascript'
            else:
                body = b'<h1>Destination reached</h1>' if self.path == '/finished' else (login_html if self.path.startswith('/real-login') else html)
                kind = 'text/html'
            self.send_response(200)
            self.send_header('Content-Type', kind)
            rendered_nonce = __import__('re').search(rb'<script nonce="([^"]+)"', body)
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
            status, content_type = 200, 'application/json'
            if mode == 'cookies':
                data = {'decision': 'block', 'next_action': 'recovery', 'reason': 'cookies'} if op == 'confirm' else data
            elif mode == 'limited':
                status = 429
                data = {}
            elif mode == 'decoy':
                data = {'decision': 'block', 'next_action': 'decoy'}
            elif mode == 'http':
                status = 503
            elif mode == 'html':
                content_type = 'text/html'
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.end_headers()
            self.wfile.write(b'not json' if mode == 'json' else json.dumps({'status': True, 'data': data}).encode())

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
            def rect():
                return page.evaluate("(()=>{let r=document.getElementById('mbg-continue').getBoundingClientRect();return {left:r.left,width:r.width,y:r.top+r.height/2}})()")
            def key(name, code):
                page.call('Input.dispatchKeyEvent', {'type': 'keyDown', 'key': name, 'windowsVirtualKeyCode': code, 'text': '\r' if name == 'Enter' else ''})
                page.call('Input.dispatchKeyEvent', {'type': 'keyUp', 'key': name, 'windowsVirtualKeyCode': code})
            def confirm():
                page.evaluate("document.getElementById('mbg-continue').click();true")
            page.call('Emulation.setDeviceMetricsOverride', {'width': 1100, 'height': 820, 'deviceScaleFactor': 1, 'mobile': False})
            fresh()
            assert page.evaluate("!document.querySelector('input[type=range]') && !document.body.textContent.includes('slider')"), 'hosted page must expose a Continue button without a drag requirement'
            page.evaluate("document.getElementById('mbg-continue').focus();true")
            assert page.evaluate("document.activeElement.id==='mbg-continue' && document.getElementById('mbg-status').getAttribute('aria-live')==='polite'"), 'keyboard focus and live status must stay available'
            Path('/tmp/bouncer-no-slider-desktop.png').write_bytes(base64.b64decode(page.call('Page.captureScreenshot', {'format': 'png'})['data']))
            key('Enter', 13)
            page.wait("location.pathname==='/finished'", 'keyboard destination')
            assert [p['hosted_gate']['operation'] for p in state['posts']] == ['check', 'confirm'], 'Continue must confirm the cookie before navigation'
            fresh()
            box = rect()
            for kind in ('mousePressed', 'mouseReleased'):
                page.call('Input.dispatchMouseEvent', {'type': kind, 'x': box['left'] + box['width']/2, 'y': box['y'], 'button': 'left', 'clickCount': 1})
            page.wait("location.pathname==='/finished'", 'mouse destination')
            page.call('Emulation.setDeviceMetricsOverride', {'width': 360, 'height': 800, 'deviceScaleFactor': 1, 'mobile': True})
            page.call('Emulation.setTouchEmulationEnabled', {'enabled': True})
            page.call('Emulation.setEmulatedMedia', {'features': [{'name': 'prefers-reduced-motion', 'value': 'reduce'}]})
            fresh()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), 'narrow layout must not overflow horizontally'
            Path('/tmp/bouncer-no-slider-mobile.png').write_bytes(base64.b64decode(page.call('Page.captureScreenshot', {'format': 'png'})['data']))
            box = rect()
            for kind in ('touchStart', 'touchEnd'):
                page.call('Input.dispatchTouchEvent', {'type': kind, 'touchPoints': [] if kind == 'touchEnd' else [{'x': box['left']+box['width']/2, 'y': box['y'], 'id': 1}]})
            page.wait("location.pathname==='/finished'", 'single mobile tap destination')
            assert [p['hosted_gate']['operation'] for p in state['posts']] == ['check', 'confirm'], 'one mobile tap must confirm the cookie and reach the destination without dragging'
            tap = state['posts'][0]['signals']
            assert tap['behavior']['touch_event_count'] > 0, 'a stationary mobile tap must be measured without dragging'
            assert tap['gate_challenge'].get('had_activation') is True, 'Continue activation must be reported before the assessment'
            fresh()
            page.evaluate("MojoHostedBouncer.mount(document, {descriptor:'x'.repeat(32),next_action:'slider',redirect_url:'/finished'});true")
            assert page.evaluate("document.getElementById('mbg-continue').textContent==='Continue' && !document.getElementById('mbg-continue').disabled"), 'legacy page configuration must still expose Continue'
            for mode in ('cookies', 'http', 'html', 'json', 'decoy', 'limited'):
                fresh(mode)
                confirm()
                time.sleep(0.25)
                assert page.evaluate("location.pathname==='/auth' && !document.body.textContent.includes('Verified')"), f'{mode} must never show success or navigate'
                if mode == 'cookies':
                    assert page.evaluate("document.getElementById('mbg-status').textContent.includes('cookies')"), 'missing cookie must explain recovery'
                if mode == 'limited':
                    assert page.evaluate("document.getElementById('mbg-status').textContent.includes('Too many checks') && document.getElementById('mbg-continue').disabled"), '429 must explain the wait and keep review reachable'
                if mode == 'decoy':
                    assert page.evaluate("document.getElementById('mbg-help').open && !document.querySelector('input[type=password]')"), 'a legacy hosted decoy response must offer recovery without collecting credentials'
            fresh('cookies')
            confirm()
            page.wait("document.getElementById('mbg-help').open", 'recovery help')
            page.evaluate("document.getElementById('mbg-review-email').value='mobile@example.test';document.getElementById('mbg-review-note').value='Mobile check failed';document.getElementById('mbg-review-submit').click();true")
            page.wait("document.getElementById('mbg-review-status').textContent.includes('recorded')", 'review receipt')
            assert state['posts'][-1]['email'] == 'mobile@example.test' and 'hosted_gate' not in state['posts'][-1], 'review submission must use independent intake, not the failed assess path'
            Path('/tmp/bouncer-mobile-review.png').write_bytes(base64.b64decode(page.call('Page.captureScreenshot', {'format': 'png'})['data']))
            fresh('html')
            for _ in range(3):
                confirm()
                time.sleep(0.15)
            assert page.evaluate("document.getElementById('mbg-continue').hidden && !document.getElementById('mbg-restart').hidden && document.getElementById('mbg-help').open"), 'three interrupted attempts must offer restart/review instead of endless Retry'
            # Storage denial still leaves controls and the original reset URL usable.
            script = page.call('Page.addScriptToEvaluateOnNewDocument', {'source': "Object.defineProperty(window,'sessionStorage',{get(){throw new Error('blocked')}});Object.defineProperty(window,'localStorage',{get(){throw new Error('blocked')}});"})
            fresh('success', '?token=pr%3Afixture-reset')
            before = state['visits'].count('/auth?token=pr%3Afixture-reset')
            confirm()
            time.sleep(0.4)
            assert state['visits'].count('/auth?token=pr%3Afixture-reset') == before + 1, 'storage-refused reset recovery must reload the original URL exactly once'
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
                if init:
                    page.call('Page.removeScriptToEvaluateOnNewDocument', {'identifier': init['identifier']})
            page.call('Emulation.setScriptExecutionDisabled', {'value': True})
            fresh_url = f'http://127.0.0.1:{server.server_port}/auth'
            page.call('Page.navigate', {'url': fresh_url})
            time.sleep(0.25)
            assert page.evaluate("!!document.querySelector('#mbg-review-form[method=post] input[name=review_ticket]') && !document.querySelector('input[type=password]')"), 'without JavaScript visitors can still submit a review and no password form is present'
    finally:
        if page:
            page.close()
        _stop(chrome)
        server.shutdown()
        server.server_close()
