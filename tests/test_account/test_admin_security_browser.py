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


# An isolated real Django ASGI process owns the custom-path/short-TTL rider.
# Delay controls exist only in this test process, never in product middleware.
SERVER = r"""
import asyncio, json, os, sys, time
sys.path.insert(0, sys.argv[1] + '/testproject/config')
sys.path.insert(0, sys.argv[1])
os.environ['DJANGO_SETTINGS_MODULE'] = 'settings'
from django.conf import settings
settings.MOJO_ADMIN_PATH = sys.argv[3]
settings.MOJO_ADMIN_SESSION_TTL = 6
settings.MOJO_ADMIN_COOKIE_SECURE = False
import django
django.setup()
from mojo.apps.realtime.routing import create_application
application = create_application()
events = []
delay = {'POST': 0, 'DELETE': 0}
async def rider(scope, receive, send):
    path = scope.get('path', '')
    method = scope.get('method', '')
    if path == '/__rider/socket' and scope['type'] == 'websocket':
        await receive()
        await send({'type':'websocket.accept'})
        await send({'type':'websocket.send','text':'fixture-ready'})
        await send({'type':'websocket.close','code':1000})
        return
    if path == '/__rider/upload':
        received = 0
        while True:
            body = await receive()
            received += len(body.get('body', b''))
            if not body.get('more_body'):
                break
        result = {'received':received,'authorization':any(k == b'authorization' for k,v in scope['headers'])}
        await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'application/json')]})
        await send({'type':'http.response.body','body':json.dumps(result).encode()})
        return
    if path == '/__rider/state':
        payload = json.dumps(events).encode()
        await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'application/json')]})
        await send({'type':'http.response.body','body':payload})
        return
    if path == '/__rider/delay':
        from urllib.parse import parse_qs
        args = parse_qs(scope.get('query_string', b'').decode())
        delay.update({k:float(args.get(k, ['0'])[0]) for k in delay})
        await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'application/json')]})
        await send({'type':'http.response.body','body':b'{}'})
        return
    if path == '/api/account/admin/session' or path.endswith('/_session'):
        events.append({'method':method,'phase':'start','at':time.monotonic()})
        async def delayed(message):
            if message['type'] == 'http.response.start':
                await asyncio.sleep(delay.get(method, 0))
            await send(message)
            if message['type'] == 'http.response.body' and not message.get('more_body'):
                events.append({'method':method,'phase':'end','at':time.monotonic()})
        await application(scope, receive, delayed)
        return
    await application(scope, receive, send)
import uvicorn
uvicorn.run(rider, host='127.0.0.1', port=int(sys.argv[2]), log_level='error', lifespan='off')
"""


def _new_page(debug_port, url):
    request = urllib.request.Request(
        f"http://127.0.0.1:{debug_port}/json/new?{url}", method="PUT")
    with urllib.request.urlopen(request, timeout=3) as response:
        target = json.loads(response.read())
    cdp = CDP(target["webSocketDebuggerUrl"])
    for domain in ("Runtime", "Log", "Page", "Network"):
        cdp.call(domain + ".enable")
    return cdp


def _public_clients(cdp):
    cdp.evaluate("""(async()=>{
      for(const src of ['/api/account/static/mojo-auth.js','/api/account/static/admin-source-session.js']){
        await new Promise((resolve,reject)=>{const s=document.createElement('script');s.src=src;s.onload=resolve;s.onerror=reject;document.head.append(s)});
      }
      MojoAuth.init({baseURL:location.origin});return true;
    })()""")


