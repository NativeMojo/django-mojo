"""
RFC 8628 device authorization, in-process.

Driven through the service functions with a private resource registry and an
explicit `cfg=` — never the shared registry or the settings singleton — so the
module is parallel-safe in the default tier. Every request carries a per-test
IP from TEST-NET-2, so the per-IP failure counters belong to this file alone.

The properties that carry the security weight, each asserted directly:

  * only a client that registered for the device grant may start a request,
    and only for `mcp` unless the deployment widens it;
  * approval needs an interactive session and names exactly one grant, with
    the lifetime the person chose and no longer than the cap;
  * a consumed device code presented again revokes what it produced;
  * a wrong client learns nothing and cannot disturb the real one's polling;
  * failed lookups are capped per person.
"""
import datetime
import re
import time

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

TEST_USER = "oauth_device_user"
OTHER_USER = "oauth_device_other"
DEVICE_CLIENT = "testit device client"
BROWSER_CLIENT = "testit device browser client"
ORIGIN = "https://oauth.testit.example"
RESOURCE_PATH = "/api/testit/oauth-device-probe"
RESOURCE = ORIGIN + RESOURCE_PATH
ROOT_PATH = "/api/testit/oauth-device-root"
ROOT_RESOURCE = ORIGIN + ROOT_PATH
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
USER_CODE_RE = re.compile(r"^[BCDFGHJKLMNPQRSTVWXZ]{4}-[BCDFGHJKLMNPQRSTVWXZ]{4}$")
IPS = [f"198.51.100.{n}" for n in range(30, 60)]


def _cfg(**overrides):
    from objict import objict

    values = dict(enabled=True, code_ttl=600, interval=5, allow_api=False,
                  max_ttl_days=90)
    values.update(overrides)
    return objict(values)


def _registry(state=None, prefix=False):
    from mojo.apps.account.services.oauth_server import resources

    state = state if state is not None else {"on": True}
    registry = resources.ResourceRegistry()
    registry.register(RESOURCE_PATH, ["mcp"], lambda: state["on"])
    if prefix:
        registry.register(ROOT_PATH, ["mcp", "api"], lambda: state["on"],
                          prefix=True)
    return registry


def _request(data, ip, user=None, token=None, bearer="bearer", meta=None):
    from django.test import RequestFactory
    from objict import objict

    request = RequestFactory().post("/api/account/oauth/device", data, **(meta or {}))
    request.DATA = objict.fromdict(data)
    request.ip = ip
    request.user_agent = "testit-agent/1.0"
    if user is not None:
        request.user = user
    request.bearer = bearer
    request.auth_token = objict(prefix="bearer", token=token) if token else None
    return request


def _session_token(user, token_type="access", auth_time=None):
    from mojo.apps.account.utils.jwtoken import JWToken

    claims = dict(uid=user.pk)
    if auth_time is not False:
        claims["auth_time"] = int(auth_time or time.time())
    return JWToken(user.get_auth_key()).create_access_token(
        token_type=token_type, **claims)


def _user(name=TEST_USER):
    from mojo.apps.account.models import User
    return User.objects.get(username=name)


def _client_id(name=DEVICE_CLIENT):
    from mojo.apps.account.models import OAuthClient
    return OAuthClient.objects.get(client_name=name).client_id


def _clear_counters(user):
    from mojo.helpers.redis import get_connection
    from mojo.apps.account.services.oauth_server import device

    keys = [f"oauth:device:fail:user:{user.pk}"]
    keys += [f"oauth:device:fail:ip:{ip}" for ip in IPS]
    get_connection().delete(*keys)


def _start(ip, cfg=None, registry=None, meta=None, **data):
    from mojo.apps.account.services.oauth_server import device

    payload = {"client_id": _client_id(), "resource": RESOURCE}
    payload.update(data)
    payload = {k: v for k, v in payload.items() if v is not None}
    return device.start(_request(payload, ip, meta=meta), ORIGIN,
                        registry=registry or _registry(), cfg=cfg or _cfg())


def _poll(raw_device, ip, client_id=None, cfg=None, registry=None):
    from mojo.apps.account.services.oauth_server import device

    request = _request({"device_code": raw_device,
                        "client_id": client_id or _client_id()}, ip)
    return device.poll(request, ORIGIN, registry=registry or _registry(),
                       cfg=cfg or _cfg())


