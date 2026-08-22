"""
Grants, access-token claims, refresh rotation, and revocation.

Rotation is the interesting part. One conditional UPDATE moves the refresh
hash, so the loser of a concurrent refresh cannot end up holding a dead pair:
it lands in the grace window and is handed a working one. Outside that window a
reused refresh token is replay, and the whole family dies.

Every `list_grants` / `revoke_all_grants` call here is scoped to this module's
own user — the database is long-lived and shared with the other modules in the
run.
"""
import datetime

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

TEST_USER = "oauth_tokens_user"
SPARE_USER = "oauth_tokens_spare"
CLIENT_ID = "testit-oauth-tokens-client"
OTHER_CLIENT_ID = "testit-oauth-tokens-other"
RESOURCE_PATH = "/api/testit/oauth-tokens-probe"
ORIGIN = "https://oauth.testit.example"
RESOURCE = ORIGIN + RESOURCE_PATH
# The second, PREFIX resource: the API-root shape an `api` grant is bound to.
ROOT_PATH = "/api/testit/oauth-tokens-root"
ROOT_RESOURCE = ORIGIN + ROOT_PATH
# A third path this module must never sweep, to prove the scoping is a
# whitelist rather than "everything the installation protects".
UNRELATED_PATH = "/api/testit/oauth-tokens-elsewhere"
UNRELATED_RESOURCE = ORIGIN + UNRELATED_PATH


def _registry(enabled=True):
    from mojo.apps.account.services.oauth_server import resources

    registry = resources.ResourceRegistry()
    registry.register(RESOURCE_PATH, ["mcp"], lambda: enabled)
    registry.register(ROOT_PATH, ["mcp", "api"], lambda: enabled, prefix=True)
    return registry


@th.django_unit_setup()
def setup_tokens(opts):
    from mojo.apps.account.models import OAuthClient, User

    User.objects.filter(username__in=[TEST_USER, SPARE_USER]).delete()
    OAuthClient.objects.filter(
        client_id__in=[CLIENT_ID, OTHER_CLIENT_ID]).delete()

    for username in (TEST_USER, SPARE_USER):
        user = User(username=username, display_name=username,
                    email=f"{username}@example.com")
        user.save()
    OAuthClient(client_id=CLIENT_ID, kind="dcr", client_name="Tokens Client",
                redirect_uris=["http://127.0.0.1:8200/cb"]).save()
    OAuthClient(client_id=OTHER_CLIENT_ID, kind="dcr", client_name="Other",
                redirect_uris=["http://127.0.0.1:8200/cb"]).save()


def _fixtures(username=TEST_USER):
    from mojo.apps.account.models import OAuthClient, User

    return (User.objects.get(username=username),
            OAuthClient.objects.get(client_id=CLIENT_ID),
            OAuthClient.objects.get(client_id=OTHER_CLIENT_ID))


def _grant(username=TEST_USER, scopes=None, resource=RESOURCE):
    from mojo.apps.account.services.oauth_server import tokens

    user, client, _other = _fixtures(username)
    return tokens.create_grant(
        user, client, scopes or ["mcp"], resource, 1700000000)


@th.django_unit_test("an access token carries exactly the claims the resource server reads")
def test_access_token_claims(opts):
    from mojo.apps.account.models import OAuthGrant
    from mojo.apps.account.utils.jwtoken import JWToken
    from mojo.apps.account.services.oauth_server import tokens

    grant = _grant()
    user, client, _other = _fixtures()
    token = tokens.mint_access_token(grant)
    payload = JWToken().decode(token, validate=False)

    assert_eq(payload.get("token_type"), "mcp",
              f"an OAuth access token must be token_type=mcp, "
              f"got {payload.get('token_type')!r}")
    assert_eq(payload.get("uid"), user.pk,
              f"the token must name the approving user, got {payload.get('uid')!r}")
    assert_eq(payload.get("aud"), RESOURCE,
              f"the audience must be the canonical resource URL, "
              f"got {payload.get('aud')!r}")
    assert_eq(payload.get("scope"), "mcp",
              f"the granted scopes must ride as a space-joined string, "
              f"got {payload.get('scope')!r}")
    assert_eq(payload.get("auth_time"), 1700000000,
              f"the approving session's auth_time must be copied in, "
              f"got {payload.get('auth_time')!r}")
    assert_eq(payload.get("exp") - payload.get("iat"), 3600,
              f"the default access-token lifetime must be one hour, got "
              f"{payload.get('exp') - payload.get('iat')} seconds")

    stored = OAuthGrant.objects.get(pk=grant.pk)
    assert_eq(payload.get("jti"), stored.access_jti,
              "the token's jti must be the grant's current access_jti — that is "
              "how a presented token resolves back to its grant")

    # Minting again replaces the link, so the previous token stops resolving.
    tokens.mint_access_token(grant)
    reread = OAuthGrant.objects.get(pk=grant.pk)
    assert_true(reread.access_jti != payload.get("jti"),
                "minting a new access token must retire the previous jti")


