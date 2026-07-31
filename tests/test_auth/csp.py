"""
Content-Security-Policy on the framework-hosted auth pages (maestro item 945).

What is covered
---------------
* The live `GET /passkey` response — the ONLY hosted auth page that is not
  bouncer-gated, and therefore the only one whose real response headers a test
  client can assert (a cold client hitting `/auth`, `/register` or `/contact` is
  served `bouncer_challenge.html`, which deliberately carries no CSP).
* In-process renders through `_serve_login` / `_serve_contact` /
  `_auth_context` + `render`, which reach every one of the four templates —
  that is what catches a child template someone forgot to nonce.
* `csp.build_policy()` / `csp.apply()` called directly for the settings
  behavior. Those CANNOT be asserted against the live server: its Django
  settings are fixed at startup and never see an in-process patch.

On the acceptance criterion "tests assert a known-bad inline injection is
blocked"
------------------------------------------------------------------------
No browser runs in this suite, so nothing here can execute a script and observe
the browser refuse it. These tests do NOT prove browser enforcement. What they
prove is the pair of properties that jointly PRODUCE the block:

  (a) the emitted `script-src` carries a nonce and no `'unsafe-inline'`
      (and no wildcard / bare-scheme source), and
  (b) every `<script>` tag in the rendered body either carries that exact
      nonce or is a `type="application/json"` data block.

Any script text that did not come from a nonce-stamped template tag therefore
lacks the nonce and cannot run under (a). `test_injection_guard_has_teeth`
confirms the (b) check is non-vacuous by feeding it a body with an injected
un-nonce'd `<script>` and requiring the check to reject it.

Header lookups are case-normalized: `testit/client.py` flattens `requests`'
`CaseInsensitiveDict` into a plain dict, and header-name casing differs between
`h11` and `httptools`.
"""
import re

from testit import helpers as th
from testit.helpers import assert_true, assert_eq


PASSKEY_PATH = "/passkey"

# Every template that extends account/auth_base.html.
HOSTED_TEMPLATES = ('account/login.html', 'account/register.html',
                    'account/passkey_enroll.html', 'account/contact.html')

# <script ...> open tags only; the closing tag is irrelevant to CSP.
SCRIPT_TAG_RE = re.compile(r'<script([^>]*)>', re.IGNORECASE)
NONCE_SOURCE_RE = re.compile(r"'nonce-([0-9a-f]{32})'")

_SENTINEL = object()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _response_headers(opts):
    """Lowercase-normalized header dict for the client's last response."""
    return {k.lower(): v for k, v in opts.client.last_response.headers.items()}


def _policy_from(opts):
    """The enforcing CSP header off the last live response, or ''."""
    return _response_headers(opts).get("content-security-policy", "")


def _directive(policy, name):
    """Value of one directive in a policy string; None when absent."""
    for chunk in policy.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(None, 1)
        if parts[0].lower() == name:
            return parts[1].strip() if len(parts) > 1 else ""
    return None


def _script_tag_attrs(html):
    """Attribute text of every <script> open tag in `html`."""
    return [m.group(1) for m in SCRIPT_TAG_RE.finditer(html)]


def _unguarded_script_tags(html, nonce):
    """Script tags that are neither nonce-stamped nor JSON data blocks.

    An empty list is the invariant that makes the nonce policy meaningful:
    anything injected into the body after render lands here, has no nonce, and
    cannot execute under a nonce-only `script-src`.
    """
    good_nonce = f'nonce="{nonce}"'
    bad = []
    for attrs in _script_tag_attrs(html):
        if nonce and good_nonce in attrs:
            continue
        if 'type="application/json"' in attrs:
            continue
        bad.append(attrs)
    return bad


def _render_hosted(template_name, group=None):
    """Render a hosted auth template through the bouncer's context builder."""
    from django.test import RequestFactory
    from django.shortcuts import render
    from mojo.apps.account.rest.bouncer.views import _auth_context
    from mojo.apps.account.services import public_message as svc

    factory = RequestFactory()
    request = factory.get('/auth')
    ctx = _auth_context(request, group=group)
    ctx['page_mode'] = 'login'
    ctx['page_title'] = 'Sign In'
    if 'contact' in template_name:
        # contact.html needs the kind block _serve_contact supplies.
        ctx.update(svc.render_context_for_kind('contact_us'))
        ctx['contact_submit_url'] = 'account/bouncer/message'
    return render(request, template_name, ctx).content.decode('utf-8'), ctx


