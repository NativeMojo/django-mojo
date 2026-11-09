"""
Regression (maestro #8579): landing back from a sign-in provider showed the
username/password form while the OAuth completion ran, so it looked as if a
second sign-in was needed.

The hosted page must render a branded "Signing you in" view in the first byte
of HTML whenever the URL carries ?code=&state=, keep the form hidden and inert
behind it, and drive that view through pending -> slow -> success -> redirect,
or failure, from the ONE completion call (#315's single-fire contract).

The state machine runs the page's real inline scripts in node against a small
fake DOM (oauth_callback_pending.js); the first-paint proof in a real browser
is the extended-tier oauth_callback_pending_browser.py.
"""
import html as html_lib
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

TESTIT_TIER = "bug"

ROOT = Path(__file__).resolve().parents[2]
THEME_CSS = ROOT / "mojo/apps/account/static/account/mojo-auth-theme.css"
CALLBACK_QUERY = {'code': 'cb-code-8579', 'state': 'cb-state-8579', 'redirect': '/dashboard'}
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
        'meta', 'source', 'track', 'wbr'}


def _render(template_name, query=None):
    from django.test import RequestFactory
    from django.shortcuts import render
    from mojo.apps.account.rest.bouncer.views import _auth_context
    is_login = template_name.endswith('login.html')
    request = RequestFactory().get('/auth' if is_login else '/register', query or {})
    ctx = _auth_context(request)
    ctx['page_mode'] = 'login' if is_login else 'register'
    ctx['page_title'] = 'Sign In' if is_login else 'Create Account'
    return render(request, template_name, ctx).content.decode('utf-8')


