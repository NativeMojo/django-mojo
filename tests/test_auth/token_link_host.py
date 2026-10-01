"""Maestro item #6225 — over HTTP, a caller cannot aim a sign-in link at their host.

The regression WMWX asked for: `auth/magic/send` and `auth/forgot` are public,
and both used to build the emailed token link on whatever `webapp_base_url` the
request carried, or on the frontend of any group the caller named.

The test project sets neither BASE_URL nor WEBAPP_BASE_URL, so the account
gets an org whose metadata names the frontend the link must land on. The link
is read back from the account's newest ShortLink row. Three sends in total,
under the endpoints' 5-per-300 s limit.
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TESTIT_TIER = "core"

EMAIL = "tlh_account@test.com"
GROUP_HOME = "tlh-home"
GROUP_FOREIGN = "tlh-foreign"
HOME_BASE = "https://home.tlh-tenant.example"
EVIL = "https://evil.example"
# `.invalid` never resolves: nothing can follow the link these tests read back.
TOKEN_URL = "https://tlh-frontend.invalid/auth?flow=magic_login&token=ml:tlh"
PREVIEW_BOTS = ["Slackbot-LinkExpanding 1.0", "WhatsApp/2.23", "facebookexternalhit/1.1"]
BROWSER = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15"
# A tenant chooses its frontend address, and a path may hold markup. The
# preview page prints the address, on the short-link origin.
EMAIL_MARKUP = "tlh_markup@test.com"
GROUP_MARKUP = "tlh-markup"
MARKUP_BASE = "https://tlh-frontend.invalid/<script>alert(6225)</script>/a&b"
SCRAPE_JOB = "mojo.apps.shortlink.services.scraper.scrape_og_metadata"
UNIFORM = {
    "auth/magic/send": "If account is in our system a login link was sent.",
    "auth/forgot": "If the account is in our system a reset code was sent.",
}


@th.django_unit_setup()
def setup_token_link_host(opts):
    from mojo.apps.account.models import Group, User
    from mojo.apps.shortlink.models import ShortLink
    from mojo.decorators.limits import clear_rate_limits

    clear_rate_limits(ip="127.0.0.1")
    ShortLink.objects.filter(user__email=EMAIL).delete()
    ShortLink.objects.filter(source="tlh_magic_login").delete()
    ShortLink.objects.filter(source="tlh_markup").delete()
    User.objects.filter(email__in=[EMAIL, EMAIL_MARKUP]).delete()
    Group.objects.filter(name__in=[GROUP_HOME, GROUP_FOREIGN, GROUP_MARKUP]).delete()

    home = Group.objects.create(
        name=GROUP_HOME, is_active=True, metadata={"webapp_base_url": HOME_BASE})
    foreign = Group.objects.create(
        name=GROUP_FOREIGN, is_active=True,
        metadata={"webapp_base_url": EVIL, "webapp_auth_path": "/steal"})
    user = User.objects.create(username=EMAIL, email=EMAIL, is_active=True, org=home)
    opts.user_id = user.pk
    opts.foreign_id = foreign.pk
    markup = Group.objects.create(
        name=GROUP_MARKUP, is_active=True, metadata={"webapp_base_url": MARKUP_BASE})
    User.objects.create(
        username=EMAIL_MARKUP, email=EMAIL_MARKUP, is_active=True, org=markup)


def _send_and_read_link(opts, path, payload, source, params=None):
    from mojo.apps.shortlink.models import ShortLink

    before = set(ShortLink.objects.filter(user_id=opts.user_id).values_list("pk", flat=True))
    resp = opts.client.post(f"/api/{path}", payload, params=params)
    assert_eq(resp.status_code, 200, f"{path} must answer 200, got {resp.status_code}")
    assert_eq(resp.response.message, UNIFORM[path],
              f"{path} must keep its uniform response")
    link = ShortLink.objects.filter(
        user_id=opts.user_id, source=source).exclude(pk__in=before).order_by("-id").first()
    assert_true(link is not None, f"{path} must have created a {source} short link")
    return link.url


def _assert_on_home(url, token_prefix, flow):
    assert_true(url.startswith(f"{HOME_BASE}/auth?flow={flow}&token={token_prefix}:"),
                f"the {flow} link must be on the account's own frontend, got {url}")
    assert_true("evil.example" not in url, f"the link must not name the foreign host, got {url}")


@th.django_unit_test("magic/send: a foreign webapp_base_url does not move the link")
def test_magic_send_ignores_foreign_webapp_base_url(opts):
    url = _send_and_read_link(
        opts, "auth/magic/send", {"email": EMAIL, "webapp_base_url": EVIL}, "magic_login")
    _assert_on_home(url, "ml", "magic_login")


@th.django_unit_test("forgot: a foreign webapp_base_url does not move the reset link")
def test_forgot_ignores_foreign_webapp_base_url(opts):
    url = _send_and_read_link(
        opts, "auth/forgot",
        {"email": EMAIL, "method": "link", "webapp_base_url": EVIL}, "password_reset")
    _assert_on_home(url, "pr", "password_reset")


@th.django_unit_test("magic/send: a group outside the account's tenant does not move the link")
def test_magic_send_ignores_foreign_group(opts):
    url = _send_and_read_link(
        opts, "auth/magic/send", {"email": EMAIL}, "magic_login",
        params={"group": opts.foreign_id})
    _assert_on_home(url, "ml", "magic_login")


@th.django_unit_test("a preview bot gets the preview page for a token short link, never the redirect")
def test_preview_bot_is_not_redirected_to_token_link(opts):
    from mojo.apps.jobs.models import Job
    from mojo.apps.shortlink import maybe_shorten_url
    from mojo.apps.shortlink.models import ShortLink

    short = maybe_shorten_url(TOKEN_URL, source="tlh_magic_login", expire_hours=1)
    assert_true(short != TOKEN_URL, "the token link must have been shortened")
    code = short.rsplit("/", 1)[-1]
    link = ShortLink.objects.get(code=code)

    for user_agent in PREVIEW_BOTS:
        resp = opts.client.get(f"/s/{code}", allow_redirects=False,
                               headers={"User-Agent": user_agent})
        headers = {k.lower(): v for k, v in opts.client.last_response.headers.items()}
        assert_eq(resp.status_code, 200,
                  f"{user_agent} must get the preview page, not a redirect to the "
                  f"token link, got {resp.status_code}")
        assert_true("location" not in headers,
                    f"{user_agent} must not be given a Location header, got "
                    f"{headers.get('location')}")
        assert_true(headers.get("content-type", "").startswith("text/html"),
                    f"{user_agent} must get HTML, got {headers.get('content-type')}")
        from mojo.apps.shortlink import TOKEN_LINK_PREVIEW_TITLE
        body = resp.response if isinstance(resp.response, str) else str(resp.response)
        assert_true(f"<title>{TOKEN_LINK_PREVIEW_TITLE}</title>" in body,
                    f"{user_agent} must get the fixed preview title")

    resp = opts.client.get(f"/s/{code}", allow_redirects=False,
                           headers={"User-Agent": BROWSER})
    headers = {k.lower(): v for k, v in opts.client.last_response.headers.items()}
    assert_eq(resp.status_code, 302, f"a browser must be redirected, got {resp.status_code}")
    assert_eq(headers.get("location"), TOKEN_URL,
              "a browser must be redirected to the token link")

    assert_eq(Job.objects.filter(func=SCRAPE_JOB, payload__shortlink_id=link.pk).count(), 0,
              "nothing may queue a fetch of the token link")


@th.django_unit_test("the preview page escapes a token link whose tenant address holds markup")
def test_preview_page_escapes_tenant_address(opts):
    import html
    import re
    from mojo.apps.account.models import User
    from mojo.apps.account.utils.webapp_url import build_token_url
    from mojo.apps.shortlink import maybe_shorten_url

    user = User.objects.get(email=EMAIL_MARKUP)
    url = build_token_url("magic_login", "ml:tlh", user=user)
    assert_eq(url, f"{MARKUP_BASE}/auth?flow=magic_login&token=ml:tlh",
              "the link is built on the account's own tenant address")
    short = maybe_shorten_url(url, source="tlh_markup", expire_hours=1)
    assert_true(short != url, "the token link must have been shortened")

    resp = opts.client.get(f"/s/{short.rsplit('/', 1)[-1]}", allow_redirects=False,
                           headers={"User-Agent": PREVIEW_BOTS[1]})
    assert_eq(resp.status_code, 200, f"a preview bot must get the preview page, got {resp.status_code}")
    body = resp.response if isinstance(resp.response, str) else str(resp.response)
    assert_true("<script" not in body.lower(),
                "the preview page must not print the tenant address as markup")
    hrefs = re.findall(r'<a href="([^"]*)">', body)
    assert_eq(len(hrefs), 1, f"the preview page must hold one link, got {hrefs}")
    assert_eq(html.unescape(hrefs[0]), url,
              "the link target, once unescaped, must be the exact token link")
    refresh = re.findall(r'<meta http-equiv="refresh" content="([^"]*)">', body)
    assert_eq([html.unescape(val) for val in refresh], [f"0;url={url}"],
              "the refresh target, once unescaped, must be the exact token link")
