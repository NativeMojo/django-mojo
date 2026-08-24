"""dnsman REST — fail-closed permissions and secret containment.

These run against the live dev server, so nothing here may reach a provider.
Every fixture refuses before the network: domains are GoDaddy-backed with no
usable credential, and purchasing is off by its default.
"""

TESTIT_TIER = "core"

import time

from testit import helpers as th

from tests.test_dnsman._helpers import (
    make_user, make_group, make_domain, make_credential, make_certificate,
    login, assert_no_secrets, DNS_PERMS,
)


@th.django_unit_setup()
def setup_rest_permissions(opts):
    from mojo.apps.dnsman.models import Domain, DnsCredential, Certificate

    # Long-lived DB: clear anything a previous run of THIS module left behind.
    Certificate.objects.filter(common_name__startswith="dm-").delete()
    Domain.objects.filter(name__startswith="dm-").delete()
    DnsCredential.objects.filter(name__startswith="cred-").delete()

    opts.group = make_group("dnsrest")

    opts.nobody, opts.nobody_email, opts.nobody_pw = make_user()
    opts.viewer, opts.viewer_email, opts.viewer_pw = make_user(["view_dns"])
    opts.manager, opts.manager_email, opts.manager_pw = make_user(["manage_dns"])
    opts.admin, opts.admin_email, opts.admin_pw = make_user(["manage_dns"], is_superuser=True)

    opts.credential = make_credential(group=None)
    # GoDaddy-backed, no credential -> every DNS op fails closed pre-network.
    opts.domain = make_domain(group=None, provider="godaddy", status="active")
    opts.certificate = make_certificate(opts.domain)


# ---------------------------------------------------------------------------
# Unauthenticated and unprivileged callers
# ---------------------------------------------------------------------------

@th.django_unit_test("anonymous callers are refused everywhere")
def test_anonymous_refused(opts):
    opts.client.logout()
    for path in ["/api/dnsman/domain", "/api/dnsman/credential", "/api/dnsman/purchase",
                 "/api/dnsman/certificate"]:
        resp = opts.client.get(path)
        assert resp.status_code in (401, 403), \
            f"{path} served an anonymous caller (status {resp.status_code})"


@th.django_unit_test("a user with no dns permission cannot read domains")
def test_no_perm_cannot_read(opts):
    login(opts, opts.nobody_email, opts.nobody_pw)
    resp = opts.client.get("/api/dnsman/domain")
    assert resp.status_code in (401, 403), \
        f"user without view_dns read the domain list (status {resp.status_code})"


@th.django_unit_test("view_dns reads but cannot write DNS records")
def test_viewer_cannot_write(opts):
    login(opts, opts.viewer_email, opts.viewer_pw)

    resp = opts.client.get("/api/dnsman/domain")
    assert resp.status_code == 200, \
        f"view_dns should read the domain list, got {resp.status_code}"

    resp = opts.client.post("/api/dnsman/dns", json=dict(
        domain=opts.domain.pk, type="TXT", name="probe", record_values=["x"]))
    assert resp.status_code in (401, 403), \
        f"view_dns must not write DNS records (status {resp.status_code})"


@th.django_unit_test("registrar search and suggest require view_dns")
def test_search_and_suggest_require_perm(opts):
    login(opts, opts.nobody_email, opts.nobody_pw)

    resp = opts.client.post("/api/dnsman/registrar/suggest", json=dict(
        domain="never-checked-example.com"))
    assert resp.status_code in (401, 403), \
        f"a user with no dns permission ran registrar/suggest (status {resp.status_code})"

    resp = opts.client.post("/api/dnsman/registrar/search", json=dict(
        domains=["never-checked-example.com"]))
    assert resp.status_code in (401, 403), \
        f"a user with no dns permission ran a batch search (status {resp.status_code})"


# ---------------------------------------------------------------------------
# The two endpoints deliberately stricter than manage_dns
# ---------------------------------------------------------------------------

@th.django_unit_test("adopt is refused to manage_dns without superuser")
def test_adopt_requires_superuser(opts):
    login(opts, opts.manager_email, opts.manager_pw)
    resp = opts.client.post("/api/dnsman/registrar/adopt", json=dict(
        group=opts.group.pk, domain="adopt-target-example.com"))
    assert resp.status_code in (401, 403), (
        "adopt must be superuser-only — it hands a group control of a zone in "
        f"the house AWS account (status {resp.status_code})")


