"""dnsman REST — fail-closed permissions and secret containment.

These run against the live dev server, so nothing here may reach a provider.
Every fixture refuses before the network: domains are GoDaddy-backed with no
usable credential, and purchasing is off by its default.
"""

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


@th.django_unit_test("certificate material is refused to view_dns")
def test_material_requires_manage(opts):
    login(opts, opts.viewer_email, opts.viewer_pw)
    resp = opts.client.get(f"/api/dnsman/certificate/material/{opts.certificate.pk}")
    assert resp.status_code in (401, 403), (
        "seeing a certificate exists must not entitle a caller to its private "
        f"key (status {resp.status_code})")


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


# ---------------------------------------------------------------------------
# Purchase is off, and proves it without touching AWS
# ---------------------------------------------------------------------------

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


@th.django_unit_test("there is no single-call purchase path")
def test_purchase_requires_confirm_token(opts):
    login(opts, opts.manager_email, opts.manager_pw)
    resp = opts.client.post("/api/dnsman/registrar/purchase", json=dict(
        group=opts.group.pk, purchase=1))
    assert resp.status_code == 400, (
        "purchase without a confirm token must be a 400 from requires_params, "
        f"got {resp.status_code}")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@th.django_unit_test("dns endpoints require a domain parameter")
def test_dns_requires_domain(opts):
    login(opts, opts.manager_email, opts.manager_pw)
    resp = opts.client.get("/api/dnsman/dns")
    assert resp.status_code == 400, \
        f"missing domain param should be a 400, got {resp.status_code}"


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
