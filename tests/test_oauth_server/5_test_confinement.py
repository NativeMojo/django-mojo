"""
Confinement: an mcp token authenticates at its resource door and nowhere else.

This is the security property the whole feature rests on, so it is asserted
from three directions:

  * ``tokens.validate_access`` directly, against a private registry;
  * ``User.validate_jwt`` — the real chokepoint every Bearer request takes —
    against the SHARED registry, using a per-run unique path that is
    unregistered in a ``finally`` so no other module can observe it;
  * over the wire, presenting a real minted token to three unrelated endpoints
    and to the refresh endpoint, and confirming an ordinary session still works.

The legacy branches must be provably untouched: a session JWT and a
``user_api_key`` JWT carry no ``aud`` and never reach the new code.
"""
import secrets
from contextlib import contextmanager

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

TEST_USER = "oauth_confine_user"
SPARE_USER = "oauth_confine_spare"
TEST_PWORD = "confine##mojo99"
CLIENT_ID = "testit-oauth-confine-client"
ORIGIN = "https://oauth.testit.example"

# Unique per run: this one path is registered in the SHARED registry, because
# User.validate_jwt consults the default instance. Any other module in the
# threaded runner must be unable to notice it.
LIVE_PATH = f"/api/testit/oauth-confine-{secrets.token_hex(4)}"
LIVE_RESOURCE = ORIGIN + LIVE_PATH

# The resource the wire tests name. It is registered on the SERVER (by the
# assistant app) but its switch is off there, so a token minted for it is
# refused at every door — which is exactly what these tests assert.
WIRE_RESOURCE = ORIGIN + "/api/assistant/mcp"


@contextmanager
def _shared_resource(enabled=True):
    from mojo.apps.account.services.oauth_server import resources

    resources.register_resource(LIVE_PATH, ["mcp"], lambda: enabled)
    try:
        yield
    finally:
        resources.unregister_resource(LIVE_PATH)


def _private_registry(enabled=True, path=LIVE_PATH):
    from mojo.apps.account.services.oauth_server import resources

    registry = resources.ResourceRegistry()
    registry.register(path, ["mcp"], lambda: enabled)
    return registry


@th.django_unit_setup()
def setup_confinement(opts):
    from mojo.apps.account.models import OAuthClient, User

    User.objects.filter(username__in=[TEST_USER, SPARE_USER]).delete()
    OAuthClient.objects.filter(client_id=CLIENT_ID).delete()

    user = User(username=TEST_USER, display_name=TEST_USER,
                email=f"{TEST_USER}@example.com")
    user.save()
    user.is_email_verified = True
    user.save_password(TEST_PWORD)
    user.remove_all_permissions()

    spare = User(username=SPARE_USER, display_name=SPARE_USER,
                 email=f"{SPARE_USER}@example.com")
    spare.save()

    OAuthClient(client_id=CLIENT_ID, kind="dcr", client_name="Confine Client",
                redirect_uris=["http://127.0.0.1:8300/cb"]).save()


def _grant_and_token(resource=LIVE_RESOURCE, username=TEST_USER, ttl=None):
    from mojo.apps.account.models import OAuthClient, User
    from mojo.apps.account.utils.jwtoken import JWToken
    from mojo.apps.account.services.oauth_server import tokens

    user = User.objects.get(username=username)
    client = OAuthClient.objects.get(client_id=CLIENT_ID)
    grant = tokens.create_grant(user, client, ["mcp"], resource, 1700000000)
    token = tokens.mint_access_token(grant, ttl=ttl)
    payload = JWToken().decode(token, validate=False)
    return grant, token, payload


@th.django_unit_test("a valid token is accepted at its own resource path")
def test_accepted_at_its_own_path(opts):
    from mojo.apps.account.models import OAuthGrant
    from mojo.apps.account.services.oauth_server import tokens

    grant, token, payload = _grant_and_token()
    request = th.get_mock_request(path=LIVE_PATH)
    user, error = tokens.validate_access(
        token, payload, request, registry=_private_registry())

    assert_true(error is None, f"a valid mcp token must be accepted, got {error!r}")
    assert_eq(user.pk, grant.user_id, "the token must authenticate its own user")
    assert_eq(getattr(request, "oauth_grant", None).pk, grant.pk,
              "the grant must be stamped on the request for the resource server")
    assert_true(OAuthGrant.objects.get(pk=grant.pk).last_used is not None,
                "an accepted token must stamp last_used on its grant")


@th.django_unit_test("a token is refused everywhere except its own path, with no challenge")
def test_refused_off_its_path(opts):
    from mojo.apps.account.services.oauth_server import tokens

    _grant, token, payload = _grant_and_token()
    registry = _private_registry()

    for path in ("/api/account/user/me", "/api/assistant/action", LIVE_PATH + "/x"):
        request = th.get_mock_request(path=path)
        user, error = tokens.validate_access(token, payload, request, registry=registry)
        assert_true(user is None, f"an mcp token must be refused at {path}")
        assert_eq(error, "Invalid token",
                  f"the refusal at {path} must be the generic error, got {error!r}")
        assert_true(getattr(request, "www_authenticate", None) is None,
                    f"{path} is not a resource door, so it must NOT grow a "
                    f"WWW-Authenticate challenge")

    # No request at all — the refresh endpoint and the realtime consumer.
    user, error = tokens.validate_access(token, payload, None, registry=registry)
    assert_true(user is None,
                "an mcp token presented with no request must be refused — that is "
                "what stops it becoming a session pair or a WebSocket")
    assert_eq(error, "Invalid token",
              f"the no-request refusal must be generic, got {error!r}")