class _Tree(HTMLParser):
    """Element tree (tag, attrs, text, children) plus the inline scripts, in order."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {'tag': '#document', 'attrs': {}, 'text': '', 'children': []}
        self.stack = [self.root]
        self.scripts = []
        self._script = None

    def handle_starttag(self, tag, attrs):
        node = {'tag': tag, 'attrs': {k: (v if v is not None else '') for k, v in attrs},
                'text': '', 'children': []}
        self.stack[-1]['children'].append(node)
        if tag == 'script':
            self._script = node
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and self.stack[-1]['tag'] == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        if tag == 'script' and self._script is not None:
            if 'src' not in self._script['attrs'] and self._script['attrs'].get('type') != 'application/json':
                self.scripts.append(self._script['text'])
            self._script = None
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i]['tag'] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1]['text'] += data


def _parse(html):
    tree = _Tree()
    tree.feed(html)
    return tree


def _find(node, pred):
    if pred(node):
        return node
    for child in node['children']:
        hit = _find(child, pred)
        if hit:
            return hit
    return None


def _by_id(tree, element_id):
    return _find(tree.root, lambda n: n['attrs'].get('id') == element_id)


def _by_tag(tree, tag):
    return _find(tree.root, lambda n: n['tag'] == tag)


def _css_blocks(css, selector_regex):
    """Return the body of every `{...}` that follows a selector match."""
    return [_brace_body(css, m.end()) for m in re.finditer(selector_regex + r"\s*\{", css)]


def _css_block(css, selector_regex):
    """Return the body of the first `{...}` that follows a selector match."""
    match = re.search(selector_regex + r"\s*\{", css)
    return _brace_body(css, match.end()) if match else None


def _brace_body(css, start):
    depth = 1
    for i in range(start, len(css)):
        if css[i] == '{':
            depth += 1
        elif css[i] == '}':
            depth -= 1
            if depth == 0:
                return css[start:i]
    return None


@th.django_unit_test("OAuth callback render: pending view in the first byte, form hidden and inert")
def test_callback_render_hides_form(opts):
    for template, path in (('account/login.html', '/auth'), ('account/register.html', '/register')):
        page = _render(template, CALLBACK_QUERY)
        tree = _parse(page)
        body = _by_tag(tree, 'body')
        assert_true(body and 'mat-oauth-pending' in body['attrs'].get('class', '').split(),
                    f"{template}: a ?code&state landing must put mat-oauth-pending on <body> so the "
                    f"render-blocking theme hides the form before first paint")
        pending = _by_id(tree, 'mat-pending')
        assert_true(pending is not None,
                    f"{template}: the callback landing must server-render #mat-pending")
        assert_eq(pending['attrs'].get('data-state'), 'pending',
                  f"{template}: #mat-pending must start in data-state=pending")
        form_page = _by_id(tree, 'mat-page')
        assert_true(form_page is not None and 'inert' in form_page['attrs'],
                    f"{template}: #mat-page must be inert while completion is pending, so its "
                    f"controls are out of the keyboard order")
        assert_eq(form_page['attrs'].get('aria-hidden'), 'true',
                  f"{template}: #mat-page must be aria-hidden while completion is pending")
        overlay = _by_id(tree, 'mat-overlay')
        assert_eq(overlay['attrs'].get('aria-hidden'), 'true',
                  f"{template}: the idle session overlay must be hidden from screen readers on a callback")
        status = _by_id(tree, 'mat-pending-status')
        assert_true(status is not None and status['attrs'].get('role') == 'status'
                    and status['attrs'].get('aria-live') == 'polite',
                    f"{template}: the pending status text must sit in a role=status aria-live=polite region")
        restart = _by_id(tree, 'mat-pending-restart')
        assert_true(restart is not None and restart['tag'] == 'a',
                    f"{template}: Start over must be a real link that works without script")
        href = html_lib.unescape(restart['attrs'].get('href', ''))
        assert_true(href.startswith(path),
                    f"{template}: Start over must open a fresh {path} page, got {href!r}")
        assert_true('code=' not in href and 'state=' not in href,
                    f"{template}: Start over must never carry the spent callback code/state, got {href!r}")
        assert_true('redirect=%2Fdashboard' in href,
                    f"{template}: Start over must keep the post-login destination, got {href!r}")
        count = page.count('completeOAuthLogin(')
        assert_eq(count, 1,
                  f"{template}: the pending view must share the single completion call site "
                  f"(#315) — got {count}")

        plain = _parse(_render(template, {'redirect': '/dashboard'}))
        plain_body = _by_tag(plain, 'body')
        assert_true('mat-oauth-pending' not in plain_body['attrs'].get('class', ''),
                    f"{template}: a normal visit must not carry the pending body class")
        assert_true(_by_id(plain, 'mat-pending') is None,
                    f"{template}: a normal visit must not render the pending view")
        plain_page = _by_id(plain, 'mat-page')
        assert_true('inert' not in plain_page['attrs'] and 'aria-hidden' not in plain_page['attrs'],
                    f"{template}: a normal visit must leave the form usable")


@th.django_unit_test("theme CSS: pending view hides the form, has both palettes and honours reduced motion")
def test_theme_css_pending_contract(opts):
    css = THEME_CSS.read_text(encoding='utf-8')
    hide = _css_block(css, r"body\.mat-oauth-pending\s+\.mat-page")
    assert_true(hide is not None and re.search(r"display:\s*none", hide),
                "theme CSS must hide .mat-page under body.mat-oauth-pending (no form flash)")
    shown = _css_block(css, r"body\.mat-oauth-pending\s+\.mat-pending")
    assert_true(shown is not None and 'position: fixed' in shown,
                "theme CSS must show .mat-pending full screen under body.mat-oauth-pending")

    motion = _css_block(css, r"@media\s*\(prefers-reduced-motion:\s*reduce\)")
    assert_true(motion is not None and '.mat-pending' in motion,
                "theme CSS must carry a prefers-reduced-motion block for the .mat-pending animation")
    assert_true('data-state="pending"' not in motion,
                "reduced motion must never zero the 15s pending-only Start over reveal")

    blocks = {
        ':root': _css_block(css, r":root"),
        'body.mat-appearance-dark': _css_block(css, r"body\.mat-appearance-dark"),
        'system dark': _css_block(_css_block(css, r"@media\s*\(prefers-color-scheme:\s*dark\)") or '',
                                  r"body\.mat-appearance-system"),
    }
    for name, block in blocks.items():
        assert_true(block is not None, f"theme CSS must define the {name} palette block")
        for token in ('--mat-track', '--mat-danger-text', '--mat-success-strong'):
            assert_true(token + ':' in block, f"theme CSS {name} must declare {token}")

    reveal = _css_block(css, r'\.mat-pending\[data-state="pending"\]\s+\.mat-pending-slow-actions')
    assert_true(reveal is not None and '15s' in reveal,
                "the no-script Start over reveal must be scoped to data-state=pending and wait 15s")
    page = _render('account/login.html', CALLBACK_QUERY)
    assert_true('PENDING_SLOW_MS = 15000' in page,
                "the script's slow threshold must equal the CSS reveal delay (15s)")

    # The tenant accent is set inline on <body>; anything derived from it on
    # :root resolves against the default blue instead.
    assert_true('var(--mat-accent)' not in blocks[':root'],
                "no accent-derived token may be declared on :root — it would ignore the tenant accent")
    body_blocks = _css_blocks(css, r"(?m)^body")
    assert_true(any(re.search(r"--mat-accent-hover:\s*color-mix\(in srgb, var\(--mat-accent\)", b or '')
                    for b in body_blocks),
                "--mat-accent-hover must be declared on body, where the tenant accent applies")
    pending_block = _css_block(css, r"(?m)^\.mat-pending")
    assert_true(pending_block is not None and 'var(--mat-accent)' in pending_block,
                "accent-derived pending colours must be declared on .mat-pending")


@th.django_unit_test("OAuth callback state machine: held completion, success, refusal, slow and late results")
def test_callback_state_machine(opts):
    node = shutil.which('node')
    assert_true(node, "Node.js is required for the hosted-page regression")
    tree = _parse(_render('account/login.html', CALLBACK_QUERY))
    payload = json.dumps({
        'url': 'https://auth.example.test/auth?' + '&'.join(f"{k}={v}" for k, v in CALLBACK_QUERY.items()),
        'tree': tree.root,
        'scripts': tree.scripts,
    })
    result = subprocess.run([node, str(Path(__file__).with_suffix('.js'))],
                            input=payload, capture_output=True, text=True, timeout=20)
    assert_eq(result.returncode, 0,
              "the hosted OAuth callback state machine must pass: " + (result.stderr or result.stdout))
