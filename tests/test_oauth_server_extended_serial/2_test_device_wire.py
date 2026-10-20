"""
The RFC 8628 device flow end to end, over the wire, against the running server.

Like 1_test_wire_flow, this needs BASE_URL, the MCP switch and the device-flow
switch on the SERVER process — `th.server_settings()` and the deployment-file
plane — which is why it lives in the serial, opt-in package. The shadowing
global Setting rows are cleared the same way, before and after.

What only the wire can show: the decorators. Every key-backed caller is refused
at lookup and approve, a stale login gets a 440 at approve, the page carries its
anti-framing and no-store headers, and the token the device flow issues opens
the MCP door.
"""
import time

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

BASE = "https://oauth.testit.example"
MCP_PATH = "/api/assistant/mcp"
RESOURCE = BASE + MCP_PATH
ROOT = "/api/account/oauth"
AS_METADATA = "/.well-known/oauth-authorization-server/api/account/oauth"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

TEST_USER = "oauth_device_wire_user"
TEST_PWORD = "devicewire##mojo99"
API_KEY_NAME = "testit oauth device wire key"
GROUP_NAME = "testit oauth device wire group"
CLIENT_NAME = "testit device wire client"

SHADOWING_KEYS = ("BASE_URL", "ASSISTANT_MCP_ENABLED")
RATE_BUCKETS = ("oauth_discovery", "oauth_register", "oauth_token",
                "oauth_device_authorization", "oauth_device_page",
                "oauth_device_lookup", "oauth_device_approve")
SETTINGS = dict(BASE_URL=BASE, ASSISTANT_MCP_ENABLED=True,
                OAUTH_DEVICE_FLOW_ENABLED=True, OAUTH_DEVICE_MAX_TTL_DAYS=90)


def _clear_shadowing_rows():
    """Drop DB/Redis values that would out-rank the file-plane override."""
    from mojo.apps.account.models import Setting

    for key in SHADOWING_KEYS:
        Setting.objects.filter(key=key, group=None).delete()
        try:
            Setting._redis().hdel(Setting._redis_key(), key)
        except Exception:
            pass


def _headers(opts):
    return {k.lower(): v for k, v in opts.client.last_response.headers.items()}


def _clear_counters(user):
    from mojo.helpers.redis import get_connection

    get_connection().delete(f"oauth:device:fail:user:{user.pk}",
                            "oauth:device:fail:ip:127.0.0.1")


@th.django_unit_setup()
def setup_device_wire(opts):
    from mojo.decorators.limits import clear_rate_limits
    from mojo.apps.account.models import (
        ApiKey, Group, OAuthClient, OAuthGrant, User)

    _clear_shadowing_rows()
    clear_rate_limits(ip="127.0.0.1", key="login")
    for bucket in RATE_BUCKETS:
        clear_rate_limits(ip="127.0.0.1", key=bucket)

    User.objects.filter(username=TEST_USER).delete()
    OAuthClient.objects.filter(client_name=CLIENT_NAME).delete()
    ApiKey.objects.filter(name=API_KEY_NAME).delete()
    Group.objects.filter(name=GROUP_NAME).delete()

    user = User(username=TEST_USER, display_name=TEST_USER,
                email=f"{TEST_USER}@example.com")
    user.save()
    user.is_email_verified = True
    user.save_password(TEST_PWORD)
    user.remove_all_permissions()
    _clear_counters(user)
    OAuthGrant.objects.filter(user=user).delete()

    group = Group.objects.create(name=GROUP_NAME, kind="organization")
    group.add_member(user)
    opts.group_id = group.pk
    _api_key, raw_token = ApiKey.create_for_group(
        group=group, name=API_KEY_NAME, permissions={}, user=user,
        override_user=True)
    opts.api_key_token = raw_token


def _rewind_poll(device_code):
    import datetime
    import hashlib
    from mojo.helpers import dates
    from mojo.apps.account.models import OAuthDeviceCode

    OAuthDeviceCode.objects.filter(
        device_code_hash=hashlib.sha256(device_code.encode()).hexdigest()).update(
            last_polled=dates.utcnow() - datetime.timedelta(seconds=120))


