"""
edge REST — fail-closed permissions, tenant isolation, and the house guard.

These run against the live dev server, so `mock.patch` in this process does not
reach the handler. Every fixture is inert: domains are GoDaddy-backed with no
credential and no test here reaches a provider or a node.

The load-bearing assertion in this file is `test_tenant_cannot_declare_upstream`.
`Vhost.upstream` being a foreign key is only a real constraint if a tenant
cannot create rows on the other end of it — otherwise the FK is a text field
with extra steps.
"""

from testit import helpers as th

from tests.test_edge._helpers import (
    declare_pools,
    cleanup, declare_reserved_names, login, make_certificate, make_domain,
    make_group, make_group_member, make_upstream, make_user, make_vhost,
)


@th.django_unit_setup()
def setup_rest_permissions(opts):
    cleanup()
    declare_reserved_names()
    declare_pools()

    opts.group = make_group("edgerest")
    opts.other_group = make_group("edgeother")

    _, opts.tenant_email, opts.tenant_pw, _ = make_group_member(
        ["manage_dns"], group=opts.group)

    opts.nobody, opts.nobody_email, opts.nobody_pw = make_user()
    opts.viewer, opts.viewer_email, opts.viewer_pw = make_user(["view_dns"])
    opts.manager, opts.manager_email, opts.manager_pw = make_user(["manage_dns"])
    opts.admin, opts.admin_email, opts.admin_pw = make_user(
        ["manage_dns"], is_superuser=True)

    # A tenant vhost, and a HOUSE one (group-less domain = platform property).
    opts.domain = make_domain(group=opts.group)
    opts.certificate = make_certificate(opts.domain)
    opts.vhost = make_vhost(opts.domain, opts.certificate, label="www")

    opts.house_domain = make_domain(group=None)
    opts.house_cert = make_certificate(opts.house_domain)
    opts.house_vhost = make_vhost(opts.house_domain, opts.house_cert, label="www")

    opts.other_domain = make_domain(group=opts.other_group)
    opts.other_cert = make_certificate(opts.other_domain)
    opts.other_vhost = make_vhost(opts.other_domain, opts.other_cert, label="www")

    opts.upstream = make_upstream(host="127.0.0.1", port=8000)
    opts.tenant_upstream = make_upstream(
        group=opts.group, host="127.0.0.1", port=8002)
    opts.other_upstream = make_upstream(
        group=opts.other_group, host="127.0.0.1", port=8003)


@th.django_unit_test("anonymous callers are refused on every edge surface")
def test_anonymous_refused(opts):
    opts.client.logout()
    for path in ["/api/edge/vhost", "/api/edge/upstream"]:
        resp = opts.client.get(path)
        assert resp.status_code in (401, 403), \
            f"{path} served an anonymous caller (status {resp.status_code})"

    resp = opts.client.post("/api/edge/upstream/declare", json=dict(
        name="up-anon", kind="http", host="127.0.0.1", port=8000))
    assert resp.status_code in (401, 403), \
        f"an anonymous caller reached upstream/declare (status {resp.status_code})"


@th.django_unit_test("a user with no dns permission cannot read vhosts")
def test_no_perm_cannot_read(opts):
    login(opts, opts.nobody_email, opts.nobody_pw)
    resp = opts.client.get("/api/edge/vhost")
    assert resp.status_code in (401, 403), \
        f"a user without view_dns read the vhost list (status {resp.status_code})"


@th.django_unit_test("view_dns reads vhosts but cannot write them")
def test_viewer_cannot_write(opts):
    login(opts, opts.viewer_email, opts.viewer_pw)

    resp = opts.client.get("/api/edge/vhost")
    assert resp.status_code == 200, \
        f"view_dns should read the vhost list, got {resp.status_code}"

    resp = opts.client.post("/api/edge/vhost", json=dict(
        domain=opts.domain.pk, certificate=opts.certificate.pk,
        label="viewerwrite", kind="site"))
    assert resp.status_code in (401, 403), \
        f"view_dns created a vhost (status {resp.status_code})"


