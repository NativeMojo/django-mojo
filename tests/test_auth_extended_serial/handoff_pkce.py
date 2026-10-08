"""
Auth handoff PKCE — the tests that need the server reconfigured (#7397).

`AUTH_HANDOFF_REQUIRE_PKCE` is read with `settings.get_static`, so the endpoint
tests reload the server through `th.server_settings`, and the in-process ones
assign the attribute on django.conf.settings. Both are process-wide, hence this
opt-in serial package. The default-mode endpoint tests are in
tests/test_auth/handoff.py.
"""
import contextlib

from testit import helpers as th
from testit.helpers import assert_true, assert_eq


USER = "hpkce_user"
PWORD = "hpkce##mojo99"
GATED_HOST = "gated.hpkce.example.net"

# RFC 7636 appendix B test vector.
VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

HTTPS_DEST = "https://example.com/app"
APP_DESTS = [
    ("myapp://auth", "a custom scheme"),
    ("com.example.app:/oauth", "a custom scheme with no authority"),
    ("http://127.0.0.1:8123/cb", "an IPv4 loopback listener"),
    ("http://[::1]:8123/cb", "an IPv6 loopback listener"),
    ("http://localhost/cb", "localhost"),
    ("https://app.localhost/cb", "a .localhost name"),
    ("javascript:alert(1)", "a URL that does not parse as a destination"),
    ("", "an empty destination"),
    (None, "no destination"),
]


@th.django_unit_setup()
def setup_handoff_pkce(opts):
    from mojo.apps.account.models import User, Group

    User.objects.filter(username=USER).delete()
    Group.objects.filter(name="hpkce_tenant").delete()
    user = User(username=USER, email=f"{USER}@example.com", display_name=USER)
    user.save()
    user.is_email_verified = True
    user.is_active = True
    user.save_password(PWORD)
    user.save()
    group = Group.objects.create(name="hpkce_tenant", kind="organization")
    group.add_member(user)
    opts.user_id = user.pk
    opts.group_id = group.pk
    opts.group_uuid = group.get_uuid()


def _clear_limits():
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="auth_handoff")
    clear_rate_limits(ip="127.0.0.1", key="auth_exchange")


def _code(resp):
    body = resp.response
    data = body.get("data") if isinstance(body, dict) else None
    return data.get("code") if isinstance(data, dict) else None


@contextlib.contextmanager
def _pkce_mode(value):
    """Set AUTH_HANDOFF_REQUIRE_PKCE in THIS process only."""
    from django.conf import settings as django_settings

    missing = object()
    previous = getattr(django_settings, "AUTH_HANDOFF_REQUIRE_PKCE", missing)
    if value is missing:
        if previous is not missing:
            delattr(django_settings, "AUTH_HANDOFF_REQUIRE_PKCE")
    else:
        django_settings.AUTH_HANDOFF_REQUIRE_PKCE = value
    try:
        yield
    finally:
        if previous is missing:
            if hasattr(django_settings, "AUTH_HANDOFF_REQUIRE_PKCE"):
                delattr(django_settings, "AUTH_HANDOFF_REQUIRE_PKCE")
        else:
            django_settings.AUTH_HANDOFF_REQUIRE_PKCE = previous


@th.django_unit_test("#7397: which destinations count as an app on the device")
def test_is_app_destination(opts):
    from mojo.apps.account.services import auth_handoff

    for dest, why in APP_DESTS:
        assert_true(auth_handoff.is_app_destination(dest),
                    f"{dest!r} ({why}) must count as an app destination")
    for dest in (HTTPS_DEST, "http://example.com/app", "https://127.example.com/x",
                 "https://localhost.example.com/x"):
        assert_true(not auth_handoff.is_app_destination(dest),
                    f"{dest!r} is a web origin and must not count as an app destination")


@th.django_unit_test("#7397: the mode setting is off by default and a typo means native")
def test_pkce_mode_and_requirement(opts):
    from mojo.apps.account.services import auth_handoff

    with _pkce_mode("off"):
        assert_eq(auth_handoff.get_pkce_mode(), "off", "explicit off")
        for dest, why in APP_DESTS:
            assert_true(not auth_handoff.pkce_required(dest),
                        f"mode off must not require a challenge for {dest!r} ({why})")
    for value in ("native", " Native ", "NATIVE"):
        with _pkce_mode(value):
            assert_eq(auth_handoff.get_pkce_mode(), "native", f"{value!r} must read as native")
            for dest, why in APP_DESTS:
                assert_true(auth_handoff.pkce_required(dest),
                            f"mode native must require a challenge for {dest!r} ({why})")
            assert_true(not auth_handoff.pkce_required(HTTPS_DEST),
                        "mode native must not require a challenge for an https web origin")
    for value in ("all", "on", "true", 1, "nativ"):
        with _pkce_mode(value):
            assert_eq(auth_handoff.get_pkce_mode(), "native",
                      f"an unknown value {value!r} must be treated as native, never as off")
    for value in ("", None):
        with _pkce_mode(value):
            assert_eq(auth_handoff.get_pkce_mode(), "off", f"{value!r} is unset, so off")


@th.django_unit_test("#7397: a database row cannot switch the requirement off")
def test_pkce_mode_ignores_the_database(opts):
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.account.services import auth_handoff

    with _pkce_mode("native"):
        Setting.set("AUTH_HANDOFF_REQUIRE_PKCE", "off")
        try:
            assert_eq(auth_handoff.get_pkce_mode(), "native",
                      "the settings file must win over a database row")
        finally:
            Setting.remove("AUTH_HANDOFF_REQUIRE_PKCE")


