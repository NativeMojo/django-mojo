"""Tenant-side delegated ACME lifecycle, isolation, and claim races."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from objict import objict
from testit import helpers as th

from tests.test_dnsman._helpers import (
    assert_no_secrets, login, make_group_member)


def _group(prefix="delegation"):
    from mojo.apps.account.models import Group

    return Group.objects.create(
        name=f"{prefix}_{uuid.uuid4().hex[:10]}", kind="organization")


def _row(group, name, state="pending", domain=None):
    from mojo.apps.dnsman.models import AcmeDelegation
    from mojo.helpers import dates

    return AcmeDelegation.objects.create(
        group=group,
        domain=domain,
        tenant_uuid=group.get_uuid(),
        tenant_name=group.name,
        domain_name=name,
        source_name=f"_acme-challenge.{name}",
        target_name=f"{uuid.uuid4().hex}.hub.example.net",
        state=state,
        verified_at=dates.utcnow() if state in ("verified", "broken") else None,
    )


@th.django_unit_test("delegation persists client_ref before allocation and replays it immutably")
def test_client_ref_precedes_allocation(opts):
    from mojo.apps.dnsman.models import AcmeDelegation
    from mojo.apps.dnsman.services import acme_hub_client, delegation

    group = _group("delegation_preallocate")
    name = f"preallocate-{uuid.uuid4().hex[:8]}.example"
    seen = []
    assigned_target = f"{uuid.uuid4().hex}.hub.example.net"

    def allocate(domain_name, client_ref):
        stored = AcmeDelegation.objects.get(client_ref=client_ref)
        seen.append((stored.pk, str(stored.client_ref), stored.target_name))
        return acme_hub_client.AcmeHubAllocation(
            success=True,
            client_ref=client_ref,
            domain=domain_name,
            source=f"_acme-challenge.{domain_name}",
            target=assigned_target,
        )

    # Hub availability rides initiate()'s `available=` seam (item #2558)
    # instead of a process-global patch of delegation.is_available.
    with mock.patch.object(acme_hub_client, "allocate", side_effect=allocate):
        first = delegation.initiate(group, None, name=name, available=True)
        retry = delegation.initiate(group, None, name=name, available=True)

    assert seen[0][2] is None, \
        "client_ref must be committed while target is still unallocated"
    assert first.pk == retry.pk and first.client_ref == retry.client_ref, \
        "a retry must replay the same durable tenant-bound client_ref"
    assert first.target_name == retry.target_name, \
        "a retry must compare and preserve the original immutable target"


@th.django_unit_test("verification creates only a certificate-only mojo Domain after proof")
def test_verify_creates_certificate_only_domain(opts):
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import delegation

    group = _group("delegation_verify")
    name = f"verify-{uuid.uuid4().hex[:8]}.example"
    row = _row(group, name)
    proof = objict(ok=True, error=None)
    with mock.patch.object(delegation.probe, "verify_one_hop_cname", return_value=proof):
        verified = delegation.verify(row)

    domain = Domain.objects.get(pk=verified.domain_id)
    assert verified.state == "verified", \
        f"successful proof should make routing sticky, got {verified.state}"
    assert domain.provider == "mojo" and domain.status == "active" and domain.verified, \
        f"external verified name must create an active certificate-only Domain, got {domain.__dict__}"
    assert domain.metadata.get("certificate_only") is True, \
        "mojo Domains must explicitly identify certificate-only semantics"
    assert not domain.requires_credential, \
        "delegated ACME must never require tenant provider credentials"


@th.django_unit_test("verification makes routing sticky without rewriting a direct provider")
def test_verify_existing_direct_domain_keeps_provider(opts):
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import delegation

    group = _group("delegation_direct")
    name = f"direct-{uuid.uuid4().hex[:8]}.example"
    domain = Domain.objects.create(
        group=group, name=name, provider="route53", status="active",
        hosted_zone_id="ZONE-DIRECT", verified=True)
    row = _row(group, name, domain=domain)
    proof = objict(ok=True, error=None)
    with mock.patch.object(delegation.probe, "verify_one_hop_cname", return_value=proof):
        delegation.verify(row)

    domain.refresh_from_db()
    row.refresh_from_db()
    assert domain.provider == "route53", \
        "verified delegation selects the certificate writer without rewriting direct DNS"
    assert row.state == "verified" and delegation.for_domain(domain).pk == row.pk, \
        "verified routing must be sticky and discoverable independently of provider"


@th.django_unit_test("pending proof failure stays inert; sticky proof failure becomes broken")
def test_sticky_proof_state(opts):
    from mojo.apps.dnsman.services import delegation

    group = _group("delegation_sticky")
    pending = _row(group, f"pending-{uuid.uuid4().hex[:8]}.example")
    sticky = _row(
        group, f"sticky-{uuid.uuid4().hex[:8]}.example", state="verified")
    failure = objict(ok=False, error="CNAME delegation does not match the allocation")
    with mock.patch.object(delegation.probe, "verify_one_hop_cname", return_value=failure):
        for row in (pending, sticky):
            try:
                delegation.prove_alias(row)
            except Exception:
                pass
            else:
                assert False, "a mismatching authoritative alias must be refused"

    pending.refresh_from_db()
    sticky.refresh_from_db()
    assert pending.state == "pending", \
        "unverified pending delegation must remain inert on proof failure"
    assert sticky.state == "broken" and sticky.verified_at is not None, \
        "first verification is sticky: failure becomes broken, never pending/direct"


@th.django_unit_test("a stale proof failure cannot demote a concurrently verified delegation")
def test_stale_proof_failure_preserves_sticky_state(opts):
    from mojo.apps.dnsman.models import AcmeDelegation
    from mojo.apps.dnsman.services import delegation
    from mojo.helpers import dates

    group = _group("delegation_proof_race")
    stale = _row(group, f"proof-race-{uuid.uuid4().hex[:8]}.example")
    AcmeDelegation.objects.filter(pk=stale.pk).update(
        state="verified", verified_at=dates.utcnow())

    delegation._proof_failure(stale, delegation.ERROR_ALIAS_MISMATCH)
    stored = AcmeDelegation.objects.get(pk=stale.pk)
    assert stored.state == "broken" and stored.verified_at is not None, \
        "failure must derive stickiness from the locked database row, not a stale object"


@th.django_unit_test("remote delegation work rechecks active immutable ownership")
def test_remote_work_rechecks_current_owner(opts):
    from mojo.apps.dnsman.models import Certificate, Domain
    from mojo.apps.dnsman.services import acme_hub_client, certs, delegation

    cases = []
    for label in ("suspended", "transferred", "deleted"):
        group = _group(f"delegation_owner_{label}")
        name = f"owner-{label}-{uuid.uuid4().hex[:8]}.example"
        domain = Domain.objects.create(
            group=group, name=name, provider="mojo", status="active", verified=True)
        row = _row(group, name, state="verified", domain=domain)
        cases.append((label, group, domain, row))

    cases[0][1].is_active = False
    cases[0][1].save(update_fields=["is_active", "modified"])
    replacement = _group("delegation_owner_replacement")
    cases[1][2].group = replacement
    cases[1][2].save(update_fields=["group", "modified"])
    cases[2][1].delete()
    cases[2][3].refresh_from_db()

    challenges = {cases[0][3].source_name: ["digest"]}
    with mock.patch.object(acme_hub_client, "publish") as publish:
        for label, unused_group, unused_domain, row in cases:
            try:
                certs._publish_delegated_challenges(
                    row, {row.source_name: ["digest"]})
            except Exception:
                pass
            else:
                assert False, f"{label} ownership must fail before a hub mutation"
    assert not publish.called, "invalid current ownership must consume no hub mutation"

    # The account loader is injected through issue()'s `acme=` seam
    # (item #2558); a recorded call would mean a CA round trip was consumed.
    account_calls = []

    def guard_account():
        account_calls.append(True)
        return None, None

    for label, unused_group, domain, unused_row in cases[:2]:
        certificate = Certificate.objects.create(
            domain=domain,
            common_name=domain.name,
            sans=[domain.name, f"*.{domain.name}"],
            status="pending")
        result = certs.issue(certificate, acme=guard_account)
        assert not result.ok, f"{label} ownership must fail issuance closed"
    assert not account_calls, \
        "suspended or transferred ownership must consume no CA account/order call"

    for label, unused_group, unused_domain, row in cases:
        row.refresh_from_db()
        assert row.state in ("broken", "retired"), \
            f"{label} ownership should durably fail closed, got {row.state}"


@th.django_unit_test("verification retires a deleted tenant under lock")
def test_deleted_group_retires(opts):
    from mojo.apps.dnsman.services import delegation

    group = _group("delegation_deleted")
    row = _row(group, f"deleted-{uuid.uuid4().hex[:8]}.example")
    group.delete()
    row.refresh_from_db()
    proof = objict(ok=True, error=None)
    with mock.patch.object(delegation.probe, "verify_one_hop_cname", return_value=proof):
        try:
            delegation.verify(row)
        except Exception as error:
            assert "no longer active" in str(error), \
                f"deleted tenant should be uniformly refused, got {error}"
        else:
            assert False, "a deleted tenant must not claim a house Domain"
    row.refresh_from_db()
    assert row.state == "retired" and row.retired_at is not None, \
        "deleted tenant allocation must remain as a retired tombstone"


@th.django_unit_test("two tenants racing one raw name produce exactly one Domain claim")
def test_raw_name_claim_race(opts):
    from django.db import close_old_connections
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import delegation

    name = f"claim-{uuid.uuid4().hex[:8]}.example"
    rows = [_row(_group("delegation_race"), name) for unused in range(2)]
    proof = objict(ok=True, error=None)

    def claim(pk):
        from mojo.apps.dnsman.models import AcmeDelegation
        close_old_connections()
        try:
            return delegation.verify(AcmeDelegation.objects.get(pk=pk)).pk, None
        except Exception as error:
            return pk, str(error)
        finally:
            close_old_connections()

    with mock.patch.object(delegation.probe, "verify_one_hop_cname", return_value=proof):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, [row.pk for row in rows]))

    errors = [error for unused, error in results if error]
    assert Domain.objects.filter(name=name).count() == 1, \
        "the globally unique Domain name must be claimed exactly once"
    assert len(errors) == 1 and "already claimed" in errors[0], \
        f"the losing tenant must get a bounded claim refusal, got {results}"


@th.django_unit_setup()
def setup_delegation_rest(opts):
    opts.delegation_user, opts.delegation_email, opts.delegation_pw, opts.delegation_group = \
        make_group_member(["view_dns", "manage_dns"])
    opts.foreign_user, opts.foreign_email, opts.foreign_pw, opts.foreign_group = \
        make_group_member(["view_dns", "manage_dns"])

    from mojo.apps.dnsman.models import Domain

    own_name = f"rest-own-{uuid.uuid4().hex[:8]}.example"
    foreign_name = f"rest-foreign-{uuid.uuid4().hex[:8]}.example"
    opts.delegation_domain = Domain.objects.create(
        group=opts.delegation_group, name=own_name, provider="mojo",
        status="active", verified=True)
    opts.foreign_domain = Domain.objects.create(
        group=opts.foreign_group, name=foreign_name, provider="mojo",
        status="active", verified=True)
    opts.delegation_row = _row(
        opts.delegation_group, own_name, state="verified",
        domain=opts.delegation_domain)
    opts.foreign_row = _row(
        opts.foreign_group, foreign_name, state="verified",
        domain=opts.foreign_domain)


@th.django_unit_test("delegation REST status is tenant-isolated and secret-minimal")
def test_rest_tenant_isolation(opts):
    login(opts, opts.delegation_email, opts.delegation_pw)
    own = opts.client.get(f"/api/dnsman/delegation/{opts.delegation_row.pk}")
    assert own.status_code == 200, \
        f"tenant should read its delegation status, got {own.status_code}: {own.response}"
    data = own.response["data"]
    assert set(data) == {
        "id", "created", "modified", "domain", "domain_name", "source",
        "target", "state", "verified_at", "last_error_code"}, \
        f"delegation status returned an unexpected shape: {data.keys()}"
    assert_no_secrets(own.response, "delegation detail")
    for forbidden in ("client_ref", "tenant_uuid", "cleanup_challenge_ref"):
        assert forbidden not in str(own.response), \
            f"delegation REST must not expose internal {forbidden}"

    foreign = opts.client.get(f"/api/dnsman/delegation/{opts.foreign_row.pk}")
    unknown = opts.client.get("/api/dnsman/delegation/999999999")
    foreign_verify = opts.client.post(
        "/api/dnsman/delegation/verify",
        json={"delegation": opts.foreign_row.pk})
    unknown_verify = opts.client.post(
        "/api/dnsman/delegation/verify",
        json={"delegation": 999999999})
    from mojo.apps.dnsman.models import AcmeDelegation
    AcmeDelegation.objects.filter(pk=opts.foreign_row.pk).update(state="retired")
    retired = opts.client.get(f"/api/dnsman/delegation/{opts.foreign_row.pk}")
    retired_verify = opts.client.post(
        "/api/dnsman/delegation/verify",
        json={"delegation": opts.foreign_row.pk})
    signatures = [(response.status_code, response.response) for response in (
        foreign, unknown, retired)]
    assert all(status == 404 for status, unused in signatures), \
        f"foreign, unknown and retired details must share one 404 shape, got {signatures}"
    assert signatures[0][1] == signatures[1][1] == signatures[2][1], \
        f"detail refusals must not disclose sequential-id state, got {signatures}"
    verify_signatures = [
        (response.status_code, response.response) for response in (
            foreign_verify, unknown_verify, retired_verify)]
    assert all(status == 404 for status, unused in verify_signatures), \
        f"foreign, unknown and retired verify must share 404, got {verify_signatures}"
    assert verify_signatures[0][1] == verify_signatures[1][1] == verify_signatures[2][1], \
        f"verify refusals must not disclose sequential-id state, got {verify_signatures}"

    listed = opts.client.get(
        f"/api/dnsman/delegation?domain={opts.delegation_domain.pk}")
    assert listed.status_code == 200 and len(listed.response["data"]) == 1, \
        f"domain-filtered status should return only the tenant row, got {listed.response}"


@th.django_unit_setup()
def cleanup_delegation_rest(opts):
    # Groups cascade the test users/members and Domains; delegations are
    # tombstones and intentionally survive with null relations.
    opts.delegation_group.delete()
    opts.foreign_group.delete()
