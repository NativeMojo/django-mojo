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
    User.objects.filter(email=EMAIL).delete()
    Group.objects.filter(name__in=[GROUP_HOME, GROUP_FOREIGN]).delete()

    home = Group.objects.create(
        name=GROUP_HOME, is_active=True, metadata={"webapp_base_url": HOME_BASE})
    foreign = Group.objects.create(
        name=GROUP_FOREIGN, is_active=True,
        metadata={"webapp_base_url": EVIL, "webapp_auth_path": "/steal"})
    user = User.objects.create(username=EMAIL, email=EMAIL, is_active=True, org=home)
    opts.user_id = user.pk
    opts.foreign_id = foreign.pk


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