def _poll_error(raw_device, ip, **kwargs):
    from mojo.apps.account.services.oauth_server import device

    try:
        _poll(raw_device, ip, **kwargs)
    except device.DeviceError as err:
        return err.code
    return None


def _row(raw_device):
    import hashlib
    from mojo.apps.account.models import OAuthDeviceCode

    return OAuthDeviceCode.objects.get(
        device_code_hash=hashlib.sha256(raw_device.encode()).hexdigest())


def _rewind_poll(raw_device):
    """Make the next poll land after the interval, as a polite client's would."""
    from mojo.apps.account.models import OAuthDeviceCode
    from mojo.helpers import dates

    OAuthDeviceCode.objects.filter(pk=_row(raw_device).pk).update(
        last_polled=dates.utcnow() - datetime.timedelta(seconds=120))


def _approve(user_code, ip, user=None, ttl_days=None, cfg=None, registry=None):
    from mojo.apps.account.services.oauth_server import device

    user = user or _user()
    data = {"user_code": user_code}
    if ttl_days is not None:
        data["ttl_days"] = ttl_days
    request = _request(data, ip, user=user, token=_session_token(user))
    return device.approve(request, ORIGIN, registry=registry or _registry(),
                          cfg=cfg or _cfg())


@th.django_unit_setup()
def setup_device(opts):
    from mojo.apps.account.models import OAuthClient, OAuthGrant, User
    from mojo.apps.incident.models import Event
    from mojo.apps.account.services.oauth_server import clients

    OAuthClient.objects.filter(client_name__in=[DEVICE_CLIENT, BROWSER_CLIENT]).delete()
    for name in (TEST_USER, OTHER_USER):
        User.objects.filter(username=name).delete()
        user = User(username=name, display_name=name, email=f"{name}@example.com")
        user.save()
        user.remove_all_permissions()
        _clear_counters(user)
        OAuthGrant.objects.filter(user=user).delete()
    Event.objects.filter(category="oauth:device_code_bruteforce",
                         model_id=_user().pk).delete()

    clients.register_client({
        "client_name": DEVICE_CLIENT,
        "grant_types": [DEVICE_GRANT, "refresh_token"],
    })
    clients.register_client({
        "client_name": BROWSER_CLIENT,
        "redirect_uris": ["http://127.0.0.1:8600/cb"],
    })


@th.django_unit_test("user codes use the RFC 8628 consonant set and normalise forgivingly")
def test_user_code_charset(opts):
    from mojo.apps.account.services.oauth_server import device

    for _ in range(50):
        code = device.generate_user_code()
        assert_true(USER_CODE_RE.match(code),
                    f"a user code must be XXXX-XXXX from the 20 consonants, got {code!r}")

    assert_eq(device.normalize_user_code("bcdf-ghjk"), "BCDFGHJK",
              "case and the dash must be ignored — people retype what they see")
    assert_eq(device.normalize_user_code(" BCDF GHJK "), "BCDFGHJK",
              "spaces must be ignored")
    for bad, why in (("BCDA-GHJK", "a vowel"), ("BCD-FGH", "a short code"),
                     ("BCDF-GHJK-L", "a long code"), ("", "an empty value"),
                     (None, "no value"), (["BCDFGHJK"], "a repeated parameter"),
                     ("B" * 40, "an oversized value")):
        assert_eq(device.normalize_user_code(bad), None,
                  f"{why} must not normalise to a code")


@th.django_unit_test("only a device-registered client may start, and only within its scope")
def test_start_refusals(opts):
    from mojo.apps.account.services.oauth_server import device

    ip = IPS[0]
    cases = [
        (dict(cfg=_cfg(enabled=False)), "not_found", "the flow switched off"),
        (dict(client_id="testit-device-unknown"), "invalid_client", "an unknown client"),
        (dict(client_id=_client_id(BROWSER_CLIENT)), "unauthorized_client",
         "a client that never registered for the device grant"),
        (dict(scope="admin"), "invalid_scope", "a scope this server does not offer"),
        (dict(resource="https://elsewhere.example/api/x"), "invalid_target",
         "an unregistered resource"),
        (dict(scope="api", resource=ROOT_RESOURCE, registry=_registry(prefix=True)),
         "invalid_scope", "the api scope while the deployment allows only mcp"),
    ]
    for kwargs, expected, why in cases:
        code = None
        try:
            _start(ip, **kwargs)
        except device.DeviceError as err:
            code = err.code
        assert_eq(code, expected, f"{why} must be refused with {expected}, got {code}")

    allowed = _start(ip, cfg=_cfg(allow_api=True), registry=_registry(prefix=True),
                     scope="api", resource=ROOT_RESOURCE)
    assert_true(bool(allowed.get("device_code")),
                "with OAUTH_DEVICE_ALLOW_API on, an api request must start")


