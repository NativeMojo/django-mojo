"""Optional ACME delegation hub: isolation, leases, reconciliation and auth."""

import uuid
from types import SimpleNamespace
from unittest import mock

from objict import objict
from testit import helpers as th


@th.django_unit_setup()
def setup_acme_hub(opts):
    from mojo.apps.account.models import ApiKey, Group
    from mojo.apps.dnsman.models import AcmeHubChallengeLease, AcmeHubDelegation

    AcmeHubChallengeLease.objects.all().delete()
    AcmeHubDelegation.objects.all().delete()
    ApiKey.objects.filter(name__startswith="acme_hub_test_").delete()
    Group.objects.filter(name__startswith="acme_hub_test_").delete()

    opts.acme_group = Group.objects.create(
        name=f"acme_hub_test_{uuid.uuid4().hex[:8]}", kind="organization")
    opts.other_group = Group.objects.create(
        name=f"acme_hub_test_other_{uuid.uuid4().hex[:8]}", kind="organization")


def _allocation(opts, client_ref="client-a", domain="example.com"):
    from mojo.apps.dnsman.models import AcmeHubChallengeLease, AcmeHubDelegation

    group = opts.acme_group
    old = AcmeHubDelegation.objects.filter(
        owner_project_uuid=group.get_uuid(), client_ref=client_ref)
    AcmeHubChallengeLease.objects.filter(allocation__in=old).delete()
    old.delete()
    return AcmeHubDelegation.objects.create(
        owner_project_uuid=group.get_uuid(),
        client_ref=client_ref,
        domain_name=domain,
        source_name=f"_acme-challenge.{domain}",
        target_name=f"{uuid.uuid4().hex}.hub.example.net",
        group=group,
        allocation_zone_name="hub.example.net",
        allocation_zone_id="ZONE-HUB",
    )


@th.django_unit_test("ACME hub allocations are idempotent tombstones and targets never reuse")
def test_allocation_idempotency_and_target_non_reuse(opts):
    from mojo.apps.dnsman.models import AcmeHubDelegation
    from mojo.apps.dnsman.services import acme_hub

    zone = objict(name="hub.example.net", id="ZONE-HUB")
    with mock.patch.object(acme_hub, "_new_zone", return_value=zone), \
            mock.patch.object(acme_hub, "_stored_zone", return_value=zone), \
            mock.patch.object(acme_hub, "_audit"), \
            mock.patch.object(acme_hub.secrets, "token_hex", side_effect=["a" * 32, "b" * 32]):
        first = acme_hub.allocate(opts.acme_group, "client-a", "Example.COM.")
        retry = acme_hub.allocate(opts.acme_group, "client-a", "example.com")
        second = acme_hub.allocate(opts.acme_group, "client-b", "example.com")

    assert retry.pk == first.pk, "same project/client_ref/domain must return the original allocation"
    assert second.pk != first.pk, "a new client_ref must create a new allocation"
    assert second.target_name != first.target_name, "targets must never be reused for re-onboarding"
    assert AcmeHubDelegation.objects.count() == 2, "expected exactly two permanent tombstones"

    try:
        with mock.patch.object(acme_hub, "_audit"):
            acme_hub.allocate(opts.acme_group, "client-a", "different.example")
    except Exception as exc:
        assert "different domain" in str(exc), f"expected the immutable-ref refusal, got {exc}"
    else:
        assert False, "same client_ref with a different domain must be refused"


@th.django_unit_test("ACME hub allocation ownership is isolated by project UUID")
def test_cross_project_isolation(opts):
    from mojo.apps.dnsman.services import acme_hub

    _allocation(opts)
    try:
        acme_hub._allocation_for(opts.other_group, "client-a")
    except Exception as exc:
        assert "Unknown ACME delegation" in str(exc), \
            f"cross-project lookup should be a uniform unknown-refusal, got {exc}"
    else:
        assert False, "another project must not resolve the allocation"


@th.django_unit_test("ACME hub zone validation requires exact public Route53 authority")
def test_zone_safety(opts):
    from mojo.apps.dnsman.services import acme_hub

    public_zone = objict(
        id="ZONE-HUB", name="hub.example.net", private=False,
        name_servers=["ns-1.awsdns.example", "ns-2.awsdns.example"])
    public_cut = objict(
        zone="hub.example.net",
        nameservers=["ns-2.awsdns.example", "ns-1.awsdns.example"], error=None)
    with mock.patch.object(acme_hub.route53, "get_zone", return_value=public_zone), \
            mock.patch.object(acme_hub.probe, "find_zone_nameservers", return_value=public_cut):
        result = acme_hub._validate_zone("hub.example.net", "ZONE-HUB")
    assert result.id == "ZONE-HUB", "the exact public zone should validate"

    private_zone = objict(**dict(public_zone, private=True))
    with mock.patch.object(acme_hub.route53, "get_zone", return_value=private_zone):
        try:
            acme_hub._validate_zone("hub.example.net", "ZONE-HUB")
        except Exception as exc:
            assert "exact public zone" in str(exc), f"expected private-zone refusal, got {exc}"
        else:
            assert False, "a private Route53 zone must be refused"

    wrong_cut = objict(zone="example.net", nameservers=public_zone.name_servers, error=None)
    with mock.patch.object(acme_hub.route53, "get_zone", return_value=public_zone), \
            mock.patch.object(acme_hub.probe, "find_zone_nameservers", return_value=wrong_cut):
        try:
            acme_hub._validate_zone("hub.example.net", "ZONE-HUB")
        except Exception as exc:
            assert "public DNS cut" in str(exc), f"expected public-cut refusal, got {exc}"
        else:
            assert False, "a parent-zone authority answer must not validate the hub zone"