def _save_conf(name):
    from django.conf import settings as dj_settings
    return getattr(dj_settings, name, _SENTINEL)


def _restore_conf(name, orig):
    from django.conf import settings as dj_settings
    if orig is _SENTINEL:
        if hasattr(dj_settings, name):
            delattr(dj_settings, name)
    else:
        setattr(dj_settings, name, orig)


def _set_conf(name, value):
    from django.conf import settings as dj_settings
    setattr(dj_settings, name, value)


# ---------------------------------------------------------------------------
# Live server — GET /passkey, the one un-gated hosted auth page.
# Asserts the SHIPPED defaults only (server settings are fixed at startup).
# ---------------------------------------------------------------------------

@th.django_unit_test("GET /passkey carries a Content-Security-Policy with every core directive")
def test_passkey_page_sets_csp_header(opts):
    resp = opts.client.get(PASSKEY_PATH)
    assert_eq(resp.status_code, 200,
              f"GET {PASSKEY_PATH} must render the passkey enrollment page, "
              f"got {resp.status_code}")

    policy = _policy_from(opts)
    assert_true(
        bool(policy),
        f"GET {PASSKEY_PATH} must set a Content-Security-Policy header. "
        f"Headers present: {sorted(_response_headers(opts))}")

    expected = {
        "default-src": "'self'",
        "base-uri": "'none'",
        "object-src": "'none'",
        "frame-ancestors": "'none'",
        "form-action": "'self'",
    }
    for name, value in expected.items():
        assert_eq(
            _directive(policy, name), value,
            f"CSP directive `{name}` must be `{value}` on the passkey page. "
            f"Full policy: {policy!r}")

    for name in ("script-src", "style-src", "img-src", "font-src", "connect-src"):
        assert_true(
            _directive(policy, name),
            f"CSP must declare a non-empty `{name}` directive. "
            f"Full policy: {policy!r}")


@th.django_unit_test("script-src is nonce-locked: no unsafe-inline, no unsafe-eval, no wildcard")
def test_script_src_is_nonce_locked(opts):
    opts.client.get(PASSKEY_PATH)
    policy = _policy_from(opts)
    script_src = _directive(policy, "script-src")
    assert_true(script_src,
                f"CSP must declare script-src. Full policy: {policy!r}")

    sources = script_src.split()
    for banned in ("'unsafe-inline'", "'unsafe-eval'", "*", "http:", "https:", "data:"):
        assert_true(
            banned not in sources,
            f"script-src must not contain {banned} — it would defeat the nonce "
            f"and let injected inline script run. Got: {script_src!r}")

    nonces = NONCE_SOURCE_RE.findall(script_src)
    assert_eq(len(nonces), 1,
              f"script-src must carry exactly one 'nonce-<32 hex>' source, "
              f"found {len(nonces)} in {script_src!r}")


@th.django_unit_test("every <script> in the served page is nonce-stamped or a JSON data block")
def test_served_scripts_are_all_guarded(opts):
    """The honest reading of 'a known-bad inline injection is blocked'.

    See the module docstring: no browser runs here, so this asserts the two
    properties that produce the block rather than the block itself.
    """
    resp = opts.client.get(PASSKEY_PATH)
    html = resp.response if isinstance(resp.response, str) else (resp.text or "")
    policy = _policy_from(opts)
    script_src = _directive(policy, "script-src") or ""
    nonces = NONCE_SOURCE_RE.findall(script_src)
    assert_eq(len(nonces), 1,
              f"expected exactly one nonce in script-src, got {script_src!r}")
    nonce = nonces[0]

    tags = _script_tag_attrs(html)
    assert_true(len(tags) >= 3,
                f"the passkey page should render at least three <script> tags "
                f"(mojo-auth.js, the auth_base IIFE, the page script), found "
                f"{len(tags)}")

    unguarded = _unguarded_script_tags(html, nonce)
    assert_eq(
        unguarded, [],
        f"every <script> tag on a hosted auth page must carry "
        f"nonce=\"{nonce}\" (the value in the CSP header) or be a "
        f"type=\"application/json\" data block. Unguarded tags: {unguarded!r}")

    assert_true(
        "'unsafe-inline'" not in script_src,
        f"the nonce only blocks injected script while script-src omits "
        f"'unsafe-inline'. Got: {script_src!r}")


