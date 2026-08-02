"""Wire tests for the GeoIP federation credential path (maestro item 1194).

The `mojo` GeoIP provider makes exactly two authenticated calls to an upstream
hub, both with `Authorization: apikey <token>`:

    GET  /api/system/geoip/lookup?graph=federation   (mojo/helpers/geoip/mojo.py)
    POST /api/system/geoip/sync                      (mojo/apps/account/asyncjobs.py)

The sync half is covered by test_geoip_sync_endpoint.py. This module covers the
lookup half, which had NO wire coverage at all — the provider's own tests mock
`requests.get`, so nothing exercised a real ApiKey through the middleware.

That gap matters because `GeoLocatedIP` is a groupless (platform-global) model,
and model security denies ApiKey identities on groupless models by default
(`api_key.groupless_denied` in mojo/models/rest.py). The lookup endpoint escapes
that only because it is gated by `@md.requires_auth()` alone and serializes
directly, never reaching `rest_check_permission_or_raise`. These tests pin that
open on purpose — see test_lookup_accepts_group_apikey.

Also covers the APIKEY_PERMS_PROTECTION floor that makes `geoip_sync`
grantable only by a global admin.
"""
from datetime import timedelta

from testit import helpers as th

# Firewall / per-fleet enforcement fields. Kept in sync with
# mojo/helpers/geoip/mojo.py `_FIREWALL_FIELDS` — the client scrubs these
# defensively, but the `federation` graph must not emit them in the first place.
FIREWALL_FIELDS = (
    "is_blocked", "is_whitelisted",
    "blocked_at", "blocked_until", "blocked_reason", "block_count",
    "whitelisted_reason", "whitelisted_until",
)

# Maestro item 274's lesson, applied: 203.0.113.0/24 is fully contested —
# test_geofence/config_plane.py draws .10-.209 at random, test_global_perms/
# apikey_gate.py .20-.219, test_geofence/evidence_plane.py .210-.249, and
# test_global_perms/base_term_expansion.py claims .250-.254. Every generator
# DELETES the row it draws, so a fixture parked there gets destroyed mid-test
# at random (modules run in parallel threads — testit/runner.py).
#
# TEST-NET-1 (192.0.2.0/24, RFC 5737) has no generator at all and only four
# fixed addresses in the whole suite (.7, .10, .11, .99). TEST-NET-2 is NOT
# safe: test_realtime/connection_limits.py draws across its entire range.
FED_IP = "192.0.2.171"


@th.django_unit_setup()
def setup_federation_wire(opts):
    """A group, a PERMISSIONLESS ApiKey, and a pre-populated GeoLocatedIP row."""
    from mojo.apps.account.models import Group, ApiKey
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    from mojo.helpers import dates
    from mojo.decorators.limits import clear_rate_limits

    # Tests run against a long-lived DB — clear before creating.
    ApiKey.objects.filter(name__startswith="geoip_fed_test_").delete()
    Group.objects.filter(name="geoip_fed_test_group").delete()
    GeoLocatedIP.objects.filter(ip_address=FED_IP).delete()

    group = Group.objects.create(name="geoip_fed_test_group", kind="organization")

    # Deliberately NO permissions: the lookup endpoint requires only
    # authentication, and proving that is the point of this module.
    _key, token = ApiKey.create_for_group(
        group=group,
        name="geoip_fed_test_lookup",
        permissions={},
    )

    # `expires_at` MUST be set in the future, and it is the ONLY thing keeping
    # this fixture intact. GeoLocatedIP.is_expired returns True when expires_at
    # is null, and geolocate() then calls refresh() — which rewrites the row
    # from the provider chain. Note that `?auto_refresh=false` does NOT save
    # you: query params arrive as raw strings (RequestDataParser does no
    # coercion), and the string "false" is truthy, so the endpoint's
    # auto_refresh flag cannot be turned off from a GET at all. Filed as a
    # sub-item of maestro 1194.
    #
    # These are RFC 5737 documentation addresses, which Python's ipaddress
    # module reports as private — so a refresh would silently overwrite
    # country_code with None and provider with "internal" rather than making a
    # network call. test_lookup_accepts_group_apikey asserts on provider to
    # make that failure loud instead of confusing.
    GeoLocatedIP.objects.create(
        ip_address=FED_IP,
        provider="maxmind",
        expires_at=dates.utcnow() + timedelta(days=1),
        country_code="US",
        country_name="United States",
        city="San Francisco",
        asn_org="Example Networks",
        threat_level="high",
        is_known_attacker=True,
        # Per-fleet enforcement state — must never cross the federation boundary.
        is_blocked=True,
        blocked_reason="local fleet decision, not federated",
        block_count=3,
        is_whitelisted=False,
    )

    clear_rate_limits(ip="127.0.0.1", key="geoip_lookup")

    opts.fed_group_id = group.id
    opts.fed_token = token


