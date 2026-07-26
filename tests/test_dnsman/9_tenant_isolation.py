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