@th.django_unit_test("house-account discovery is refused to manage_dns without superuser")
def test_discover_requires_superuser(opts):
    login(opts, opts.manager_email, opts.manager_pw)
    resp = opts.client.get("/api/dnsman/registrar/discover")
    assert resp.status_code in (401, 403), (
        "discovery lists every domain in the house AWS account, including other "
        f"tenants' — it must be superuser-only (status {resp.status_code})")


@th.django_unit_test("naming your own group does not open house-account discovery")
def test_discover_group_param_does_not_open_it(opts):
    """
    The model permission check is NOT the gate here. With ?group=<own group> it
    consults Group.user_has_permission, which honors GroupMember grants — so a
    group admin passes it. The superuser gate runs FIRST for exactly this
    reason; this pins that ordering.
    """
    login(opts, opts.manager_email, opts.manager_pw)
    resp = opts.client.get("/api/dnsman/registrar/discover",
                           params=dict(group=opts.group.pk))
    assert resp.status_code in (401, 403), (
        "supplying a group the caller has rights in must not satisfy the "
        f"platform-admin gate (status {resp.status_code})")


@th.django_unit_test("assigning a domain to a group is refused to manage_dns without superuser")
def test_assign_group_requires_superuser(opts):
    login(opts, opts.manager_email, opts.manager_pw)
    resp = opts.client.post("/api/dnsman/registrar/assign-group", json=dict(
        domain=opts.domain.pk, group=opts.group.pk))
    assert resp.status_code in (401, 403), (
        "assignment turns a house asset into a tenant's — it must be "
        f"superuser-only (status {resp.status_code})")

    opts.domain.refresh_from_db()
    assert opts.domain.group_id is None, \
        "a refused assignment must not have moved the domain into a group"


@th.tier("extended")
@th.django_unit_test("adopt refuses a group id that does not resolve")
def test_adopt_refuses_an_unresolvable_group(opts):
    """
    Group.get_active returns None for an inactive or nonexistent id, silently.
    Without a guard a typo'd id would quietly adopt the domain platform-scoped —
    indistinguishable from a deliberate house adopt, and assign-group only ever
    fires once, so it could never be corrected.
    """
    from mojo.apps.dnsman.models import Domain

    login(opts, opts.admin_email, opts.admin_pw)
    resp = opts.client.post("/api/dnsman/registrar/adopt", json=dict(
        group=99999999, domain="dm-unresolvable-group.com"))
    assert resp.status_code != 200, (
        "an unresolvable group must be refused, not silently treated as "
        f"'no group' (status {resp.status_code})")
    assert not Domain.objects.filter(name="dm-unresolvable-group.com").exists(), (
        "a refused adopt must create nothing — least of all a house domain the "
        "caller did not ask for")


@th.tier("extended")
@th.django_unit_test("a superuser assigns an unowned house domain to a group")
def test_assign_group_happy_path(opts):
    """No network on this path: it is a plain field write, so it is safe to
    exercise end-to-end against the live server."""
    from tests.test_dnsman._helpers import make_domain

    # A fresh row on purpose — opts.domain is the module's shared null-group
    # fixture and opts.certificate hangs off it.
    domain = make_domain(name="dm-assignable-example.com", group=None,
                         provider="godaddy", status="active")

    login(opts, opts.admin_email, opts.admin_pw)
    resp = opts.client.post("/api/dnsman/registrar/assign-group", json=dict(
        domain=domain.pk, group=opts.group.pk))
    assert resp.status_code == 200, (
        f"a superuser must be able to assign an unowned domain "
        f"(status {resp.status_code}: {resp.response})")

    domain.refresh_from_db()
    assert domain.group_id == opts.group.pk, (
        f"expected the domain to land in group {opts.group.pk}, "
        f"got {domain.group_id}")


@th.django_unit_test("a domain that already belongs to a group is never re-homed")
def test_assign_group_refuses_rehoming(opts):
    from tests.test_dnsman._helpers import make_domain, make_group

    owned = make_domain(name="dm-owned-example.com", group=opts.group,
                        provider="godaddy", status="active")
    other = make_group("dnsrest-other")

    login(opts, opts.admin_email, opts.admin_pw)
    resp = opts.client.post("/api/dnsman/registrar/assign-group", json=dict(
        domain=owned.pk, group=other.pk))
    assert resp.status_code != 200, (
        "moving a domain between tenants is a cross-tenant transfer primitive "
        f"and must be refused (status {resp.status_code})")

    owned.refresh_from_db()
    assert owned.group_id == opts.group.pk, (
        f"the domain must stay with its original group, got {owned.group_id}")