@th.django_unit_test("an api grant mints a root-audience token carrying both scopes")
def test_api_grant_claims_and_refresh(opts):
    from mojo.apps.account.utils.jwtoken import JWToken
    from mojo.apps.account.services.oauth_server import tokens

    grant = _grant(scopes=["mcp", "api"], resource=ROOT_RESOURCE)
    _user, client, _other = _fixtures()
    payload = JWToken().decode(
        tokens.mint_access_token(grant), validate=False)

    assert_eq(payload.get("aud"), ROOT_RESOURCE,
              f"an api grant's audience must be the API root, not the door it "
              f"happens to sit above, got {payload.get('aud')!r}")
    assert_eq(payload.get("scope"), "mcp api",
              f"both granted scopes must ride space-joined, in the order they "
              f"were requested, got {payload.get('scope')!r}")
    assert_eq(payload.get("token_type"), "mcp",
              f"the api scope introduces no new token type — the chokepoint is "
              f"the same branch, got {payload.get('token_type')!r}")

    pair = tokens.issue_tokens(grant)
    assert_eq(pair["scope"], "mcp api",
              f"the token response must echo the granted scope string, "
              f"got {pair['scope']!r}")

    # The refresh endpoint compares the client's echoed `scope` to the stored
    # string, so a client cannot quietly widen or narrow what it holds.
    error = None
    try:
        tokens.refresh_grant(pair["refresh_token"], client, scope="api mcp",
                             registry=_registry())
    except tokens.TokenError as err:
        error = err.code
    assert_eq(error, "invalid_grant",
              f"a refresh naming a DIFFERENT scope string must be refused, "
              f"got {error!r}")