@th.django_unit_test("ACME hub parallel leases reconcile an exact additive union")
def test_publish_union_and_withdraw_without_cname(opts):
    from mojo.apps.dnsman.models import AcmeHubChallengeLease
    from mojo.apps.dnsman.services import acme_hub

    allocation = _allocation(opts)
    writes = []

    def fake_write(row, values):
        writes.append((row.pk, list(values)))

    proof = objict(ok=True, error=None)
    with mock.patch.object(acme_hub.probe, "verify_one_hop_cname", return_value=proof), \
            mock.patch.object(acme_hub, "_write_exact", fake_write), \
            mock.patch.object(acme_hub, "_audit"):
        first = acme_hub.publish(
            opts.acme_group, "client-a", "challenge-apex", ["digest-b", "digest-a"])
        second = acme_hub.publish(
            opts.acme_group, "client-a", "challenge-wildcard", ["digest-c"])

    assert first.active_value_count == 2, "first complete digest set should be active"
    assert second.active_value_count == 3, "parallel leases must reconcile their complete union"
    assert writes[-1][1] == ["digest-a", "digest-b", "digest-c"], \
        f"expected exact sorted union, got {writes[-1][1]}"

    # Withdrawal deliberately runs with no CNAME proof patch at all.  If the
    # service consulted the tenant CNAME, this test would attempt real DNS and fail.
    with mock.patch.object(acme_hub, "_write_exact", fake_write), \
            mock.patch.object(acme_hub, "_audit"):
        withdrawn = acme_hub.withdraw(
            opts.acme_group, "client-a", "challenge-apex")
    assert withdrawn.active_value_count == 1, "withdraw must retire only the named lease"
    assert writes[-1][1] == ["digest-c"], "remaining challenge must stay published"
    assert AcmeHubChallengeLease.objects.get(
        allocation=allocation, challenge_ref="challenge-apex").state == "withdrawn", \
        "the named lease should be durably retired"


@th.django_unit_test("ACME hub commits desired lease state before ambiguous provider failure")
def test_ambiguous_write_is_sweep_recoverable(opts):
    from mojo.apps.dnsman.models import AcmeHubChallengeLease
    from mojo.apps.dnsman.services import acme_hub

    allocation = _allocation(opts)
    proof = objict(ok=True, error=None)
    with mock.patch.object(acme_hub.probe, "verify_one_hop_cname", return_value=proof), \
            mock.patch.object(acme_hub, "_write_exact", side_effect=RuntimeError("ambiguous")), \
            mock.patch.object(acme_hub, "_audit"):
        try:
            acme_hub.publish(opts.acme_group, "client-a", "challenge-crash", ["digest-a"])
        except Exception as exc:
            assert getattr(exc, "status", None) == 503, f"provider failure should map to 503, got {exc}"
        else:
            assert False, "ambiguous provider failure must be loud"

    lease = AcmeHubChallengeLease.objects.get(
        allocation=allocation, challenge_ref="challenge-crash")
    assert lease.state == "pending", "desired lease must be committed before the network write"
    assert lease.reconciled_at is None, "failed reconciliation must remain visible to the sweeper"
    assert lease.record_values == ["digest-a"], "the durable desired value must survive the crash"

    recovered = []
    with mock.patch.object(acme_hub, "_write_exact", lambda row, values: recovered.append(list(values))), \
            mock.patch.object(acme_hub, "_audit"):
        result = acme_hub.sweep()
    lease.refresh_from_db()
    assert result.errors == 0 and result.reconciled == 1, f"sweeper should recover intent, got {result}"
    assert lease.state == "active", "recovered pending lease should become active"
    assert recovered == [["digest-a"]], f"sweeper reconciled the wrong desired set: {recovered}"


