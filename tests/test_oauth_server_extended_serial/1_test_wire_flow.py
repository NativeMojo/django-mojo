"""
The whole OAuth 2.1 flow over the wire, against the running server.

This module needs BASE_URL and the resource enable switch set on the SERVER
process, which means `th.server_settings()` and the deployment-file plane.

`settings.get` is DATABASE-first, so a leftover global `Setting` row for
BASE_URL or ASSISTANT_MCP_ENABLED — written by any other module, in any earlier
run — silently shadows the file-plane override and makes every assertion here
lie. Both keys are therefore cleared through the queryset (Setting.delete()
refuses protected keys) and out of the Redis settings hash, in setup and in a
`finally` around every override. Neither key is ever WRITTEN by these tests.
"""
import base64
import hashlib
import re
import secrets
from urllib.parse import parse_qs, urlsplit

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

BASE = "https://oauth.testit.example"
MCP_PATH = "/api/assistant/mcp"
RESOURCE = BASE + MCP_PATH
ROOT = "/api/account/oauth"
AS_METADATA = "/.well-known/oauth-authorization-server/api/account/oauth"
PRM = "/.well-known/oauth-protected-resource/api/assistant/mcp"
# The second registered resource: the REST API root, reached with `api`.
API_ROOT_PATH = "/api"
API_ROOT_RESOURCE = BASE + API_ROOT_PATH
API_PRM = "/.well-known/oauth-protected-resource/api"

TEST_USER = "oauth_wire_user"
TEST_PWORD = "wire##mojo99"
API_KEY_NAME = "testit oauth wire key"
API_KEY_GROUP = "testit oauth wire group"
CLIENT_NAME = "testit wire client"
REDIRECT = "http://127.0.0.1:8500/cb"

SHADOWING_KEYS = ("BASE_URL", "ASSISTANT_MCP_ENABLED")
RATE_BUCKETS = ("oauth_discovery", "oauth_register", "oauth_authorize",
                "oauth_approve", "oauth_token", "oauth_revoke")


def _clear_shadowing_rows():
    """Drop DB/Redis values that would out-rank the file-plane override."""
    from mojo.apps.account.models import Setting

    for key in SHADOWING_KEYS:
        Setting.objects.filter(key=key, group=None).delete()
        try:
            Setting._redis().hdel(Setting._redis_key(), key)
        except Exception:
            pass


def _verifier():
    return secrets.token_urlsafe(48)[:64]


def _challenge(verifier):
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _headers(opts):
    return {k.lower(): v for k, v in opts.client.last_response.headers.items()}


@th.django_unit_setup()
def setup_wire(opts):
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
    Group.objects.filter(name=API_KEY_GROUP).delete()

    user = User(username=TEST_USER, display_name=TEST_USER,
                email=f"{TEST_USER}@example.com")
    user.save()
    user.is_email_verified = True
    user.save_password(TEST_PWORD)
    user.remove_all_permissions()

    group = Group.objects.create(name=API_KEY_GROUP, kind="organization")
    _api_key, raw_token = ApiKey.create_for_group(
        group=group, name=API_KEY_NAME, permissions={"view_global": True})
    opts.api_key_token = raw_token
    OAuthGrant.objects.filter(user=user).delete()


def _register(opts, **overrides):
    payload = {"redirect_uris": [REDIRECT], "client_name": CLIENT_NAME}
    payload.update(overrides)
    return opts.client.post(f"{ROOT}/register", payload)