@th.django_unit_test("a tenant admin cannot DECLARE an upstream")
def test_tenant_cannot_declare_upstream(opts):
    """The foreign key is only a constraint if this is refused.

    A tenant who could add an Upstream row would simply declare one pointing at
    the metadata service and select it from their vhost — the exact escalation
    the structured model exists to prevent.
    """
    login(opts, opts.manager_email, opts.manager_pw)
    resp = opts.client.post("/api/edge/upstream/declare", json=dict(
        name="up-tenant", kind="http", host="169.254.169.254", port=80))
    assert resp.status_code in (401, 403), \
        f"a manage_dns holder declared an upstream (status {resp.status_code})"

    resp = opts.client.post("/api/edge/upstream/declare", json=dict(
        group=opts.group.pk, name="up-tenant2", kind="http",
        host="10.0.0.9", port=8000))
    assert resp.status_code in (401, 403), (
        "naming their own group let a tenant admin declare an upstream "
        f"(status {resp.status_code})")


@th.django_unit_test("a platform admin declares an upstream, and IMDS is still refused")
def test_admin_declares_upstream(opts):
    login(opts, opts.admin_email, opts.admin_pw)

    resp = opts.client.post("/api/edge/upstream/declare", json=dict(
        name="up-admin1", kind="http", host="127.0.0.1", port=8001))
    assert resp.status_code == 200, \
        f"a platform admin could not declare an upstream: {resp.status_code} {resp.body}"

    resp = opts.client.post("/api/edge/upstream/declare", json=dict(
        name="up-admin2", kind="http", host="169.254.169.254", port=80))
    assert resp.status_code not in (200, 201), (
        "even a platform admin must not be able to point an upstream at the "
        f"instance metadata service (status {resp.status_code})")


@th.django_unit_test("upstream destination fields are not writable over REST")
def test_upstream_destination_is_immutable(opts):
    """NO_SAVE_FIELDS pins host/port/socket_path/kind.

    Without this, a manage_dns holder could repoint an existing house upstream
    at anything — reaching the same place as declaring one.
    """
    login(opts, opts.admin_email, opts.admin_pw)
    resp = opts.client.post(f"/api/edge/upstream/{opts.upstream.pk}", json=dict(
        host="169.254.169.254", port=80))
    opts.upstream.refresh_from_db()
    assert opts.upstream.host == "127.0.0.1", (
        "an upstream's host was rewritten over REST — NO_SAVE_FIELDS is not "
        f"holding (status {resp.status_code}, host now {opts.upstream.host})")


@th.django_unit_test("a tenant's upstream list includes shared rows, never another tenant")
def test_upstream_list_scoped_with_shared(opts):
    login(opts, opts.tenant_email, opts.tenant_pw)
    resp = opts.client.get(f"/api/edge/upstream?group={opts.group.pk}")
    assert resp.status_code == 200, (
        "a tenant admin could not list tenant and shared upstreams "
        f"(status {resp.status_code})")

    ids = {row.get("id") for row in (resp.json.get("data") or [])}
    assert opts.tenant_upstream.pk in ids, \
        "the tenant's own upstream was missing from its scoped list"
    assert opts.upstream.pk in ids, \
        "the shared house upstream was missing from the tenant's scoped list"
    assert opts.other_upstream.pk not in ids, \
        "another tenant's upstream appeared in the tenant's scoped list"


@th.django_unit_test("a global manage_dns grant does not reach a HOUSE vhost")
def test_house_vhost_guard(opts):
    """Vhost scopes through domain__group; a house domain resolves that to None
    and the check falls through to GLOBAL permissions. The platform guard is
    what stands there — same shape as dnsman's house-certificate guard."""
    login(opts, opts.manager_email, opts.manager_pw)
    resp = opts.client.get(f"/api/edge/vhost/{opts.house_vhost.pk}")
    assert resp.status_code in (401, 403), (
        "a global manage_dns holder read a house vhost — the platform's own "
        f"serving config (status {resp.status_code})")


