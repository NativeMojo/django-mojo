"""Certificate retirement — repoint every referencing vhost, then delete.

The service half runs in-process (no provider is ever reachable: retirement is
DB-only by design). The REST half runs against the live server and stays
vhost-free so it needs no pool declarations server-side; the repointing
behavior itself is pinned in the service tests.

Fixtures use the `certret-` name prefix — NOT `dm-` — so this module's setup
and `8_rest_permissions.py`'s cleanup can never sweep each other's rows.
"""

from datetime import timedelta

from testit import helpers as th

from tests.test_dnsman._helpers import (
    make_user, make_group, make_domain, login,
)
from tests.test_edge._helpers import make_certificate, make_vhost, declare_pools


POOL = "dmretire"


def _domain(opts, tag, group=None):
    return make_domain(name=f"certret-{tag}.com", group=group,
                       provider="godaddy", status="active")


def _raises_value(callable_, fragment):
    from mojo import errors as me

    try:
        callable_()
    except me.ValueException as err:
        message = str(err)
        assert fragment.lower() in message.lower(), (
            f"expected the refusal to mention {fragment!r}, got: {message}")
        return
    raise AssertionError(
        f"expected a ValueException mentioning {fragment!r}, nothing raised")


@th.django_unit_setup()
def setup_cert_retire(opts):
    from mojo.apps.dnsman.models import Certificate, Domain
    from mojo.apps.edge.models import Vhost

    # Long-lived DB: clear anything a previous run of THIS module left behind.
    # Vhosts first — Vhost.certificate is PROTECT.
    Vhost.objects.filter(domain__name__startswith="certret-").delete()
    Certificate.objects.filter(domain__name__startswith="certret-").delete()
    Domain.objects.filter(name__startswith="certret-").delete()

    declare_pools()

    opts.group = make_group("certret")
    opts.viewer, opts.viewer_email, opts.viewer_pw = make_user(["view_dns"])
    opts.manager, opts.manager_email, opts.manager_pw = make_user(["manage_dns"])
    opts.admin, opts.admin_email, opts.admin_pw = make_user(
        ["manage_dns"], is_superuser=True)


# ---------------------------------------------------------------------------
# Service: the eligibility matrix
# ---------------------------------------------------------------------------

@th.django_unit_test("retire is refused when nothing covers the target's names")
def test_retire_refused_without_covering(opts):
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.services import certs

    domain = _domain(opts, "nocover", group=opts.group)
    target = make_certificate(
        domain, sans=[domain.name, f"*.{domain.name}"])
    make_certificate(
        domain, status="failed", common_name=f"www.{domain.name}",
        sans=[f"www.{domain.name}"])

    _raises_value(lambda: certs.retire_certificate(target), "covers every name")
    assert Certificate.objects.filter(pk=target.pk).exists(), (
        "a refused retirement must not delete the target certificate")


@th.django_unit_test("retire is refused when the only replacement is due for renewal")
def test_retire_refused_when_replacement_due_for_renewal(opts):
    from mojo.helpers import dates
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.services import certs

    domain = _domain(opts, "duerenew", group=opts.group)
    target = make_certificate(
        domain, common_name=f"www.{domain.name}", sans=[f"www.{domain.name}"])
    stale = make_certificate(domain, sans=[domain.name, f"*.{domain.name}"])
    stale.renew_after = dates.utcnow() - timedelta(days=1)
    stale.save()

    _raises_value(lambda: certs.retire_certificate(target), "due for renewal")
    assert Certificate.objects.filter(pk=target.pk).exists(), (
        "a refused retirement must not delete the target certificate")


@th.django_unit_test("retire is refused when the replacement cannot serve a referenced address")
def test_retire_refused_when_replacement_misses_an_address(opts):
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.services import certs
    from mojo.apps.edge.models import Vhost

    domain = _domain(opts, "gap", group=opts.group)
    # Only candidate: covers the apex — the target's whole name set — but not
    # the www address a live vhost still serves.
    make_certificate(domain, common_name=domain.name, sans=[domain.name])
    target = make_certificate(
        domain, common_name=domain.name, sans=[domain.name])
    # Enable a www vhost legally (against a cert that covers it), then bypass
    # save() validation to point it at the target — rows written by services
    # or migrations can hold anything, and retirement must fail closed on
    # what IS referenced, not on what save() would have allowed.
    wildcard = make_certificate(
        domain, status="revoked", sans=[domain.name, f"*.{domain.name}"])
    vhost = make_vhost(domain, wildcard, label="www", pool=POOL)
    Vhost.objects.filter(pk=vhost.pk).update(certificate=target.pk)

    _raises_value(
        lambda: certs.retire_certificate(target), f"www.{domain.name}")
    assert Certificate.objects.filter(pk=target.pk).exists(), (
        "a refused retirement must not delete the target certificate")
    vhost.refresh_from_db()
    assert vhost.certificate_id == target.pk, (
        "a refused retirement must not repoint any vhost")