def _use_apikey(opts, token):
    """Switch the test client to send `Authorization: apikey <token>`."""
    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = token
    opts.client.is_authenticated = True


@th.django_unit_test()
def test_lookup_accepts_group_apikey(opts):
    """A group-scoped ApiKey must be able to read the federation lookup.

    THIS IS A TRIPWIRE. GET /api/system/geoip/lookup is gated by
    @md.requires_auth() alone and responds through the serializer directly, so
    it never reaches model security's groupless-deny branch. Every downstream
    instance running GEOIP_PRIMARY_PROVIDER='mojo' depends on that.

    If this test starts failing with a 403, someone has "conformed" the endpoint
    by adding rest_check_permission_or_raise / uses_model_security to it. That
    change silently breaks GeoIP federation for every fleet. Do not fix this
    test by relaxing the assertion — either revert that gate or give the
    federation a deliberately designed replacement path.
    """
    _use_apikey(opts, opts.fed_token)
    resp = opts.client.get(
        "/api/system/geoip/lookup",
        params={"ip": FED_IP, "graph": "federation"},
    )
    assert resp.status_code == 200, (
        f"a group ApiKey must still reach the federation lookup — got "
        f"{resp.status_code}. If this is 403, the groupless-deny has been "
        f"applied to this endpoint and GeoIP federation is broken fleet-wide. "
        f"body={opts.client.last_response.body}"
    )
    data = resp.response.data
    assert data.provider == "maxmind", (
        f"fixture was clobbered by an auto-refresh (provider={data.provider!r}) "
        f"— expires_at must stay in the future; see setup_federation_wire"
    )
    assert data.country_code == "US", (
        f"federation payload must carry location data, got {data!r}"
    )
    assert data.is_known_attacker is True, (
        f"federation payload must carry abuse signals, got {data!r}"
    )
    opts.client.logout()


@th.django_unit_test()
def test_lookup_federation_graph_strips_firewall_state(opts):
    """The `federation` graph must never emit per-fleet enforcement state.

    The row created in setup is blocked locally. Blocking is a per-fleet
    decision; leaking it would let one fleet's enforcement silently become
    another's.
    """
    _use_apikey(opts, opts.fed_token)
    resp = opts.client.get(
        "/api/system/geoip/lookup",
        params={"ip": FED_IP, "graph": "federation"},
    )
    assert resp.status_code == 200, (
        f"lookup failed: {resp.status_code} {opts.client.last_response.body}"
    )
    data = resp.response.data
    leaked = [f for f in FIREWALL_FIELDS if f in data]
    assert not leaked, (
        f"federation graph leaked per-fleet firewall state {leaked} — "
        f"local enforcement must not cross the federation boundary. "
        f"payload keys={sorted(data.keys())}"
    )
    opts.client.logout()


@th.django_unit_test()
def test_lookup_forces_federation_graph_for_unprivileged_callers(opts):
    """A caller may not widen its own payload by asking for a richer graph.

    REGRESSION (found by post-build security review of 3fc821c9): the endpoint
    is open to any authenticated identity, and on_rest_get honours a
    caller-supplied ?graph=. The `detailed` graph carries the raw provider blob
    and every graph except `federation` carries per-fleet enforcement state —
    data the model gates behind VIEW_PERMS on its CRUD endpoints. So a
    zero-permission ApiKey could read exactly what Ian's "provider, not manage"
    boundary is meant to withhold, just by adding a query param.
    """
    _use_apikey(opts, opts.fed_token)
    for requested in ("detailed", "basic", "default"):
        resp = opts.client.get(
            "/api/system/geoip/lookup",
            params={"ip": FED_IP, "graph": requested},
        )
        assert resp.status_code == 200, (
            f"graph={requested} must still be served (downgraded, not denied) "
            f"— got {resp.status_code} {opts.client.last_response.body}"
        )
        data = resp.response.data
        leaked = [f for f in FIREWALL_FIELDS if f in data]
        assert not leaked, (
            f"?graph={requested} let an unprivileged key read per-fleet "
            f"enforcement state {leaked}; the endpoint must force the "
            f"federation graph for callers without VIEW_PERMS"
        )
        assert "data" not in data, (
            f"?graph={requested} leaked the raw provider blob to an "
            f"unprivileged key"
        )
    opts.client.logout()