@th.django_unit_test("the Admin grant API can span both resources in one query")
def test_admin_api_spans_resources(opts):
    from mojo.apps.account.models import OAuthGrant, User
    from mojo.apps.account.services.oauth_server import tokens

    user = User.objects.get(username=TEST_USER)
    OAuthGrant.objects.filter(user=user).delete()

    door = _grant()
    root = _grant(scopes=["mcp", "api"], resource=ROOT_RESOURCE)
    _grant(resource=UNRELATED_RESOURCE)
    both = [RESOURCE_PATH, ROOT_PATH]

    rows = tokens.list_grants(user=user, resource_path=both)
    assert_eq(sorted(row["id"] for row in rows), sorted([door.pk, root.pk]),
              f"a two-path scope must list both resources' grants and nothing "
              f"else, got {[row['id'] for row in rows]}")
    assert_eq(tokens.count_grants(user=user, resource_path=both), 2,
              "the count must be taken on the same two-path predicate")
    assert_eq(tokens.count_grants(user=user, resource_path=[]), 0,
              "asking for no resources at all must select nothing, not "
              "everything")
    assert_eq(tokens.count_grants(user=user, resource_path=RESOURCE_PATH), 1,
              "a bare string must still mean exactly that one path")

    # A MOJO_APPEND_SLASH deployment registers and stores the SLASHED path, so
    # the Admin's scoping has to be given that exact string. This is the shape
    # that silently listed nothing when the path was re-derived unslashed.
    slashed_path = RESOURCE_PATH + "/"
    slashed = _grant(resource=ORIGIN + slashed_path)
    assert_eq([row["id"] for row in
               tokens.list_grants(user=user, resource_path=[slashed_path])],
              [slashed.pk],
              "a slashed resource path must list its own grant and no other")
    assert_eq(tokens.count_grants(user=user, resource_path=[slashed_path]), 1,
              "the count must agree with the listing for a slashed path")
    assert_eq(len(tokens.list_grants(user=user, resource_path=[RESOURCE_PATH])), 1,
              "the unslashed path must NOT collect the slashed resource's grant "
              "— they are different resources")
    assert_eq(tokens.revoke_all_grants(user=user, resource_path=[slashed_path]), 1,
              "the sweep must reach a grant stored at a slashed resource path")

    # The SQL suffix filter is a superset, so the count has to re-check the
    # parsed path in Python or it reports rows the listing drops.
    nested = _grant(resource=f"{ORIGIN}/nested{RESOURCE_PATH}")
    assert_eq(tokens.count_grants(user=user, resource_path=[RESOURCE_PATH]),
              len(tokens.list_grants(user=user, resource_path=[RESOURCE_PATH])),
              "count_grants must agree with list_grants: the SQL endswith match "
              "also catches /nested/api/… and only the Python re-check drops it")
    assert_eq(tokens.count_grants(user=user, resource_path=[RESOURCE_PATH]), 1,
              "a grant at a DIFFERENT resource whose URL merely ends with this "
              "path must not be counted")
    OAuthGrant.objects.filter(pk=nested.pk).delete()

    assert_eq(tokens.revoke_all_grants(user=user, resource_path=both), 2,
              "the sweep must move both resources' rows in one UPDATE")
    survivors = tokens.list_grants(user=user)
    assert_eq([row["resource"] for row in survivors], [UNRELATED_RESOURCE],
              f"the sweep must leave a grant at an unlisted resource alone, "
              f"got {[row['resource'] for row in survivors]}")
    OAuthGrant.objects.filter(user=user).delete()


@th.django_unit_test("issue_tokens returns the RFC 6749 shape")
def test_issue_tokens_shape(opts):
    from mojo.apps.account.services.oauth_server import tokens

    grant = _grant()
    pair = tokens.issue_tokens(grant)
    assert_eq(sorted(pair.keys()),
              ["access_token", "expires_in", "refresh_token", "scope", "token_type"],
              f"the token response must carry exactly the RFC 6749 fields, "
              f"got {sorted(pair.keys())}")
    assert_eq(pair["token_type"], "Bearer",
              f"token_type must be Bearer, got {pair['token_type']!r}")
    assert_eq(pair["expires_in"], 3600,
              f"expires_in must match the access TTL, got {pair['expires_in']}")
    assert_eq(pair["scope"], "mcp", f"scope must be echoed, got {pair['scope']!r}")
    assert_true(len(pair["refresh_token"]) >= 32,
                "the refresh token must be a high-entropy opaque secret")


@th.django_unit_test("refresh rotates the pair and retires the previous access token")
def test_refresh_rotation(opts):
    from mojo.apps.account.models import OAuthGrant
    from mojo.apps.account.utils.jwtoken import JWToken
    from mojo.apps.account.services.oauth_server import tokens

    grant = _grant()
    _user, client, _other = _fixtures()
    first = tokens.issue_tokens(grant)
    first_jti = JWToken().decode(first["access_token"], validate=False).get("jti")

    second = tokens.refresh_grant(first["refresh_token"], client,
                                  registry=_registry())
    assert_true(second["refresh_token"] != first["refresh_token"],
                "every refresh must hand back a NEW refresh token")
    assert_true(second["access_token"] != first["access_token"],
                "every refresh must hand back a new access token")
    assert_true(
        not OAuthGrant.objects.filter(access_jti=first_jti, is_active=True).exists(),
        "the previous access token's jti must stop resolving after a refresh")


