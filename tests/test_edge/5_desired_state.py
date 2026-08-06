"""
The node-facing endpoints, and the authorization bug that nearly shipped.

`test_member_scoped_grant_is_refused` is the reason this file exists. The first
draft of item #1433's plan gated these endpoints with `@md.requires_perms`,
which falls back to a client-supplied `?group=` param, and — unlike the ApiKey
path — the GroupMember side of the permission floor is empty by default. That
composed into: any tenant group admin grants themselves `edge_node` in their own
group, and reads every vhost in the fleet plus every referenced certificate's
private key.

No existing test covered that path (the shipped `geoip_sync` floor test covers
the ApiKey half only), which is precisely why it survived the plan and the
red-team pass and was caught by re-reading `mojo/decorators/auth.py`.
"""

from testit import helpers as th

from tests.test_edge._helpers import (
    cleanup, declare_reserved_names, login, make_certificate, make_domain,
    make_group, make_group_member, make_upstream, make_user, make_vhost,
)


@th.django_unit_setup()
def setup_desired_state(opts):
    from mojo.apps.account.models import ApiKey

    cleanup()
    ApiKey.objects.filter(name__startswith="edge_node_test_").delete()
    declare_reserved_names()

    opts.group = make_group("edgenode")
    opts.domain = make_domain(group=opts.group)
    opts.certificate = make_certificate(opts.domain)
    opts.vhost = make_vhost(opts.domain, opts.certificate, label="www")

    # A second pool, so pool filtering is actually observable.
    opts.staging_domain = make_domain(group=opts.group)
    opts.staging_cert = make_certificate(opts.staging_domain)
    opts.staging_vhost = make_vhost(
        opts.staging_domain, opts.staging_cert, label="www", pool="staging")

    # A certificate no enabled vhost references.
    opts.orphan_domain = make_domain(group=opts.group)
    opts.orphan_cert = make_certificate(opts.orphan_domain)

    opts.node_user, opts.node_email, opts.node_pw = make_user(["edge_node"])
    opts.manager, opts.manager_email, opts.manager_pw = make_user(["manage_dns"])
    opts.admin, opts.admin_email, opts.admin_pw = make_user(
        ["manage_dns"], is_superuser=True)


def _state(opts, pool=None):
    """The desired-state payload, unwrapped.

    A plain dict returned from a view is wrapped by the framework as
    {"status": True, "code": 200, "data": {...}} (mojo/decorators/http.py),
    so every read here goes through `data`.
    """
    path = "/api/edge/desired_state"
    if pool:
        path = f"{path}?pool={pool}"
    return opts.client.get(path).json.get("data") or {}


def _use_apikey(opts, token):
    opts.client.logout()
    opts.client.session.headers["Authorization"] = f"apikey {token}"


def _clear_apikey(opts):
    opts.client.session.headers.pop("Authorization", None)


@th.django_unit_test("anonymous callers are refused on the node endpoints")
def test_anonymous_refused(opts):
    opts.client.logout()
    for path in ["/api/edge/desired_state",
                 f"/api/edge/material/{opts.certificate.pk}"]:
        resp = opts.client.get(path)
        assert resp.status_code in (401, 403), \
            f"{path} served an anonymous caller (status {resp.status_code})"


@th.django_unit_test("manage_dns does not open the node endpoints")
def test_manage_dns_refused(opts):
    login(opts, opts.manager_email, opts.manager_pw)
    resp = opts.client.get("/api/edge/desired_state")
    assert resp.status_code in (401, 403), (
        "a manage_dns holder read the fleet's desired state "
        f"(status {resp.status_code})")

    resp = opts.client.get(f"/api/edge/material/{opts.certificate.pk}")
    assert resp.status_code in (401, 403), (
        "a manage_dns holder reached node certificate material "
        f"(status {resp.status_code})")