@th.django_unit_test()
def test_lookup_denies_anonymous(opts):
    """No credential -> denied.

    Guards the test above: without this, an accidentally public endpoint would
    make test_lookup_accepts_group_apikey pass for the wrong reason.
    """
    opts.client.logout()
    resp = opts.client.get(
        "/api/system/geoip/lookup",
        params={"ip": FED_IP},
    )
    assert resp.status_code in (401, 403), (
        f"unauthenticated lookup must be denied, got {resp.status_code} "
        f"{opts.client.last_response.body}"
    )


# ── APIKEY_PERMS_PROTECTION floor for geoip_sync ──────────────────────────
#
# geoip_sync writes GLOBAL threat intel. Before the floor, the protection map
# defaulted to {} and can_change_permission fell through to generic
# key-management perms — so any group admin could self-mint a key that raises
# suspicion fleet-wide. These tests pin the floor and its escape hatches.


def _make_group_admin(opts, tag, perms):
    """A verified, MFA-free user who is a MEMBER of a fresh group with `perms`.

    Returns (user, group, email, password). No GLOBAL permissions are granted —
    that is the whole point: a group admin must not clear the floor.
    """
    from mojo.apps.account.models import User, Group, GroupMember

    email = f"geoip_fed_{tag}@fedwire.test"
    password = "FedWire##adm99"
    user = User.objects.create_user(username=email, email=email, password=password)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()

    group = Group.objects.create(name=f"geoip_fed_grp_{tag}", kind="organization")
    member, _ = GroupMember.objects.get_or_create(user=user, group=group)
    member.permissions = perms
    member.save()
    return user, group, email, password


def _login(opts, email, password):
    from mojo.decorators.limits import clear_rate_limits

    opts.client.logout()
    clear_rate_limits(ip="127.0.0.1", key="login")
    assert opts.client.login(email, password), (
        f"login failed for {email}: {opts.client.last_response.body}"
    )


@th.django_unit_test()
def test_geoip_sync_is_protected_from_group_admins(opts):
    """A group admin with no global grants cannot mint a geoip_sync key."""
    from mojo.apps.account.models import User, Group, GroupMember, ApiKey

    user, group, email, password = _make_group_admin(
        opts, "ga", {"manage_group": True, "manage_members": True})
    try:
        _login(opts, email, password)
        resp = opts.client.post("/api/group/apikey", {
            "group": group.pk,
            "name": "fed_key_denied",
            "permissions": {"geoip_sync": True},
        })
        assert resp.status_code == 403, (
            f"a group admin must not be able to grant geoip_sync (it writes "
            f"fleet-wide threat intel), got {resp.status_code}: "
            f"{opts.client.last_response.body}"
        )
        assert not ApiKey.objects.filter(
            group=group, permissions__contains={"geoip_sync": True}).exists(), (
            "geoip_sync must not have landed on any key in this group"
        )
    finally:
        opts.client.logout()
        ApiKey.objects.filter(group=group).delete()
        GroupMember.objects.filter(group=group).delete()
        Group.objects.filter(pk=group.pk).delete()
        User.objects.filter(pk=user.pk).delete()