@th.django_unit_test("the whole authorization-code flow, end to end over the wire")
def test_wire_flow(opts):
    from mojo.apps.account.models import OAuthClient, OAuthGrant, User
    from mojo.apps.account.utils.jwtoken import JWToken
    from mojo.apps.account.services.oauth_server import tokens

    _clear_shadowing_rows()
    try:
        with th.server_settings(BASE_URL=BASE, ASSISTANT_MCP_ENABLED=True,
                                AUTH_CSP_ENABLED=True):
            # --- discovery -------------------------------------------------
            resp = opts.client.get(AS_METADATA)
            assert_eq(resp.status_code, 200,
                      f"the path-suffixed AS metadata must be served, "
                      f"got {resp.status_code}")
            document = resp.response
            assert_eq(document.get("issuer"), f"{BASE}{ROOT}",
                      f"the issuer must be the path-suffixed root, "
                      f"got {document.get('issuer')!r}")
            assert_true("data" not in document,
                        "discovery must be RAW RFC JSON, not the framework "
                        f"envelope — got keys {sorted(document.keys())}")
            assert_eq(_headers(opts).get("access-control-allow-origin"), "*",
                      "discovery must be readable from any origin")

            resp = opts.client.get(PRM)
            assert_eq(resp.status_code, 200,
                      f"protected-resource metadata must be served for a live "
                      f"resource, got {resp.status_code}")
            assert_eq(resp.response.get("resource"), RESOURCE,
                      f"the PRM must name the canonical resource URL, "
                      f"got {resp.response.get('resource')!r}")
            resp = opts.client.get(
                "/.well-known/oauth-protected-resource/api/not/a/resource")
            assert_eq(resp.status_code, 404,
                      f"an unregistered path must have no PRM, got {resp.status_code}")

            # --- dynamic client registration -------------------------------
            resp = _register(opts)
            assert_eq(resp.status_code, 201,
                      f"registration must answer 201, got {resp.status_code}")
            client_id = resp.response.get("client_id")
            assert_true(bool(client_id), "registration must return a client_id")
            assert_eq(resp.response.get("token_endpoint_auth_method"), "none",
                      "the server issues no client secrets")

            for overrides, why in (
                    ({"redirect_uris": []}, "an empty redirect set"),
                    ({"redirect_uris": ["myapp://cb"]}, "a custom-scheme redirect"),
                    ({"redirect_uris": ["http://evil.example/cb"]},
                     "a remote http redirect")):
                bad = _register(opts, **overrides)
                assert_eq(bad.status_code, 400,
                          f"{why} must be refused, got {bad.status_code}")
                assert_true(bool(bad.response.get("error")),
                            f"{why} must answer an RFC error code")

            # --- the consent page ------------------------------------------
            verifier = _verifier()
            challenge = _challenge(verifier)
            params = {
                "client_id": client_id,
                "redirect_uri": REDIRECT,
                "response_type": "code",
                "state": "wire-state",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": RESOURCE,
            }
            resp = opts.client.get(f"{ROOT}/authorize", params=params,
                                   allow_redirects=False)
            assert_eq(resp.status_code, 200,
                      f"a well-formed authorize request must render the consent "
                      f"page, got {resp.status_code}")
            headers = _headers(opts)
            assert_eq(headers.get("x-frame-options"), "DENY",
                      "the consent page must never be frameable")
            policy = headers.get("content-security-policy", "")
            assert_true("frame-ancestors 'none'" in policy,
                        f"the live CSP must forbid framing, got {policy!r}")
            nonces = re.findall(r"'nonce-([0-9a-f]{32})'", policy)
            assert_true(bool(nonces),
                        f"the live CSP must carry a script nonce, got {policy!r}")
            html = resp.text
            assert_true(f'nonce="{nonces[0]}"' in html,
                        "the header nonce and the markup nonce must match")
            assert_true("access_denied" in html,
                        "the page must carry a server-built deny URL")

            unknown = dict(params, client_id="testit-wire-unknown")
            resp = opts.client.get(f"{ROOT}/authorize", params=unknown,
                                   allow_redirects=False)
            assert_eq(resp.status_code, 400,
                      f"an unknown client must render an error, got {resp.status_code}")
            assert_true(_headers(opts).get("location") is None,
                        "an unknown client must NEVER produce a redirect")

            bad_pkce = dict(params, code_challenge_method="plain")
            resp = opts.client.get(f"{ROOT}/authorize", params=bad_pkce,
                                   allow_redirects=False)
            assert_eq(resp.status_code, 302,
                      f"a parameter error must redirect to the client, "
                      f"got {resp.status_code}")
            query = parse_qs(urlsplit(_headers(opts)["location"]).query)
            assert_eq(query.get("error"), ["invalid_request"],
                      f"the plain PKCE method must be refused, got {query.get('error')}")
            assert_eq(query.get("state"), ["wire-state"],
                      "the client's state must be echoed on the error redirect")
            assert_eq(query.get("iss"), [f"{BASE}{ROOT}"],
                      "the RFC 9207 issuer must ride on the error redirect")

            # --- approve requires an interactive session --------------------
            opts.client.logout()
            resp = opts.client.post(f"{ROOT}/approve", params)
            assert_true(resp.status_code in (401, 403),
                        f"approve must refuse an anonymous caller, "
                        f"got {resp.status_code}")

            opts.client.bearer = "apikey"
            opts.client.access_token = opts.api_key_token
            opts.client.is_authenticated = True
            resp = opts.client.post(f"{ROOT}/approve", params)
            assert_true(resp.status_code in (401, 403),
                        f"approve must refuse an API key — it must never mint a "
                        f"credential that outlives the key, got {resp.status_code}")
            opts.client.logout()

            user = User.objects.get(username=TEST_USER)
            from mojo.apps.account.models import UserAPIKey
            UserAPIKey.objects.filter(user=user).delete()
            api_jwt = UserAPIKey.create_for_user(
                user, expire_days=1, label="wire").token
            resp = opts.client.post(f"{ROOT}/approve", params,
                                    headers={"Authorization": f"Bearer {api_jwt}"})
            assert_true(resp.status_code in (401, 403),
                        f"approve must refuse a user_api_key JWT even though it "
                        f"authenticates, got {resp.status_code}")
            UserAPIKey.objects.filter(user=user).delete()

            # --- approve with a real session --------------------------------
            assert_true(opts.client.login(TEST_USER, TEST_PWORD),
                        "the wire user must be able to sign in")
            resp = opts.client.post(f"{ROOT}/approve", params)
            assert_eq(resp.status_code, 200,
                      f"an interactive session must be able to approve, "
                      f"got {resp.status_code} {resp.response}")
            redirect_url = resp.response.data.redirect_url
            query = parse_qs(urlsplit(redirect_url).query)
            assert_true(redirect_url.startswith(REDIRECT),
                        f"approve must return the client's redirect, got {redirect_url}")
            assert_eq(query.get("state"), ["wire-state"],
                      "the state must be echoed on the success redirect")
            assert_eq(query.get("iss"), [f"{BASE}{ROOT}"],
                      "the issuer must ride on the success redirect")
            code = query["code"][0]
            opts.client.logout()

            # --- token exchange, form-encoded, loopback port drift ----------
            resp = opts.client.post(f"{ROOT}/token", data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "client_id": client_id,
                # A CLI client binds a fresh ephemeral port every run.
                "redirect_uri": "http://127.0.0.1:59999/cb",
                "resource": RESOURCE,
            })
            assert_eq(resp.status_code, 200,
                      f"a form-encoded exchange with a drifted loopback port must "
                      f"succeed, got {resp.status_code} {resp.response}")
            pair = resp.response
            assert_eq(pair.get("token_type"), "Bearer",
                      f"token_type must be Bearer, got {pair.get('token_type')!r}")
            claims = JWToken().decode(pair.get("access_token"), validate=False)
            assert_eq(claims.get("token_type"), "mcp",
                      f"the issued access token must be token_type=mcp, "
                      f"got {claims.get('token_type')!r}")
            assert_eq(claims.get("aud"), RESOURCE,
                      f"the audience must be the canonical resource, "
                      f"got {claims.get('aud')!r}")
            assert_eq(claims.get("uid"), user.pk,
                      "the token must name the approving user")

            # --- the issued token opens no other door -----------------------
            resp = opts.client.get(
                "/api/account/user/me",
                headers={"Authorization": f"Bearer {pair.get('access_token')}"})
            assert_eq(resp.status_code, 401,
                      f"a wire-minted mcp token must not authenticate at "
                      f"/api/account/user/me, got {resp.status_code}")

            # --- replaying the code kills the family ------------------------
            grant = OAuthGrant.objects.filter(user=user).order_by("-created").first()
            assert_true(grant is not None, "the exchange must have created a grant")
            resp = opts.client.post(f"{ROOT}/token", data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "client_id": client_id,
                "redirect_uri": REDIRECT,
            })
            assert_eq(resp.status_code, 400,
                      f"a replayed code must be refused, got {resp.status_code}")
            assert_eq(resp.response.get("error"), "invalid_grant",
                      f"a replayed code must answer invalid_grant, "
                      f"got {resp.response.get('error')!r}")
            assert_true(not OAuthGrant.objects.get(pk=grant.pk).is_active,
                        "a code replay must revoke the grant that code produced")

            # --- refresh rotation, grace and replay -------------------------
            client = OAuthClient.objects.get(client_id=client_id)
            fresh = tokens.create_grant(user, client, ["mcp"], RESOURCE, 1700000000)
            first = tokens.issue_tokens(fresh)

            def _refresh(raw):
                return opts.client.post(f"{ROOT}/token", data={
                    "grant_type": "refresh_token",
                    "refresh_token": raw,
                    "client_id": client_id,
                })

            resp = _refresh(first["refresh_token"])
            assert_eq(resp.status_code, 200,
                      f"a live refresh token must rotate, got {resp.status_code} "
                      f"{resp.response}")
            second = resp.response.get("refresh_token")
            assert_true(second != first["refresh_token"],
                        "every refresh must hand back a new refresh token")

            resp = _refresh(first["refresh_token"])
            assert_eq(resp.status_code, 200,
                      "a retry inside the grace window must be forgiven, "
                      f"got {resp.status_code} {resp.response}")
            third = resp.response.get("refresh_token")
            assert_true(bool(third) and third not in (first["refresh_token"], second),
                        "the grace path must mint a genuinely new pair")
            assert_true(OAuthGrant.objects.get(pk=fresh.pk).is_active,
                        "a grace retry must not revoke the grant — it is "
                        "indistinguishable from a lost response")

            # The grace hit ORPHANED `second`. That token, not the one that was
            # retried, is the tripwire: whoever still holds it is either the
            # client whose response was lost or whoever stole the first token,
            # and after the window either way it means the pair was used twice.
            stored = OAuthGrant.objects.get(pk=fresh.pk)
            import datetime
            OAuthGrant.objects.filter(pk=fresh.pk).update(
                last_refreshed=stored.last_refreshed - datetime.timedelta(hours=1))
            resp = _refresh(second)
            assert_eq(resp.status_code, 400,
                      f"the orphaned successor must be refused outside the "
                      f"window, got {resp.status_code}")
            assert_eq(resp.response.get("error"), "invalid_grant",
                      "a refresh replay must answer invalid_grant")
            assert_true(not OAuthGrant.objects.get(pk=fresh.pk).is_active,
                        "a refresh replay must revoke the grant family, so a "
                        "stolen refresh token surfaces instead of quietly working")

            # --- revocation --------------------------------------------------
            revocable = tokens.create_grant(user, client, ["mcp"], RESOURCE, 1700000000)
            live = tokens.issue_tokens(revocable)
            resp = opts.client.post(f"{ROOT}/revoke", data={
                "token": live["refresh_token"], "client_id": client_id})
            assert_eq(resp.status_code, 200,
                      f"revocation must always answer 200, got {resp.status_code}")
            assert_true(not OAuthGrant.objects.get(pk=revocable.pk).is_active,
                        "revoking a refresh token must kill its grant")
            resp = _refresh(live["refresh_token"])
            assert_eq(resp.status_code, 400,
                      "a revoked refresh token must stop working immediately")

            # --- the RFC 9728 challenge at a live resource door --------------
            challenged = tokens.create_grant(user, client, ["mcp"], RESOURCE, 1700000000)
            expired = tokens.mint_access_token(challenged, ttl=-5)
            resp = opts.client.post(
                MCP_PATH, {},
                headers={"Authorization": f"Bearer {expired}"})
            assert_eq(resp.status_code, 401,
                      f"an expired mcp token must be refused at its resource, "
                      f"got {resp.status_code}")
            headers = _headers(opts)
            assert_eq(
                headers.get("www-authenticate"),
                'Bearer error="invalid_token", '
                f'resource_metadata="{BASE}/.well-known/oauth-protected-resource'
                f'{MCP_PATH}"',
                f"a refusal at a live resource door must tell the client where "
                f"to authenticate, got {headers.get('www-authenticate')!r}")
            assert_true(
                "WWW-Authenticate" in headers.get("access-control-expose-headers", ""),
                f"the challenge must be readable by a browser client, "
                f"expose-headers is {headers.get('access-control-expose-headers')!r}")

            opts.wire_client_id = client_id
            opts.wire_refresh = tokens.issue_tokens(
                tokens.create_grant(user, client, ["mcp"], RESOURCE, 1700000000)
            )["refresh_token"]
            opts.wire_expired_token = expired
    finally:
        _clear_shadowing_rows()