@th.django_unit_test("a rotated refresh token is forgiven inside the grace window")
def test_refresh_grace(opts):
    from mojo.apps.account.models import OAuthGrant
    from mojo.apps.account.services.oauth_server import tokens

    grant = _grant()
    _user, client, _other = _fixtures()
    first = tokens.issue_tokens(grant)
    second = tokens.refresh_grant(first["refresh_token"], client, registry=_registry())
    before = OAuthGrant.objects.get(pk=grant.pk)

    # The client never received `second` — it retries with the old token.
    third = tokens.refresh_grant(first["refresh_token"], client, registry=_registry())
    assert_true(third["refresh_token"] not in
                (first["refresh_token"], second["refresh_token"]),
                "the grace path must mint a genuinely new pair")
    assert_true(OAuthGrant.objects.get(pk=grant.pk).is_active,
                "a retry inside the grace window must NOT revoke the grant")

    stored = OAuthGrant.objects.get(pk=grant.pk)
    assert_eq(stored.prev_refresh_hash, tokens._sha256_hex(second["refresh_token"]),
              "the grace path must park the ORPHANED successor in "
              "prev_refresh_hash, so whoever holds it trips replay later "
              "instead of getting a silent invalid_grant")
    assert_eq(stored.last_refreshed, before.last_refreshed,
              "the grace path must not move last_refreshed — the window keeps "
              "ticking from the original rotation, so retrying cannot walk it "
              "forward forever")

    # The new pair works and the grace-issued one is the live one.
    fourth = tokens.refresh_grant(third["refresh_token"], client, registry=_registry())
    assert_true(fourth["access_token"], "the grace-issued pair must be usable")


@th.django_unit_test("a grace-window takeover is detected, not silently allowed")
def test_grace_takeover_is_detected(opts):
    from mojo.apps.account.models import OAuthGrant
    from mojo.apps.account.services.oauth_server import tokens

    grant = _grant()
    _user, client, _other = _fixtures()
    first = tokens.issue_tokens(grant)                      # the client holds R1
    second = tokens.refresh_grant(first["refresh_token"], client,
                                  registry=_registry())     # legit rotation -> R2

    # A thief who captured R1 refreshes inside the window and is served, exactly
    # as a client whose response was lost would be. Nothing here can tell them
    # apart — which is why the detection has to land on the OTHER party.
    third = tokens.refresh_grant(first["refresh_token"], client, registry=_registry())
    assert_true(third["refresh_token"] not in
                (first["refresh_token"], second["refresh_token"]),
                "the grace path must mint a genuinely new pair")
    assert_true(OAuthGrant.objects.get(pk=grant.pk).is_active,
                "a grace hit alone must not revoke — it is indistinguishable "
                "from a lost response")

    # The legitimate holder of R2 comes back after the window. R2 is the pair
    # the thief orphaned, so this is where the theft surfaces.
    stored = OAuthGrant.objects.get(pk=grant.pk)
    OAuthGrant.objects.filter(pk=grant.pk).update(
        last_refreshed=stored.last_refreshed - datetime.timedelta(hours=1))
    error = None
    try:
        tokens.refresh_grant(second["refresh_token"], client, registry=_registry())
    except tokens.TokenError as err:
        error = err.code
    assert_eq(error, "invalid_grant",
              f"the orphaned successor must be refused, got {error!r}")
    dead = OAuthGrant.objects.get(pk=grant.pk)
    assert_true(not dead.is_active,
                "the orphaned successor must trip REPLAY, not a generic refusal "
                "— that is what makes a stolen refresh token visible")
    assert_eq(dead.revoked_reason, "refresh_replay",
              f"the takeover must be recorded as a replay, got "
              f"{dead.revoked_reason!r}")


@th.django_unit_test("the orphaned successor is forgiven inside the window")
def test_grace_orphan_inside_the_window(opts):
    from mojo.apps.account.models import OAuthGrant
    from mojo.apps.account.services.oauth_server import tokens

    grant = _grant()
    _user, client, _other = _fixtures()
    first = tokens.issue_tokens(grant)
    second = tokens.refresh_grant(first["refresh_token"], client, registry=_registry())
    third = tokens.refresh_grant(first["refresh_token"], client, registry=_registry())

    # R2 was orphaned by the grace hit, but the window has not passed: this is
    # still the lost-response shape, so it must be served, not revoked.
    fourth = tokens.refresh_grant(second["refresh_token"], client, registry=_registry())
    assert_true(fourth["refresh_token"] not in
                (first["refresh_token"], second["refresh_token"],
                 third["refresh_token"]),
                "an orphan presented inside the window must get a fresh pair")
    assert_true(OAuthGrant.objects.get(pk=grant.pk).is_active,
                "an orphan inside the window must not revoke the family")