@th.django_unit_test("a MEMBER-scoped edge_node grant plus ?group= is refused")
def test_member_scoped_grant_is_refused(opts):
    """The regression test for the bug the first plan would have shipped.

    A group admin can put arbitrary keys in their own GroupMember.permissions —
    `MEMBER_PERMS_PROTECTION` is empty by default and has no framework floor.
    Under `requires_perms` this call succeeds and returns the whole fleet.
    Under `requires_global_perms` it is refused, because the member/group
    fallback is never consulted.
    """
    user, email, password, group = make_group_member(["edge_node"])

    login(opts, email, password)
    resp = opts.client.get(f"/api/edge/desired_state?group={group.pk}")
    assert resp.status_code in (401, 403), (
        "a member-scoped edge_node grant read the fleet's desired state — "
        "the endpoint is using the group-permission fallback "
        f"(status {resp.status_code})")

    resp = opts.client.get(
        f"/api/edge/material/{opts.certificate.pk}?group={group.pk}")
    assert resp.status_code in (401, 403), (
        "a member-scoped edge_node grant reached certificate material "
        f"(status {resp.status_code})")


@th.django_unit_test("a global edge_node grant reads the desired state")
def test_global_grant_allowed(opts):
    login(opts, opts.node_email, opts.node_pw)
    resp = opts.client.get("/api/edge/desired_state")
    assert resp.status_code == 200, \
        f"a global edge_node holder was refused: {resp.status_code} {resp.body}"

    data = resp.json.get("data") or {}
    assert data.get("generation"), "the payload carries no generation id"
    assert data.get("pool") == "default", f"wrong pool echoed: {data.get('pool')}"

    ids = {row["id"] for row in data.get("vhosts") or []}
    assert opts.vhost.pk in ids, "the default-pool vhost is missing"
    assert opts.staging_vhost.pk not in ids, \
        "a staging-pool vhost leaked into the default pool's desired state"


@th.django_unit_test("the desired state carries no key material")
def test_desired_state_has_no_material(opts):
    """The whole certificate design: identifiers travel, keys do not."""
    login(opts, opts.node_email, opts.node_pw)
    resp = opts.client.get("/api/edge/desired_state")

    blob = str(resp.json)
    for marker in ["private_key_pem", "cert_pem", "BEGIN PRIVATE KEY",
                   "BEGIN CERTIFICATE"]:
        assert marker not in blob, \
            f"the desired-state payload leaked {marker}"


@th.django_unit_test("pool filtering selects a different set and a different generation")
def test_pool_filtering(opts):
    login(opts, opts.node_email, opts.node_pw)

    default = opts.client.get("/api/edge/desired_state?pool=default").json.get("data") or {}
    staging = opts.client.get("/api/edge/desired_state?pool=staging").json.get("data") or {}

    assert default.get("generation") != staging.get("generation"), \
        "two pools produced the same generation id — nodes would cross-install"

    staging_ids = {row["id"] for row in staging.get("vhosts") or []}
    assert staging_ids == {opts.staging_vhost.pk}, \
        f"the staging pool returned the wrong vhosts: {staging_ids}"


@th.django_unit_test("the generation id is stable across identical polls")
def test_generation_is_stable(opts):
    """A node compares this to what it installed; churn would mean a reload
    storm across the fleet."""
    login(opts, opts.node_email, opts.node_pw)

    first = _state(opts).get("generation")
    second = _state(opts).get("generation")
    assert first == second, \
        "two identical polls returned different generation ids"


@th.django_unit_test("a disabled vhost drops out of the desired state")
def test_disabled_vhost_excluded(opts):
    from mojo.apps.edge.models import Vhost

    login(opts, opts.node_email, opts.node_pw)
    before = _state(opts).get("generation")

    Vhost.objects.filter(pk=opts.vhost.pk).update(is_enabled=False)
    try:
        after = _state(opts)
        ids = {row["id"] for row in after.get("vhosts") or []}
        assert opts.vhost.pk not in ids, "a disabled vhost is still being served"
        assert after.get("generation") != before, \
            "disabling a vhost did not move the generation id"
    finally:
        Vhost.objects.filter(pk=opts.vhost.pk).update(is_enabled=True)


@th.django_unit_test("node material is refused for a certificate nothing serves")
def test_material_requires_a_referencing_vhost(opts):
    """Tighter than the endpoint it works around: authorization is not enough,
    the certificate must actually be installed somewhere."""
    login(opts, opts.node_email, opts.node_pw)

    resp = opts.client.get(f"/api/edge/material/{opts.orphan_cert.pk}")
    assert resp.status_code == 404, (
        "node material was served for a certificate no enabled vhost "
        f"references (status {resp.status_code})")