@th.django_unit_test("switching the resource off makes the whole server dormant")
def test_disabled_resource(opts):
    _clear_shadowing_rows()
    try:
        with th.server_settings(BASE_URL=BASE, ASSISTANT_MCP_ENABLED=False):
            for path, why in ((AS_METADATA, "authorization-server metadata"),
                              (PRM, "protected-resource metadata")):
                resp = opts.client.get(path)
                assert_eq(resp.status_code, 404,
                          f"{why} must 404 while no resource is enabled, "
                          f"got {resp.status_code}")

            resp = opts.client.get(f"{ROOT}/authorize", params={
                "client_id": opts.wire_client_id, "redirect_uri": REDIRECT,
                "response_type": "code"}, allow_redirects=False)
            assert_eq(resp.status_code, 404,
                      f"the consent page must be unavailable, got {resp.status_code}")

            resp = _register(opts)
            assert_eq(resp.status_code, 404,
                      f"registration must be closed while the feature is off — an "
                      f"open endpoint would only collect scanner rows, "
                      f"got {resp.status_code}")

            # 404, not 400: with the only registered resource switched off the
            # whole authorization server is unconfigured, and every endpoint
            # answers alike. The per-resource `invalid_grant` refusal (server
            # ready, THIS resource off) is asserted in
            # tests/test_oauth_server/4_test_tokens.py.
            resp = opts.client.post(f"{ROOT}/token", data={
                "grant_type": "refresh_token",
                "refresh_token": opts.wire_refresh,
                "client_id": opts.wire_client_id})
            assert_eq(resp.status_code, 404,
                      f"issuance must stop while the server is unconfigured, "
                      f"got {resp.status_code}")

            resp = opts.client.post(
                MCP_PATH, {},
                headers={"Authorization": f"Bearer {opts.wire_expired_token}"})
            assert_eq(resp.status_code, 401,
                      f"a bad token at a switched-off path must still be refused, "
                      f"got {resp.status_code}")
            assert_true(_headers(opts).get("www-authenticate") is None,
                        "a switched-off resource is not a live door and must not "
                        "advertise itself")
    finally:
        _clear_shadowing_rows()

    # Re-enabled: the grant was dormant, never revoked, so it works again.
    _clear_shadowing_rows()
    try:
        with th.server_settings(BASE_URL=BASE, ASSISTANT_MCP_ENABLED=True):
            resp = opts.client.post(f"{ROOT}/token", data={
                "grant_type": "refresh_token",
                "refresh_token": opts.wire_refresh,
                "client_id": opts.wire_client_id})
            assert_eq(resp.status_code, 200,
                      f"a dormant grant must refresh again once the resource is "
                      f"re-enabled, got {resp.status_code} {resp.response}")
    finally:
        _clear_shadowing_rows()