@th.django_unit_test("a disabled resource makes its tokens dormant, silently")
def test_disabled_resource_is_dormant(opts):
    from mojo.apps.account.services.oauth_server import tokens

    _grant, token, payload = _grant_and_token()
    request = th.get_mock_request(path=LIVE_PATH)
    user, error = tokens.validate_access(
        token, payload, request, registry=_private_registry(enabled=False))
    assert_true(user is None, "a token for a switched-off resource must be refused")
    assert_eq(error, "Invalid token",
              f"the refusal must be generic, got {error!r}")
    assert_true(getattr(request, "www_authenticate", None) is None,
                "a switched-off resource is not a live door and must not "
                "advertise itself with a challenge")


@th.django_unit_test("refusals at a live door carry the RFC 9728 challenge")
def test_live_door_refusals_carry_the_challenge(opts):
    from mojo.apps.account.models import OAuthGrant, User
    from mojo.apps.account.services.oauth_server import resources, tokens
    from mojo.apps.account.services import disable

    registry = _private_registry()

    def _refuse(token, payload):
        request = th.get_mock_request(path=LIVE_PATH)
        user, error = tokens.validate_access(token, payload, request, registry=registry)
        return user, error, getattr(request, "www_authenticate", None)

    # 1. a revoked grant
    grant, token, payload = _grant_and_token()
    tokens.revoke_grant(grant, reason="admin")
    user, error, challenge = _refuse(token, payload)
    assert_true(user is None, "a revoked grant's token must be refused")
    assert_eq(error, "Invalid token",
              f"a revoked grant must not be an oracle, got {error!r}")

    # 2. a tampered audience (same path, different host)
    grant2, token2, payload2 = _grant_and_token()
    tampered = dict(payload2)
    tampered["aud"] = "https://attacker.example" + LIVE_PATH
    user, error, _ = _refuse(token2, tampered)
    assert_true(user is None,
                "an aud the grant does not name must be refused even at the "
                "right path")

    # 3. a list-valued audience — PyJWT would otherwise match by membership
    listed = dict(payload2)
    listed["aud"] = [LIVE_RESOURCE, "https://attacker.example/anything"]
    request = th.get_mock_request(path=LIVE_PATH)
    user, error = tokens.validate_access(token2, listed, request, registry=registry)
    assert_true(user is None, "a list-valued aud claim must be refused outright")

    # 4. a missing jti
    nojti = dict(payload2)
    nojti.pop("jti", None)
    user, error, challenge = _refuse(token2, nojti)
    assert_true(user is None, "a token with no jti resolves no grant and must fail")
    assert_true(challenge is not None,
                "a refusal at a live door must carry a WWW-Authenticate challenge")

    # 5. expired
    _grant3, token3, payload3 = _grant_and_token(ttl=-5)
    user, error, challenge = _refuse(token3, payload3)
    assert_true(user is None, "an expired token must be refused")
    assert_eq(error, "Token expired",
              f"expiry is the ONE thing the refusal may disclose, got {error!r}")
    assert_true(challenge is not None,
                "the expiry refusal at a live door must carry the challenge")
    assert_true(challenge.startswith('Bearer error="invalid_token"'),
                f"the challenge must be the invalid_token form, got {challenge!r}")
    origin = resources.public_origin()
    if origin:
        assert_true(
            f'resource_metadata="{resources.prm_url(origin, LIVE_PATH)}"' in challenge,
            f"a configured installation must point the client at its "
            f"protected-resource metadata, got {challenge!r}")

    # 6. a disabled account — the auth_key rotation kills every live token
    spare_grant, spare_token, spare_payload = _grant_and_token(username=SPARE_USER)
    disable.disable_entity(
        User.objects.get(username=SPARE_USER), reason="test",
        reporter=lambda **kwargs: None)
    try:
        user, error, _ = _refuse(spare_token, spare_payload)
        assert_true(user is None,
                    "disabling an account must kill its live mcp tokens")
        assert_eq(error, "Invalid token",
                  f"a disabled account must not be an oracle, got {error!r}")
    finally:
        User.objects.filter(username=SPARE_USER).update(is_active=True)
        OAuthGrant.objects.filter(pk=spare_grant.pk).delete()