@th.django_unit_test("the injected-script guard is non-vacuous")
def test_injection_guard_has_teeth(opts):
    """Prove the check in test_served_scripts_are_all_guarded can fail.

    An invariant that no input can violate proves nothing. Feed the same check
    a body carrying an un-nonce'd `<script>` — exactly the shape a reflected or
    stored injection would take — and require it to be flagged.
    """
    nonce = "a" * 32
    clean = f'<script nonce="{nonce}">var x = 1;</script>'
    injected = clean + '<script>alert(1)</script>'

    assert_eq(_unguarded_script_tags(clean, nonce), [],
              "a body whose only script tag carries the nonce must produce no "
              "findings — otherwise the guard is a false-positive machine")
    flagged = _unguarded_script_tags(injected, nonce)
    assert_eq(len(flagged), 1,
              f"an injected un-nonce'd <script> must be flagged by the same "
              f"check the served-page test relies on, got {flagged!r}")


@th.django_unit_test("the nonce is fresh per request and matches that response's markup")
def test_nonce_is_per_request(opts):
    seen = []
    for attempt in (1, 2):
        resp = opts.client.get(PASSKEY_PATH)
        html = resp.response if isinstance(resp.response, str) else (resp.text or "")
        script_src = _directive(_policy_from(opts), "script-src") or ""
        nonces = NONCE_SOURCE_RE.findall(script_src)
        assert_eq(len(nonces), 1,
                  f"request {attempt}: expected one nonce in script-src, "
                  f"got {script_src!r}")
        nonce = nonces[0]
        assert_eq(
            _unguarded_script_tags(html, nonce), [],
            f"request {attempt}: this response's markup must carry THIS "
            f"response's nonce ({nonce}) on every script tag")
        seen.append(nonce)

    assert_true(
        seen[0] != seen[1],
        f"a CSP nonce must be minted per request — two /passkey responses "
        f"reused {seen[0]!r}. A predictable/reused nonce is forgeable by an "
        f"attacker who can read one page.")


@th.django_unit_test("json_script data blocks are served without a nonce, by design")
def test_json_script_is_an_unnonced_data_block(opts):
    """`{{ x|json_script:"id" }}` emits type="application/json", which HTML
    treats as a data block: it is never executed and never CSP-checked, and it
    is read via .textContent. Giving it a nonce would be noise."""
    resp = opts.client.get(PASSKEY_PATH)
    html = resp.response if isinstance(resp.response, str) else (resp.text or "")

    idx = html.find('id="mat-login-methods"')
    assert_true(idx != -1,
                "auth_base.html must still emit the mat-login-methods "
                "json_script block that the inline IIFE parses")
    start = html.rfind("<script", 0, idx)
    end = html.find(">", idx)
    tag = html[start:end + 1]
    assert_true('type="application/json"' in tag,
                f"the login-methods block must be a type=\"application/json\" "
                f"data block, got: {tag!r}")
    assert_true('nonce=' not in tag,
                f"a json_script data block must NOT be nonce'd — it is not "
                f"executable content. Got: {tag!r}")


# ---------------------------------------------------------------------------
# In-process renders — reach all four templates, and the real view helpers.
# ---------------------------------------------------------------------------