@th.django_unit_test("the api scope, end to end: consent, a root-bound token, and its limits")
def test_api_scope_wire(opts):
    from mojo.decorators.limits import clear_rate_limits
    from mojo.apps.account.models import OAuthClient, OAuthGrant, User
    from mojo.apps.account.utils.jwtoken import JWToken
    from mojo.apps.account.services.oauth_server import tokens

    _clear_shadowing_rows()
    for bucket in RATE_BUCKETS:
        clear_rate_limits(ip="127.0.0.1", key=bucket)
    clear_rate_limits(ip="127.0.0.1", key="login")
    user = User.objects.get(username=TEST_USER)
    try:
        with th.server_settings(BASE_URL=BASE, ASSISTANT_MCP_ENABLED=True):
            # --- discovery advertises the second resource ------------------
            resp = opts.client.get(AS_METADATA)
            assert_eq(resp.status_code, 200,
                      f"the AS metadata must be served, got {resp.status_code}")
            scopes = resp.response.get("scopes_supported")
            assert_eq(scopes, ["mcp", "api"],
                      f"the server must advertise both scopes once the API root "
                      f"is registered, got {scopes!r}")

            resp = opts.client.get(API_PRM)
            assert_eq(resp.status_code, 200,
                      f"the API root must publish protected-resource metadata, "
                      f"got {resp.status_code}")
            assert_eq(resp.response.get("resource"), API_ROOT_RESOURCE,
                      f"the root PRM must name the canonical root URL, "
                      f"got {resp.response.get('resource')!r}")
            assert_eq(resp.response.get("scopes_supported"), ["mcp", "api"],
                      f"the root must publish both scopes, "
                      f"got {resp.response.get('scopes_supported')!r}")

            # --- consent names full API access in plain words ---------------
            verifier = _verifier()
            params = {
                "client_id": opts.wire_client_id,
                "redirect_uri": REDIRECT,
                "response_type": "code",
                "state": "api-state",
                "code_challenge": _challenge(verifier),
                "code_challenge_method": "S256",
                "resource": API_ROOT_RESOURCE,
                "scope": "mcp api",
            }
            resp = opts.client.get(f"{ROOT}/authorize", params=params,
                                   allow_redirects=False)
            assert_eq(resp.status_code, 200,
                      f"an `mcp api` request at the root must render the consent "
                      f"page, got {resp.status_code}")
            html = resp.text
            assert_true("Full API access as" in html,
                        "the page must state that this grants full API access")
            assert_true("approval step does not apply" in html,
                        "the page must warn that the Assistant's approval step "
                        "does not cover direct API calls")
            assert_true("the same permissions as your account" in html,
                        "the tool-door sentence must still be shown when `mcp` "
                        "is granted too")
            assert_true(f"Access to: {API_ROOT_RESOURCE}" in html,
                        "the page must name the API root as the resource")

            # --- approve and exchange ---------------------------------------
            assert_true(opts.client.login(TEST_USER, TEST_PWORD),
                        "the wire user must be able to sign in")
            resp = opts.client.post(f"{ROOT}/approve", params)
            assert_eq(resp.status_code, 200,
                      f"an interactive session must be able to approve full API "
                      f"access, got {resp.status_code} {resp.response}")
            code = parse_qs(
                urlsplit(resp.response.data.redirect_url).query)["code"][0]
            opts.client.logout()

            resp = opts.client.post(f"{ROOT}/token", data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "client_id": opts.wire_client_id,
                "redirect_uri": REDIRECT,
                "resource": API_ROOT_RESOURCE,
            })
            assert_eq(resp.status_code, 200,
                      f"the exchange must succeed, got {resp.status_code} "
                      f"{resp.response}")
            assert_eq(resp.response.get("scope"), "mcp api",
                      f"the token response must echo both scopes, "
                      f"got {resp.response.get('scope')!r}")
            access = resp.response.get("access_token")
            claims = JWToken().decode(access, validate=False)
            assert_eq(claims.get("aud"), API_ROOT_RESOURCE,
                      f"the audience must be the API root, "
                      f"got {claims.get('aud')!r}")

            # --- it IS the person's session, everywhere beneath the root -----
            resp = opts.client.get("/api/account/user/me",
                                   headers={"Authorization": f"Bearer {access}"})
            assert_eq(resp.status_code, 200,
                      f"an api token must authenticate at an ordinary REST "
                      f"endpoint, got {resp.status_code} {resp.response}")
            assert_eq(resp.response.data.id, user.pk,
                      f"the api token must act as the approving user, "
                      f"got {resp.response.data!r}")

            user.add_permission("assistant")
            try:
                resp = opts.client.post(
                    MCP_PATH, {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                    headers={"Authorization": f"Bearer {access}"})
                assert_eq(resp.status_code, 200,
                          f"an `mcp api` grant must still open the tool door "
                          f"that sits beneath its root, got {resp.status_code} "
                          f"{resp.body}")

                # api WITHOUT mcp is full REST reach and no tools.
                client = OAuthClient.objects.get(client_id=opts.wire_client_id)
                api_only = tokens.create_grant(
                    user, client, ["api"], API_ROOT_RESOURCE, 1700000000)
                api_only_token = tokens.mint_access_token(api_only)
                resp = opts.client.post(
                    MCP_PATH, {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                    headers={"Authorization": f"Bearer {api_only_token}"})
                assert_eq(resp.status_code, 403,
                          f"an api-only grant must be refused at the tool door, "
                          f"got {resp.status_code} {resp.body}")
                challenge = _headers(opts).get("www-authenticate", "")
                assert_true('error="insufficient_scope"' in challenge,
                            f"the door's refusal must name insufficient_scope, "
                            f"got {challenge!r}")
                resp = opts.client.get(
                    "/api/account/user/me",
                    headers={"Authorization": f"Bearer {api_only_token}"})
                assert_eq(resp.status_code, 200,
                          f"an api-only grant must still be the person's session "
                          f"on the REST API, got {resp.status_code}")
                tokens.revoke_grant(api_only, reason="test")
            finally:
                user.remove_all_permissions()

            # --- what it may never do ---------------------------------------
            headers = {"Authorization": f"Bearer {access}"}
            resp = opts.client.post(f"{ROOT}/approve", params, headers=headers)
            assert_true(resp.status_code in (401, 403),
                        f"an api token must never approve a NEW grant — one "
                        f"credential must not mint another without a person, "
                        f"got {resp.status_code}")

            resp = opts.client.post("/api/account/jwt/refresh",
                                    {"refresh_token": access})
            assert_eq(resp.status_code, 401,
                      f"an api token must never be exchanged for a session pair, "
                      f"got {resp.status_code}")

            resp = opts.client.get(AS_METADATA, headers=headers)
            assert_eq(resp.status_code, 401,
                      f"an api token is worth nothing OUTSIDE the root, "
                      f"got {resp.status_code}")
            assert_true(_headers(opts).get("www-authenticate") is None,
                        "a path outside every live resource must not advertise "
                        "one")

            # --- revocation is immediate, and the 401 says where to go -------
            grant = OAuthGrant.objects.filter(
                user=user, resource=API_ROOT_RESOURCE,
                is_active=True).order_by("-created").first()
            assert_true(grant is not None, "the exchange must have created a grant")
            tokens.revoke_grant(grant, reason="admin")
            resp = opts.client.get("/api/account/user/me", headers=headers)
            assert_eq(resp.status_code, 401,
                      f"a revoked api grant must stop working immediately, "
                      f"got {resp.status_code}")
            assert_eq(
                _headers(opts).get("www-authenticate"),
                'Bearer error="invalid_token", '
                f'resource_metadata="{BASE}{API_PRM}"',
                f"a refusal beneath a live root must point the client at the "
                f"ROOT's metadata, got {_headers(opts).get('www-authenticate')!r}")
    finally:
        _clear_shadowing_rows()
