"""dnsman — cross-tenant isolation.

A member of one group must never see or touch another group's domains,
credentials, purchases or certificates, and `?group=` must never widen what a
caller can reach.
"""

from testit import helpers as th

from tests.test_dnsman._helpers import (
    make_group_member, make_group, make_domain, make_credential,
    make_certificate, login, DNS_PERMS,
)


@th.django_unit_setup()
def setup_tenant_isolation(opts):
    from mojo.apps.dnsman.models import Domain, DnsCredential, Certificate, DomainPurchase

    Certificate.objects.filter(common_name__startswith="ti-").delete()
    Domain.objects.filter(name__startswith="ti-").delete()
    DnsCredential.objects.filter(name__startswith="cred-").delete()
    DomainPurchase.objects.filter(domain_name__startswith="ti-").delete()

    # Two tenants, each with a member holding dns perms in their OWN group only.
    opts.user_a, opts.email_a, opts.pw_a, opts.group_a = make_group_member(DNS_PERMS)
    opts.user_b, opts.email_b, opts.pw_b, opts.group_b = make_group_member(DNS_PERMS)

    opts.domain_a = make_domain(name="ti-alpha-example.com", group=opts.group_a,
                                provider="godaddy", status="active")
    opts.domain_b = make_domain(name="ti-bravo-example.com", group=opts.group_b,
                                provider="godaddy", status="active")

    opts.credential_b = make_credential(group=opts.group_b)
    opts.certificate_b = make_certificate(opts.domain_b)

    opts.purchase_b = DomainPurchase.objects.create(
        group=opts.group_b, user=opts.user_b,
        domain_name="ti-bravo-example.com", status="completed",
        price="12.00", cost="12.00", currency="USD")

    # A HOUSE domain: no group at all. This is the normal state of a domain
    # adopted out of the house AWS account before anyone assigns it, so every
    # tenant boundary has to hold against it — including for the rows that hang
    # off it, which scope through GROUP_FIELD rather than a direct group FK.
    opts.domain_house = make_domain(name="ti-house-example.com", group=None,
                                    provider="godaddy", status="active")
    opts.certificate_house = make_certificate(opts.domain_house)


@th.django_unit_test("a domain list never contains another tenant's rows")
def test_domain_list_scoped(opts):
    login(opts, opts.email_a, opts.pw_a)
    resp = opts.client.get("/api/dnsman/domain", params=dict(group=opts.group_a.pk))
    assert resp.status_code == 200, f"tenant A could not list its own domains ({resp.status_code})"
    body = str(resp.response)
    assert "ti-bravo-example.com" not in body, \
        "tenant A's domain list leaked tenant B's domain"


@th.django_unit_test("fetching another tenant's domain by pk is refused")
def test_domain_detail_cross_tenant(opts):
    login(opts, opts.email_a, opts.pw_a)
    resp = opts.client.get(f"/api/dnsman/domain/{opts.domain_b.pk}")
    assert resp.status_code in (401, 403, 404), \
        f"tenant A fetched tenant B's domain by pk (status {resp.status_code})"
    assert "ti-bravo-example.com" not in str(resp.response), \
        "the refusal itself leaked tenant B's domain name"


@th.django_unit_test("passing another tenant's group id does not widen access")
def test_group_param_does_not_widen(opts):
    login(opts, opts.email_a, opts.pw_a)
    resp = opts.client.get("/api/dnsman/domain", params=dict(group=opts.group_b.pk))
    if resp.status_code == 200:
        assert "ti-bravo-example.com" not in str(resp.response), \
            "?group= widened tenant A's access into tenant B's rows"
    else:
        assert resp.status_code in (401, 403, 404), \
            f"unexpected status for a foreign ?group= ({resp.status_code})"


@th.django_unit_test("another tenant's credential is not readable")
def test_credential_cross_tenant(opts):
    login(opts, opts.email_a, opts.pw_a)
    resp = opts.client.get(f"/api/dnsman/credential/{opts.credential_b.pk}")
    assert resp.status_code in (401, 403, 404), \
        f"tenant A read tenant B's credential (status {resp.status_code})"


@th.django_unit_test("another tenant's purchase ledger is not readable")
def test_purchase_cross_tenant(opts):
    login(opts, opts.email_a, opts.pw_a)
    resp = opts.client.get(f"/api/dnsman/purchase/{opts.purchase_b.pk}")
    assert resp.status_code in (401, 403, 404), \
        f"tenant A read tenant B's purchase row (status {resp.status_code})"


@th.django_unit_test("another tenant's certificate is not readable and its key is unreachable")
def test_certificate_cross_tenant(opts):
    login(opts, opts.email_a, opts.pw_a)

    resp = opts.client.get(f"/api/dnsman/certificate/{opts.certificate_b.pk}")
    assert resp.status_code in (401, 403, 404), \
        f"tenant A read tenant B's certificate (status {resp.status_code})"

    # Certificate has no direct group FK — it scopes through GROUP_FIELD
    # ("domain__group"). If that resolution were missing, this is the call that
    # would hand a tenant someone else's private key.
    resp = opts.client.get(f"/api/dnsman/certificate/material/{opts.certificate_b.pk}")
    assert resp.status_code in (401, 403, 404), \
        f"tenant A reached tenant B's certificate MATERIAL (status {resp.status_code})"
    assert "private_key_pem" not in str(resp.response), \
        "cross-tenant material refusal still returned a key field"