@th.django_unit_test("certificate material is refused to view_dns")
def test_material_requires_manage(opts):
    login(opts, opts.viewer_email, opts.viewer_pw)
    resp = opts.client.get(f"/api/dnsman/certificate/material/{opts.certificate.pk}")
    assert resp.status_code in (401, 403), (
        "seeing a certificate exists must not entitle a caller to its private "
        f"key (status {resp.status_code})")


@th.tier("extended")
@th.django_unit_test("material reports 503 when custody is unavailable, not 'no key'")
def test_material_custody_unavailable(opts):
    login(opts, opts.manager_email, opts.manager_pw)
    resp = opts.client.get(f"/api/dnsman/certificate/material/{opts.certificate.pk}")
    # The fixture has no stored key (KMS is not configured in tests), which is
    # exactly the custody-unavailable branch. Reporting it as "no key" would
    # send a consumer off to reissue for nothing.
    assert resp.status_code != 200, \
        "material endpoint returned success for a certificate with no key"
    assert_no_secrets(resp.response, "certificate material (unavailable)")


@th.tier("extended")
@th.django_unit_test("a superuser can remove only failed certificate attempts")
def test_remove_failed_certificate_attempt(opts):
    from mojo.apps.dnsman.models import Certificate

    failed = make_certificate(opts.domain, status="failed")
    login(opts, opts.admin_email, opts.admin_pw)
    resp = opts.client.post("/api/dnsman/certificate/remove-failed", json=dict(
        certificate=failed.pk))
    assert resp.status_code == 200, (
        f"a superuser could not remove a failed attempt: {resp.response}")
    assert not Certificate.objects.filter(pk=failed.pk).exists(), (
        "the failed attempt still exists after a successful removal")

    resp = opts.client.post("/api/dnsman/certificate/remove-failed", json=dict(
        certificate=opts.certificate.pk))
    assert resp.status_code != 200, "the cleanup endpoint removed an active certificate"
    assert Certificate.objects.filter(pk=opts.certificate.pk).exists(), (
        "an active certificate was deleted")


# ---------------------------------------------------------------------------
# Purchase is off, and proves it without touching AWS
# ---------------------------------------------------------------------------

@th.tier("extended")
@th.django_unit_test("quote refuses while purchasing is disabled")
def test_quote_disabled(opts):
    login(opts, opts.manager_email, opts.manager_pw)
    resp = opts.client.post("/api/dnsman/registrar/quote", json=dict(
        group=opts.group.pk, domain="never-bought-example.com"))
    assert resp.status_code != 200, \
        "quote succeeded while DNSMAN_PURCHASE_ENABLED defaults to False"

    from mojo.apps.dnsman.models import DomainPurchase
    assert not DomainPurchase.objects.filter(
        domain_name="never-bought-example.com").exists(), \
        "a refused quote must not leave a purchase row behind"


@th.django_unit_test("purchase requires token plus typed domain and price")
def test_purchase_requires_confirm_token(opts):
    login(opts, opts.manager_email, opts.manager_pw)
    resp = opts.client.post("/api/dnsman/registrar/purchase", json=dict(
        group=opts.group.pk, purchase=1))
    assert resp.status_code == 400, (
        "purchase without a confirm token must be a 400 from requires_params, "
        f"got {resp.status_code}")


@th.django_unit_test("purchase refuses machine credentials and logins older than 600 seconds")
def test_purchase_requires_recent_interactive_auth(opts):
    from mojo.apps.account.models import ApiKey
    from mojo.apps.account.utils.jwtoken import JWToken

    _, token = ApiKey.create_for_group(
        opts.group, "dns-purchase-machine", permissions={"manage_dns": True})
    opts.client.logout()
    opts.client.session.headers["Authorization"] = f"apikey {token}"
    try:
        machine = opts.client.post("/api/dnsman/registrar/purchase", json=dict(
            group=opts.group.pk, purchase=1, confirm_token="opaque",
            confirm_domain="never-bought-example.com", confirm_price="12.00"))
        assert machine.status_code == 403, \
            f"a machine credential reached the real-money mutation ({machine.status_code})"
    finally:
        opts.client.session.headers.pop("Authorization", None)

    opts.client.is_authenticated = True
    opts.client.bearer = "bearer"
    opts.client.access_token = JWToken(opts.manager.get_auth_key()).create_access_token(
        uid=opts.manager.pk, auth_time=int(time.time()) - 601)
    stale = opts.client.post("/api/dnsman/registrar/purchase", json=dict(
        group=opts.group.pk, purchase=1, confirm_token="opaque",
        confirm_domain="never-bought-example.com", confirm_price="12.00"))
    assert stale.status_code == 440, \
        f"a 601-second-old login bypassed the 600-second purchase gate ({stale.status_code})"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@th.tier("extended")