@th.django_unit_test("ACME hub reconciliation marks only leases present in its write snapshot")
def test_reconcile_snapshot_does_not_ack_late_lease(opts):
    from mojo.apps.dnsman.models import AcmeHubChallengeLease
    from mojo.apps.dnsman.services import acme_hub
    from mojo.helpers import dates

    allocation = _allocation(opts)
    original = AcmeHubChallengeLease.objects.create(
        allocation=allocation, challenge_ref="challenge-snapshot",
        record_values=["digest-a"], state="pending",
        expires_at=dates.add(seconds=300))
    late = []

    def write_with_crash_window(row, values):
        assert values == ["digest-a"], f"unexpected snapshot values: {values}"
        # Simulate state becoming durable after the snapshot but before the
        # provider acknowledgement. Public persist paths hold the same lock;
        # this direct insert pins the crash-window bookkeeping invariant.
        late.append(AcmeHubChallengeLease.objects.create(
            allocation=row, challenge_ref="challenge-late",
            record_values=["digest-b"], state="pending",
            expires_at=dates.add(seconds=300)))

    with mock.patch.object(acme_hub, "_write_exact", side_effect=write_with_crash_window), \
            mock.patch.object(acme_hub, "_audit"):
        acme_hub.reconcile(allocation)

    original.refresh_from_db()
    late[0].refresh_from_db()
    assert original.state == "active" and original.reconciled_at is not None, \
        "the lease included in the exact provider write should be acknowledged"
    assert late[0].state == "pending" and late[0].reconciled_at is None, \
        "a lease outside the write snapshot must remain pending for the sweeper"


@th.django_unit_test("ACME hub sweeper expires stale leases and clears their target")
def test_sweeper_expires_stale_lease(opts):
    from mojo.helpers import dates
    from mojo.apps.dnsman.models import AcmeHubChallengeLease
    from mojo.apps.dnsman.services import acme_hub

    allocation = _allocation(opts)
    lease = AcmeHubChallengeLease.objects.create(
        allocation=allocation, challenge_ref="challenge-expired",
        record_values=["digest-expired"], state="active",
        expires_at=dates.subtract(seconds=1), reconciled_at=dates.utcnow())
    writes = []
    with mock.patch.object(acme_hub, "_write_exact", lambda row, values: writes.append(list(values))), \
            mock.patch.object(acme_hub, "_audit"):
        result = acme_hub.sweep()
    lease.refresh_from_db()
    assert result.expired == 1, f"expected one expired lease, got {result}"
    assert lease.state == "expired", "stale lease must be retired durably"
    assert writes == [[]], f"an allocation with no live leases must clear its exact RRset, got {writes}"


@th.django_unit_test("ACME hub auth floor reads only the underlying active project ApiKey")
def test_auth_floor(opts):
    from mojo import errors as me
    from mojo.apps.account.models import ApiKey
    from mojo.apps.dnsman.rest.acme_hub import (
        _require_federation_key, on_acme_delegation)

    request = SimpleNamespace(api_key=None, user=SimpleNamespace(is_authenticated=True))
    try:
        on_acme_delegation(request)
    except me.PermissionDeniedException:
        pass
    else:
        assert False, "machine authentication must run before request-body validation"

    try:
        _require_federation_key(request)
    except me.PermissionDeniedException:
        pass
    else:
        assert False, "a JWT/user session must not reach the federation hub"

    denied_key, unused = ApiKey.create_for_group(
        opts.acme_group, "acme_hub_test_denied", permissions={})
    request = SimpleNamespace(api_key=denied_key, user=denied_key, group=opts.acme_group)
    try:
        _require_federation_key(request)
    except me.PermissionDeniedException:
        pass
    else:
        assert False, "a permissionless key must be denied"

    allowed_key, unused = ApiKey.create_for_group(
        opts.acme_group, "acme_hub_test_allowed",
        permissions={"dnsman_acme_federation": True})
    request = SimpleNamespace(api_key=allowed_key, user=allowed_key, group=opts.other_group)
    assert _require_federation_key(request).pk == allowed_key.pk, \
        "the protected underlying key permission should authorize"
    assert request.group.pk == opts.acme_group.pk, \
        "authorization must pin project identity to the key's group"

    opts.acme_group.is_active = False
    opts.acme_group.save()
    try:
        _require_federation_key(request)
    except me.PermissionDeniedException:
        pass
    else:
        assert False, "a key from an inactive project must be denied"
    opts.acme_group.is_active = True
    opts.acme_group.save()


@th.django_unit_test("ACME hub response allocation shape omits zone ids and challenge values")
def test_safe_response_shape(opts):
    from mojo.apps.dnsman.rest.acme_hub import _allocation_payload

    allocation = _allocation(opts)
    payload = _allocation_payload(allocation)
    assert set(payload) == {"client_ref", "domain", "source", "target"}, \
        f"response should carry only bounded allocation identifiers, got {payload.keys()}"
    assert allocation.allocation_zone_id not in str(payload), "hosted zone id must never be echoed"


@th.django_unit_setup()
def cleanup_acme_hub(opts):
    from mojo.apps.account.models import ApiKey, Group
    from mojo.apps.dnsman.models import AcmeHubChallengeLease, AcmeHubDelegation

    AcmeHubChallengeLease.objects.all().delete()
    AcmeHubDelegation.objects.all().delete()
    ApiKey.objects.filter(name__startswith="acme_hub_test_").delete()
    Group.objects.filter(name__startswith="acme_hub_test_").delete()