@th.django_unit_test()
def test_group_admin_can_still_grant_unprotected_perms(opts):
    """The floor must not turn into a blanket ban on key provisioning."""
    from mojo.apps.account.models import User, Group, GroupMember, ApiKey

    user, group, email, password = _make_group_admin(
        opts, "unp", {"manage_group": True, "manage_members": True})
    try:
        _login(opts, email, password)
        resp = opts.client.post("/api/group/apikey", {
            "group": group.pk,
            "name": "fed_key_ordinary",
            "permissions": {"some_group_perm": True},
        })
        assert resp.status_code == 200, (
            f"an unprotected perm must still be assignable by a group admin, "
            f"got {resp.status_code}: {opts.client.last_response.body}"
        )
        key = ApiKey.objects.filter(group=group, name="fed_key_ordinary").first()
        assert key is not None and key.permissions.get("some_group_perm") is True, (
            f"unprotected perm must land, got {key.permissions if key else None}"
        )
    finally:
        opts.client.logout()
        ApiKey.objects.filter(group=group).delete()
        GroupMember.objects.filter(group=group).delete()
        Group.objects.filter(pk=group.pk).delete()
        User.objects.filter(pk=user.pk).delete()


@th.django_unit_test()
def test_unchanged_protected_perm_does_not_block_a_save(opts):
    """A no-op on a protected perm must not 403 the whole save.

    REGRESSION (found red-teaming item 1194, would have shipped a broken admin
    UI): set_permissions gates EVERY key in the incoming dict regardless of its
    value, and the admin UI submits the entire switch catalog on every save —
    web-mojo's FormView.getFormData re-collects every checkbox by name,
    including disabled ones, so `permissions.geoip_sync: false` rides along on
    every ApiKey write.

    Before the no-op short-circuit, that meant a group admin renaming a key,
    flipping is_active, or creating any key at all got a bare 403 with no
    indication why — the instant the floor made geoip_sync protected. An empty
    protection map hid this; a non-empty one exposes it.

    Revoking a protected perm the caller could not grant is still denied — only
    a genuine no-op is waved through.
    """
    from mojo.apps.account.models import User, Group, GroupMember, ApiKey

    user, group, email, password = _make_group_admin(
        opts, "noop", {"manage_group": True, "manage_members": True})
    try:
        _login(opts, email, password)
        # Exactly what the admin UI submits: the whole catalog, with the
        # protected switch present and off.
        resp = opts.client.post("/api/group/apikey", {
            "group": group.pk,
            "name": "fed_key_full_payload",
            "permissions": {"some_group_perm": True, "geoip_sync": False},
        })
        assert resp.status_code == 200, (
            f"a full permissions payload carrying geoip_sync=False (the admin "
            f"UI's normal shape) must NOT be denied — the protected perm is "
            f"unchanged. got {resp.status_code}: "
            f"{opts.client.last_response.body}"
        )
        key = ApiKey.objects.filter(group=group, name="fed_key_full_payload").first()
        assert key is not None, "the key must have been created"
        assert key.permissions.get("some_group_perm") is True, (
            f"the ordinary perm must land, got {key.permissions!r}"
        )
        assert "geoip_sync" not in key.permissions, (
            f"a False value must not grant the protected perm, got "
            f"{key.permissions!r}"
        )
    finally:
        opts.client.logout()
        ApiKey.objects.filter(group=group).delete()
        GroupMember.objects.filter(group=group).delete()
        Group.objects.filter(pk=group.pk).delete()
        User.objects.filter(pk=user.pk).delete()


@th.django_unit_test()
def test_revoking_a_protected_perm_is_still_denied(opts):
    """The no-op short-circuit must not become a revocation loophole.

    A group admin who cannot GRANT geoip_sync must not be able to strip it off
    an existing federation key either — that would let a tenant admin silently
    break fleet federation.
    """
    from mojo.apps.account.models import User, Group, GroupMember, ApiKey

    user, group, email, password = _make_group_admin(
        opts, "rev", {"manage_group": True, "manage_members": True})
    try:
        # Provisioned by a trusted path, as a real federation key would be.
        key, _tok = ApiKey.create_for_group(
            group=group, name="fed_key_existing",
            permissions={"geoip_sync": True})

        _login(opts, email, password)
        resp = opts.client.post(f"/api/group/apikey/{key.pk}", {
            "permissions": {"geoip_sync": False},
        })
        assert resp.status_code == 403, (
            f"a group admin must not be able to revoke a protected perm they "
            f"cannot grant, got {resp.status_code}: "
            f"{opts.client.last_response.body}"
        )
        key.refresh_from_db()
        assert key.permissions.get("geoip_sync") is True, (
            f"the protected perm must survive the denied revocation, got "
            f"{key.permissions!r}"
        )
    finally:
        opts.client.logout()
        ApiKey.objects.filter(group=group).delete()
        GroupMember.objects.filter(group=group).delete()
        Group.objects.filter(pk=group.pk).delete()
        User.objects.filter(pk=user.pk).delete()