@th.django_unit_test("start answers the RFC 8628 response and stores no raw code")
def test_start_response(opts):
    from mojo.apps.account.models import OAuthDeviceCode
    from mojo.apps.account.services.oauth_server import resources

    started = _start(IPS[1])
    assert_eq(sorted(started.keys()),
              ["device_code", "expires_in", "interval", "user_code",
               "verification_uri", "verification_uri_complete"],
              f"the response must carry exactly the RFC 8628 fields, got {sorted(started)}")
    assert_true(USER_CODE_RE.match(started["user_code"]),
                f"the user code must be XXXX-XXXX, got {started['user_code']!r}")
    page = f"{ORIGIN}{resources.SERVER_PATH}/device"
    assert_eq(started["verification_uri"], page,
              f"the verification URI must be this installation's page, "
              f"got {started['verification_uri']!r}")
    assert_eq(started["verification_uri_complete"],
              f"{page}?user_code={started['user_code']}",
              "the complete URI must pre-fill exactly the issued code")
    assert_eq((started["expires_in"], started["interval"]), (600, 5),
              f"expires_in and interval must come from the config, got "
              f"{(started['expires_in'], started['interval'])}")

    row = _row(started["device_code"])
    assert_eq(row.status, "pending", f"a new request must be pending, got {row.status}")
    assert_eq(row.requester_ip, IPS[1], "the requester's IP must be recorded")
    assert_eq(row.requester_ua, "testit-agent/1.0", "the requester's agent must be recorded")
    raw_values = (started["device_code"], started["user_code"],
                  started["user_code"].replace("-", ""))
    stored = OAuthDeviceCode.objects.filter(pk=row.pk).values_list(
        "device_code_hash", "user_code_hash").first()
    assert_true(not any(value in stored for value in raw_values),
                "neither code may be stored raw")


@th.django_unit_test("every poll state answers its RFC 8628 error")
def test_poll_states(opts):
    from mojo.apps.account.models import OAuthDeviceCode
    from mojo.apps.account.services.oauth_server import device
    from mojo.helpers import dates

    ip = IPS[2]
    started = _start(ip)
    raw = started["device_code"]

    assert_eq(_poll_error(raw, ip), "authorization_pending",
              "an unanswered request must answer authorization_pending")
    assert_eq(_poll_error(raw, ip), "slow_down",
              "a poll inside the interval must answer slow_down")
    assert_eq(_row(raw).interval, 10,
              f"slow_down must add 5 seconds to the interval, got {_row(raw).interval}")

    assert_eq(_poll_error(raw, ip, client_id=_client_id(BROWSER_CLIENT)), "invalid_grant",
              "another client presenting the code must get invalid_grant")
    _rewind_poll(raw)
    before = _row(raw).interval
    assert_eq(_poll_error(raw, ip), "authorization_pending",
              "the wrong client's attempt must not have disturbed the real one")
    assert_eq(_row(raw).interval, before,
              "a wrong client must not be able to slow the real client down")

    assert_eq(_poll_error("not-a-device-code", ip), "invalid_grant",
              "an unknown device code must answer invalid_grant")
    assert_eq(_poll_error(raw, ip, cfg=_cfg(enabled=False)), "unsupported_grant_type",
              "with the flow off the grant type must be unsupported")

    user = _user()
    request = _request({"user_code": started["user_code"]}, ip, user=user,
                       token=_session_token(user))
    device.deny(request, ORIGIN, registry=_registry(), cfg=_cfg())
    _rewind_poll(raw)
    assert_eq(_poll_error(raw, ip), "access_denied",
              "a denied request must answer access_denied")

    expiring = _start(ip)
    OAuthDeviceCode.objects.filter(pk=_row(expiring["device_code"]).pk).update(
        expires=dates.utcnow() - datetime.timedelta(seconds=1))
    assert_eq(_poll_error(expiring["device_code"], ip), "expired_token",
              "an expired request must answer expired_token")


