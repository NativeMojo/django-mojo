"""Maestro item #6225 — a token link only points at a trusted frontend.

`password_reset`, `magic_login` and `invite` links carry a sign-in token, so
their host must be one the operator configured or the frontend of the tenant
that created the account. A request can choose between those and can never add
one, and the framework never fetches such a link itself.

No settings are changed here: operator origins go in through the
`operator_origins=` seam, and tenant values through a test-owned org, as in
tests/test_account/test_create_user_command.py. The over-HTTP regression lives
in tests/test_auth/token_link_host.py.
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

SCRAPE_JOB = "mojo.apps.shortlink.services.scraper.scrape_og_metadata"
EMAIL_SCRAPE = "wut_scrape@test.com"
EMAIL_PLAIN = "wut_plain@test.com"
EMAIL_HOME = "wut_home@test.com"
EMAIL_INVITE_FOREIGN = "wut_invite_foreign@test.com"
EMAIL_INVITE_OWN = "wut_invite_own@test.com"
EMAIL_SHAPE = "wut_shape@test.com"
EMAILS = [EMAIL_SCRAPE, EMAIL_PLAIN, EMAIL_HOME, EMAIL_INVITE_FOREIGN, EMAIL_INVITE_OWN,
          EMAIL_SHAPE]

# `.invalid` never resolves, so the control job below can fetch nothing even
# where a jobs runner picks it up.
TOKEN_URL = "https://wut-frontend.invalid/auth?flow=magic_login&token=ml:wut"
CONTROL_URL = "https://wut-control.invalid/page"

OPERATOR = "https://app.wut-operator.example"
EXTRA = "https://extra.wut-operator.example"
WILDCARD = "https://*.wut-tenants.example"
OPERATOR_ORIGINS = [OPERATOR, EXTRA, WILDCARD]
EVIL = "https://evil.example"

GROUP_HOME = "wut-home"
GROUP_HOME_CHILD = "wut-home-child"
GROUP_FOREIGN = "wut-foreign"
GROUP_PATHS = "wut-home-paths"
HOME_BASE = "https://home.wut-tenant.example"
CHILD_BASE = "https://child.wut-tenant.example"
GROUP_SHAPE = "wut-shape-org"
GROUPS = [GROUP_HOME, GROUP_HOME_CHILD, GROUP_FOREIGN, GROUP_PATHS, GROUP_SHAPE]

# A tenant's own webapp_base_url that is not a plain http(s) URL or a plain
# relative path: each would put the auth path and token somewhere else.
BAD_TENANT_BASES = [
    HOME_BASE + "#x",
    HOME_BASE + "?x=1",
    "myapp://callback",
    "//home.wut-tenant.example",
    "https://u:p@home.wut-tenant.example",
    HOME_BASE + "\\x",
]
GOOD_TENANT_BASES = [
    ("/portal", "/portal/auth?flow=magic_login&token=tok"),
    (HOME_BASE + "/portal", HOME_BASE + "/portal/auth?flow=magic_login&token=tok"),
]


def _request(webapp_base_url=None, origin=None):
    import objict
    req = objict.objict()
    req.DATA = objict.objict()
    if webapp_base_url is not None:
        req.DATA.webapp_base_url = webapp_base_url
    req.META = {}
    if origin is not None:
        req.META["HTTP_ORIGIN"] = origin
    return req


def _host(url):
    from urllib.parse import urlsplit
    return urlsplit(url).hostname


def _link(flow="magic_login", **kwargs):
    from mojo.apps.account.utils.webapp_url import build_token_url
    return build_token_url(flow, "tok", **kwargs)


def _scrape_jobs(short_url):
    from mojo.apps.jobs.models import Job
    from mojo.apps.shortlink.models import ShortLink

    link = ShortLink.objects.get(code=short_url.rsplit("/", 1)[-1])
    return link, Job.objects.filter(func=SCRAPE_JOB, payload__shortlink_id=link.pk)


@th.django_unit_setup()
def setup_webapp_url_trust(opts):
    from mojo.apps.account.models import Group, User
    from mojo.apps.shortlink.models import ShortLink

    ShortLink.objects.filter(source__startswith="wut_").delete()
    ShortLink.objects.filter(user__email__in=EMAILS).delete()
    User.objects.filter(email__in=EMAILS).delete()
    Group.objects.filter(name__in=GROUPS, parent__isnull=False).delete()
    Group.objects.filter(name__in=GROUPS).delete()

    opts.home = Group.objects.create(
        name=GROUP_HOME, is_active=True, metadata={"webapp_base_url": HOME_BASE})
    opts.home_child = Group.objects.create(
        name=GROUP_HOME_CHILD, is_active=True, parent=opts.home,
        metadata={"webapp_base_url": CHILD_BASE, "webapp_auth_path": "/login"})
    opts.home_paths = Group.objects.create(
        name=GROUP_PATHS, is_active=True, parent=opts.home, metadata={})
    opts.foreign = Group.objects.create(
        name=GROUP_FOREIGN, is_active=True,
        metadata={"webapp_base_url": EVIL, "webapp_auth_path": "/steal"})

    opts.scrape_user = User.objects.create(
        username=EMAIL_SCRAPE, email=EMAIL_SCRAPE, is_active=True)
    opts.plain_user = User.objects.create(
        username=EMAIL_PLAIN, email=EMAIL_PLAIN, is_active=True)
    opts.home_user = User.objects.create(
        username=EMAIL_HOME, email=EMAIL_HOME, is_active=True, org=opts.home)
    opts.shape_org = Group.objects.create(name=GROUP_SHAPE, is_active=True, metadata={})
    opts.shape_user = User.objects.create(
        username=EMAIL_SHAPE, email=EMAIL_SHAPE, is_active=True, org=opts.shape_org)


@th.django_unit_test("a foreign webapp_base_url cannot move a magic or reset link off the operator origin")
def test_foreign_request_value_lands_on_operator_origin(opts):
    user = opts.plain_user
    user.set_protected_metadata("orig_webapp_url", OPERATOR)
    try:
        for flow in ("magic_login", "password_reset"):
            url = _link(flow, request=_request(webapp_base_url=EVIL), user=user,
                        operator_origins=OPERATOR_ORIGINS)
            assert_eq(url, f"{OPERATOR}/auth?flow={flow}&token=tok",
                      f"a {flow} link must stay on the operator origin when the "
                      f"request names a foreign webapp_base_url")
    finally:
        user.set_protected_metadata("orig_webapp_url", None)


@th.django_unit_test("a request value selects a configured frontend and never its own path")
def test_request_value_selects_configured_value(opts):
    from mojo.apps.account.utils.webapp_url import get_webapp_base_url

    user = opts.home_user
    cases = [
        (OPERATOR, OPERATOR, "a request value equal to an operator origin selects it"),
        (EXTRA + "/", EXTRA, "a request value equal to an allowlist entry selects it"),
        (EXTRA + "/phish/page", EXTRA,
         "a request value with an extra path returns the configured value without the path"),
        ("https://acme.wut-tenants.example/x", "https://acme.wut-tenants.example",
         "a wildcard match returns the bare origin"),
        ("https://a.b.wut-tenants.example", HOME_BASE,
         "a wildcard covers one label only"),
        (HOME_BASE, HOME_BASE, "a request value equal to the home tenant's value selects it"),
        (HOME_BASE + "/phish", HOME_BASE,
         "a path under the home tenant's value does not select anything"),
    ]
    for value, expected, why in cases:
        got = get_webapp_base_url(request=_request(webapp_base_url=value), user=user,
                                  operator_origins=OPERATOR_ORIGINS)
        assert_eq(got, expected, f"{why} (request value {value!r})")


@th.django_unit_test("look-alike, credential, script and non-string values are refused without an error")
def test_refused_shapes(opts):
    user = opts.home_user
    refused = [
        EVIL,
        HOME_BASE + ".evil.example",
        HOME_BASE + "@evil.example",
        "https://evil.example\\@home.wut-tenant.example",
        "//evil.example",
        "javascript:alert(1)",
        HOME_BASE + "?next=https://evil.example",
        HOME_BASE + "#@evil.example",
        " " + HOME_BASE,
        ["https://evil.example"],
        {"url": EVIL},
        42,
    ]
    expected = f"{HOME_BASE}/auth?flow=magic_login&token=tok"
    for value in refused:
        url = _link(request=_request(webapp_base_url=value), user=user)
        assert_eq(url, expected,
                  f"the request value {value!r} must be ignored and the link "
                  f"must go to the account's own tenant")
    for origin in ("null", EVIL, HOME_BASE + ".evil.example"):
        url = _link(request=_request(origin=origin), user=opts.plain_user)
        assert_true("evil.example" not in url and _host(url) is None,
                    f"an untrusted Origin header {origin!r} must not become "
                    f"the link's host, got {url}")


@th.django_unit_test("a group outside the account's tenant cannot set the base or the auth path")
def test_foreign_group_is_ignored(opts):
    user = opts.home_user
    expected = f"{HOME_BASE}/auth?flow=magic_login&token=tok"

    assert_eq(_link(user=user, group=opts.foreign), expected,
              "a group outside the account's tenant tree must not set the link's host or path")

    membership = opts.foreign.add_member(user)
    try:
        assert_true(membership.is_active, "the account is an active member of the foreign group")
        assert_eq(_link(user=user, group=opts.foreign), expected,
                  "membership of a foreign group must not make its webapp_base_url trusted")
    finally:
        membership.delete()

    assert_eq(_link(user=user, group=opts.home_child),
              f"{CHILD_BASE}/login?flow=magic_login&token=tok",
              "a group inside the account's tenant tree keeps its own base and auth path")

    assert_eq(_link(user=opts.plain_user, group=opts.home_child),
              "/auth?flow=magic_login&token=tok",
              "an account with no org takes no tenant's value")
    assert_eq(_link(user=opts.plain_user, group=opts.home_child,
                    operator_origins=[CHILD_BASE]),
              f"{CHILD_BASE}/auth?flow=magic_login&token=tok",
              "a foreign group's base is used once the operator lists it, with the default path")


@th.django_unit_test("an invite from another tenant does not carry the account's token to that tenant")
def test_invite_from_another_tenant(opts):
    from mojo.apps.account.models import User
    from mojo.apps.shortlink.models import ShortLink

    def invite_url(user, group):
        user.send_invite(group=group)
        return ShortLink.objects.filter(user=user, source="invite").order_by("-id").first().url

    foreign_created = User.objects.create(
        username=EMAIL_INVITE_FOREIGN, email=EMAIL_INVITE_FOREIGN, is_active=True,
        org=opts.home)
    url = invite_url(foreign_created, opts.foreign)
    assert_eq(_host(url), _host(HOME_BASE),
              f"an invite from a second tenant must land on the frontend of the "
              f"tenant that created the account, got {url}")
    assert_true(url.startswith(f"{HOME_BASE}/auth?flow=invite&token="),
                f"the second tenant must not set the path either, got {url}")

    own = User.objects.create(
        username=EMAIL_INVITE_OWN, email=EMAIL_INVITE_OWN, is_active=True,
        org=opts.foreign)
    url = invite_url(own, opts.foreign)
    assert_true(url.startswith(f"{EVIL}/steal?flow=invite&token="),
                f"a tenant's invite to an account it created lands on its own frontend, got {url}")


@th.django_unit_test("a group auth path cannot move the link's host")
def test_auth_path_cannot_move_host(opts):
    user = opts.home_user
    group = opts.home_paths
    expected = f"{HOME_BASE}/auth?flow=magic_login&token=tok"
    bad_paths = ["@evil.example/a", ".evil.example/a", "//evil.example", "/a//evil.example",
                 "/a@evil.example", "/a?x=1", "/a#x", "/a b", "/a\\b", ":8443/a", ["/a"]]
    try:
        for path in bad_paths:
            group.metadata = {"webapp_auth_path": path}
            group.save()
            url = _link(user=user, group=group)
            assert_eq(url, expected,
                      f"the auth path {path!r} must be dropped for the default /auth")
        group.metadata = {"webapp_auth_path": "/sign-in/"}
        group.save()
        assert_eq(_link(user=user, group=group),
                  f"{HOME_BASE}/sign-in?flow=magic_login&token=tok",
                  "a plain auth path from the account's own tenant is still used")
    finally:
        group.metadata = {}
        group.save()


@th.django_unit_test("an org's webapp_base_url is used only as a plain URL or relative path")
def test_org_value_shape(opts):
    from mojo.apps.account.models import User

    org = opts.shape_org
    try:
        for value in BAD_TENANT_BASES:
            org.metadata = {"webapp_base_url": value}
            org.save()
            user = User.objects.get(email=EMAIL_SHAPE)
            assert_eq(_link(user=user), "/auth?flow=magic_login&token=tok",
                      f"the org value {value!r} must be skipped for the next source")
        for value, expected in GOOD_TENANT_BASES:
            org.metadata = {"webapp_base_url": value}
            org.save()
            user = User.objects.get(email=EMAIL_SHAPE)
            assert_eq(_link(user=user), expected,
                      f"the org value {value!r} is plain and must still be used")
    finally:
        org.metadata = {}
        org.save()


@th.django_unit_test("a home group's webapp_base_url is used only as a plain URL or relative path")
def test_home_group_value_shape(opts):
    from mojo.apps.account.models import Group

    user = opts.home_user
    try:
        for value in BAD_TENANT_BASES:
            opts.home_paths.metadata = {"webapp_base_url": value}
            opts.home_paths.save()
            group = Group.objects.get(pk=opts.home_paths.pk)
            assert_eq(_link(user=user, group=group),
                      f"{HOME_BASE}/auth?flow=magic_login&token=tok",
                      f"the home group value {value!r} must be skipped for the org's value")
        for value, expected in GOOD_TENANT_BASES:
            opts.home_paths.metadata = {"webapp_base_url": value}
            opts.home_paths.save()
            group = Group.objects.get(pk=opts.home_paths.pk)
            assert_eq(_link(user=user, group=group), expected,
                      f"the home group value {value!r} is plain and must still be used")
    finally:
        opts.home_paths.metadata = {}
        opts.home_paths.save()


@th.django_unit_test("an empty WEBAPP_AUTH_PATH gives a link with no path")
def test_empty_auth_path(opts):
    user = opts.home_user
    assert_eq(_link(user=user, auth_path=""), f"{HOME_BASE}?flow=magic_login&token=tok",
              "an empty WEBAPP_AUTH_PATH must put the query straight after the base")
    assert_eq(_link(user=user, auth_path="/sign-in"),
              f"{HOME_BASE}/sign-in?flow=magic_login&token=tok",
              "a plain WEBAPP_AUTH_PATH is used")
    for value in ("@evil.example/a", "//evil.example", None, 7):
        assert_eq(_link(user=user, auth_path=value),
                  f"{HOME_BASE}/auth?flow=magic_login&token=tok",
                  f"the WEBAPP_AUTH_PATH {value!r} must fall back to /auth")


@th.django_unit_test("an Origin header selects a trusted frontend and can never add one")
def test_origin_header(opts):
    user = opts.plain_user
    url = _link(request=_request(origin=EVIL), user=user)
    assert_eq(url, "/auth?flow=magic_login&token=tok",
              "with nothing configured an Origin header must not become the link's host")

    url = _link(request=_request(origin=EVIL), user=user, operator_origins=OPERATOR_ORIGINS)
    assert_eq(url, "/auth?flow=magic_login&token=tok",
              "an Origin header off the operator's list must not become the link's host")

    url = _link(request=_request(origin=EXTRA), user=user, operator_origins=OPERATOR_ORIGINS)
    assert_eq(url, f"{EXTRA}/auth?flow=magic_login&token=tok",
              "a trusted Origin header selects its configured value")


@th.django_unit_test("a stored first-login origin is checked when it is read")
def test_orig_webapp_url(opts):
    user = opts.plain_user
    try:
        user.set_protected_metadata("orig_webapp_url", EVIL)
        assert_eq(_link(user=user), "/auth?flow=magic_login&token=tok",
                  "a poisoned orig_webapp_url must be ignored")
        assert_eq(_link(user=user, operator_origins=OPERATOR_ORIGINS),
                  "/auth?flow=magic_login&token=tok",
                  "a poisoned orig_webapp_url must be ignored when operator origins are set")

        user.set_protected_metadata("orig_webapp_url", EXTRA + "/")
        assert_eq(_link(user=user, operator_origins=OPERATOR_ORIGINS),
                  f"{EXTRA}/auth?flow=magic_login&token=tok",
                  "a trusted orig_webapp_url is used")

        for stored in (["https://evil.example"], 7):
            user.set_protected_metadata("orig_webapp_url", stored)
            assert_eq(_link(user=user, operator_origins=OPERATOR_ORIGINS),
                      "/auth?flow=magic_login&token=tok",
                      f"a non-string orig_webapp_url {stored!r} must be ignored without an error")
    finally:
        user.set_protected_metadata("orig_webapp_url", None)

    from mojo.apps.account.utils.webapp_url import clean_webapp_origin

    for value in (None, "null", 7, ["https://a.example"], "javascript:alert(1)",
                  "//evil.example", "https://u:p@a.example"):
        assert_eq(clean_webapp_origin(value), None,
                  f"jwt_login must not store {value!r} as a webapp origin")
    assert_eq(clean_webapp_origin(OPERATOR), OPERATOR,
              "jwt_login still stores a well-formed origin")


@th.django_unit_test("token short links never queue a fetch of their destination")
def test_token_shortlink_queues_no_scrape(opts):
    from mojo.apps.shortlink import maybe_shorten_url, shorten

    short = maybe_shorten_url(TOKEN_URL, source="wut_magic_login",
                              user=opts.scrape_user, expire_hours=1)
    assert_true(short != TOKEN_URL, "the token link must have been shortened")
    link, jobs = _scrape_jobs(short)
    assert_eq(link.url, TOKEN_URL, "the short link must keep the token URL as its destination")
    assert_true(link.bot_passthrough is False,
                "token links must keep bot_passthrough=False so preview bots can't use the token")
    assert_true(link.get_og_metadata().get("og:title"),
                "a token link must carry a preview title: without preview data "
                "the redirect handler sends a bot on to the token URL")
    assert_eq(jobs.count(), 0,
              "maybe_shorten_url must not queue a scrape job: it would fetch the token URL")

    short = shorten(TOKEN_URL, source="wut_scrape_off", user=opts.scrape_user,
                    expire_hours=1, scrape=False)
    _, jobs = _scrape_jobs(short)
    assert_eq(jobs.count(), 0, "shorten(scrape=False) must not queue a scrape job")

    # Control: an ordinary short link still gets its preview scrape.
    short = shorten(CONTROL_URL, source="wut_scrape_on", user=opts.scrape_user, expire_hours=1)
    _, jobs = _scrape_jobs(short)
    assert_eq(jobs.count(), 1, "shorten() without scrape=False must still queue the scrape job")
    jobs.delete()


@th.django_unit_test("a bad WEBAPP_ALLOWED_ORIGINS is rejected with ImproperlyConfigured")
def test_allowed_origins_validator(opts):
    from django.core.exceptions import ImproperlyConfigured
    from mojo.apps.account.utils.webapp_url import allowed_origins

    assert_eq(allowed_origins([]), (), "an empty list is valid")
    assert_eq(allowed_origins([EXTRA + "/", WILDCARD, "http://localhost:5173"]),
              (EXTRA, WILDCARD, "http://localhost:5173"),
              "valid origins are returned with the trailing slash stripped")

    bad_values = [
        "not-a-list",
        None,
        ["ftp://x"],
        [42],
        [""],
        ["app.example.com"],
        ["//app.example.com"],
        ["https://"],
        ["https://app.example.com/portal"],
        ["https://app.example.com?x=1"],
        ["https://user:pw@app.example.com"],
        ["https://app.*.example.com"],
        ["https://app.example.com\n"],
        ["myapp://callback"],
    ]
    for value in bad_values:
        try:
            allowed_origins(value)
        except ImproperlyConfigured as err:
            assert_true("WEBAPP_ALLOWED_ORIGINS" in str(err),
                        f"the error for {value!r} must name the setting, got {err}")
        else:
            raise AssertionError(f"{value!r} must be rejected")