@th.django_unit_test("a platform admin can read a house vhost")
def test_house_vhost_admin_allowed(opts):
    login(opts, opts.admin_email, opts.admin_pw)
    resp = opts.client.get(f"/api/edge/vhost/{opts.house_vhost.pk}")
    assert resp.status_code == 200, \
        f"a platform admin could not read a house vhost: {resp.status_code} {resp.body}"


@th.django_unit_test("global dns managers cannot list house vhosts")
def test_house_vhost_hidden_from_global_manager_list(opts):
    login(opts, opts.manager_email, opts.manager_pw)
    resp = opts.client.get("/api/edge/vhost?size=20")
    assert resp.status_code == 200, (
        "a global manage_dns holder could not list tenant vhosts "
        f"(status {resp.status_code})")

    ids = {row.get("id") for row in (resp.json.get("data") or [])}
    assert opts.vhost.pk in ids, \
        "a tenant vhost was missing from the global manager's list"
    assert opts.other_vhost.pk in ids, \
        "an unrelated tenant vhost was missing from the global manager's list"
    assert opts.house_vhost.pk not in ids, \
        "a house vhost appeared in a non-superuser global manager's list"


@th.django_unit_test("literal superusers retain house vhost inventory")
def test_house_vhost_visible_to_superuser_list(opts):
    login(opts, opts.admin_email, opts.admin_pw)
    resp = opts.client.get("/api/edge/vhost?size=20")
    assert resp.status_code == 200, (
        "a platform superuser could not list vhosts "
        f"(status {resp.status_code})")

    ids = {row.get("id") for row in (resp.json.get("data") or [])}
    assert opts.house_vhost.pk in ids, \
        "the house vhost was missing from a literal superuser's list"


@th.django_unit_test("an unauthenticated caller cannot tell a house vhost from a tenant one")
def test_house_guard_is_not_an_oracle(opts):
    """The model check runs BEFORE the platform guard for this reason.

    Leading with the guard would answer 403 'platform administrators' for a
    house vhost and 401 for a tenant one — a free classification oracle over
    sequential pks.
    """
    opts.client.logout()
    house = opts.client.get(f"/api/edge/vhost/{opts.house_vhost.pk}")
    tenant = opts.client.get(f"/api/edge/vhost/{opts.vhost.pk}")
    assert house.status_code == tenant.status_code, (
        "a house vhost and a tenant vhost answered an anonymous caller "
        f"differently ({house.status_code} vs {tenant.status_code}) — that "
        "classifies the platform's own inventory")


@th.django_unit_test("a tenant cannot read another tenant's vhost")
def test_tenant_isolation(opts):
    from mojo.apps.account.models import GroupMember

    user, email, password = make_user()
    member, _ = GroupMember.objects.get_or_create(user=user, group=opts.group)
    member.permissions = {"manage_dns": True}
    member.save()

    login(opts, email, password)
    resp = opts.client.get(
        f"/api/edge/vhost/{opts.other_vhost.pk}?group={opts.group.pk}")
    assert resp.status_code in (401, 403, 404), (
        "a tenant admin read another tenant's vhost "
        f"(status {resp.status_code})")


@th.django_unit_test("a vhost list is scoped to the caller's tenant")
def test_vhost_list_scoped(opts):
    from mojo.apps.account.models import GroupMember

    user, email, password = make_user()
    member, _ = GroupMember.objects.get_or_create(user=user, group=opts.group)
    member.permissions = {"manage_dns": True}
    member.save()

    login(opts, email, password)
    resp = opts.client.get(f"/api/edge/vhost?group={opts.group.pk}")
    assert resp.status_code == 200, \
        f"a tenant admin could not list their own vhosts: {resp.status_code}"

    ids = {row.get("id") for row in (resp.json.get("data") or [])}
    assert opts.other_vhost.pk not in ids, \
        "another tenant's vhost appeared in a scoped list"
    assert opts.house_vhost.pk not in ids, \
        "a house vhost appeared in a tenant's list"