@th.django_unit_test("an unknown certificate and an unreferenced one look identical")
def test_material_is_not_an_enumeration_oracle(opts):
    login(opts, opts.node_email, opts.node_pw)

    unknown = opts.client.get("/api/edge/material/99999999")
    unreferenced = opts.client.get(f"/api/edge/material/{opts.orphan_cert.pk}")
    assert unknown.status_code == unreferenced.status_code, (
        "a missing certificate and an unreferenced one answered differently "
        f"({unknown.status_code} vs {unreferenced.status_code}) — that "
        "enumerates the certificate table")


@th.django_unit_test("material reports 503 when custody is unavailable, not 'no key'")
def test_material_kms_unavailable(opts):
    """The fixture certificate has no secrets, which is exactly the
    KMS-unavailable branch. Reporting 'no key' would send the installer off to
    reissue a certificate that is fine."""
    login(opts, opts.node_email, opts.node_pw)

    resp = opts.client.get(f"/api/edge/material/{opts.certificate.pk}")
    # `ValueException(code=503)` sets the code in the BODY; the HTTP status
    # stays 400. The shipped dnsman test for this same branch asserts != 200
    # for exactly that reason — matched here rather than inventing a contract.
    assert resp.status_code != 200, \
        "material was served for a certificate whose custody is unreadable"
    assert resp.json.get("code") == 503, (
        "an unreadable custody layer must report 503 ('temporarily "
        f"unavailable'), not 'no key': {resp.json}")


@th.django_unit_test("a tenant admin cannot mint an ApiKey carrying edge_node")
def test_apikey_floor_blocks_edge_node(opts):
    """`edge_node` is on APIKEY_PERMS_PROTECTION_DEFAULTS, so granting it
    requires the granter's GLOBAL `sys.edge_node` — which a group admin does
    not have. Mirrors the shipped geoip_sync floor test."""
    from mojo.apps.account.models import GroupMember

    user, email, password = make_user()
    group = make_group("edgefloor")
    member, _ = GroupMember.objects.get_or_create(user=user, group=group)
    member.permissions = {"manage_group": True, "manage_members": True}
    member.save()

    login(opts, email, password)
    resp = opts.client.post("/api/group/apikey", {
        "group": group.pk,
        "name": "edge_node_test_floor",
        "permissions": {"edge_node": True},
    })
    assert resp.status_code == 403, (
        "a group admin minted an ApiKey carrying edge_node — that key reads "
        f"the whole fleet's certificates (status {resp.status_code})")


@th.django_unit_test("a node ApiKey holding edge_node reads the desired state")
def test_node_apikey_works(opts):
    """The intended caller. `allow_api_keys=True` exists for exactly this.

    `create_for_group` is the trusted internal path and assigns permissions
    directly, which is how a platform operator provisions a node credential —
    the REST path is the one the floor above closes.
    """
    from mojo.apps.account.models import ApiKey

    group = make_group("edgenodekey")
    key, token = ApiKey.create_for_group(
        group, "edge_node_test_node", permissions={"edge_node": True})

    _use_apikey(opts, token)
    try:
        resp = opts.client.get("/api/edge/desired_state")
        assert resp.status_code == 200, (
            "a node ApiKey holding edge_node was refused: "
            f"{resp.status_code} {resp.body}")
        assert (resp.json.get("data") or {}).get("generation"), \
            "the node received no generation id"
    finally:
        _clear_apikey(opts)


@th.django_unit_test("an ApiKey without edge_node is refused")
def test_apikey_without_perm_refused(opts):
    from mojo.apps.account.models import ApiKey

    group = make_group("edgenokey")
    key, token = ApiKey.create_for_group(
        group, "edge_node_test_plain", permissions={"manage_dns": True})

    _use_apikey(opts, token)
    try:
        resp = opts.client.get("/api/edge/desired_state")
        assert resp.status_code in (401, 403), (
            "an ApiKey without edge_node read the fleet's desired state "
            f"(status {resp.status_code})")
    finally:
        _clear_apikey(opts)
