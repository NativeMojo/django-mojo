"""
The hosted bot-check page (maestro #9132): "One quick check" follows the
hosted sign-in theme and keeps every bouncer mechanic it had.

These are server-render checks against the real `_serve_challenge` output.
The state machine (ready -> checking -> verified / failed -> review -> sent)
runs the real mojo-hosted-bouncer.js in node (challenge_states.py), and the
rendered-browser proof is the opt-in browser.py.
"""
import re
from html.parser import HTMLParser
from pathlib import Path

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

TESTIT_TIER = "framework"

ROOT = Path(__file__).resolve().parents[2]
THEME_CSS = ROOT / "mojo/apps/account/static/account/mojo-auth-theme.css"
REFERENCE = "9132-0123456789abcdef01234567"
TICKET = "ticket-9132-fixture"
GROUP_NAME = "test-9132-challenge-theme"
NOSCRIPT = "JavaScript is needed for verification. You can still request help using the form below."
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
        'meta', 'source', 'track', 'wbr'}


def challenge(query=None, group=None, config=None, page_type='login'):
    """Render the challenge through `_serve_challenge` with a hosted config that
    already carries a reference (so no diagnostic row or Redis budget is used)."""
    import objict
    from django.test import RequestFactory
    from mojo.apps.account.rest.bouncer.views import _serve_challenge

    path = {'login': '/auth', 'registration': '/register', 'public_message': '/contact'}[page_type]
    request = RequestFactory().get(path, query or {})
    request.DATA = objict.objict(query or {})
    cfg = {'descriptor': 'x' * 32, 'next_action': 'check', 'reference': REFERENCE,
           'review_ticket': TICKET}
    cfg.update(config or {})
    return _serve_challenge(request, page_type=page_type, group=group, hosted_config=cfg)


def render(**kwargs):
    return challenge(**kwargs).content.decode('utf-8')