@th.django_unit_test("every hosted auth template nonces every inline script it renders")
def test_all_hosted_templates_nonce_their_scripts(opts):
    for template_name in HOSTED_TEMPLATES:
        html, ctx = _render_hosted(template_name)
        nonce = ctx.get('csp_nonce') or ''
        assert_eq(len(nonce), 32,
                  f"_auth_context must put a 32-hex csp_nonce in the context "
                  f"for {template_name}, got {nonce!r}")
        tags = _script_tag_attrs(html)
        assert_true(len(tags) >= 3,
                    f"{template_name} should render at least three <script> "
                    f"tags, found {len(tags)}")
        unguarded = _unguarded_script_tags(html, nonce)
        assert_eq(
            unguarded, [],
            f"{template_name} renders a <script> tag with neither "
            f"nonce=\"{{{{ csp_nonce }}}}\" nor type=\"application/json\": "
            f"{unguarded!r}. It will not execute under the page's CSP.")


@th.django_unit_test("login/register/passkey forbid framing; /contact deliberately does not")
def test_frame_ancestors_is_per_page(opts):
    """`/contact` is documented as iframe-embeddable from an external marketing
    site (docs/web_developer/account/public_messages.md), so it must NOT carry
    frame-ancestors. The token-holding pages must."""
    from django.test import RequestFactory
    from mojo.apps.account.rest.bouncer.views import _serve_login, _serve_contact
    from mojo.apps.account.services import csp

    factory = RequestFactory()

    for page_mode, label in (('login', '/auth'), ('register', '/register')):
        response = _serve_login(factory.get('/auth'), page_mode=page_mode)
        policy = response[csp.HEADER]
        assert_eq(_directive(policy, "frame-ancestors"), "'none'",
                  f"{label} holds tokens in localStorage and must refuse "
                  f"framing. Policy: {policy!r}")

    response = _serve_contact(factory.get('/contact'), kind='contact_us')
    policy = response[csp.HEADER]
    assert_true(
        bool(policy),
        "/contact must still carry a CSP — only the frame-ancestors directive "
        "is dropped")
    assert_eq(
        _directive(policy, "frame-ancestors"), None,
        f"/contact must OMIT frame-ancestors entirely — embedding "
        f"/contact?kind=<kind> in an iframe is the documented integration for "
        f"an external marketing site. Policy: {policy!r}")
    assert_eq(_directive(policy, "form-action"), "'self'",
              f"/contact must keep the rest of the policy. Got: {policy!r}")


@th.django_unit_test("build_policy: frame_ancestors argument controls the directive")
def test_build_policy_frame_ancestors_argument(opts):
    from mojo.apps.account.services import csp

    default_policy = csp.build_policy('abc')
    assert_eq(_directive(default_policy, "frame-ancestors"), "'none'",
              f"build_policy must default to frame-ancestors 'none'. "
              f"Got: {default_policy!r}")

    dropped = csp.build_policy('abc', frame_ancestors="")
    assert_eq(_directive(dropped, "frame-ancestors"), None,
              f"frame_ancestors='' must drop the directive entirely, not emit "
              f"an empty one. Got: {dropped!r}")
    assert_true("frame-ancestors" not in dropped,
                f"the directive name must not appear at all when dropped. "
                f"Got: {dropped!r}")


@th.django_unit_test("a cross-origin api_base reaches connect-src and style-src, never script-src")
def test_api_origin_placement(opts):
    from mojo.apps.account.services import csp

    origin = 'https://api.example.com'
    policy = csp.build_policy('abc', api_base=f'{origin}/some/path')

    assert_true(origin in (_directive(policy, "connect-src") or ""),
                f"a cross-origin api_base must be allowed in connect-src — "
                f"mojo-auth.js and contact.html fetch it. Got: {policy!r}")
    assert_true(origin in (_directive(policy, "style-src") or ""),
                f"a cross-origin api_base serves mojo-auth-theme.css, so its "
                f"origin must be in style-src. Got: {policy!r}")
    assert_true(origin not in (_directive(policy, "script-src") or ""),
                f"script-src must stay nonce-only — the nonce on the "
                f"mojo-auth.js tag authorizes the cross-origin load without a "
                f"host source. Got: {policy!r}")

    plain = csp.build_policy('abc')
    assert_eq(_directive(plain, "connect-src"), "'self'",
              f"an empty api_base must add no origin. Got: {plain!r}")
    relative = csp.build_policy('abc', api_base='/api')
    assert_eq(_directive(relative, "connect-src"), "'self'",
              f"a relative api_base has no origin to add. Got: {relative!r}")