def _poll(opts, device_code, client_id):
    return opts.client.post(f"{ROOT}/token", data={
        "grant_type": DEVICE_GRANT,
        "device_code": device_code,
        "client_id": client_id,
    })


@th.django_unit_test("the device flow, end to end over the wire")
def test_device_wire_flow(opts):
    from mojo.apps.account.models import OAuthGrant, User, UserAPIKey
    from mojo.apps.account.services import group_token
    from mojo.apps.account.models import Group
    from mojo.apps.account.utils.jwtoken import JWToken
    from mojo.helpers import dates

    user = User.objects.get(username=TEST_USER)
    _clear_shadowing_rows()
    try:
        with th.server_settings(**SETTINGS):
            # --- discovery and registration ---------------------------------
            resp = opts.client.get(AS_METADATA)
            assert_eq(resp.status_code, 200,
                      f"AS metadata must be served, got {resp.status_code}")
            assert_eq(resp.response.get("device_authorization_endpoint"),
                      f"{BASE}{ROOT}/device_authorization",
                      "a deployment with the flow on must advertise its endpoint")
            assert_true(DEVICE_GRANT in resp.response.get("grant_types_supported", []),
                        "a deployment with the flow on must advertise the grant")

            resp = opts.client.post(f"{ROOT}/register", {
                "client_name": CLIENT_NAME,
                "grant_types": [DEVICE_GRANT, "refresh_token"]})
            assert_eq(resp.status_code, 201,
                      f"a device-only client must register with no redirect, "
                      f"got {resp.status_code} {resp.response}")
            client_id = resp.response.get("client_id")

            # --- the agent starts a request ----------------------------------
            resp = opts.client.post(f"{ROOT}/device_authorization", data={
                "client_id": client_id, "scope": "mcp", "resource": RESOURCE})
            assert_eq(resp.status_code, 200,
                      f"device_authorization must answer 200, got "
                      f"{resp.status_code} {resp.response}")
            started = resp.response
            assert_true("data" not in started,
                        "device_authorization must be RAW RFC JSON, not the envelope")
            user_code = started.get("user_code")
            device_code = started.get("device_code")
            assert_true(bool(user_code) and bool(device_code),
                        f"both codes must be issued, got {sorted(started.keys())}")
            assert_true("no-store" in (_headers(opts).get("cache-control") or ""),
                        "a response carrying a device code must not be cached")

            resp = _poll(opts, device_code, client_id)
            assert_eq(resp.response.get("error"), "authorization_pending",
                      f"an unanswered request must poll as pending, got {resp.response}")

            # --- the page ----------------------------------------------------
            opts.client.logout()
            resp = opts.client.get(f"{ROOT}/device", params={"user_code": user_code})
            assert_eq(resp.status_code, 200,
                      f"the verification page must render, got {resp.status_code}")
            headers = _headers(opts)
            assert_eq(headers.get("x-frame-options"), "DENY",
                      "the verification page must never be frameable")
            assert_true("no-store" in (headers.get("cache-control") or ""),
                        f"the verification page must not be cached, got "
                        f"{headers.get('cache-control')!r}")
            assert_true(f'value="{user_code}"' in resp.text,
                        "the link's code must pre-fill the box")

            # --- every key-backed caller is refused -------------------------
            body = {"user_code": user_code, "ttl_days": 90}
            UserAPIKey.objects.filter(user=user).delete()
            user_key = UserAPIKey.create_for_user(user, expire_days=1, label="device").token
            gs_token = group_token.mint(user, Group.objects.get(pk=opts.group_id))
            callers = (
                ({}, "an anonymous caller"),
                ({"Authorization": f"apikey {opts.api_key_token}"}, "an ApiKey"),
                ({"Authorization": f"Bearer {user_key}"}, "a user API key"),
                ({"Authorization": f"grouptoken {gs_token}"}, "a group token"),
            )
            for action in ("lookup", "approve", "deny"):
                for headers, why in callers:
                    resp = opts.client.post(f"{ROOT}/device/{action}", body,
                                            headers=headers)
                    assert_true(resp.status_code in (401, 403),
                                f"device/{action} must refuse {why}, got "
                                f"{resp.status_code} {resp.response}")
            UserAPIKey.objects.filter(user=user).delete()

            # --- a stale login may look, but not approve ---------------------
            stale = JWToken(user.get_auth_key()).create_access_token(
                uid=user.pk, auth_time=int(time.time()) - 3600)
            stale_headers = {"Authorization": f"Bearer {stale}"}
            resp = opts.client.post(f"{ROOT}/device/lookup", {"user_code": user_code},
                                    headers=stale_headers)
            assert_eq(resp.status_code, 200,
                      f"lookup only reads, so an older login may use it, got "
                      f"{resp.status_code} {resp.response}")
            facts = resp.response.data
            assert_eq(facts.requester.ip, "127.0.0.1",
                      f"the page must show the requester's IP, got {facts.requester}")
            assert_eq(list(facts.lifetime_options), [30, 60, 90],
                      f"a 90-day cap must offer 30/60/90, got {facts.lifetime_options}")
            resp = opts.client.post(f"{ROOT}/device/approve", body, headers=stale_headers)
            assert_eq(resp.status_code, 440,
                      f"approving with a login older than ten minutes must answer "
                      f"440, got {resp.status_code} {resp.response}")

            # --- a fresh login approves -------------------------------------
            assert_true(opts.client.login(TEST_USER, TEST_PWORD),
                        "the wire user must be able to sign in")
            resp = opts.client.post(f"{ROOT}/device/approve", body)
            assert_eq(resp.status_code, 200,
                      f"a fresh interactive session must approve, got "
                      f"{resp.status_code} {resp.response}")
            opts.client.logout()

            _rewind_poll(device_code)
            resp = _poll(opts, device_code, client_id)
            assert_eq(resp.status_code, 200,
                      f"the approved request must poll a token pair, got "
                      f"{resp.status_code} {resp.response}")
            access = resp.response.get("access_token")
            claims = JWToken().decode(access, validate=False)
            assert_eq((claims.get("token_type"), claims.get("aud")), ("mcp", RESOURCE),
                      f"the token must be an mcp token for the MCP door, got {claims}")
            grant = OAuthGrant.objects.filter(user=user).order_by("-created").first()
            assert_eq(grant.flow, "device_code",
                      f"the grant must record the device flow, got {grant.flow!r}")
            days = (grant.refresh_expires - dates.utcnow()).total_seconds() / 86400
            assert_true(89.9 < days <= 90,
                        f"the grant must live the chosen 90 days, got {days:.2f}")

            # --- the token opens the MCP door --------------------------------
            user.add_permission("assistant")
            try:
                resp = opts.client.post(
                    MCP_PATH, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"Authorization": f"Bearer {access}"})
                assert_eq(resp.status_code, 200,
                          f"a device token must list the MCP tools, got "
                          f"{resp.status_code} {resp.body}")
                result = (resp.response or {}).get("result") or {}
                assert_true(isinstance(result.get("tools"), list),
                            f"tools/list must answer a tool list, got {resp.response}")
            finally:
                user.remove_all_permissions()

            # --- deny ---------------------------------------------------------
            resp = opts.client.post(f"{ROOT}/device_authorization", data={
                "client_id": client_id, "resource": RESOURCE})
            second = resp.response
            assert_true(opts.client.login(TEST_USER, TEST_PWORD),
                        "the wire user must be able to sign in again")
            resp = opts.client.post(f"{ROOT}/device/deny",
                                    {"user_code": second.get("user_code")})
            assert_eq(resp.status_code, 200,
                      f"an interactive session must be able to deny, got "
                      f"{resp.status_code} {resp.response}")
            opts.client.logout()
            resp = _poll(opts, second.get("device_code"), client_id)
            assert_eq(resp.response.get("error"), "access_denied",
                      f"a denied request must poll access_denied, got {resp.response}")
    finally:
        _clear_shadowing_rows()
        _clear_counters(user)

    # With the switch back off, nothing about the flow is reachable.
    resp = opts.client.post(f"{ROOT}/device_authorization", data={"client_id": "x"})
    assert_true(resp.status_code in (404, 405),
                f"with the flow off device_authorization must not answer, got "
                f"{resp.status_code}")