@th.django_unit_test("dns endpoints require a domain parameter")
def test_dns_requires_domain(opts):
    login(opts, opts.manager_email, opts.manager_pw)
    resp = opts.client.get("/api/dnsman/dns")
    assert resp.status_code == 400, \
        f"missing domain param should be a 400, got {resp.status_code}"


@th.tier("extended")
@th.django_unit_test("registrar search refuses bad shapes before any AWS call")
def test_search_shape_validation(opts):
    # Every one of these refusals happens in shape validation, pre-network —
    # safe against the live server.
    login(opts, opts.viewer_email, opts.viewer_pw)

    resp = opts.client.post("/api/dnsman/registrar/search", json=dict())
    assert resp.status_code == 400, \
        f"search with no input must keep answering 400, got {resp.status_code}"

    resp = opts.client.post("/api/dnsman/registrar/search", json=dict(domains=[]))
    assert resp.status_code == 400, \
        f"an empty batch must be a 400, got {resp.status_code}"

    resp = opts.client.post("/api/dnsman/registrar/search", json=dict(
        domains=["a-example.com"], tlds=["com"]))
    assert resp.status_code == 400, \
        f"mixing 'domains' with 'tlds' must be a 400, got {resp.status_code}"


@th.django_unit_test("a domain with no usable credential fails closed")
def test_dns_fails_closed_without_credential(opts):
    login(opts, opts.manager_email, opts.manager_pw)
    resp = opts.client.get("/api/dnsman/dns", params=dict(domain=opts.domain.pk))
    # The domain is GoDaddy-backed with no credential. The refusal must come
    # from the central gate, before any socket is opened.
    assert resp.status_code != 200, (
        "a GoDaddy domain with no linked credential must refuse DNS operations "
        f"(status {resp.status_code})")


# ---------------------------------------------------------------------------
# Secret containment across every read surface
# ---------------------------------------------------------------------------

@th.django_unit_test("no dnsman read surface leaks secret material")
def test_no_secret_leaks(opts):
    login(opts, opts.admin_email, opts.admin_pw)
    for path in ["/api/dnsman/domain", "/api/dnsman/credential", "/api/dnsman/purchase",
                 "/api/dnsman/certificate"]:
        resp = opts.client.get(path)
        assert resp.status_code == 200, f"{path} returned {resp.status_code}"
        assert_no_secrets(resp.response, path)


@th.django_unit_test("credential graphs expose masked values only")
def test_credential_masked(opts):
    login(opts, opts.admin_email, opts.admin_pw)
    resp = opts.client.get(f"/api/dnsman/credential/{opts.credential.pk}")
    assert resp.status_code == 200, f"credential detail returned {resp.status_code}"
    body = str(resp.response)
    assert "api_key_masked" in body, "credential detail should expose api_key_masked"
    assert "GDKEYtest1234" not in body, "credential detail leaked the raw api key"
    assert_no_secrets(resp.response, "credential detail")


@th.tier("extended")
@th.django_unit_test("adopt refuses an unresolvable group_uuid, not just an unresolvable group id")
def test_adopt_refuses_an_unresolvable_group_uuid(opts):
    """
    The dispatcher populates request.group from ?group_uuid= as well as ?group=.
    Keying the guard on `group` alone would leave the uuid form taking the exact
    silent path the guard exists to close.
    """
    from mojo.apps.dnsman.models import Domain

    login(opts, opts.admin_email, opts.admin_pw)
    resp = opts.client.post("/api/dnsman/registrar/adopt", json=dict(
        group_uuid="00000000-0000-0000-0000-000000000000",
        domain="dm-unresolvable-uuid.com"))
    assert resp.status_code != 200, (
        "an unresolvable group_uuid must be refused, not silently treated as "
        f"'no group' (status {resp.status_code})")
    assert not Domain.objects.filter(name="dm-unresolvable-uuid.com").exists(), \
        "a refused adopt must create nothing"