@th.django_unit_test()
def test_a_key_cannot_mint_a_key_with_a_protected_perm(opts):
    """The floor must not be bypassable by laundering it through a second key.

    REGRESSION (found by post-build security review of 3fc821c9). This was a
    two-call escalation:

      1. A group admin mints key A with the UNPROTECTED `groups` perm — allowed,
         since `groups` is not on the floor and is in this model's SAVE_PERMS.
      2. Authenticating AS key A, mint key B carrying `geoip_sync`. The
         short-circuit at the top of can_change_permission reads
         `user.has_permission(["manage_groups", "manage_users"])`, and for a
         non-override key session request.user IS the ApiKey — so it read key
         A's own dict, where implied_perms expands manage_groups -> groups.
         It returned True and the protection map was never consulted.

    Net: the tenant admin the floor exists to stop obtained fleet-wide
    threat-intel write in two REST calls.
    """
    from mojo.apps.account.models import User, Group, GroupMember, ApiKey

    user, group, email, password = _make_group_admin(
        opts, "mint", {"manage_group": True, "manage_members": True})
    try:
        # Step 1: a group admin legitimately mints a key holding `groups`.
        _login(opts, email, password)
        resp = opts.client.post("/api/group/apikey", {
            "group": group.pk,
            "name": "fed_key_launderer",
            "permissions": {"groups": True},
        })
        assert resp.status_code == 200, (
            f"minting a key with the unprotected `groups` perm must still "
            f"work, got {resp.status_code}: {opts.client.last_response.body}"
        )
        token_a = resp.response.data.token
        assert token_a, f"create echo must carry the raw token: {resp.response.data!r}"

        # Step 2: as that key, try to mint a key carrying the protected perm.
        _use_apikey(opts, token_a)
        resp = opts.client.post("/api/group/apikey", {
            "group": group.pk,
            "name": "fed_key_laundered",
            "permissions": {"geoip_sync": True},
        })
        assert resp.status_code == 403, (
            f"a key-backed session must not be able to grant a PROTECTED perm "
            f"— this is the floor-laundering escalation. got "
            f"{resp.status_code}: {opts.client.last_response.body}"
        )
        assert not ApiKey.objects.filter(
            group=group, permissions__contains={"geoip_sync": True}).exists(), (
            "no key in this group may have ended up with geoip_sync"
        )
    finally:
        opts.client.logout()
        ApiKey.objects.filter(group=group).delete()
        GroupMember.objects.filter(group=group).delete()
        Group.objects.filter(pk=group.pk).delete()
        User.objects.filter(pk=user.pk).delete()


@th.django_unit_test()
def test_a_key_can_still_grant_unprotected_perms(opts):
    """The key-backed block is scoped to PROTECTED perms only.

    Guards the fix above from over-reaching: ordinary key-provisions-key flows
    must keep working, or the fix would be a silent breaking change for any
    deployment that provisions keys with a key.
    """
    from mojo.apps.account.models import User, Group, GroupMember, ApiKey

    user, group, email, password = _make_group_admin(
        opts, "mintok", {"manage_group": True, "manage_members": True})
    try:
        _login(opts, email, password)
        resp = opts.client.post("/api/group/apikey", {
            "group": group.pk,
            "name": "fed_key_parent",
            "permissions": {"groups": True},
        })
        assert resp.status_code == 200, f"setup key failed: {opts.client.last_response.body}"
        token_a = resp.response.data.token

        _use_apikey(opts, token_a)
        resp = opts.client.post("/api/group/apikey", {
            "group": group.pk,
            "name": "fed_key_child",
            "permissions": {"some_group_perm": True},
        })
        assert resp.status_code == 200, (
            f"a key must still be able to grant UNPROTECTED perms, got "
            f"{resp.status_code}: {opts.client.last_response.body}"
        )
        child = ApiKey.objects.filter(group=group, name="fed_key_child").first()
        assert child is not None and child.permissions.get("some_group_perm") is True, (
            f"unprotected perm must land, got {child.permissions if child else None}"
        )
    finally:
        opts.client.logout()
        ApiKey.objects.filter(group=group).delete()
        GroupMember.objects.filter(group=group).delete()
        Group.objects.filter(pk=group.pk).delete()
        User.objects.filter(pk=user.pk).delete()