@th.django_unit_test("writing DNS on another tenant's domain is refused")
def test_dns_write_cross_tenant(opts):
    login(opts, opts.email_a, opts.pw_a)
    resp = opts.client.post("/api/dnsman/dns", json=dict(
        domain=opts.domain_b.pk, type="TXT", name="probe", record_values=["x"]))
    assert resp.status_code in (401, 403, 404), \
        f"tenant A wrote a DNS record on tenant B's domain (status {resp.status_code})"


# ---------------------------------------------------------------------------
# House (group-less) rows — the platform tier a tenant must never reach
#
# A Domain scopes on a DIRECT group FK, so a null group rebinds request.group to
# None and the check falls through to the caller's GLOBAL permissions, which a
# GroupMember grant is not. A Certificate scopes through
# GROUP_FIELD = "domain__group", and THAT rebind is skipped when it resolves to
# None — so the caller-supplied ?group= survives into a membership check. Same
# null group, opposite outcomes; hence the explicit guards in rest/certificate.py.
# ---------------------------------------------------------------------------

@th.django_unit_test("a house domain is absent from a tenant's domain list")
def test_house_domain_not_in_tenant_list(opts):
    login(opts, opts.email_a, opts.pw_a)
    resp = opts.client.get("/api/dnsman/domain", params=dict(group=opts.group_a.pk))
    assert resp.status_code == 200, f"tenant A could not list its own domains ({resp.status_code})"
    assert "ti-house-example.com" not in str(resp.response), (
        "a platform-scoped domain leaked into a tenant's list — 'no group' must "
        "mean 'no tenant', not 'every tenant'")


@th.django_unit_test("a house domain cannot be fetched by pk by a tenant")
def test_house_domain_detail_refused(opts):
    login(opts, opts.email_a, opts.pw_a)
    resp = opts.client.get(f"/api/dnsman/domain/{opts.domain_house.pk}",
                           params=dict(group=opts.group_a.pk))
    assert resp.status_code in (401, 403, 404), (
        f"a tenant fetched a platform-scoped domain by pk (status {resp.status_code})")
    assert "ti-house-example.com" not in str(resp.response), \
        "the refusal itself leaked the house domain name"


@th.django_unit_test("a house certificate cannot be read by naming your own group")
def test_house_certificate_detail_refused(opts):
    """
    The GROUP_FIELD rebind is skipped at a null group, so ?group=<own group>
    survives into Group.user_has_permission — which honors GroupMember grants.
    Without the explicit house guard this returns the certificate AND the owning
    domain through the default graph's {"domain": "basic"} sub-graph, and
    certificate pks are sequential: an enumeration oracle over the house
    inventory that registrar/discover keeps superuser-only.
    """
    login(opts, opts.email_a, opts.pw_a)
    resp = opts.client.get(f"/api/dnsman/certificate/{opts.certificate_house.pk}",
                           params=dict(group=opts.group_a.pk))
    assert resp.status_code in (401, 403, 404), (
        f"a tenant read a house certificate by naming its own group "
        f"(status {resp.status_code})")
    assert "ti-house-example.com" not in str(resp.response), \
        "the refusal leaked the house domain name through the certificate graph"


@th.django_unit_test("a house certificate cannot be revoked by a tenant")
def test_house_certificate_revoke_refused(opts):
    """The destructive twin of the read above: revocation is irreversible and
    reaches the CA."""
    from mojo.apps.dnsman.models import Certificate

    login(opts, opts.email_a, opts.pw_a)
    resp = opts.client.post("/api/dnsman/certificate/revoke", json=dict(
        certificate=opts.certificate_house.pk, group=opts.group_a.pk))
    assert resp.status_code in (401, 403, 404), (
        f"a tenant reached revocation on a house certificate "
        f"(status {resp.status_code})")

    fresh = Certificate.objects.get(pk=opts.certificate_house.pk)
    assert fresh.status != "revoked", \
        "a house certificate was revoked by a caller from another tenant"


@th.django_unit_test("a house certificate's key material stays out of tenant hands")
def test_house_certificate_material_refused(opts):
    login(opts, opts.email_a, opts.pw_a)
    resp = opts.client.get(
        f"/api/dnsman/certificate/material/{opts.certificate_house.pk}",
        params=dict(group=opts.group_a.pk))
    assert resp.status_code in (401, 403, 404), (
        f"a tenant reached house certificate MATERIAL (status {resp.status_code})")
    assert "private_key_pem" not in str(resp.response), \
        "the refusal still returned a key field"