@th.tier("core")
@th.django_unit_test("approval yields exactly one device grant, and replay revokes it")
def test_approve_then_replay(opts):
    from mojo.apps.account.models import OAuthGrant
    from mojo.apps.account.services.oauth_server import tokens
    from mojo.helpers import dates

    ip = IPS[3]
    user = _user()
    OAuthGrant.objects.filter(user=user).delete()
    started = _start(ip)
    result = _approve(started["user_code"], ip, ttl_days=60)
    assert_eq(result.get("ttl_days"), 60, f"approve must echo the chosen lifetime, got {result}")

    _rewind_poll(started["device_code"])
    pair = _poll(started["device_code"], ip)
    assert_eq(sorted(pair.keys()),
              ["access_token", "expires_in", "refresh_token", "scope", "token_type"],
              f"an approved poll must answer a token pair, got {sorted(pair)}")
    grants = list(OAuthGrant.objects.filter(user=user))
    assert_eq(len(grants), 1, f"one approval must create exactly one grant, got {len(grants)}")
    grant = grants[0]
    assert_eq(grant.flow, "device_code", f"the grant must record its flow, got {grant.flow!r}")
    assert_eq(grant.resource, RESOURCE, f"the grant must bind the resource, got {grant.resource}")
    assert_eq(grant.scopes, ["mcp"], f"the grant must carry mcp only, got {grant.scopes}")
    days = (grant.refresh_expires - dates.utcnow()).total_seconds() / 86400
    assert_true(59.9 < days <= 60,
                f"the grant must live the chosen 60 days, got {days:.2f}")

    # The pair behaves like any grant's: it refreshes and it revokes.
    client = grant.client
    rotated = tokens.refresh_grant(pair["refresh_token"], client, registry=_registry())
    assert_true(bool(rotated.get("refresh_token")),
                "a device grant's refresh token must rotate like any other")

    assert_eq(_poll_error(started["device_code"], ip), "invalid_grant",
              "a consumed device code presented again must be refused")
    grant.refresh_from_db()
    assert_true(not grant.is_active, "replaying a device code must revoke its grant")
    assert_eq(grant.revoked_reason, "device_code_replay",
              f"the revocation must name the replay, got {grant.revoked_reason!r}")