class Tree(HTMLParser):
    """Element tree (tag, attrs, text, children), as in test_auth/oauth_callback_pending.py."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {'tag': '#document', 'attrs': {}, 'text': '', 'children': []}
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = {'tag': tag, 'attrs': {k: (v if v is not None else '') for k, v in attrs},
                'text': '', 'children': []}
        self.stack[-1]['children'].append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and self.stack[-1]['tag'] == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i]['tag'] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1]['text'] += data


def parse(html):
    tree = Tree()
    tree.feed(html)
    return tree


def walk(node):
    yield node
    for child in node['children']:
        yield from walk(child)


def find_all(tree, pred):
    return [n for n in walk(tree.root) if pred(n)]


def by_id(tree, element_id):
    hits = find_all(tree, lambda n: n['attrs'].get('id') == element_id)
    return hits[0] if hits else None


def by_tag(tree, tag):
    hits = find_all(tree, lambda n: n['tag'] == tag)
    return hits[0] if hits else None


def text(node):
    return node['text'] + ''.join(text(c) for c in node['children'])


def css_block(css, selector_regex):
    match = re.search(selector_regex + r"\s*\{", css)
    if not match:
        return None
    depth = 1
    for i in range(match.end(), len(css)):
        if css[i] == '{':
            depth += 1
        elif css[i] == '}':
            depth -= 1
            if depth == 0:
                return css[match.end():i]
    return None


def make_group(theme):
    from mojo.apps.account.models import Group
    Group.objects.filter(name=GROUP_NAME).delete()
    return Group.objects.create(name=GROUP_NAME, kind="organization",
                                metadata={"auth_config": {"theme": theme}})


@th.django_unit_test("challenge: follows the hosted sign-in theme (appearance, accent, brand, provider, tenant CSS)")
def test_challenge_follows_hosted_theme(opts):
    import hashlib
    from mojo.apps.account.models import Group
    from mojo.apps.account.rest.bouncer.views import _auth_context
    from django.test import RequestFactory

    theme_version = hashlib.sha256(THEME_CSS.read_bytes()).hexdigest()[:10]
    group = make_group({
        "appearance": "dark", "accent_color": "#0f9d8a", "app_title": "Fixture Tenant 9132",
        "logo_url": "https://cdn.example.test/9132-logo.svg", "auth_provider_name": "Fixture Provider",
        "custom_css_url": "https://cdn.example.test/9132-tenant.css",
        "custom_css": ":root{--mat-radius:4px}",
    })
    try:
        tree = parse(render(group=group))
        body = by_tag(tree, 'body')
        classes = body['attrs'].get('class', '').split()
        assert_true('mat-appearance-dark' in classes,
                    f"the tenant's dark appearance must theme the challenge body, got {classes}")
        assert_eq(body['attrs'].get('style'), '--mat-accent:#0f9d8a',
                  "the tenant accent must be the only inline token on <body>")
        links = [n['attrs'].get('href', '') for n in find_all(tree, lambda n: n['tag'] == 'link')]
        assert_true(f"/api/account/static/mojo-auth-theme.css?v={theme_version}" in links,
                    f"the challenge must load the versioned hosted theme stylesheet, got {links}")
        assert_true("https://cdn.example.test/9132-tenant.css" in links,
                    "the tenant's custom_css_url must load on the challenge, as on the sign-in page")
        assert_true(links.index("https://cdn.example.test/9132-tenant.css") > links.index(
            f"/api/account/static/mojo-auth-theme.css?v={theme_version}"),
            "tenant CSS must load after the theme so it can restyle the tokens")
        styles = [text(n) for n in find_all(tree, lambda n: n['tag'] == 'style')]
        assert_true(any(s.strip() == ':root{--mat-radius:4px}' for s in styles),
                    "the tenant's inline custom_css must be emitted as its own <style>")
        logo = find_all(tree, lambda n: n['tag'] == 'img' and 'mbg-logo' in n['attrs'].get('class', ''))
        assert_true(logo and logo[0]['attrs'].get('src') == "https://cdn.example.test/9132-logo.svg",
                    "the tenant logo must head the challenge")
        assert_eq(logo[0]['attrs'].get('alt'), '', "the logo is decorative next to the brand name")
        brand = find_all(tree, lambda n: 'mbg-brand-name' in n['attrs'].get('class', ''))
        assert_true(brand and text(brand[0]).strip() == 'Fixture Tenant 9132',
                    "the tenant app_title must name the brand row")
        footer = by_tag(tree, 'footer')
        assert_true(footer and 'Secure sign-in via Fixture Provider' in text(footer),
                    "the footer must name the tenant's auth provider")

        for requested in ('light', 'system', 'neon'):
            request = RequestFactory().get('/auth', {'auth_appearance': requested})
            import objict
            request.DATA = objict.objict({'auth_appearance': requested})
            expected = _auth_context(request, group=group)['auth_appearance']
            page = parse(render(group=group, query={'auth_appearance': requested}))
            got = [c for c in by_tag(page, 'body')['attrs']['class'].split() if c.startswith('mat-appearance-')]
            assert_eq(got, [f'mat-appearance-{expected}'],
                      f"?auth_appearance={requested} must resolve exactly as the sign-in page does")
    finally:
        Group.objects.filter(name=GROUP_NAME).delete()

    plain = parse(render())
    assert_true(not find_all(plain, lambda n: n['tag'] == 'img'),
                "with no tenant logo and no explicit BOUNCER_CHALLENGE_LOGO_URL, the challenge shows no logo "
                "(the sign-in page's fallback), never the MojoVerify logo")
    brand = find_all(plain, lambda n: 'mbg-brand-name' in n['attrs'].get('class', ''))
    assert_true(brand and text(brand[0]).strip() == 'DJANGO MOJO',
                "with no tenant title and no explicit BOUNCER_CHALLENGE_BRAND, the brand is the sign-in page's default")
    page = render()
    assert_true('MOJO VERIFY' not in page and 'mojoverify' not in page.lower(),
                "an unbranded tenant must never see MojoVerify branding on its bot check")


@th.django_unit_test("challenge: no hard-coded palette; colours come from theme tokens")
def test_challenge_has_no_hard_coded_palette(opts):
    page = render()
    tree = parse(page)
    accent = by_tag(tree, 'body')['attrs'].get('style', '')
    scan = page.replace(accent, '')
    allowed = {'#000', '#fff'}
    hexes = {h.lower() for h in re.findall(r"#[0-9a-fA-F]{3,8}\b", scan)}
    assert_true(hexes <= allowed, f"hard-coded hex colours on the challenge: {sorted(hexes - allowed)}")
    assert_true(not re.search(r"\brgba?\(", scan), "the challenge must not hard-code rgb()/rgba() colours")
    for node in walk(tree.root):
        for attr in ('fill', 'stroke', 'color', 'bgcolor'):
            value = node['attrs'].get(attr)
            assert_true(value is None or value.lower() in ('#000', '#fff', 'currentcolor', 'none'),
                        f"<{node['tag']} {attr}={value!r}> hard-codes a colour")
    assert_true('var(--mat-warn)' in page and 'var(--mat-warn-text)' in page,
                "the failure ring uses the theme's warn tokens")

    css = THEME_CSS.read_text(encoding='utf-8')
    blocks = {
        ':root': css_block(css, r":root"),
        'body.mat-appearance-dark': css_block(css, r"body\.mat-appearance-dark"),
        'system dark': css_block(css_block(css, r"@media\s*\(prefers-color-scheme:\s*dark\)") or '',
                                 r"body\.mat-appearance-system"),
    }
    for name, block in blocks.items():
        assert_true(block is not None, f"theme CSS must define the {name} palette block")
        for token in ('--mat-warn:', '--mat-warn-text:'):
            assert_true(token in block, f"theme CSS {name} must declare {token}")
    fallback = css_block(css, r"@supports\s+not\s*\(color:\s*color-mix\([^)]*\)\)")
    assert_true(fallback is not None and '--mat-pending-strong: var(--mat-accent)' in fallback,
                "browsers without color-mix() must fall back to the plain accent for the pending button")
    page_fallback = re.search(r"@supports\s+not\s*\(color:\s*color-mix\([^)]*\)\)\s*\{(.*?)\}\}", page, re.S)
    assert_true(page_fallback and '--mbg-strong:var(--mat-accent)' in page_fallback.group(1),
                "browsers without color-mix() must fall back to the plain accent for the challenge buttons")
    mbg = css_block(page, r"(?m)^\s*\.mbg")
    assert_true(mbg is not None and 'var(--mat-accent)' in mbg,
                "accent-derived shades must be declared on .mbg, where the tenant accent applies")


@th.django_unit_test("challenge: markup keeps every bouncer mechanic (honeypot, noscript, review form, nonce)")
def test_challenge_markup_keeps_bouncer_mechanics(opts):
    response = challenge()
    page = response.content.decode('utf-8')
    tree = parse(page)

    hp = by_id(tree, 'mbg-hp-field')
    assert_true(hp is not None and hp['attrs'].get('tabindex') == '-1' and hp['attrs'].get('autocomplete') == 'off'
                and hp['attrs'].get('type') == 'text', "the honeypot field must keep its attributes")
    hp_wrap = find_all(tree, lambda n: 'mbg-hp' in n['attrs'].get('class', '').split())
    assert_true(hp_wrap and hp_wrap[0]['attrs'].get('aria-hidden') == 'true',
                "the honeypot stays hidden from assistive technology")
    noscripts = [text(n).strip() for n in find_all(tree, lambda n: n['tag'] == 'noscript')]
    assert_true(NOSCRIPT in noscripts, f"the noscript text must be unchanged, got {noscripts}")
    top = by_id(tree, 'mbg-top-level')
    assert_true(top is not None and top['attrs'].get('href') == '/auth' and top['attrs'].get('target') == '_blank'
                and top['attrs'].get('rel') == 'noopener' and 'hidden' in top['attrs'],
                "the in-frame cookies escape link keeps its href/target/rel and starts hidden")

    form = by_id(tree, 'mbg-review-form')
    assert_true(form is not None and form['attrs'].get('method') == 'post'
                and form['attrs'].get('action') == '/api/auth/bouncer/recovery' and 'hidden' not in form['attrs'],
                "the review form posts to the unchanged intake endpoint")
    ticket = by_id(tree, 'mbg-review-ticket')
    assert_true(ticket['attrs'].get('name') == 'review_ticket' and ticket['attrs'].get('value') == TICKET,
                "the review ticket rides in the form")
    email = by_id(tree, 'mbg-review-email')
    assert_true(email['attrs'].get('name') == 'email' and email['attrs'].get('maxlength') == '254'
                and 'required' in email['attrs'] and email['attrs'].get('type') == 'email',
                "the review email keeps its name and limits")
    note = by_id(tree, 'mbg-review-note')
    assert_true(note['attrs'].get('name') == 'note' and note['attrs'].get('maxlength') == '500',
                "the review note keeps its name and limit")
    assert_eq(text(by_id(tree, 'mbg-review-submit')).strip(), 'Send request', "the review submit label")
    assert_true('Please don’t include passwords or verification codes.' in page,
                "the review form keeps its no-passwords hint")
    assert_true(not find_all(tree, lambda n: n['tag'] == 'input' and n['attrs'].get('type') == 'password'),
                "the challenge never renders a password input")

    for action, reason in (('check', ''), ('recovery', 'cookies'), ('error', 'unavailable')):
        cfg = {'next_action': action}
        if reason:
            cfg['reason'] = reason
        rendered = parse(render(config=cfg))
        root = by_id(rendered, 'mbg-root')
        assert_eq(root['attrs'].get('data-state'), 'ready',
                  f"{action}: the server renders the ready state; the script moves it")
        assert_true('details' not in [n['tag'] for n in walk(rendered.root)] and 'open' not in by_id(rendered, 'mbg-review')['attrs'],
                    f"{action}: the review step never starts open")

    buttons = find_all(tree, lambda n: n['tag'] == 'button')
    assert_eq(buttons[0]['attrs'].get('id'), 'mbg-continue', "#mbg-continue must be the first <button>")
    assert_true('disabled' in buttons[0]['attrs'], "Continue starts disabled until the script mounts")
    assert_eq(text(by_id(tree, 'mbg-continue-label')), 'Continue', "the Continue label")

    nonce = re.search(r"'nonce-([^']+)'", response['Content-Security-Policy'])
    assert_true(nonce, "the challenge carries a nonce CSP")
    for script in find_all(tree, lambda n: n['tag'] == 'script'):
        attrs = script['attrs']
        assert_true(attrs.get('type') == 'application/json' or attrs.get('nonce') == nonce.group(1),
                    f"every <script> must be nonce'd or a JSON data block: {attrs}")
    assert_true("frame-ancestors 'none'" in response['Content-Security-Policy'],
                "the login challenge must refuse framing")
    contact = challenge(page_type='public_message')
    assert_true('frame-ancestors' not in contact['Content-Security-Policy'],
                "the /contact challenge stays embeddable")
    assert_eq(response['Cache-Control'], 'no-store', "the challenge response is never cached")

    live = by_id(tree, 'mbg-live')
    assert_true(live['attrs'].get('role') == 'status' and live['attrs'].get('aria-live') == 'polite'
                and live['attrs'].get('aria-atomic') == 'true',
                "#mbg-live is the polite atomic status region")
    assert_true(_find_in(live, 'mbg-heading') and _find_in(live, 'mbg-status'),
                "the heading and status sentence sit inside #mbg-live")
    assert_true('aria-live' not in by_id(tree, 'mbg-status')['attrs'],
                "#mbg-status must not nest a second live region")
    assert_eq(by_id(tree, 'mbg-heading')['attrs'].get('tabindex'), '-1', "the heading is focusable by script")
    assert_eq(text(by_id(tree, 'mbg-reference')), REFERENCE, "the reference appears once, value only")
    assert_eq(page.count(REFERENCE), 2, "the reference renders in the row and the JSON config only")
    assert_true('Reference:' not in page, "the status never appends 'Reference:'")
    assert_true('Verified' not in page and 'slider' not in page.lower(),
                "the template must hold no static 'Verified' or 'slider' text, hidden or not")
    assert_true('human' not in page.lower(), "Continue is never described as proof of being human")

    script_tag = find_all(tree, lambda n: n['tag'] == 'script' and 'src' in n['attrs'])
    assert_true(script_tag and re.search(r"mojo-hosted-bouncer\.js\?v=[0-9a-f]{10}$", script_tag[0]['attrs']['src']),
                "the hosted controller loads behind its content hash")


def _find_in(node, element_id):
    return any(n['attrs'].get('id') == element_id for n in walk(node))


@th.django_unit_test("challenge: no review ticket explains the unavailable path and links support")
def test_challenge_without_ticket_explains_unavailable(opts):
    tree = parse(render(config={'review_ticket': '', 'next_action': 'recovery', 'reason': 'operator'}))
    form = by_id(tree, 'mbg-review-form')
    assert_true('hidden' in form['attrs'], "without a ticket the review form is hidden")
    review = by_id(tree, 'mbg-review')
    assert_true('Review requests are temporarily unavailable. Please try again later or contact site support.'
                in text(review), "without a ticket the review step explains why")
    assert_true(by_id(tree, 'mbg-review-open') is not None,
                "Ask for a review still opens the step, which explains the unavailable path")


BRAND_GROUP = "test-9132-challenge-brand"


def _brand(tree):
    brand = find_all(tree, lambda n: 'mbg-brand-name' in n['attrs'].get('class', ''))
    return text(brand[0]).strip() if brand else None


@th.django_unit_test("challenge brand: tenant app_title, then an explicit BOUNCER_CHALLENGE_BRAND, then the sign-in default")
def test_challenge_brand_precedence(opts):
    # The setting is written on a test-owned group only, so no other module
    # can ever resolve it (no process-wide settings mutation).
    from mojo.apps.account.models import Group
    from mojo.apps.account.models.setting import Setting

    Group.objects.filter(name=BRAND_GROUP).delete()
    group = Group.objects.create(name=BRAND_GROUP, kind="organization", metadata={})
    try:
        assert_eq(_brand(parse(render(group=group))), 'DJANGO MOJO',
                  "with no tenant title and no BOUNCER_CHALLENGE_BRAND the sign-in default names the page")

        Setting.objects.create(key='BOUNCER_CHALLENGE_BRAND', value='Explicit Challenge Brand', group=group)
        try:
            Setting.warm_cache(group_id=group.pk)
        except Exception:
            pass
        assert_eq(_brand(parse(render(group=group))), 'Explicit Challenge Brand',
                  "an explicitly set BOUNCER_CHALLENGE_BRAND must win over the default title "
                  "(resolve_auth_config always fills app_title with the code default)")

        group.metadata = {"auth_config": {"theme": {"app_title": "Tenant Title 9132"}}}
        group.save(update_fields=['metadata'])
        assert_eq(_brand(parse(render(group=group))), 'Tenant Title 9132',
                  "a tenant app_title must win over BOUNCER_CHALLENGE_BRAND")
    finally:
        Setting.objects.filter(key='BOUNCER_CHALLENGE_BRAND', group__name=BRAND_GROUP).delete()
        Group.objects.filter(name=BRAND_GROUP).delete()