@th.django_unit_test("retiring the covering certificate itself is refused")
def test_retire_refuses_the_covering_certificate_itself(opts):
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.services import certs

    domain = _domain(opts, "self", group=opts.group)
    wildcard = make_certificate(domain, sans=[domain.name, f"*.{domain.name}"])
    make_certificate(
        domain, status="failed", common_name=f"www.{domain.name}",
        sans=[f"www.{domain.name}"])

    _raises_value(
        lambda: certs.retire_certificate(wildcard), "covers every name")
    assert Certificate.objects.filter(pk=wildcard.pk).exists(), (
        "the covering certificate itself must never be retired")


@th.django_unit_test("retire repoints every referencing vhost, deletes the row, and publishes convergence")
def test_retire_happy_path(opts):
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.services import certs
    from mojo.apps.edge.services import convergence

    domain = _domain(opts, "happy", group=opts.group)
    target = make_certificate(
        domain, common_name=f"www.{domain.name}", sans=[f"www.{domain.name}"])
    wildcard = make_certificate(domain, sans=[domain.name, f"*.{domain.name}"])

    live = make_vhost(domain, target, label="www", pool=POOL)
    # A parked row keeps its reference too — PROTECT means retirement must
    # repoint it or the delete cannot happen. Coverage is only gated on
    # enabled rows, exactly as Vhost.save enforces.
    parked = make_vhost(domain, target, label="old", pool=POOL,
                        is_enabled=False)

    # delete() zeroes the instance's pk, so capture it first.
    target_pk = target.pk
    published = []
    with convergence.publisher_scope(lambda pool: published.append(pool)):
        result = certs.retire_certificate(target)

    assert result.retired == target_pk, (
        f"expected retired={target_pk}, got {result.retired}")
    assert result.replaced_by == wildcard.pk, (
        f"expected replaced_by={wildcard.pk}, got {result.replaced_by}")
    assert result.vhosts_repointed == 2, (
        f"both referencing vhosts must be repointed, got {result.vhosts_repointed}")
    assert not Certificate.objects.filter(pk=target_pk).exists(), (
        "the retired certificate row must be deleted")
    live.refresh_from_db()
    parked.refresh_from_db()
    assert live.certificate_id == wildcard.pk, (
        f"the enabled vhost must point at the wildcard, got {live.certificate_id}")
    assert parked.certificate_id == wildcard.pk, (
        f"the parked vhost must point at the wildcard, got {parked.certificate_id}")
    assert POOL in published, (
        f"repointing must publish convergence for pool {POOL}, got {published}")


@th.django_unit_test("retire_eligibility maps each certificate to its replacement or None")
def test_retire_eligibility_map(opts):
    from mojo.apps.dnsman.services import certs

    domain = _domain(opts, "elig", group=opts.group)
    narrow = make_certificate(
        domain, common_name=f"www.{domain.name}", sans=[f"www.{domain.name}"])
    wildcard = make_certificate(domain, sans=[domain.name, f"*.{domain.name}"])
    failed = make_certificate(
        domain, status="failed", common_name=f"www.{domain.name}",
        sans=[f"www.{domain.name}"])

    eligibility = certs.retire_eligibility(domain)
    assert eligibility[narrow.pk] == wildcard.pk, (
        f"the narrow cert must be retirable to the wildcard, got "
        f"{eligibility[narrow.pk]}")
    assert eligibility[failed.pk] == wildcard.pk, (
        "a failed attempt with no references must be retirable to the wildcard")
    assert eligibility[wildcard.pk] is None, (
        "nothing covers the wildcard, so it must not be marked retirable")


# ---------------------------------------------------------------------------
# REST: guards mirror remove-failed, eligibility mirrors the dns read gate
# ---------------------------------------------------------------------------