@th.django_unit_test("the lifetime offered never exceeds the cap, and approve re-checks it")
def test_lifetime_choice(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import OAuthGrant
    from mojo.apps.account.services.oauth_server import device
    from mojo.helpers import dates

    assert_eq(device.lifetime_options(_cfg(max_ttl_days=90)), [30, 60, 90],
              "a 90-day cap must offer 30, 60 and 90")
    assert_eq(device.lifetime_options(_cfg(max_ttl_days=30)), [30],
              "the shipped 30-day cap must offer 30 only")
    assert_eq(device.lifetime_options(_cfg(max_ttl_days=45)), [30, 45],
              "a cap between the standard options must itself be offered")
    assert_eq(device.lifetime_options(_cfg(max_ttl_days=7)), [7],
              "a cap below 30 must be the only choice")

    ip = IPS[4]
    for cfg, ttl, why in ((_cfg(max_ttl_days=30), 60, "a lifetime above the cap"),
                          (_cfg(), 91, "a lifetime above 90"),
                          (_cfg(), "forever", "a lifetime that is not a number")):
        started = _start(ip, cfg=cfg)
        refused = False
        try:
            _approve(started["user_code"], ip, ttl_days=ttl, cfg=cfg)
        except merrors.MojoException:
            refused = True
        assert_true(refused, f"approve must refuse {why}")
        assert_eq(_row(started["device_code"]).status, "pending",
                  f"a refused {why} must leave the request pending")

    user = _user()
    OAuthGrant.objects.filter(user=user).delete()
    started = _start(ip)
    _approve(started["user_code"], ip)
    _rewind_poll(started["device_code"])
    _poll(started["device_code"], ip)
    grant = OAuthGrant.objects.get(user=user)
    days = (grant.refresh_expires - dates.utcnow()).total_seconds() / 86400
    assert_true(29.9 < days <= 30,
                f"with no choice posted the shortest option must apply, got {days:.2f}")


@th.django_unit_test("a disabled user or resource between approval and poll gets no grant")
def test_disabled_between_approve_and_poll(opts):
    from mojo.apps.account.models import OAuthGrant, User

    ip = IPS[5]
    other = _user(OTHER_USER)
    OAuthGrant.objects.filter(user=other).delete()

    started = _start(ip)
    _approve(started["user_code"], ip, user=other)
    User.objects.filter(pk=other.pk).update(is_active=False)
    try:
        _rewind_poll(started["device_code"])
        assert_eq(_poll_error(started["device_code"], ip), "invalid_grant",
                  "a user disabled after approving must not receive a grant")
    finally:
        User.objects.filter(pk=other.pk).update(is_active=True)

    state = {"on": True}
    registry = _registry(state)
    started = _start(ip, registry=registry)
    _approve(started["user_code"], ip, user=other, registry=registry)
    state["on"] = False
    _rewind_poll(started["device_code"])
    assert_eq(_poll_error(started["device_code"], ip, registry=registry), "invalid_grant",
              "a resource switched off after approval must not receive a grant")
    assert_eq(OAuthGrant.objects.filter(user=other).count(), 0,
              "neither refusal may have left a grant behind")


@th.tier("core")
@th.django_unit_test("only an interactive session may look up, approve or deny a code")
def test_session_required(opts):
    from mojo import errors as merrors
    from mojo.apps.account.services.oauth_server import device

    ip = IPS[6]
    user = _user()
    started = _start(ip)
    cases = [
        (dict(token=_session_token(user), bearer="apikey"), "an API key"),
        (dict(token=_session_token(user), bearer="grouptoken"), "a group token"),
        (dict(token=_session_token(user, token_type="user_api_key")), "a user_api_key JWT"),
        (dict(token=_session_token(user, auth_time=False)), "a token with no auth_time"),
        (dict(token=None), "no credential at all"),
    ]
    for action in (device.lookup, device.approve, device.deny):
        for kwargs, why in cases:
            request = _request({"user_code": started["user_code"]}, ip, user=user, **kwargs)
            refused = False
            try:
                action(request, ORIGIN, registry=_registry(), cfg=_cfg())
            except merrors.PermissionDeniedException:
                refused = True
            assert_true(refused, f"{action.__name__} must refuse {why}")
    assert_eq(_row(started["device_code"]).status, "pending",
              "no refused caller may have moved the request")


@th.django_unit_test("lookup shows the requester's facts and warns where it should")
def test_lookup_facts(opts):
    from mojo.apps.account.services.oauth_server import device

    ip = IPS[7]
    user = _user()
    started = _start(ip)

    def _lookup(approver_ip, **kwargs):
        request = _request({"user_code": started["user_code"].lower()}, approver_ip,
                           user=user, token=_session_token(user))
        return device.lookup(request, ORIGIN, registry=_registry(),
                             cfg=_cfg(**kwargs), geo_lookup=lambda _ip: "Testville, TX, US")

    facts = _lookup("203.0.113.9")
    assert_eq(facts["client_name"], DEVICE_CLIENT, "the page must name the client")
    assert_true(facts["client_unverified"], "a self-registered name must be marked unverified")
    assert_eq(facts["requested_resource"], RESOURCE, "the page must name the resource")
    assert_eq(facts["user_code"], started["user_code"],
              "the page must echo the code in its canonical form for comparison")
    assert_eq(facts["requester"]["ip"], ip, "the requester's IP must be shown")
    assert_eq(facts["requester"]["location"], "Testville, TX, US",
              "the requester's location must be shown")
    assert_eq(facts["requester"]["user_agent"], "testit-agent/1.0",
              "the requester's user agent must be shown")
    assert_true(0 <= facts["requester"]["age_seconds"] < 120,
                f"the request's age must be shown, got {facts['requester']['age_seconds']}")
    assert_true(facts["network_mismatch"],
                "a different network than the approver's must be flagged")
    assert_true(not facts["started_in_browser"],
                "a request with no browser fetch metadata came from a device")
    assert_true(facts["missing_permission"],
                "a person without view_admin or assistant must be warned the tool "
                "door will refuse them")
    assert_true(any(user.email in line for line in facts["access_lines"]),
                "the access sentence must name the approving account")

    same_net = ip.rsplit(".", 1)[0] + ".200"
    assert_true(not _lookup(same_net)["network_mismatch"],
                "an approver on the requester's /24 must not be warned")
    user.add_permission("assistant")
    try:
        assert_true(not _lookup(same_net)["missing_permission"],
                    "a person holding assistant must not be warned")
    finally:
        user.remove_all_permissions()
    assert_eq(_lookup(same_net, max_ttl_days=60)["lifetime_options"], [30, 60],
              "the page must offer only lifetimes within the cap")


@th.tier("core")
@th.django_unit_test("failed lookups are capped per person, then even a right code is refused")
def test_bruteforce_limit(opts):
    from mojo import errors as merrors
    from mojo.apps.incident.models import Event
    from mojo.apps.account.services.oauth_server import device

    ip = IPS[8]
    user = _user()
    _clear_counters(user)
    started = _start(ip)

    def _try(code):
        request = _request({"user_code": code}, ip, user=user, token=_session_token(user))
        try:
            device.lookup(request, ORIGIN, registry=_registry(), cfg=_cfg(),
                          geo_lookup=lambda _ip: "")
        except merrors.RateLimitException:
            return "limited"
        except merrors.ValueException:
            return "wrong"
        return "ok"

    try:
        for attempt in range(device.USER_FAILURE_LIMIT):
            assert_eq(_try("ZZZZ-ZZZZ"), "wrong",
                      f"wrong guess {attempt + 1} must be refused as wrong")
        assert_eq(_try(started["user_code"]), "limited",
                  "once the per-person limit is spent, even the right code must be "
                  "refused — otherwise the limit only slows a guesser down")
        assert_true(Event.objects.filter(category="oauth:device_code_bruteforce",
                                         model_id=user.pk).exists(),
                    "crossing the limit must raise oauth:device_code_bruteforce")
    finally:
        _clear_counters(user)
    assert_eq(_try(started["user_code"]), "ok",
              "after the window the right code must work again")


@th.django_unit_test("discovery advertises the device flow only while it is on")
def test_discovery_gating(opts):
    from mojo.apps.account.services.oauth_server import discovery

    off = discovery.authorization_server_metadata(
        ORIGIN, registry=_registry(), device_enabled=False)
    assert_true("device_authorization_endpoint" not in off,
                "a deployment without the flow must not advertise its endpoint")
    assert_true(DEVICE_GRANT not in off["grant_types_supported"],
                "a deployment without the flow must not advertise the grant")

    on = discovery.authorization_server_metadata(
        ORIGIN, registry=_registry(), device_enabled=True)
    assert_eq(on.get("device_authorization_endpoint"),
              f"{on['issuer']}/device_authorization",
              f"the endpoint must sit under the issuer, got "
              f"{on.get('device_authorization_endpoint')!r}")
    assert_true(DEVICE_GRANT in on["grant_types_supported"],
                "the grant type must be advertised while the flow is on")


@th.django_unit_test("registration stores grant types; only device-only clients skip redirects")
def test_registration_grant_types(opts):
    from mojo.apps.account.models import OAuthClient
    from mojo.apps.account.services.oauth_server import clients

    device_client = OAuthClient.objects.get(client_name=DEVICE_CLIENT)
    assert_eq(device_client.redirect_uris, [],
              "a device-only client must be able to register without a redirect")
    assert_eq(device_client.metadata.get("grant_types"), ["refresh_token", DEVICE_GRANT],
              f"the granted types must be stored, got {device_client.metadata}")

    browser = OAuthClient.objects.get(client_name=BROWSER_CLIENT)
    assert_eq(browser.metadata.get("grant_types"), ["authorization_code", "refresh_token"],
              "a client that names no grant types gets the browser flow only — the "
              "device grant is never implied")

    refused = False
    try:
        clients.register_client({"client_name": "testit device bad", "redirect_uris": [],
                                 "grant_types": ["authorization_code", DEVICE_GRANT]})
    except clients.ClientError:
        refused = True
    assert_true(refused, "a client asking for the browser flow must still list a redirect")

    legacy = OAuthClient(client_id="testit-device-legacy", client_name="legacy",
                         metadata={})
    assert_eq(clients.client_grant_types(legacy), ["authorization_code", "refresh_token"],
              "a row stored before grant types were kept must read as the browser flow")


@th.django_unit_test("the verification page is an un-frameable, uncached shell")
def test_device_page(opts):
    from django.test import RequestFactory
    from objict import objict
    from mojo.apps.account.services.oauth_server import device

    def _get(params, cfg):
        request = RequestFactory().get("/api/account/oauth/device", params)
        request.DATA = objict.fromdict(params)
        request.ip = IPS[9]
        request.user_agent = "testit"
        request.bearer = None
        return device.handle_page(request, ORIGIN, registry=_registry(), cfg=cfg)

    response = _get({"user_code": "bcdf ghjk"}, _cfg())
    assert_eq(response.status_code, 200, f"the page must render, got {response.status_code}")
    assert_eq(response.get("X-Frame-Options"), "DENY", "the page must be un-frameable")
    assert_eq(response.get("Cache-Control"), "no-store", "the page must not be cached")
    html = response.content.decode("utf-8")
    assert_true('value="BCDF-GHJK"' in html,
                "a code in the URL must pre-fill the box in its canonical form")
    assert_true("btn-device-continue" in html and "btn-device-approve" in html,
                "the page must make the person continue, then approve")

    hostile = _get({"user_code": '"><script>alert(1)</script>'}, _cfg())
    assert_true("<script>alert(1)" not in hostile.content.decode("utf-8"),
                "a hostile user_code must never reach the page")

    assert_eq(_get({}, _cfg(enabled=False)).status_code, 404,
              "with the flow off the page must not be available")


@th.tier("core")
@th.django_unit_test("a request a web page started is flagged on the consent screen")
def test_browser_started_request_is_flagged(opts):
    from mojo.apps.account.services.oauth_server import device

    # The phishing shape: the victim's own browser calls device_authorization
    # from the attacker's page, so the requester facts are the victim's.
    ip = IPS[10]
    user = _user()
    for meta, why in (({"HTTP_ORIGIN": "https://phish.example"}, "an Origin header"),
                      ({"HTTP_SEC_FETCH_MODE": "cors"}, "Sec-Fetch-Mode"),
                      ({"HTTP_SEC_FETCH_SITE": "cross-site"}, "Sec-Fetch-Site")):
        started = _start(ip, meta=meta)
        request = _request({"user_code": started["user_code"]}, ip, user=user,
                           token=_session_token(user))
        facts = device.lookup(request, ORIGIN, registry=_registry(), cfg=_cfg(),
                              geo_lookup=lambda _ip: "")
        assert_true(facts["started_in_browser"],
                    f"a request carrying {why} must be flagged as started by a web page")
        assert_true(not facts["network_mismatch"],
                    "the phishing shape shows the approver's own network — which is "
                    "exactly why the browser flag, not the network check, must catch it")


@th.django_unit_test("ending a person's sessions cancels an approval the device has not collected")
def test_end_sessions_cancels_uncollected_approval(opts):
    from mojo.apps.account.models import OAuthGrant
    from mojo.apps.account.services.oauth_server import tokens

    ip = IPS[11]
    other = _user(OTHER_USER)
    OAuthGrant.objects.filter(user=other).delete()
    started = _start(ip)
    _approve(started["user_code"], ip, user=other)

    tokens.revoke_all_grants(user=other, reason="sessions_ended")
    _rewind_poll(started["device_code"])
    assert_eq(_poll_error(started["device_code"], ip), "access_denied",
              "after the person ends their sessions, the device's next poll must "
              "be refused rather than mint a grant the sweep never saw")
    assert_eq(OAuthGrant.objects.filter(user=other).count(), 0,
              "no grant may be created after the sweep")


@th.django_unit_test("failed lookups from IPv6 count against the whole /64")
def test_ipv6_failures_bucket_by_64(opts):
    from objict import objict
    from mojo.apps.account.services.oauth_server import device

    def _key(ip):
        request = objict(ip=ip, META={}, user=objict(pk=1))
        return device._failure_keys(request)[1]

    assert_eq(_key("2001:db8:1:2::1"), _key("2001:db8:1:2:ffff:ffff:ffff:fffe"),
              "two addresses in one /64 must share a failure counter")
    assert_true(_key("2001:db8:1:2::1") != _key("2001:db8:1:3::1"),
                "different /64s must not share a failure counter")
    assert_true(_key("198.51.100.1") != _key("198.51.100.2"),
                "IPv4 addresses keep their own counters")