@th.django_unit_test("a concurrent rotation loser is handed a working pair, not a dead one")
def test_concurrent_rotation_loser(opts):
    from mojo.apps.account.services.oauth_server import tokens

    grant = _grant()
    _user, client, _other = _fixtures()
    first = tokens.issue_tokens(grant)
    stale_hash = tokens._sha256_hex(first["refresh_token"])
    tokens.refresh_grant(first["refresh_token"], client, registry=_registry())

    # This is what the losing process's UPDATE does: its WHERE clause no longer
    # matches, so it must not silently succeed and strand a dead pair.
    lost = False
    try:
        tokens.issue_tokens(grant, expected_refresh_hash=stale_hash)
    except tokens.RotationLost:
        lost = True
    assert_true(lost,
                "rotating on an already-rotated hash must raise RotationLost "
                "rather than issue a pair the row does not know about")


@th.django_unit_test("a refresh token reused outside the window is replay")
def test_refresh_replay_revokes_the_family(opts):
    from mojo.apps.account.models import OAuthGrant
    from mojo.apps.account.services.oauth_server import tokens

    grant = _grant()
    _user, client, _other = _fixtures()
    first = tokens.issue_tokens(grant)
    tokens.refresh_grant(first["refresh_token"], client, registry=_registry())

    # Age the rotation out of the grace window.
    stored = OAuthGrant.objects.get(pk=grant.pk)
    OAuthGrant.objects.filter(pk=grant.pk).update(
        last_refreshed=stored.last_refreshed - datetime.timedelta(hours=1))

    error = None
    try:
        tokens.refresh_grant(first["refresh_token"], client, registry=_registry())
    except tokens.TokenError as err:
        error = err.code
    assert_eq(error, "invalid_grant",
              f"a replayed refresh token must answer invalid_grant, got {error!r}")

    dead = OAuthGrant.objects.get(pk=grant.pk)
    assert_true(not dead.is_active, "a refresh replay must revoke the grant family")
    assert_eq(dead.revoked_reason, "refresh_replay",
              f"the revocation must be attributed to the replay, "
              f"got {dead.revoked_reason!r}")


@th.django_unit_test("refresh is refused for every state that should stop issuance")
def test_refresh_refusals(opts):
    from mojo.apps.account.models import OAuthClient, OAuthGrant, User
    from mojo.apps.account.services.oauth_server import tokens
    from mojo.helpers import dates

    _user, client, other = _fixtures()

    def _refuse(setup, presented_client=None, registry=None, **kwargs):
        grant = _grant()
        pair = tokens.issue_tokens(grant)
        setup(grant)
        error = None
        try:
            tokens.refresh_grant(
                pair["refresh_token"], presented_client or client,
                registry=registry or _registry(), **kwargs)
        except tokens.TokenError as err:
            error = err.code
        return error

    assert_eq(_refuse(lambda g: tokens.revoke_grant(g, reason="admin")),
              "invalid_grant", "a revoked grant must never refresh")
    assert_eq(_refuse(lambda g: None, presented_client=other),
              "invalid_grant", "another client must never refresh this grant")
    assert_eq(_refuse(lambda g: OAuthGrant.objects.filter(pk=g.pk).update(
                  refresh_expires=dates.utcnow() - datetime.timedelta(seconds=1))),
              "invalid_grant", "an expired refresh lifetime must never refresh")
    assert_eq(_refuse(lambda g: None, registry=_registry(enabled=False)),
              "invalid_grant", "a disabled resource must refuse refresh (dormant)")
    assert_eq(_refuse(lambda g: None, resource="https://elsewhere.example/x"),
              "invalid_grant", "a mismatched resource must refuse refresh")
    assert_eq(_refuse(lambda g: None, scope="admin"),
              "invalid_grant", "a mismatched scope must refuse refresh")

    # An inactive client and an inactive user each stop issuance.
    OAuthClient.objects.filter(pk=client.pk).update(is_active=False)
    try:
        assert_eq(_refuse(lambda g: None), "invalid_grant",
                  "a deactivated client must never refresh")
    finally:
        OAuthClient.objects.filter(pk=client.pk).update(is_active=True)

    spare_grant = _grant(username=SPARE_USER)
    spare_pair = tokens.issue_tokens(spare_grant)
    User.objects.filter(username=SPARE_USER).update(is_active=False)
    try:
        error = None
        try:
            tokens.refresh_grant(spare_pair["refresh_token"], client,
                                 registry=_registry())
        except tokens.TokenError as err:
            error = err.code
        assert_eq(error, "invalid_grant",
                  "a deactivated user's grant must never refresh")
    finally:
        User.objects.filter(username=SPARE_USER).update(is_active=True)