@th.unit_test("#7397 native: no code for an app destination without a challenge")
def test_native_mode_requires_a_challenge(opts):
    from mojo.apps.incident.models import Event as IncidentEvent
    from mojo.apps import incident
    from mojo.helpers.redis import get_connection

    redis = get_connection()
    for host in ("myapp", "127.0.0.1", "localhost", "none", ""):
        redis.delete(incident.notice_key("auth:handoff_pkce_refused", host))
    IncidentEvent.objects.filter(category="auth:handoff_pkce_refused").delete()

    with th.server_settings(AUTH_HANDOFF_REQUIRE_PKCE="native"):
        _clear_limits()
        assert_true(opts.client.login(USER, PWORD), "login should succeed")

        refused = [
            ({"redirect_uri": "myapp://auth"}, "a custom scheme"),
            ({"redirect_uri": "http://127.0.0.1:8123/cb"}, "a loopback listener"),
            ({"redirect_uri": "http://localhost/cb"}, "localhost"),
            ({}, "no redirect_uri"),
        ]
        for body, why in refused:
            resp = opts.client.post("/api/auth/handoff", body)
            assert_eq(resp.status_code, 400,
                      f"native mode must refuse {why} with no challenge, "
                      f"got {resp.status_code}: {resp.response}")
            assert_true(_code(resp) is None,
                        f"a refused handoff must not return a code, got {resp.response}")

        resp = opts.client.post("/api/auth/handoff", {"redirect_uri": HTTPS_DEST})
        assert_eq(resp.status_code, 200,
                  f"native mode must still mint for an https web destination with "
                  f"no challenge, got {resp.status_code}: {resp.response}")
        web_code = _code(resp)

        resp = opts.client.post("/api/auth/handoff", {
            "redirect_uri": "myapp://auth",
            "code_challenge": CHALLENGE, "code_challenge_method": "S256"})
        assert_eq(resp.status_code, 200,
                  f"native mode must mint for a custom scheme WITH a challenge, "
                  f"got {resp.status_code}: {resp.response}")
        app_code = _code(resp)
        opts.client.logout()

        resp = opts.client.post("/api/auth/exchange", {"code": web_code})
        assert_eq(resp.status_code, 200,
                  f"the web code must exchange as before, got {resp.status_code}: {resp.response}")
        resp = opts.client.post(
            "/api/auth/exchange", {"code": app_code, "code_verifier": VERIFIER})
        assert_eq(resp.status_code, 200,
                  f"the app code must exchange with its secret, "
                  f"got {resp.status_code}: {resp.response}")

    assert_true(
        IncidentEvent.objects.filter(category="auth:handoff_pkce_refused").exists(),
        "a refused handoff must file an auth:handoff_pkce_refused incident")


@th.unit_test("#7397 off: an app destination without a challenge still mints, and is reported")
def test_off_mode_mints_and_reports(opts):
    from mojo.apps.incident.models import Event as IncidentEvent
    from mojo.apps import incident
    from mojo.helpers.redis import get_connection

    get_connection().delete(incident.notice_key("auth:handoff_pkce_missing", "hpkceapp"))
    IncidentEvent.objects.filter(
        category="auth:handoff_pkce_missing", metadata__redirect_host="hpkceapp").delete()

    _clear_limits()
    assert_true(opts.client.login(USER, PWORD), "login should succeed")
    resp = opts.client.post("/api/auth/handoff", {"redirect_uri": "hpkceapp://auth"})
    opts.client.logout()
    assert_eq(resp.status_code, 200,
              f"with the setting off a custom scheme must mint as before, "
              f"got {resp.status_code}: {resp.response}")
    code = _code(resp)
    resp = opts.client.post("/api/auth/exchange", {"code": code})
    assert_eq(resp.status_code, 200,
              f"and exchange as before, got {resp.status_code}: {resp.response}")
    assert_true(
        IncidentEvent.objects.filter(
            category="auth:handoff_pkce_missing", metadata__redirect_host="hpkceapp").exists(),
        "an app handoff with no challenge must be reported, so an operator can "
        "see who still signs in without one before turning the requirement on")


@th.unit_test("#7397: a gated code minted with a challenge needs the secret too")
def test_gated_code_needs_the_secret(opts):
    gated = f"https://{GATED_HOST}/"
    with th.server_settings(
            AUTH_HANDOFF_ALLOWED_URLS=[gated],
            AUTH_HANDOFF_GROUP_TOKEN_MODE="enforce",
            AUTH_HANDOFF_GROUP_TOKEN_HOSTS={GATED_HOST: opts.group_uuid}):
        _clear_limits()
        assert_true(opts.client.login(USER, PWORD), "login should succeed")
        codes = []
        for _ in range(2):
            resp = opts.client.post("/api/auth/handoff", {
                "redirect_uri": gated,
                "code_challenge": CHALLENGE, "code_challenge_method": "S256"})
            assert_eq(resp.status_code, 200,
                      f"a gated handoff with a challenge must mint, "
                      f"got {resp.status_code}: {resp.response}")
            codes.append(_code(resp))
        opts.client.logout()

        resp = opts.client.post("/api/auth/exchange", {"code": codes[0]})
        assert_eq(resp.status_code, 401,
                  f"a gated code minted with a challenge must not exchange without "
                  f"the secret, got {resp.status_code}: {resp.response}")

        resp = opts.client.post(
            "/api/auth/exchange", {"code": codes[1], "code_verifier": VERIFIER})
        assert_eq(resp.status_code, 200,
                  f"with the secret it must exchange, got {resp.status_code}: {resp.response}")
        token = (resp.response.get("data") or {}).get("access_token")
        assert_true(isinstance(token, str) and token.startswith("gt1."),
                    f"and still into a group-scoped token, got {token!r}")