@th.django_unit_test("retire endpoint refuses anonymous and read-only callers")
def test_retire_endpoint_perms(opts):
    from mojo.apps.dnsman.models import Certificate

    domain = _domain(opts, "restperm", group=opts.group)
    target = make_certificate(
        domain, common_name=f"www.{domain.name}", sans=[f"www.{domain.name}"])
    replacement = make_certificate(
        domain, sans=[domain.name, f"*.{domain.name}"])

    opts.client.logout()
    resp = opts.client.post("/api/dnsman/certificate/retire", json=dict(
        certificate=target.pk))
    assert resp.status_code in (401, 403), (
        f"an anonymous caller reached retire (status {resp.status_code})")

    login(opts, opts.viewer_email, opts.viewer_pw)
    resp = opts.client.post("/api/dnsman/certificate/retire", json=dict(
        certificate=target.pk))
    assert resp.status_code in (401, 403), (
        f"view_dns must not retire certificates (status {resp.status_code})")
    assert Certificate.objects.filter(pk=target.pk).exists(), (
        "a refused retire must not have deleted the certificate")

    login(opts, opts.manager_email, opts.manager_pw)
    resp = opts.client.post("/api/dnsman/certificate/retire", json=dict(
        certificate=target.pk))
    assert resp.status_code == 200, (
        f"manage_dns could not retire a tenant certificate: {resp.response}")
    data = resp.response.get("data", resp.response)
    assert data.get("retired") == target.pk, (
        f"the response must name the retired certificate, got {data}")
    assert data.get("replaced_by") == replacement.pk, (
        f"the response must name the replacement, got {data}")
    assert data.get("vhosts_repointed") == 0, (
        f"no vhost referenced this certificate, got {data}")
    assert not Certificate.objects.filter(pk=target.pk).exists(), (
        "the retired certificate row must be deleted")


@th.django_unit_test("retiring a house certificate requires a platform admin")
def test_retire_house_guard(opts):
    from mojo.apps.dnsman.models import Certificate

    domain = _domain(opts, "house", group=None)
    target = make_certificate(
        domain, common_name=f"www.{domain.name}", sans=[f"www.{domain.name}"])
    make_certificate(domain, sans=[domain.name, f"*.{domain.name}"])

    login(opts, opts.manager_email, opts.manager_pw)
    resp = opts.client.post("/api/dnsman/certificate/retire", json=dict(
        certificate=target.pk))
    assert resp.status_code in (401, 403), (
        "a global manage_dns holder must not retire a house certificate "
        f"(status {resp.status_code})")
    assert Certificate.objects.filter(pk=target.pk).exists(), (
        "a refused house retire must not have deleted the certificate")

    login(opts, opts.admin_email, opts.admin_pw)
    resp = opts.client.post("/api/dnsman/certificate/retire", json=dict(
        certificate=target.pk))
    assert resp.status_code == 200, (
        f"a platform admin could not retire a house certificate: {resp.response}")
    assert not Certificate.objects.filter(pk=target.pk).exists(), (
        "the retired house certificate row must be deleted")


@th.django_unit_test("retire-eligibility is a gated, DB-only read")
def test_retire_eligibility_endpoint(opts):
    domain = _domain(opts, "eligrest", group=opts.group)
    narrow = make_certificate(
        domain, common_name=f"www.{domain.name}", sans=[f"www.{domain.name}"])
    wildcard = make_certificate(domain, sans=[domain.name, f"*.{domain.name}"])

    opts.client.logout()
    resp = opts.client.get("/api/dnsman/certificate/retire-eligibility",
                           params=dict(domain=domain.pk))
    assert resp.status_code in (401, 403), (
        f"an anonymous caller read retire eligibility (status {resp.status_code})")

    login(opts, opts.viewer_email, opts.viewer_pw)
    resp = opts.client.get("/api/dnsman/certificate/retire-eligibility")
    assert resp.status_code == 400, (
        f"a missing domain param must be a 400, got {resp.status_code}")

    resp = opts.client.get("/api/dnsman/certificate/retire-eligibility",
                           params=dict(domain=domain.pk))
    assert resp.status_code == 200, (
        f"view_dns could not read retire eligibility: {resp.response}")
    data = resp.response.get("data", resp.response)
    eligibility = data.get("eligibility") or {}
    assert eligibility.get(str(narrow.pk)) == wildcard.pk, (
        f"the narrow cert must map to the wildcard, got {eligibility}")
    assert eligibility.get(str(wildcard.pk)) is None, (
        f"the wildcard must map to null, got {eligibility}")

    house = _domain(opts, "elighouse", group=None)
    make_certificate(house, sans=[house.name, f"*.{house.name}"])
    login(opts, opts.manager_email, opts.manager_pw)
    resp = opts.client.get("/api/dnsman/certificate/retire-eligibility",
                           params=dict(domain=house.pk))
    assert resp.status_code in (401, 403), (
        "a global manage_dns holder must not probe house certificate "
        f"eligibility (status {resp.status_code})")