@th.django_unit_test()
def test_no_op_payload_cannot_wipe_a_string_permissions_column(opts):
    """An all-no-op payload must not silently clear a JSON-STRING permissions column.

    REGRESSION (found by post-build security review of 3fc821c9). The no-op
    short-circuit was added with the `isinstance(self.permissions, dict)` reset
    hoisted ABOVE the gate, so a column holding a JSON string got materialized
    to {} before any authorization ran — and because every key in the payload
    then looked like a no-op, no gate ever ran and the wipe stuck. A stringy
    permissions column is a shape this model supports for authorization
    (_get_permissions_dict feeds has_permission), so a federation key stored
    that way could be silently stripped of geoip_sync by any caller who could
    reach it.
    """
    import json
    from mojo.apps.account.models import Group, ApiKey

    Group.objects.filter(name="geoip_fed_strperm_group").delete()
    group = Group.objects.create(name="geoip_fed_strperm_group", kind="organization")
    try:
        key, _tok = ApiKey.create_for_group(
            group=group, name="geoip_fed_test_strperm", permissions={})
        # The stringy shape, written past the setter the way a legacy row or a
        # trusted internal call could have left it.
        ApiKey.objects.filter(pk=key.pk).update(
            permissions=json.dumps({"geoip_sync": True}))
        key.refresh_from_db()
        assert key.has_permission("geoip_sync"), (
            "precondition: the stringy column must authorize geoip_sync"
        )

        # An all-no-op payload, with no request at all (the harshest case —
        # can_change_permission would return False for a None user).
        key.set_permissions({"unrelated_perm": False})
        assert key.has_permission("geoip_sync"), (
            f"a no-op payload wiped the permissions column — geoip_sync was "
            f"silently revoked with no authorization check. now: "
            f"{key.permissions!r}"
        )
    finally:
        ApiKey.objects.filter(group=group).delete()
        Group.objects.filter(pk=group.pk).delete()


@th.django_unit_test()
def test_global_admin_can_still_grant_geoip_sync(opts):
    """Provisioning a federation key must remain possible for a global admin.

    can_change_permission returns early for a global manage_users/manage_groups
    holder, so protection is never consulted for them.
    """
    from mojo.apps.account.models import User, Group, GroupMember, ApiKey

    user, group, email, password = _make_group_admin(
        opts, "gl", {"manage_group": True})
    user.add_permission(["manage_users"])   # GLOBAL grant
    user.save()
    try:
        _login(opts, email, password)
        resp = opts.client.post("/api/group/apikey", {
            "group": group.pk,
            "name": "fed_key_allowed",
            "permissions": {"geoip_sync": True},
        })
        assert resp.status_code == 200, (
            f"a global admin must still be able to provision a federation key, "
            f"got {resp.status_code}: {opts.client.last_response.body}"
        )
        key = ApiKey.objects.filter(group=group, name="fed_key_allowed").first()
        assert key is not None and key.permissions.get("geoip_sync") is True, (
            f"geoip_sync must land for a global admin, got "
            f"{key.permissions if key else None}"
        )
    finally:
        opts.client.logout()
        ApiKey.objects.filter(group=group).delete()
        GroupMember.objects.filter(group=group).delete()
        Group.objects.filter(pk=group.pk).delete()
        User.objects.filter(pk=user.pk).delete()


@th.django_unit_test()
def test_federation_wire_cleanup(opts):
    from mojo.apps.account.models import User, Group, GroupMember, ApiKey
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    from mojo.apps.account.models.setting import Setting

    opts.client.logout()
    Setting.remove("APIKEY_PERMS_PROTECTION")
    ApiKey.objects.filter(name__startswith="geoip_fed_test_").delete()
    Group.objects.filter(name="geoip_fed_test_group").delete()
    Group.objects.filter(name__startswith="geoip_fed_grp_").delete()
    User.objects.filter(email__endswith="@fedwire.test").delete()
    GeoLocatedIP.objects.filter(ip_address=FED_IP).delete()