@th.django_unit_test("the blocklist is fleet-scoped: member grants plus ?group= open nothing")
def test_blocklist_member_grant_refused(opts):
    """BlocklistEntry is deliberately group-less, so permissions resolve on
    GLOBAL grants only. A tenant group admin can hand themselves any key in
    their own GroupMember.permissions — naming their group must still buy
    nothing on a fleet-wide security control."""
    from tests.test_edge._helpers import make_group_member

    user, email, password, group = make_group_member(
        ["manage_security", "view_security"])

    login(opts, email, password)
    resp = opts.client.get(f"/api/edge/blocklist?group={group.pk}")
    assert resp.status_code in (401, 403), (
        "a member-scoped security grant read the fleet blocklist "
        f"(status {resp.status_code})")

    resp = opts.client.post(f"/api/edge/blocklist?group={group.pk}", json=dict(
        kind="ua", value="memberbot", mode="enforce"))
    assert resp.status_code not in (200, 201), (
        "a member-scoped security grant WROTE a fleet-wide block rule "
        f"(status {resp.status_code})")

    from mojo.apps.edge.models import BlocklistEntry
    assert not BlocklistEntry.objects.filter(value="memberbot").exists(), \
        "the member-written blocklist row landed despite the refusal"


@th.django_unit_test("global security grants manage the blocklist; view is read-only")
def test_blocklist_global_grants(opts):
    from mojo.apps.edge.models import BlocklistEntry

    BlocklistEntry.objects.filter(value__in=["restbot", "viewerbot"]).delete()
    viewer, viewer_email, viewer_pw = make_user(["view_security"])
    manager, manager_email, manager_pw = make_user(["manage_security"])

    login(opts, viewer_email, viewer_pw)
    resp = opts.client.get("/api/edge/blocklist")
    assert resp.status_code == 200, \
        f"view_security could not read the blocklist: {resp.status_code}"
    resp = opts.client.post("/api/edge/blocklist", json=dict(
        kind="ua", value="viewerbot", mode="log"))
    assert resp.status_code not in (200, 201), (
        f"view_security WROTE a blocklist row (status {resp.status_code})")

    login(opts, manager_email, manager_pw)
    resp = opts.client.post("/api/edge/blocklist", json=dict(
        kind="ua", value="restbot", mode="log", note="bltest rest"))
    assert resp.status_code == 200, (
        f"a global manage_security holder could not add a blocklist row: "
        f"{resp.status_code} {resp.body}")
    row = BlocklistEntry.objects.filter(value="restbot").first()
    assert row is not None and row.mode == "log", \
        "the blocklist row did not land as written"

    resp = opts.client.post("/api/edge/blocklist", json=dict(
        kind="ua", value='rest" 1; } server {', mode="log"))
    assert resp.status_code not in (200, 201), (
        "a hostile pattern was accepted over REST "
        f"(status {resp.status_code})")

    opts.client.logout()
    resp = opts.client.get("/api/edge/blocklist")
    assert resp.status_code in (401, 403), \
        f"the blocklist served an anonymous caller (status {resp.status_code})"


@th.django_unit_test("a vhost cannot be moved between domains over REST")
def test_domain_is_immutable(opts):
    """Re-pointing `domain` would move the row between TENANTS."""
    login(opts, opts.admin_email, opts.admin_pw)
    resp = opts.client.post(f"/api/edge/vhost/{opts.vhost.pk}", json=dict(
        domain=opts.other_domain.pk))
    opts.vhost.refresh_from_db()
    assert opts.vhost.domain_id == opts.domain.pk, (
        "a vhost was moved to another tenant's domain over REST "
        f"(status {resp.status_code})")