@th.django_unit_test("whitelabel assets keep loading: img-src is open, style-src allows inline")
def test_whitelabel_assets_still_permitted(opts):
    from mojo.apps.account.services import csp

    policy = csp.build_policy('abc')
    img_src = _directive(policy, "img-src") or ""
    assert_true('*' in img_src.split(),
                f"logo_url / hero_image_url / favicon_url are tenant-supplied "
                f"with no host validation, so img-src must permit any origin. "
                f"Got: {img_src!r}")
    assert_true('data:' in img_src.split(),
                f"validate_custom_css permits data: URIs, so img-src must too. "
                f"Got: {img_src!r}")

    style_src = _directive(policy, "style-src") or ""
    assert_true("'unsafe-inline'" in style_src,
                f"the auth templates use style attributes and inline "
                f"<style>{{ custom_css }}</style>, so style-src must keep "
                f"'unsafe-inline'. Got: {style_src!r}")
    assert_true("nonce-" not in style_src,
                f"a nonce in style-src makes browsers IGNORE 'unsafe-inline', "
                f"breaking every style attribute on the page. Got: {style_src!r}")
    assert_true('https:' in style_src.split(),
                f"a tenant custom_css_url is https-validated but arbitrary-host, "
                f"so style-src must allow https:. Got: {style_src!r}")


# ---------------------------------------------------------------------------
# Settings behavior — direct calls only. The live server loaded its settings at
# startup and can never observe an in-process patch.
# ---------------------------------------------------------------------------

@th.django_unit_test("AUTH_CSP_DIRECTIVES replaces a directive wholesale")
def test_directive_override_replaces(opts):
    from mojo.apps.account.services import csp

    orig = _save_conf('AUTH_CSP_DIRECTIVES')
    try:
        _set_conf('AUTH_CSP_DIRECTIVES', {"frame-ancestors": "'self'"})
        policy = csp.build_policy('abc')
        assert_eq(_directive(policy, "frame-ancestors"), "'self'",
                  f"a deployment-supplied frame-ancestors must replace the "
                  f"baseline value. Got: {policy!r}")
        assert_eq(_directive(policy, "default-src"), "'self'",
                  f"an override must leave the other directives alone. "
                  f"Got: {policy!r}")
    finally:
        _restore_conf('AUTH_CSP_DIRECTIVES', orig)


@th.django_unit_test("AUTH_CSP_DIRECTIVES can add an unknown directive and drop a known one")
def test_directive_override_adds_and_drops(opts):
    from mojo.apps.account.services import csp

    orig = _save_conf('AUTH_CSP_DIRECTIVES')
    try:
        _set_conf('AUTH_CSP_DIRECTIVES', {
            "report-uri": "/csp/report",
            "font-src": "",
        })
        policy = csp.build_policy('abc')
        assert_eq(_directive(policy, "report-uri"), "/csp/report",
                  f"an unknown key must be emitted as-is so a deployment can "
                  f"add report-uri. Got: {policy!r}")
        assert_eq(_directive(policy, "font-src"), None,
                  f"an empty override value must DROP the directive. "
                  f"Got: {policy!r}")
    finally:
        _restore_conf('AUTH_CSP_DIRECTIVES', orig)


@th.django_unit_test("the nonce is appended to a deployment-supplied script-src and cannot be removed")
def test_nonce_survives_script_src_override(opts):
    from mojo.apps.account.services import csp

    orig = _save_conf('AUTH_CSP_DIRECTIVES')
    try:
        _set_conf('AUTH_CSP_DIRECTIVES', {"script-src": "'self' https://cdn.example.com"})
        policy = csp.build_policy('deadbeef')
        script_src = _directive(policy, "script-src") or ""
        assert_true("'nonce-deadbeef'" in script_src,
                    f"the per-request nonce must always be appended to the "
                    f"final script-src, or the page's own inline scripts stop "
                    f"running. Got: {script_src!r}")
        assert_true('https://cdn.example.com' in script_src,
                    f"the deployment's own script source must survive. "
                    f"Got: {script_src!r}")

        _set_conf('AUTH_CSP_DIRECTIVES', {"script-src": ""})
        policy = csp.build_policy('deadbeef')
        assert_eq(_directive(policy, "script-src"), "'nonce-deadbeef'",
                  f"emptying script-src must NOT drop it — the nonce is not "
                  f"removable, AUTH_CSP_ENABLED=False is the opt-out. "
                  f"Got: {policy!r}")
    finally:
        _restore_conf('AUTH_CSP_DIRECTIVES', orig)