@th.django_unit_test("User.validate_jwt takes the mcp branch through the shared registry")
def test_validate_jwt_mcp_branch(opts):
    from mojo.apps.account.models import User

    _grant, token, _payload = _grant_and_token()
    with _shared_resource(enabled=True):
        request = th.get_mock_request(path=LIVE_PATH)
        user, error = User.validate_jwt(token, request)
        assert_true(error is None,
                    f"validate_jwt must accept a valid mcp token at its own "
                    f"resource path, got {error!r}")
        assert_eq(user.username, TEST_USER,
                  "validate_jwt must return the grant's user")

        elsewhere = th.get_mock_request(path="/api/account/user/me")
        user, error = User.validate_jwt(token, elsewhere)
        assert_true(user is None,
                    "validate_jwt must refuse an mcp token off its resource path")
        assert_eq(error, "Invalid token",
                  f"the refusal must be the generic error, got {error!r}")

        user, error = User.validate_jwt(token)
        assert_true(user is None,
                    "validate_jwt with no request must refuse an mcp token")

    # Unregistered again: the same token is now refused everywhere.
    request = th.get_mock_request(path=LIVE_PATH)
    user, error = User.validate_jwt(token, request)
    assert_true(user is None,
                "an unregistered resource path must refuse its own tokens")


@th.django_unit_test("the legacy validate_jwt branches are untouched by the new check")
def test_legacy_branches_unaffected(opts):
    from mojo.apps.account.models import User, UserAPIKey
    from mojo.apps.account.utils.jwtoken import JWToken

    user = User.objects.get(username=TEST_USER)

    session = JWToken(user.get_auth_key()).create_access_token(uid=user.pk)
    for request in (th.get_mock_request(path="/api/account/user/me"), None):
        found, error = User.validate_jwt(session, request)
        assert_true(error is None,
                    f"an ordinary session JWT must still validate, got {error!r}")
        assert_eq(found.pk, user.pk, "the session JWT must resolve its own user")

    UserAPIKey.objects.filter(user=user).delete()
    api_token = UserAPIKey.create_for_user(
        user, expire_days=1, label="confinement").token
    for request in (th.get_mock_request(path="/api/account/user/me"), None):
        found, error = User.validate_jwt(api_token, request)
        assert_true(error is None,
                    f"a user_api_key JWT must still validate, got {error!r}")
        assert_eq(found.pk, user.pk, "the api-key JWT must resolve its own user")
    UserAPIKey.objects.filter(user=user).delete()


@th.django_unit_test("_build_request_meta reports an mcp caller as such")
def test_request_meta_marks_mcp(opts):
    from testit import TestitSkip

    if not th.is_app_installed("mojo.apps.assistant"):
        raise TestitSkip("mojo.apps.assistant is not installed")
    from mojo.apps.assistant.services.agent import _build_request_meta

    grant, _token, _payload = _grant_and_token()

    request = th.get_mock_request(path=LIVE_PATH)
    request.bearer = "bearer"
    request.oauth_grant = grant
    meta = _build_request_meta(request)
    assert_eq(meta.bearer, "mcp",
              f"a grant-carrying request must report bearer=mcp so tools can "
              f"tell it from a person at a keyboard, got {meta.bearer!r}")
    assert_true(not meta.key_backed,
                "an OAuth grant is not a key-backed session")

    plain = th.get_mock_request(path="/api/assistant/action")
    plain.bearer = "bearer"
    assert_eq(_build_request_meta(plain).bearer, "bearer",
              "an ordinary session must still report bearer=bearer")


@th.django_unit_test("over the wire: an mcp token opens no door on this platform")
def test_wire_confinement(opts):
    from mojo.apps.account.models import OAuthClient, User
    from mojo.apps.account.services.oauth_server import tokens

    user = User.objects.get(username=TEST_USER)
    client = OAuthClient.objects.get(client_id=CLIENT_ID)
    grant = tokens.create_grant(user, client, ["mcp"], WIRE_RESOURCE, 1700000000)
    token = tokens.mint_access_token(grant)

    opts.client.logout()
    headers = {"Authorization": f"Bearer {token}"}
    for path, method in (("/api/account/user/me", "GET"),
                         ("/api/assistant/action", "POST"),
                         ("/api/auth/generate_api_key", "POST")):
        if method == "GET":
            resp = opts.client.get(path, headers=headers)
        else:
            resp = opts.client.post(path, {}, headers=headers)
        assert_eq(resp.status_code, 401,
                  f"an mcp token must not authenticate at {path}, "
                  f"got {resp.status_code}")
        challenge = {k.lower(): v for k, v in
                     opts.client.last_response.headers.items()}.get("www-authenticate")
        assert_true(challenge is None,
                    f"{path} is not a registered resource door and must not "
                    f"advertise one, got {challenge!r}")

    resp = opts.client.post("/api/account/jwt/refresh",
                            {"refresh_token": token})
    assert_eq(resp.status_code, 401,
              f"an mcp token must never be exchanged for a session pair, "
              f"got {resp.status_code}")

    # The control: an ordinary login still works, so the checks above are not
    # simply a broken auth path.
    assert_true(opts.client.login(TEST_USER, TEST_PWORD),
                "the control login must succeed")
    resp = opts.client.get("/api/account/user/me")
    assert_eq(resp.status_code, 200,
              f"an ordinary session must still reach /api/account/user/me, "
              f"got {resp.status_code}")
    opts.client.logout()