@th.django_unit_test("revocation works by refresh token and by access token")
def test_revoke_token(opts):
    from mojo.apps.account.models import OAuthGrant
    from mojo.apps.account.services.oauth_server import tokens

    _user, client, other = _fixtures()

    grant = _grant()
    pair = tokens.issue_tokens(grant)
    tokens.revoke_token(pair["refresh_token"], other)
    assert_true(OAuthGrant.objects.get(pk=grant.pk).is_active,
                "a client must not be able to revoke another client's grant")
    tokens.revoke_token(pair["refresh_token"], client)
    assert_true(not OAuthGrant.objects.get(pk=grant.pk).is_active,
                "revoking by refresh token must kill the grant")

    grant2 = _grant()
    pair2 = tokens.issue_tokens(grant2)
    tokens.revoke_token(pair2["access_token"], client)
    assert_true(not OAuthGrant.objects.get(pk=grant2.pk).is_active,
                "revoking by access token must kill the grant")

    # Garbage is a no-op, never an exception.
    assert_true(tokens.revoke_token("nonsense", client) is None,
                "revoking an unknown token must be a silent no-op")


@th.django_unit_test("the Admin grant API lists, revokes and is idempotent")
def test_admin_grant_api(opts):
    from mojo.apps.account.models import OAuthGrant, User
    from mojo.apps.account.services.oauth_server import tokens

    user = User.objects.get(username=TEST_USER)
    OAuthGrant.objects.filter(user=user).delete()

    grant = _grant()
    tokens.issue_tokens(grant)
    rows = tokens.list_grants(user=user)
    assert_eq(len(rows), 1, f"the live grant must be listed, got {len(rows)} rows")
    row = rows[0]
    assert_eq(sorted(row.keys()),
              ["client", "created", "expires", "id", "is_active", "last_used",
               "resource", "revoked_reason", "scopes", "user"],
              f"the Admin row shape is a contract, got {sorted(row.keys())}")
    assert_eq(row["resource"], RESOURCE,
              f"the row must name the resource, got {row['resource']!r}")
    assert_true(row["is_active"], "a fresh grant must list as active")
    assert_eq(row["client"]["client_id"], CLIENT_ID,
              f"the row must name the client, got {row['client']['client_id']!r}")
    assert_eq(row["user"]["id"], user.pk, "the row must name the user")
    for field in ("created", "expires"):
        assert_true(isinstance(row[field], str),
                    f"{field} must be an ISO-8601 string for the Admin, "
                    f"got {type(row[field])}")

    assert_true(tokens.revoke_grant_by_id(grant.pk, actor=user),
                "revoking a live grant must report success")
    assert_true(not tokens.revoke_grant_by_id(grant.pk, actor=user),
                "revoking an already-dead grant must report False, not raise")
    assert_eq(tokens.list_grants(user=user), [],
              "a revoked grant must drop out of the default listing")
    inactive = tokens.list_grants(user=user, include_inactive=True)
    assert_eq(len(inactive), 1,
              "include_inactive must still show the revoked grant to the Admin")
    assert_true(not inactive[0]["is_active"],
                "the revoked grant must list as inactive")

    OAuthGrant.objects.filter(user=user).delete()
    _grant()
    _grant()
    assert_eq(tokens.revoke_all_grants(user=user), 2,
              "revoke_all_grants must report how many live grants it killed")
    assert_eq(tokens.revoke_all_grants(user=user), 0,
              "a second sweep must find nothing left to revoke")
    OAuthGrant.objects.filter(user=user).delete()