@th.django_unit_test("real Chrome proves packaged Portal privacy, renewal and two-tab logout ordering")
@th.requires_extra("slow")
def test_admin_security_real_chrome(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import admin_artifact, admin_assets
    executable = os.environ.get("MOJO_ADMIN_CHROME", "")
    assert executable and Path(executable).is_file(), (
        "Set MOJO_ADMIN_CHROME to an exact executable; browser acceptance cannot skip.")
    email = "packaged-portal-browser-4060@test.com"
    password = "Portal-browser-test-4060!"
    User.objects.filter(username=email).delete()
    user = User.objects.create_user(username=email, email=email, password=password)
    user.is_active = user.is_email_verified = user.is_superuser = True
    user.requires_mfa = False
    user.save()
    evidence = ROOT / "testproject/var/admin-browser-4060"
    evidence.mkdir(parents=True, exist_ok=True)
    identity = admin_artifact.validate(admin_assets.ROOT_V2, admin_artifact.PINNED_MANIFEST_SHA256)
    (evidence / "artifact.json").write_text(json.dumps({
        "manifest_sha256": identity["manifest_sha256"], "inventory": list(identity["inventory"])}))
    try:
        # Both mounts run the exact same packaged bytes; every process has its
        # own origin, profile, deadlines and response-delay controls.
        for mount in ("admin", "operations"):
            server = chrome = None
            pages = []
            with tempfile.TemporaryDirectory(prefix="mojo-admin-chrome-") as profile:
                try:
                    port, debug_port = _port(), _port()
                    origin = f"http://127.0.0.1:{port}"
                    root_path = f"/{mount}/"
                    server = subprocess.Popen(
                        [sys.executable, "-c", SERVER, str(ROOT), str(port), mount],
                        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    _json(origin + "/__rider/state", time.monotonic() + DEADLINE_SECONDS)
                    chrome = subprocess.Popen([
                        executable, "--headless=new", "--no-first-run", "--no-default-browser-check",
                        "--disable-background-networking", "--disable-component-update",
                        "--remote-debugging-address=127.0.0.1",
                        f"--remote-debugging-port={debug_port}", f"--user-data-dir={profile}",
                        "--remote-allow-origins=*", "about:blank",
                    ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    _json(f"http://127.0.0.1:{debug_port}/json/version", time.monotonic() + DEADLINE_SECONDS)
                    cdp = _new_page(debug_port, origin + root_path)
                    pages.append(cdp)
                    cdp.wait("!!window.MojoAuth && !!window.MojoAdminSourceSession", "public gate")
                    assert cdp.evaluate("document.body.textContent.includes('Admin access')"), "anonymous gate leaked private UI"
                    assert cdp.evaluate(f"fetch('{root_path}v2/index.html').then(r=>r.status)") == 404, "anonymous private document was readable"
                    cdp.evaluate(
                        "MojoAdminSourceSession.explicitLogin(()=>MojoAuth.login("
                        + json.dumps(email) + "," + json.dumps(password) + ")).then(()=>MojoAdminSourceSession.issue(MojoAuth)).then(()=>true)")
                    cdp.call("Page.navigate", {"url": origin + root_path})
                    cdp.wait("!![...document.querySelectorAll('a')].find(x=>x.textContent==='Open Portal')", "legacy Open Portal")
                    assert cdp.evaluate("[...document.querySelectorAll('a')].find(x=>x.textContent==='Open Portal').getAttribute('href')") == root_path + "v2/", "configured path handoff drifted"
                    cdp.evaluate("[...document.querySelectorAll('a')].find(x=>x.textContent==='Open Portal').click()")
                    cdp.wait("!!document.querySelector('.app')", "packaged Portal")
                    assert cdp.evaluate("location.pathname") == root_path + "v2/", "Portal mount changed"
                    # Exact byte identity plus a protected lazy chunk. Metadata
                    # is intentionally not an HTTP endpoint.
                    entry = admin_assets.ROOT_V2.joinpath("index.html").read_bytes()
                    browser_hash = cdp.evaluate("""fetch(location.pathname+'index.html').then(r=>r.arrayBuffer()).then(b=>crypto.subtle.digest('SHA-256',b)).then(b=>[...new Uint8Array(b)].map(x=>x.toString(16).padStart(2,'0')).join(''))""")
                    import hashlib
                    assert browser_hash == hashlib.sha256(entry).hexdigest(), "artifact identity differs in the browser"
                    lazy = next(name for name in identity["allowlist"] if name.endswith(".js") and "index-" not in name)
                    assert cdp.evaluate(f"fetch({json.dumps(root_path + 'v2/' + lazy)}).then(r=>r.status)") == 200, "protected lazy chunk unavailable"
                    assert cdp.evaluate("fetch('admin-artifact.json').then(r=>r.status)") == 404, "provenance was deliverable"
                    before = len(_json(origin + "/__rider/state", time.monotonic() + 2))
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        if len(_json(origin + "/__rider/state", deadline)) >= before + 2:
                            break
                        time.sleep(.1)
                    else:
                        raise AssertionError("short-TTL renewal did not occur")
                    cdp.call("Page.reload")
                    cdp.wait("!!document.querySelector('.app')", "reload after renewal")
                    api_urls = cdp.evaluate("performance.getEntriesByType('resource').map(x=>x.name).filter(x=>x.includes('/api/'))")
                    assert api_urls and all(url.startswith(origin + "/api/") for url in api_urls), "API derived an endpoint from the Admin mount"
                    # The unremembered choice survives a full page reload.
                    cdp.evaluate("for(const k of ['access_token','refresh_token']){const v=localStorage.getItem(k);if(v)sessionStorage.setItem(k,v);localStorage.removeItem(k)};true")
                    cdp.call("Page.reload")
                    cdp.wait("!!document.querySelector('.app')", "sessionStorage reload")
                    assert cdp.evaluate("!!sessionStorage.getItem('access_token') && !localStorage.getItem('access_token')"), "reload promoted an unremembered session"
                    # Another legacy tab uses its historical localStorage auth;
                    # this tab retains its own sessionStorage grant and binding.
                    cdp.evaluate("for(const k of ['access_token','refresh_token']){const v=sessionStorage.getItem(k);if(v)localStorage.setItem(k,v)};true")

                    for theme in ("light", "dark"):
                        cdp.call("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-color-scheme", "value": theme}]})
                        cdp.evaluate(f"document.documentElement.dataset.theme={json.dumps(theme)}")
                        for width, label in ((1280, "desktop"), (390, "narrow")):
                            cdp.call("Emulation.setDeviceMetricsOverride", {
                                "width": width, "height": 844, "deviceScaleFactor": 1, "mobile": False})
                            assert cdp.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), "narrow viewport overflows"
                            png = base64.b64decode(cdp.call("Page.captureScreenshot", {"format": "png"})["data"])
                            assert len(png) > 1000, "dark theme or desktop screenshot was empty"
                            (evidence / f"{mount}-{theme}-{label}.png").write_bytes(png)

                    # Strict script policy is measured in the protected document.
                    csp = cdp.evaluate("""(async()=>{
                      window.__csp=[];document.addEventListener('securitypolicyviolation',e=>__csp.push({directive:e.effectiveDirective,blocked:e.blockedURI}));
                      const s=document.createElement('script');s.textContent='window.__inlineRan=true';document.head.append(s);
                      let evalDenied=false;try{window.eval('1+1')}catch(_){evalDenied=true}
                      let foreignDenied=false;try{await fetch('https://example.invalid/forbidden')}catch(_){foreignDenied=true}
                      return {inline:!!window.__inlineRan,evalDenied,foreignDenied};
                    })()""")
                    assert csp == {"inline": False, "evalDenied": True, "foreignDenied": True}, "CSP script/connect denials weakened"
                    (evidence / f"{mount}-csp.json").write_text(json.dumps(csp))
                    # Controlled provider/media/frame/realtime fixtures exercise
                    # the actual browser mechanisms without external writes.
                    # Run current policy first; a failed required mechanism is
                    # evidence for a narrow v2 directive change, never a skip.
                    surfaces = cdp.evaluate("""(async()=>{
                      const svg='<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"><rect width="2" height="2" fill="red"/></svg>';
                      const blob=URL.createObjectURL(new Blob([svg],{type:'image/svg+xml'}));
                      const image=src=>new Promise(resolve=>{const i=new Image();i.onload=()=>resolve(true);i.onerror=()=>resolve(false);i.src=src;setTimeout(()=>resolve(false),1500)});
                      const audio=new Uint8Array(48),v=new DataView(audio.buffer),word=(o,s)=>[...s].forEach((c,i)=>v.setUint8(o+i,c.charCodeAt(0)));
                      word(0,'RIFF');v.setUint32(4,40,true);word(8,'WAVE');word(12,'fmt ');v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);v.setUint32(24,8000,true);v.setUint32(28,8000,true);v.setUint16(32,1,true);v.setUint16(34,8,true);word(36,'data');v.setUint32(40,4,true);
                      const mediaUrl=URL.createObjectURL(new Blob([audio],{type:'audio/wav'}));
                      const media=new Promise(resolve=>{const a=document.createElement('audio');a.preload='metadata';a.onloadedmetadata=()=>resolve(true);a.onerror=()=>resolve(false);a.src=mediaUrl;document.body.append(a);setTimeout(()=>resolve(false),1500)});
                      const ws=new Promise(resolve=>{const w=new WebSocket(location.origin.replace('http','ws')+'/__rider/socket');w.onmessage=()=>{resolve(true);w.close()};w.onerror=()=>resolve(false);setTimeout(()=>{resolve(false);w.close()},1500)});
                      const frame=new Promise(resolve=>{const f=document.createElement('iframe');f.sandbox='';f.onload=()=>resolve(true);f.srcdoc='<meta http-equiv="Content-Security-Policy" content="default-src &apos;none&apos;; style-src &apos;unsafe-inline&apos;"><p>Sanitized email fixture</p>';document.body.append(f);setTimeout(()=>resolve(false),1500)});
                      const upload=fetch('/__rider/upload',{method:'PUT',credentials:'omit',body:new Blob(['fixture'])}).then(r=>r.json());
                      const [dataImage,blobImage,blobMedia,realtime,sandboxedFrame,provider]=await Promise.all([image('data:image/svg+xml,'+encodeURIComponent(svg)),image(blob),media,ws,frame,upload]);
                      URL.revokeObjectURL(blob);URL.revokeObjectURL(mediaUrl);
                      return {dataImage,blobImage,blobMedia,realtime,sandboxedFrame,provider};
                    })()""")
                    (evidence / f"{mount}-csp-surfaces.json").write_text(json.dumps(surfaces))
                    assert surfaces["provider"] == {"received": 7, "authorization": False}, "provider fixture received API credentials"
                    assert all(surfaces[key] for key in ("dataImage", "blobImage", "blobMedia", "realtime", "sandboxedFrame")), f"current packaged CSP blocks a required mechanism: {surfaces}"

                    # Real Set-Cookie race: the server delays POST headers. The
                    # other tab tombstones immediately and must wait for POST's
                    # complete response before its DELETE is allowed to run.
                    second = _new_page(debug_port, origin + root_path)
                    pages.append(second)
                    second.wait("!!window.MojoAuth && !!window.MojoAdminSourceSession", "second legacy tab")
                    await_state = _json(origin + "/__rider/delay?POST=1", time.monotonic() + 2)
                    _public_clients(cdp)
                    cdp.evaluate("window.__issueDone=false;window.__issueError='';MojoAdminSourceSession.issue(MojoAuth).then(()=>__issueDone=true).catch(e=>{__issueError=e.message;__issueDone=true});true")
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        history = _json(origin + "/__rider/state", deadline)
                        if history and history[-1]["phase"] == "start":
                            break
                        time.sleep(.025)
                    second.evaluate(f"window.__logoutDone=false;MojoAdminSourceSession.revoke({json.dumps(root_path)},()=>MojoAuth.logout()).then(()=>__logoutDone=true);true")
                    second.wait("JSON.parse(localStorage.getItem('mojo:admin-source-generation:v1')).state==='revoked'", "logout tombstone")
                    second.wait("window.__logoutDone", "two-tab revocation")
                    cdp.wait("window.__issueDone", "superseded issuance")
                    assert cdp.evaluate("!!window.__issueError"), "pending issuer declared ready after logout"
                    history = _json(origin + "/__rider/state", time.monotonic() + 2)
                    delete_at = next(i for i, row in enumerate(history) if row["method"] == "DELETE" and row["phase"] == "start")
                    assert history[delete_at - 1]["method"] == "POST" and history[delete_at - 1]["phase"] == "end", "DELETE raced ahead of delayed Set-Cookie"
                    assert second.evaluate(f"fetch({json.dumps(root_path + 'v2/' + lazy)}).then(r=>r.status)") == 404, "post-logout denial failed"
                    assert second.evaluate("MojoAdminSourceSession.issue(MojoAuth).then(()=>false).catch(()=>true)"), "revoked generation issued again"
                    cdp.call("Page.reload")
                    cdp.wait("document.body.textContent.includes('Admin access')", "suspended sessionStorage resume")
                    assert not cdp.evaluate("!!document.querySelector('.app')"), "suspended tab resurrected private UI"
                    # Reverse ordering: DELETE owns the lock before an old
                    # issuer wakes. It must never POST, even after DELETE ends.
                    second.evaluate(
                        "MojoAdminSourceSession.explicitLogin(()=>MojoAuth.login("
                        + json.dumps(email) + "," + json.dumps(password)
                        + ")).then(()=>MojoAdminSourceSession.issue(MojoAuth)).then(()=>true)")
                    _json(origin + "/__rider/delay?DELETE=1", time.monotonic() + 2)
                    count_before = len([row for row in _json(origin + "/__rider/state", time.monotonic() + 2) if row["method"] == "POST"])
                    second.evaluate(f"window.__logoutDone=false;MojoAdminSourceSession.revoke({json.dumps(root_path)},()=>MojoAuth.logout()).then(()=>__logoutDone=true);true")
                    assert cdp.evaluate("MojoAdminSourceSession.issue(MojoAuth).then(()=>false).catch(()=>true)"), "old issuer ran behind pending DELETE"
                    second.wait("window.__logoutDone", "revoke-before-issue ordering")
                    count_after = len([row for row in _json(origin + "/__rider/state", time.monotonic() + 2) if row["method"] == "POST"])
                    assert count_before == count_after, "an invalidated issuer POSTed after revocation"
                    # Missing browser coordination must be visibly unsupported.
                    unsupported = _new_page(debug_port, "about:blank")
                    pages.append(unsupported)
                    unsupported.call("Page.addScriptToEvaluateOnNewDocument", {"source": "Object.defineProperty(navigator,'locks',{value:undefined})"})
                    unsupported.call("Page.navigate", {"url": origin + root_path})
                    unsupported.wait("document.body.textContent.includes('supported browser')", "unsupported coordination")
                    state = second.evaluate("JSON.parse(localStorage.getItem('mojo:admin-source-generation:v1'))")
                    assert set(state) == {"version", "generation", "state"}, "protocol persisted credential data"
                    history = _json(origin + "/__rider/state", time.monotonic() + 2)
                    (evidence / f"{mount}-races.json").write_text(json.dumps(history))
                    (evidence / f"{mount}-console.json").write_text(json.dumps(cdp.failures))
                finally:
                    for page in pages:
                        page.close()
                    _stop(chrome)
                    _stop(server)
    finally:
        User.objects.filter(pk=user.pk).delete()