@th.django_unit_test("AUTH_CSP_ENABLED=False sets no CSP header at all")
def test_csp_can_be_disabled(opts):
    from django.http import HttpResponse
    from mojo.apps.account.services import csp

    orig = _save_conf('AUTH_CSP_ENABLED')
    try:
        _set_conf('AUTH_CSP_ENABLED', False)
        response = csp.apply(HttpResponse('ok'), 'abc')
        assert_true(csp.HEADER not in response,
                    f"AUTH_CSP_ENABLED=False must set no enforcing header. "
                    f"Got: {response.get(csp.HEADER)!r}")
        assert_true(csp.REPORT_ONLY_HEADER not in response,
                    f"AUTH_CSP_ENABLED=False must set no report-only header "
                    f"either. Got: {response.get(csp.REPORT_ONLY_HEADER)!r}")
    finally:
        _restore_conf('AUTH_CSP_ENABLED', orig)

    response = csp.apply(HttpResponse('ok'), 'abc')
    assert_true(csp.HEADER in response,
                "with AUTH_CSP_ENABLED restored to the test project's True the "
                "header must come back — the setting is read per request, not "
                "at import")


@th.django_unit_test("CSP is OPT-IN: absent AUTH_CSP_ENABLED sets no header")
def test_csp_is_opt_in_by_default(opts):
    """The shipped default is OFF. This is the upgrade-safety test.

    A deployment that upgrades without asking for a CSP must get exactly what
    it had before: no header. The test project sets AUTH_CSP_ENABLED = True so
    the live-server tests above can assert a real header, so this test removes
    the setting entirely to see what a stock deployment gets.
    """
    from django.http import HttpResponse
    from mojo.apps.account.services import csp

    orig = _save_conf('AUTH_CSP_ENABLED')
    try:
        from django.conf import settings as dj_settings
        if hasattr(dj_settings, 'AUTH_CSP_ENABLED'):
            delattr(dj_settings, 'AUTH_CSP_ENABLED')
        response = csp.apply(HttpResponse('ok'), 'abc')
        assert_true(csp.HEADER not in response,
                    f"with AUTH_CSP_ENABLED unset the framework must send NO "
                    f"enforcing CSP — the header is opt-in. "
                    f"Got: {response.get(csp.HEADER)!r}")
        assert_true(csp.REPORT_ONLY_HEADER not in response,
                    f"an unset AUTH_CSP_ENABLED must not send a report-only "
                    f"header either. Got: {response.get(csp.REPORT_ONLY_HEADER)!r}")
    finally:
        _restore_conf('AUTH_CSP_ENABLED', orig)


@th.django_unit_test("AUTH_CSP_REPORT_ONLY=True swaps the enforcing header for the report-only one")
def test_report_only_mode(opts):
    from django.http import HttpResponse
    from mojo.apps.account.services import csp

    orig = _save_conf('AUTH_CSP_REPORT_ONLY')
    try:
        _set_conf('AUTH_CSP_REPORT_ONLY', True)
        response = csp.apply(HttpResponse('ok'), 'abc')
        assert_true(csp.REPORT_ONLY_HEADER in response,
                    "AUTH_CSP_REPORT_ONLY=True must set "
                    "Content-Security-Policy-Report-Only")
        assert_true(csp.HEADER not in response,
                    f"report-only mode must NOT also set the enforcing header "
                    f"— the page would still be enforced. Got: "
                    f"{response.get(csp.HEADER)!r}")
        assert_true("'nonce-abc'" in response[csp.REPORT_ONLY_HEADER],
                    f"the report-only policy must still carry the nonce. "
                    f"Got: {response[csp.REPORT_ONLY_HEADER]!r}")
    finally:
        _restore_conf('AUTH_CSP_REPORT_ONLY', orig)
