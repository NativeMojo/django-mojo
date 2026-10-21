"""Optional ACME delegation hub: isolation, leases, reconciliation and auth."""

TESTIT_TIER = "extended"

import uuid
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest import mock

from objict import objict
from testit import helpers as th


def _shared_state():
    return SimpleNamespace(mode="enabled")


@th.django_unit_setup()
def setup_acme_hub(opts):
    from mojo.apps.account.models import ApiKey, Group
    from mojo.apps.dnsman.models import AcmeHubChallengeLease, AcmeHubDelegation, AcmeHubParticipant

    AcmeHubChallengeLease.objects.all().delete()
    AcmeHubParticipant.objects.all().delete()
    AcmeHubDelegation.objects.all().delete()
    ApiKey.objects.filter(name__startswith="acme_hub_test_").delete()
    Group.objects.filter(name__startswith="acme_hub_test_").delete()

    opts.acme_group = Group.objects.create(
        name=f"acme_hub_test_{uuid.uuid4().hex[:8]}", kind="organization")
    opts.other_group = Group.objects.create(
        name=f"acme_hub_test_other_{uuid.uuid4().hex[:8]}", kind="organization")


def _allocation(opts, client_ref="client-a", domain="example.com"):
    from mojo.apps.dnsman.models import AcmeHubChallengeLease, AcmeHubDelegation, AcmeHubParticipant

    group = opts.acme_group
    old = AcmeHubDelegation.objects.filter(
        owner_project_uuid=group.get_uuid(), client_ref=client_ref)
    AcmeHubChallengeLease.objects.filter(allocation__in=old).delete()
    AcmeHubParticipant.objects.filter(allocation__in=old).delete()
    old.delete()
    allocation = AcmeHubDelegation.objects.create(
        owner_project_uuid=group.get_uuid(),
        client_ref=client_ref,
        domain_name=domain,
        source_name=f"_acme-challenge.{domain}",
        target_name=f"{uuid.uuid4().hex}.hub.example.net",
        group=group,
        allocation_zone_name="hub.example.net",
        allocation_zone_id="ZONE-HUB",
    )
    AcmeHubParticipant.objects.create(allocation=allocation, consumer_project_uuid=group.get_uuid(),
                                      client_ref=client_ref, group=group, role="owner")
    return allocation


HUB_NAMESERVERS = ["ns-1.awsdns.example", "ns-2.awsdns.example"]


class FakeRoute53:
    """Route53 stand-in threaded through the hub's real write path.

    Injected with ``route53_client=`` rather than patched, so the whole
    ``_stored_zone`` -> ``_validate_zone`` -> write chain runs for real.
    ``get_change`` deliberately raises: the hub must never block an HTTP reply
    on Route53 convergence.
    """

    def __init__(self, zone_id="ZONE-HUB", zone_name="hub.example.net"):
        self.zone_id = zone_id
        self.zone_name = zone_name
        self.calls = []
        self.records = {}

    def find_zone_id(self, name):
        self.calls.append(("find_zone_id", name, []))
        return self.zone_id

    def get_zone(self, zone_id):
        self.calls.append(("get_zone", zone_id, []))
        return objict(
            id=self.zone_id, name=self.zone_name, private=False,
            name_servers=list(HUB_NAMESERVERS))

    def upsert_record(self, zone_id, rtype, name, values, ttl=None,
                      zone_name=None, comment=None):
        self.calls.append(("upsert", name, sorted(values)))
        self.records[(rtype, name)] = objict(
            type=rtype, name=name, record_values=list(values), ttl=ttl or 60)
        return "CHANGE-UPSERT"

    def list_records(self, zone_id):
        self.calls.append(("list_records", zone_id, []))
        return list(self.records.values())

    def delete_record(self, zone_id, rtype, name, values=None, ttl=None,
                      zone_name=None, comment=None):
        self.calls.append(("delete", name, sorted(values or [])))
        self.records.pop((rtype, name), None)
        return "CHANGE-DELETE"

    def get_change(self, change_id):
        raise AssertionError(
            f"the hub must not poll Route53 for change {change_id}")

    @property
    def writes(self):
        return [call for call in self.calls if call[0] in ("upsert", "delete")]


class FakeProbe:
    """`probe` stand-in injected with ``dns_probe=``.

    ``txt``/``error`` drive ``query_txt``; ``raises=True`` makes any TXT lookup
    an assertion failure, which is how the write path proves it no longer waits
    on authoritative visibility.
    """

    def __init__(self, txt=None, error=None, raises=False,
                 zone_name="hub.example.net"):
        self.txt = list(txt or [])
        self.error = error
        self.raises = raises
        self.zone_name = zone_name
        self.txt_queries = []

    def normalize_name(self, value):
        from mojo.helpers.dns import probe

        return probe.normalize_name(value)

    def find_zone_nameservers(self, name, **kwargs):
        return objict(
            zone=self.zone_name, nameservers=list(HUB_NAMESERVERS), error=None)

    def verify_one_hop_cname(self, source, target, **kwargs):
        return objict(ok=True, error=None)

    def query_txt(self, name, **kwargs):
        self.txt_queries.append(name)
        if self.raises:
            raise AssertionError(
                f"the hub write path must not poll authoritative DNS for {name}")
        return objict(
            txt_values=list(self.txt), zone=self.zone_name,
            nameservers=list(HUB_NAMESERVERS), error=self.error)


@th.django_unit_test("ACME hub allocations are idempotent tombstones and targets never reuse")
def test_allocation_idempotency_and_target_non_reuse(opts):
    from mojo.apps.dnsman.models import AcmeHubDelegation
    from mojo.apps.dnsman.services import acme_hub

    zone = objict(name="hub.example.net", id="ZONE-HUB")
    fake_secrets = SimpleNamespace(
        token_hex=mock.Mock(side_effect=["a" * 32, "b" * 32]))
    with mock.patch.object(acme_hub, "_new_zone", return_value=zone), \
            mock.patch.object(acme_hub, "_stored_zone", return_value=zone), \
            mock.patch.object(acme_hub, "_audit"), \
            mock.patch.object(acme_hub, "secrets", fake_secrets):
        first = acme_hub.allocate(opts.acme_group, "client-a", "Example.COM.", state_reader=_shared_state)
        retry = acme_hub.allocate(opts.acme_group, "client-a", "example.com", state_reader=_shared_state)
        second = acme_hub.allocate(opts.acme_group, "client-b", "example.com", state_reader=_shared_state)

    assert retry.pk == first.pk, "same project/client_ref/domain must return the original allocation"
    assert second.pk != first.pk, "a new client_ref must create a new allocation"
    assert second.target_name != first.target_name, "targets must never be reused for re-onboarding"
    assert AcmeHubDelegation.objects.count() == 2, "expected exactly two permanent tombstones"

    try:
        with mock.patch.object(acme_hub, "_audit"):
            acme_hub.allocate(opts.acme_group, "client-a", "different.example", state_reader=_shared_state)
    except Exception as exc:
        assert "different domain" in str(exc), f"expected the immutable-ref refusal, got {exc}"
    else:
        assert False, "same client_ref with a different domain must be refused"


@th.tier("core")
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

    def fake_write(row, values, **kwargs):
        writes.append((row.pk, list(values)))

    proof = objict(ok=True, error=None)
    with mock.patch.object(acme_hub.probe, "verify_one_hop_cname", return_value=proof), \
            mock.patch.object(acme_hub, "_write_exact", fake_write), \
            mock.patch.object(acme_hub, "_audit"):
        first = acme_hub.publish(
            opts.acme_group, "client-a", "challenge-apex", ["digest-b", "digest-a"], state_reader=_shared_state)
        second = acme_hub.publish(
            opts.acme_group, "client-a", "challenge-wildcard", ["digest-c"], state_reader=_shared_state)

    assert first.active_value_count == 2, "first complete digest set should be active"
    assert second.active_value_count == 3, "parallel leases must reconcile their complete union"
    assert writes[-1][1] == ["digest-a", "digest-b", "digest-c"], \
        f"expected exact sorted union, got {writes[-1][1]}"

    # Withdrawal deliberately runs with no CNAME proof patch at all.  If the
    # service consulted the tenant CNAME, this test would attempt real DNS and fail.
    with mock.patch.object(acme_hub, "_write_exact", fake_write), \
            mock.patch.object(acme_hub, "_audit"):
        withdrawn = acme_hub.withdraw(
            opts.acme_group, "client-a", "challenge-apex", state_reader=_shared_state)
    assert withdrawn.active_value_count == 1, "withdraw must retire only the named lease"
    assert writes[-1][1] == ["digest-c"], "remaining challenge must stay published"
    assert AcmeHubChallengeLease.objects.get(
        allocation=allocation, participant=_owner(allocation), challenge_ref="challenge-apex").state == "withdrawn", \
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
            acme_hub.publish(opts.acme_group, "client-a", "challenge-crash", ["digest-a"], state_reader=_shared_state)
        except Exception as exc:
            assert getattr(exc, "status", None) == 503, f"provider failure should map to 503, got {exc}"
        else:
            assert False, "ambiguous provider failure must be loud"

    lease = AcmeHubChallengeLease.objects.get(
        allocation=allocation, participant=_owner(allocation), challenge_ref="challenge-crash")
    assert lease.state == "pending", "desired lease must be committed before the network write"
    assert lease.reconciled_at is None, "failed reconciliation must remain visible to the sweeper"
    assert lease.record_values == ["digest-a"], "the durable desired value must survive the crash"

    recovered = []
    # The sweeper confirms before it writes, so it needs an injected probe:
    # without one it would do a live lookup of the opaque target, and a
    # deliberate mismatch is what keeps the recovery write on the tested path.
    with mock.patch.object(acme_hub, "_write_exact", lambda row, values, **kwargs: recovered.append(list(values))), \
            mock.patch.object(acme_hub, "_audit"):
        result = acme_hub.sweep(dns_probe=FakeProbe(txt=["digest-stale"]), state_reader=_shared_state)
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
        allocation=allocation, participant=_owner(allocation), challenge_ref="challenge-snapshot",
        record_values=["digest-a"], state="pending",
        expires_at=dates.add(seconds=300))
    late = []

    def write_with_crash_window(row, values, **kwargs):
        assert values == ["digest-a"], f"unexpected snapshot values: {values}"
        # Simulate state becoming durable after the snapshot but before the
        # provider acknowledgement. Public persist paths hold the same lock;
        # this direct insert pins the crash-window bookkeeping invariant.
        late.append(AcmeHubChallengeLease.objects.create(
            allocation=row, participant=_owner(row), challenge_ref="challenge-late",
            record_values=["digest-b"], state="pending",
            expires_at=dates.add(seconds=300)))

    with mock.patch.object(acme_hub, "_write_exact", side_effect=write_with_crash_window), \
            mock.patch.object(acme_hub, "_audit"):
        # A deliberate mismatch: reconcile() confirms first, and a matching
        # probe would skip the write entirely and leave this invariant untested.
        acme_hub.reconcile(allocation, dns_probe=FakeProbe(txt=["digest-mismatch"]), state_reader=_shared_state)

    original.refresh_from_db()
    late[0].refresh_from_db()
    assert original.state == "active", \
        "the lease included in the exact provider write should be acknowledged"
    assert original.reconciled_at is None, \
        "a submitted write is not a confirmed one — reconciled_at belongs to a probe match"
    assert late[0].state == "pending" and late[0].reconciled_at is None, \
        "a lease outside the write snapshot must remain pending for the sweeper"


@th.django_unit_test("ACME hub sweeper expires stale leases and clears their target")
def test_sweeper_expires_stale_lease(opts):
    from mojo.helpers import dates
    from mojo.apps.dnsman.models import AcmeHubChallengeLease
    from mojo.apps.dnsman.services import acme_hub

    allocation = _allocation(opts)
    lease = AcmeHubChallengeLease.objects.create(
        allocation=allocation, participant=_owner(allocation), challenge_ref="challenge-expired",
        record_values=["digest-expired"], state="active",
        expires_at=dates.subtract(seconds=1), reconciled_at=dates.utcnow())
    writes = []
    # The probe must report a stale NON-EMPTY value.  An empty answer is
    # exactly what an already-cleared target returns (NXDOMAIN -> error=None,
    # no values), which the sweeper correctly confirms as a match and skips —
    # and this test would then assert nothing at all.
    with mock.patch.object(acme_hub, "_write_exact", lambda row, values, **kwargs: writes.append(list(values))), \
            mock.patch.object(acme_hub, "_audit"):
        result = acme_hub.sweep(dns_probe=FakeProbe(txt=["digest-expired"]), state_reader=_shared_state)
    lease.refresh_from_db()
    assert result.expired == 1, f"expected one expired lease, got {result}"
    assert lease.state == "expired", "stale lease must be retired durably"
    assert writes == [[]], f"an allocation with no live leases must clear its exact RRset, got {writes}"


@th.tier("bug")
@th.django_unit_test("ACME hub publish replies without waiting for Route53 propagation")
def test_publish_replies_without_waiting_for_propagation(opts):
    from mojo.apps.dnsman.models import AcmeHubChallengeLease
    from mojo.apps.dnsman.services import acme_hub

    allocation = _allocation(opts)
    r53 = FakeRoute53()
    dns = FakeProbe(raises=True)

    result = acme_hub.publish(
        opts.acme_group, "client-a", "challenge-apex", ["digest-b", "digest-a"],
        route53_client=r53, dns_probe=dns, state_reader=_shared_state)

    assert result.active_value_count == 2, \
        f"the complete digest set must be reported active, got {result.active_value_count}"
    assert r53.writes == [
        ("upsert", allocation.target_name, ["digest-a", "digest-b"])], \
        f"expected exactly one exact-union upsert at the target, got {r53.writes}"
    lease = AcmeHubChallengeLease.objects.get(
        allocation=allocation, participant=_owner(allocation), challenge_ref="challenge-apex")
    assert lease.state == "active", \
        f"an accepted challenge write must leave the lease active, got {lease.state}"
    assert lease.reconciled_at is None, \
        "reconciled_at means a probe confirmed the RRset, not that a write was submitted"


@th.tier("bug")
@th.django_unit_test("ACME hub withdraw replies without waiting for Route53 propagation")
def test_withdraw_replies_without_waiting_for_propagation(opts):
    from mojo.apps.dnsman.models import AcmeHubChallengeLease
    from mojo.apps.dnsman.services import acme_hub

    allocation = _allocation(opts)
    r53 = FakeRoute53()
    dns = FakeProbe(raises=True)

    acme_hub.publish(
        opts.acme_group, "client-a", "challenge-apex", ["digest-b", "digest-a"],
        route53_client=r53, dns_probe=dns, state_reader=_shared_state)
    result = acme_hub.withdraw(
        opts.acme_group, "client-a", "challenge-apex",
        route53_client=r53, dns_probe=dns, state_reader=_shared_state)

    assert result.active_value_count == 0, \
        f"the last lease's withdrawal must empty the RRset, got {result.active_value_count}"
    assert ("delete", allocation.target_name, ["digest-a", "digest-b"]) in r53.writes, \
        f"withdraw must issue the exact delete, got {r53.writes}"
    lease = AcmeHubChallengeLease.objects.get(
        allocation=allocation, participant=_owner(allocation), challenge_ref="challenge-apex")
    assert lease.state == "withdrawn", \
        f"the named lease must be durably retired, got {lease.state}"
    assert lease.reconciled_at is None, \
        "a submitted delete is not a confirmed one; the sweeper owns reconciled_at"


@th.django_unit_test("ACME hub sweep confirms an already-visible RRset without rewriting it")
def test_sweep_confirms_without_writing_when_already_visible(opts):
    from mojo.helpers import dates
    from mojo.apps.dnsman.models import AcmeHubChallengeLease
    from mojo.apps.dnsman.services import acme_hub

    allocation = _allocation(opts)
    lease = AcmeHubChallengeLease.objects.create(
        allocation=allocation, participant=_owner(allocation), challenge_ref="challenge-visible",
        record_values=["digest-a"], state="pending",
        expires_at=dates.add(seconds=300))
    r53 = FakeRoute53()
    dns = FakeProbe(txt=["digest-a"])

    result = acme_hub.sweep(route53_client=r53, dns_probe=dns, state_reader=_shared_state)

    lease.refresh_from_db()
    assert result.errors == 0, f"a confirming sweep must not error, got {result}"
    assert lease.state == "active", \
        f"a confirmed pending lease should become active, got {lease.state}"
    assert lease.reconciled_at is not None, \
        "an exact probe match is what stamps reconciled_at"
    assert r53.calls == [], \
        f"a confirmed RRset must cost no Route53 call at all, got {r53.calls}"


@th.django_unit_test("ACME hub sweep rewrites a mismatched RRset and stays unconfirmed")
def test_sweep_rewrites_and_stays_unconfirmed_on_mismatch(opts):
    from mojo.helpers import dates
    from mojo.apps.dnsman.models import AcmeHubChallengeLease
    from mojo.apps.dnsman.services import acme_hub

    allocation = _allocation(opts)
    lease = AcmeHubChallengeLease.objects.create(
        allocation=allocation, participant=_owner(allocation), challenge_ref="challenge-stale",
        record_values=["digest-a"], state="pending",
        expires_at=dates.add(seconds=300))
    r53 = FakeRoute53()
    dns = FakeProbe(txt=["digest-stale"])

    result = acme_hub.sweep(route53_client=r53, dns_probe=dns, state_reader=_shared_state)

    lease.refresh_from_db()
    assert result.errors == 0, f"a rewriting sweep must not error, got {result}"
    assert r53.writes == [
        ("upsert", allocation.target_name, ["digest-a"])], \
        f"a mismatch must rewrite the exact desired union, got {r53.writes}"
    assert lease.reconciled_at is None, \
        "a rewrite is not a confirmation; the next sweep must probe again"


@th.django_unit_test("ACME hub sweep never writes on an unreadable probe")
def test_sweep_probe_error_does_not_write(opts):
    from mojo.helpers import dates
    from mojo.apps.dnsman.models import AcmeHubChallengeLease
    from mojo.apps.dnsman.services import acme_hub

    allocation = _allocation(opts)
    lease = AcmeHubChallengeLease.objects.create(
        allocation=allocation, participant=_owner(allocation), challenge_ref="challenge-unknown",
        record_values=["digest-a"], state="pending",
        expires_at=dates.add(seconds=300))
    r53 = FakeRoute53()
    dns = FakeProbe(txt=[], error="all nameservers failed to answer")

    result = acme_hub.sweep(route53_client=r53, dns_probe=dns, state_reader=_shared_state)

    lease.refresh_from_db()
    assert result.errors == 0, f"an unreadable probe is not a sweep error, got {result}"
    assert r53.calls == [], \
        f"an unknown verdict must not write — that is a fleet-wide write storm, got {r53.calls}"
    assert lease.state == "pending", \
        f"nothing was written, so nothing may be acknowledged, got {lease.state}"
    assert lease.reconciled_at is None, \
        "an unreadable probe must leave the allocation for the next pass"


@th.tier("core")
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


def _owner(allocation):
    from mojo.apps.dnsman.models import AcmeHubParticipant
    return AcmeHubParticipant.objects.get(allocation=allocation, role="owner")


def _request(group, permissions=None):
    from mojo.apps.account.models import ApiKey
    key, unused = ApiKey.create_for_group(
        group, "acme_hub_test_" + uuid.uuid4().hex,
        permissions={"dnsman_acme_federation": True} if permissions is None else permissions)
    return SimpleNamespace(api_key=key, user=key, group=group)


def _descriptor(allocation, group, ref="consumer-a"):
    return dict(version=1, consumer_project_uuid=group.get_uuid(), client_ref=ref,
                domain=allocation.domain_name, allocation_ref=allocation.target_name)


def _error(func, status, token=None):
    try:
        func()
    except Exception as exc:
        assert getattr(exc, "status", None) == status, f"expected HTTP {status}, got {type(exc).__name__}: {exc}"
        if token:
            assert str(exc) == token, f"expected safe token {token}, got {exc}"
        return exc
    assert False, f"expected HTTP {status} refusal"


class AliasProbe(FakeProbe):
    def __init__(self, allocation, source_targets=None, target_targets=None, **kwargs):
        super().__init__(**kwargs)
        self.allocation = allocation
        self.source_targets = [allocation.target_name] if source_targets is None else source_targets
        self.target_targets = [] if target_targets is None else target_targets
        self.cname_calls = []

    def query_cname(self, name):
        self.cname_calls.append(name)
        return objict(targets=self.source_targets if name == self.allocation.source_name else self.target_targets,
                      error=self.error)


@th.django_unit_test("Shared ACME grants are explicit, permanent and join never allocates")
def test_shared_grant_join_and_canonical_refs(opts):
    from mojo.apps.dnsman.models import AcmeHubDelegation, AcmeHubParticipant
    from mojo.apps.dnsman.services import acme_hub as hub
    allocation = _allocation(opts)
    controller, consumer = _request(opts.acme_group), _request(opts.other_group)
    descriptor = _descriptor(allocation, opts.other_group)
    dns, r53 = AliasProbe(allocation), FakeRoute53()
    seams = dict(dns_probe=dns, route53_client=r53)
    initial_count = AcmeHubDelegation.objects.count()
    assert hub.lookup_participants(consumer, allocation.domain_name, state_reader=_shared_state)["items"] == [], "public CNAME knowledge must grant nothing"
    _error(lambda: hub.join(consumer, allocation.domain_name, "consumer-a", allocation.target_name, **seams, state_reader=_shared_state),
           404, "acme_not_authorized")
    first = hub.grant(controller, descriptor, **seams, state_reader=_shared_state)
    retry = hub.grant(controller, descriptor, **seams, state_reader=_shared_state)
    assert first == retry and first["role"] == "trusted", "exact grant retry must return one permanent binding"
    joined = hub.join(consumer, allocation.domain_name, "consumer-a", allocation.target_name, **seams, state_reader=_shared_state)
    assert set(joined) == {"client_ref", "domain", "source", "target"}, "join must retain exact allocation wire shape"
    assert joined["client_ref"] == "consumer-a" and joined["target"] == allocation.target_name, "join must echo consumer ref and existing target"
    assert AcmeHubDelegation.objects.count() == initial_count, "lookup/grant/join must not mint any competing target"
    _error(lambda: hub.allocate(opts.other_group, "consumer-a", allocation.domain_name, **seams, state_reader=_shared_state),
           409, "acme_reference_conflict")
    _error(lambda: hub.grant(controller, dict(descriptor, domain="wrong.example"), **seams, state_reader=_shared_state), 409, "acme_reference_conflict")
    owner = _owner(allocation)
    _error(lambda: hub.revoke(controller, allocation.target_name, str(owner.participant_ref), **seams, state_reader=_shared_state), 403)
    revoked = hub.revoke(controller, allocation.target_name, first["participant_ref"], **seams, state_reader=_shared_state)
    assert revoked["trust_state"] == "revoked", "revocation must persist as a tombstone"
    _error(lambda: hub.grant(controller, descriptor, **seams, state_reader=_shared_state), 409, "acme_reference_conflict")
    _error(lambda: hub.join(consumer, allocation.domain_name, "consumer-a", allocation.target_name, **seams, state_reader=_shared_state), 403, "acme_trust_revoked")
    fresh = hub.grant(controller, dict(descriptor, client_ref="consumer-fresh"), **seams, state_reader=_shared_state)
    assert fresh["participant_ref"] != first["participant_ref"], "regrant requires a fresh isolated namespace"
    assert AcmeHubParticipant.objects.filter(allocation=allocation).count() == 3, "owner, revoked and fresh binding must survive"


@th.django_unit_test("Shared ACME controller checks exclude forged/acting/group authority")
def test_shared_controller_authority(opts):
    from mojo.apps.account.models import User
    from mojo.apps.dnsman.services import acme_hub as hub
    import importlib
    rest = importlib.import_module("mojo.apps.dnsman.rest.acme_hub")
    allocation = _allocation(opts)
    owner, foreign = _request(opts.acme_group), _request(opts.other_group)
    hub.require_controller(owner, allocation)
    _error(lambda: hub.require_controller(foreign, allocation), 403, "acme_not_authorized")
    root = User.objects.create(username="acme_hub_test_root_" + uuid.uuid4().hex, is_superuser=True, is_active=True)
    person = User.objects.create(username="acme_hub_test_person_" + uuid.uuid4().hex,
                                 permissions={"manage_dns": True, "security": True, "manage_sites": True, "manage_groups": True})
    try:
        human = SimpleNamespace(api_key=None, user=root)
        zone_resolver = lambda **kwargs: objict(name="hub.example.net", id="ZONE-HUB")
        owner_cap = hub.capabilities(owner, zone_resolver=zone_resolver, state_reader=_shared_state)
        assert owner_cap == dict(version=2, shared_validation=True, consumer_project_uuid=opts.acme_group.get_uuid(),
                                 can_consume=True, can_manage_trust=True, trust_scope="apex_and_wildcard"), "protected capabilities must identify original project"
        human_cap = hub.capabilities(human, zone_resolver=zone_resolver, state_reader=_shared_state)
        assert human_cap["can_manage_trust"] and not human_cap["can_consume"] and human_cap["consumer_project_uuid"] is None, "superuser may control but never implicitly consume"
        ordinary_cap = hub.capabilities(SimpleNamespace(api_key=None, user=person), zone_resolver=zone_resolver, state_reader=_shared_state)
        assert not ordinary_cap["can_manage_trust"] and not ordinary_cap["can_consume"], "ordinary authenticated humans must get false capability flags"
        _error(lambda: hub.capabilities(owner, state_reader=_shared_state), 503, "acme_hub_unavailable")
        assert hub.require_controller(human, allocation).pk == root.pk, "genuine platform superuser must control existing allocations"
        denied = _request(opts.other_group, permissions={"manage_dns": True, "manage_groups": True})
        denied.user = root
        for request in [denied, SimpleNamespace(api_key=None, user=person),
                        SimpleNamespace(api_key=None, user=root, group_token=SimpleNamespace(group=opts.acme_group))]:
            _error(lambda: hub.require_controller(request, allocation), 403, "acme_not_authorized")
            # No DATA: auth MUST fail before any body work/rate decorator.
            _error(lambda: rest.on_acme_trust_grant(request), 403, "acme_not_authorized")
        foreign.user, foreign.group = root, opts.acme_group
        _error(lambda: hub.require_controller(foreign, allocation), 403, "acme_not_authorized")
        dns = AliasProbe(allocation)
        for ref in ("foreign-ref", "missing-ref"):
            _error(lambda: hub.participant_status(foreign, ref, dns_probe=dns, state_reader=_shared_state), 404, "acme_not_authorized")
        assert dns.cname_calls == [], "foreign and missing refs must fail before DNS observation"
        _error(lambda: hub.resolve_recipient(foreign, allocation.target_name, "bad-uuid", state_reader=_shared_state), 403, "acme_not_authorized")
        _error(lambda: hub.resolve_recipient(foreign, "missing.hub.example.net", "bad-uuid", state_reader=_shared_state), 403, "acme_not_authorized")
    finally:
        User.objects.filter(pk__in=[root.pk, person.pk]).delete()


@th.django_unit_test("Shared ACME lookup pages are own-only and alias observations are bounded")
def test_shared_lookup_and_alias(opts):
    from mojo.apps.dnsman.models import AcmeHubParticipant
    from mojo.apps.dnsman.services import acme_hub as hub
    allocation = _allocation(opts)
    controller, consumer = _request(opts.acme_group), _request(opts.other_group)
    for index in range(23):
        AcmeHubParticipant.objects.create(allocation=allocation, group=opts.other_group, role="trusted",
                                          consumer_project_uuid=opts.other_group.get_uuid(), client_ref=f"page-{index}")
    first = hub.lookup_participants(consumer, allocation.domain_name, state_reader=_shared_state)
    assert len(first["items"]) == 20 and first["next_cursor"], "page must stop at twenty with a bounded cursor"
    second = hub.lookup_participants(consumer, allocation.domain_name, cursor=first["next_cursor"], state_reader=_shared_state)
    assert len(second["items"]) == 3 and second["next_cursor"] is None, "next page must contain only remaining own participants"
    assert len(hub.lookup_participants(controller, allocation.domain_name, state_reader=_shared_state)["items"]) == 1, "owner must not enumerate consumer participants through lookup"
    for cursor in ("0", "-1", "1 OR 1=1", "9" * 30, 1, "01"):
        _error(lambda: hub.lookup_participants(consumer, allocation.domain_name, cursor=cursor, state_reader=_shared_state), 400, "acme_invalid_request")
    assert hub.lookup_participants(consumer, allocation.domain_name, cursor="9223372036854775807", state_reader=_shared_state)["items"] == [], "forged valid cursor must only narrow"
    for dns, state, observed, hop in [
            (AliasProbe(allocation), "match", allocation.target_name, True),
            (AliasProbe(allocation, source_targets=[]), "mismatch", None, None),
            (AliasProbe(allocation, source_targets=[allocation.target_name, "extra.example"]), "mismatch", None, None),
            (AliasProbe(allocation, target_targets=["chain.example"]), "mismatch", allocation.target_name, False),
            (AliasProbe(allocation, error="private resolver error"), "lookup_failed", None, None)]:
        status = hub.participant_status(consumer, "page-0", dns_probe=dns, state_reader=_shared_state)
        alias = status["alias"]
        assert set(alias) == {"state", "observed_target", "one_hop", "checked_at"}, "alias must expose only its fixed DTO"
        assert (alias["state"], alias["observed_target"], alias["one_hop"]) == (state, observed, hop), f"incorrect alias observation {alias}"
        assert "private resolver" not in str(status), "DNS raw errors must never escape"
    assert hub.list_allocations(consumer, allocation.domain_name, state_reader=_shared_state)["items"] == [], "consumer controller eligibility is not authority on this allocation"
    assert len(hub.list_allocations(controller, allocation.domain_name, state_reader=_shared_state)["items"]) == 1, "owner must resolve its allocation"


@th.django_unit_test("Shared ACME participants isolate equal challenge refs through revoke, expiry and retries")
def test_shared_union_revocation_and_cleanup(opts):
    from mojo.apps.dnsman.models import AcmeHubChallengeLease, AcmeHubParticipant
    from mojo.apps.dnsman.services import acme_hub as hub
    from mojo.helpers import dates
    allocation = _allocation(opts)
    owner, consumer = _request(opts.acme_group), _request(opts.other_group)
    dns, r53 = AliasProbe(allocation), FakeRoute53()
    seams = dict(dns_probe=dns, route53_client=r53)
    first = hub.grant(owner, _descriptor(allocation, opts.other_group, "shared-1"), **seams, state_reader=_shared_state)
    hub.grant(owner, _descriptor(allocation, opts.other_group, "shared-2"), **seams, state_reader=_shared_state)
    for group, request, ref, values in [
            (opts.acme_group, owner, "client-a", ["owner", "same"]),
            (opts.other_group, consumer, "shared-1", ["first", "same"]),
            (opts.other_group, consumer, "shared-2", ["second", "same"])]:
        hub.publish(group, ref, "equal-ref", values, request=request, **seams, state_reader=_shared_state)
    assert r53.writes[-1][2] == ["first", "owner", "same", "second"], "provider must get ONE deduplicated union across participants"
    _error(lambda: hub.publish(opts.other_group, "shared-1", "equal-ref", ["changed"], request=consumer, **seams, state_reader=_shared_state), 400)
    with mock.patch.object(r53, "upsert_record", side_effect=RuntimeError("ambiguous publish")):
        _error(lambda: hub.publish(opts.other_group, "shared-1", "ambiguous", ["extra"], request=consumer, **seams, state_reader=_shared_state), 503)
    hub.publish(opts.other_group, "shared-1", "ambiguous", ["extra"], request=consumer, **seams, state_reader=_shared_state)
    assert r53.writes[-1][2] == ["extra", "first", "owner", "same", "second"], "ambiguous publish retry must preserve every participant's intent"
    hub.withdraw(opts.other_group, "shared-1", "ambiguous", request=consumer, **seams, state_reader=_shared_state)
    with mock.patch.object(r53, "upsert_record", side_effect=RuntimeError("ambiguous")):
        _error(lambda: hub.revoke(owner, allocation.target_name, first["participant_ref"], **seams, state_reader=_shared_state), 503, "acme_hub_unavailable")
    row = AcmeHubParticipant.objects.get(participant_ref=first["participant_ref"])
    assert row.revoked_at is not None, "provider failure must not roll back revocation"
    lease = AcmeHubChallengeLease.objects.get(participant=row, challenge_ref="equal-ref")
    assert lease.state == "withdrawn" and lease.reconciled_at is None, "revocation must retire only its durable leases before provider call"
    _error(lambda: hub.publish(opts.other_group, "shared-1", "late", ["late"], request=consumer, **seams, state_reader=_shared_state), 403, "acme_trust_revoked")
    recovered = hub.sweep(route53_client=r53, dns_probe=AliasProbe(allocation, txt=["stale"]), state_reader=_shared_state)
    assert recovered.errors == 0 and recovered.reconciled == 1, f"sweep must repair durable revocation cleanup: {recovered}"
    hub.revoke(owner, allocation.target_name, first["participant_ref"], **seams, state_reader=_shared_state)
    assert r53.writes[-1][2] == ["owner", "same", "second"], "repeat revoke must repair owed cleanup while retaining siblings"
    rotated = _request(opts.other_group)
    hub.withdraw(opts.other_group, "shared-1", "equal-ref", request=rotated, **seams, state_reader=_shared_state)
    assert r53.writes[-1][2] == ["owner", "same", "second"], "rotated authorized key must clean revoked namespace only"
    AcmeHubChallengeLease.objects.filter(participant__client_ref="shared-2").update(expires_at=dates.subtract(seconds=1))
    hub.publish(opts.acme_group, "client-a", "new-owner", ["new"], request=owner, **seams, state_reader=_shared_state)
    assert r53.writes[-1][2] == ["new", "owner", "same"], "elapsed participant must be excluded before union even without a sweep"
    with mock.patch.object(r53, "upsert_record", side_effect=RuntimeError("ambiguous withdraw")):
        _error(lambda: hub.withdraw(opts.acme_group, "client-a", "new-owner", request=owner, **seams, state_reader=_shared_state), 503)
    hub.withdraw(opts.acme_group, "client-a", "new-owner", request=owner, **seams, state_reader=_shared_state)
    assert r53.writes[-1][2] == ["owner", "same"], "repeat retired withdrawal must repair prior ambiguous cleanup"
    _error(lambda: hub.publish(opts.acme_group, "client-a", "new-owner", ["new"], request=owner, **seams, state_reader=_shared_state), 400)


@th.django_unit_test("Shared ACME stale keys/projects cannot publish and rotations retain project authority")
def test_shared_fresh_principal(opts):
    from mojo.apps.account.models import ApiKey, Group
    from mojo.apps.dnsman.services import acme_hub as hub
    from mojo.helpers import dates
    allocation = _allocation(opts)
    request = _request(opts.acme_group)
    dns, r53 = AliasProbe(allocation), FakeRoute53()
    for changes in [dict(is_active=False), dict(expires_at=dates.subtract(seconds=1)), dict(permissions={})]:
        ApiKey.objects.filter(pk=request.api_key.pk).update(**changes)
        _error(lambda: hub.publish(opts.acme_group, "client-a", "denied", ["secret"], request=request,
                                  dns_probe=dns, route53_client=r53, state_reader=_shared_state), 403)
        ApiKey.objects.filter(pk=request.api_key.pk).update(is_active=True, expires_at=None, permissions={"dnsman_acme_federation": True})
    parent = Group.objects.create(name="acme_hub_test_parent_" + uuid.uuid4().hex, is_active=False)
    try:
        Group.objects.filter(pk=opts.acme_group.pk).update(parent=parent)
        _error(lambda: hub.federation_principal(request), 403)
        Group.objects.filter(pk=parent.pk).update(is_active=True)
        assert hub.federation_principal(request).group_id == opts.acme_group.pk, "fresh active ancestor should restore access"
        Group.objects.filter(pk=opts.acme_group.pk).update(is_active=False)
        _error(lambda: hub.withdraw(opts.acme_group, "client-a", "unknown", request=request, dns_probe=dns, route53_client=r53, state_reader=_shared_state), 403)
    finally:
        Group.objects.filter(pk=opts.acme_group.pk).update(parent=None, is_active=True)
        parent.delete()
    assert r53.writes == [], "dead credentials must never touch provider"
    doomed = Group.objects.create(name="acme_hub_test_deleted_" + uuid.uuid4().hex)
    doomed_uuid = doomed.get_uuid()
    descriptor = _descriptor(allocation, doomed, "deleted-binding")
    controller = _request(opts.acme_group)
    hub.grant(controller, descriptor, dns_probe=dns, route53_client=r53, state_reader=_shared_state)
    doomed.delete()
    replacement = Group.objects.create(name="acme_hub_test_replacement_" + uuid.uuid4().hex, uuid=doomed_uuid)
    _error(lambda: hub.participant_status(_request(replacement), "deleted-binding", dns_probe=dns, state_reader=_shared_state), 404, "acme_not_authorized")


def _wait_for_advisory_waiter(pid):
    from django.db import connection
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=%s AND locktype='advisory' AND NOT granted)", [pid])
            if cursor.fetchone()[0]:
                return True
        threading.Event().wait(0.01)
    return False


@th.django_unit_test("Shared ACME concurrent publishers serialize a complete union in both orders")
def test_shared_concurrent_publications(opts):
    from django.db import close_old_connections, connection
    from mojo.apps.dnsman.services import acme_hub as hub
    for first_ref, second_ref in [("parallel-a", "parallel-b"), ("parallel-b", "parallel-a")]:
        allocation = _allocation(opts)
        owner = _request(opts.acme_group)
        seams = dict(dns_probe=AliasProbe(allocation), route53_client=FakeRoute53())
        for ref in (first_ref, second_ref):
            hub.grant(owner, _descriptor(allocation, opts.other_group, ref), **seams, state_reader=_shared_state)
        barrier = threading.Barrier(2)
        provider_entered, release_provider, second_proved = threading.Event(), threading.Event(), threading.Event()
        writes, db_pids = [], {}

        class ConcurrentProvider(FakeRoute53):
            def upsert_record(self, zone_id, rtype, name, values, **kwargs):
                writes.append(sorted(values))
                if len(writes) == 1:
                    provider_entered.set()
                    assert release_provider.wait(10), "first provider submission was not released"
                return super().upsert_record(zone_id, rtype, name, values, **kwargs)
        r53 = ConcurrentProvider()

        class ConcurrentProbe(AliasProbe):
            def __init__(self, row, first):
                super().__init__(row)
                self.first = first
            def verify_one_hop_cname(self, source, target, **kwargs):
                barrier.wait(timeout=10)
                if not self.first:
                    assert provider_entered.wait(10), "first publisher never acquired allocation lock"
                    second_proved.set()
                return objict(ok=True, error=None)

        def run(ref, first):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    db_pids[ref] = cursor.fetchone()[0]
                request = _request(opts.other_group)
                return hub.publish(opts.other_group, ref, "same-ref", [ref], request=request,
                                   dns_probe=ConcurrentProbe(allocation, first), route53_client=r53, state_reader=_shared_state)
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run, first_ref, True)
            second = pool.submit(run, second_ref, False)
            try:
                assert second_proved.wait(10), "both publishers must overlap on independent connections"
                assert _wait_for_advisory_waiter(db_pids[second_ref]), "second publisher must actually wait on the PostgreSQL advisory lock held through provider submission"
                assert not second.done(), "second publish must wait while first holds the provider lock"
                assert writes == [[first_ref]], f"a provider write escaped the allocation lock: {writes}"
            finally:
                release_provider.set()
            first.result(timeout=10)
            second.result(timeout=10)
        assert len(set(db_pids.values())) == 2, f"concurrency proof must use independent database sessions, got {db_pids}"
        assert writes == [[first_ref], sorted([first_ref, second_ref])], f"both orderings must preserve full union, got {writes}"
        assert r53.records[("TXT", allocation.target_name)].record_values == sorted([first_ref, second_ref]), "final provider state must retain both participants, not just capture a correct intermediate call"


@th.django_unit_test("Shared ACME revoke wins against a publish already proving DNS on another connection")
def test_shared_revoke_publish_race(opts):
    from django.db import close_old_connections, connection
    from mojo.apps.dnsman.models import AcmeHubChallengeLease
    from mojo.apps.dnsman.services import acme_hub as hub
    allocation = _allocation(opts)
    owner = _request(opts.acme_group)
    dns, r53 = AliasProbe(allocation), FakeRoute53()
    first = hub.grant(owner, _descriptor(allocation, opts.other_group, "racing"), dns_probe=dns, route53_client=r53, state_reader=_shared_state)
    proof_entered, proof_resume = threading.Event(), threading.Event()
    provider_entered, provider_resume = threading.Event(), threading.Event()
    rendezvous = threading.Barrier(2)
    pids = {}

    class SlowProof(AliasProbe):
        def verify_one_hop_cname(self, source, target, **kwargs):
            proof_entered.set()
            rendezvous.wait(timeout=10)
            assert proof_resume.wait(10), "DNS proof was not released during concurrent revocation"
            return objict(ok=True, error=None)

    class SlowCleanup(FakeRoute53):
        def list_records(self, zone_id):
            provider_entered.set()
            assert provider_resume.wait(10), "cleanup provider was not released"
            return super().list_records(zone_id)

    def run(operation):
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                pids[operation] = cursor.fetchone()[0]
            if operation == "publish":
                return _error(lambda: hub.publish(opts.other_group, "racing", "losing-publish", ["must-not-publish"],
                    request=_request(opts.other_group), dns_probe=SlowProof(allocation), route53_client=r53, state_reader=_shared_state), 403, "acme_trust_revoked")
            rendezvous.wait(timeout=10)
            return hub.revoke(_request(opts.acme_group), allocation.target_name, first["participant_ref"],
                              dns_probe=dns, route53_client=SlowCleanup(), state_reader=_shared_state)
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        publication = pool.submit(run, "publish")
        assert proof_entered.wait(10), "publisher must resolve grant before revocation"
        revocation = pool.submit(run, "revoke")
        try:
            assert provider_entered.wait(10), "revocation must durably commit before concurrent publish resumes"
            proof_resume.set()
            assert _wait_for_advisory_waiter(pids["publish"]), "the already-proved publisher must actually wait on revocation's PostgreSQL lock"
            assert not publication.done(), "publish must wait for allocation lock held through cleanup"
        finally:
            proof_resume.set()
            provider_resume.set()
        revocation.result(timeout=10)
        publication.result(timeout=10)
    assert len(set(pids.values())) == 2, f"race must use independent connections: {pids}"
    assert not AcmeHubChallengeLease.objects.filter(allocation=allocation).exists(), "losing publish must persist no intent"
    assert r53.writes == [], "revocation winner must prevent provider publication"


@th.django_unit_test("Shared ACME allocate/grant DB uniqueness collision leaves no orphan target")
def test_shared_allocate_grant_collision(opts):
    from django.db import close_old_connections, connection
    from mojo.apps.dnsman.models import AcmeHubDelegation, AcmeHubParticipant
    from mojo.apps.dnsman.services import acme_hub as hub
    allocation = _allocation(opts)
    owner = _request(opts.acme_group)
    descriptor = _descriptor(allocation, opts.other_group, "collision")
    barrier = threading.Barrier(2)
    allocations = AcmeHubDelegation.objects.filter(
        owner_project_uuid=opts.other_group.get_uuid(), client_ref="collision")
    before = allocations.count()
    outcomes = []

    class RacingProof(AliasProbe):
        def query_cname(self, name):
            result = super().query_cname(name)
            if name == allocation.target_name:
                barrier.wait(timeout=10)
            return result

    def racing_zone(**kwargs):
        barrier.wait(timeout=10)
        return objict(name="hub.example.net", id="ZONE-HUB")

    def run(kind):
        close_old_connections()
        try:
            seams = dict(route53_client=FakeRoute53(), dns_probe=RacingProof(allocation))
            try:
                if kind == "allocate":
                    hub.allocate(opts.other_group, "collision", allocation.domain_name, zone_resolver=racing_zone, **seams, state_reader=_shared_state)
                else:
                    hub.grant(owner, descriptor, **seams, state_reader=_shared_state)
                outcomes.append((kind, "success"))
            except Exception as exc:
                assert getattr(exc, "status", None) == 409, f"collision loser must be a safe 409, got {exc}"
                outcomes.append((kind, "conflict"))
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, kind) for kind in ("allocate", "grant")]
        for future in futures:
            future.result(timeout=15)
    rows = AcmeHubParticipant.objects.filter(consumer_project_uuid=opts.other_group.get_uuid(), client_ref="collision")
    assert rows.count() == 1, f"canonical project/ref must have exactly one binding, got {list(rows)}"
    row = rows.get()
    assert sorted(result for unused, result in outcomes) == ["conflict", "success"], f"one winner/loser required: {outcomes}"
    assert allocations.count() == before + (1 if row.role == "owner" else 0), "losing allocation transaction must leave no orphan target"


@th.django_unit_test("Shared ACME old-schema migration preserves every allocation and historical lease field")
def test_shared_historical_upgrade(opts):
    import importlib
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    from mojo.helpers import dates
    migration = importlib.import_module("mojo.apps.dnsman.migrations.0005_acmehubparticipant_and_more")
    executor = MigrationExecutor(connection)
    before = executor.loader.project_state([("dnsman", "0004_dnsrecordreservation")])
    after = before.clone()
    # A unique temporary PostgreSQL schema isolates historical table creation and
    # migration operations; the shared suite's public schema never goes backward.
    schema = "acme_upgrade_" + uuid.uuid4().hex
    old_allocation = before.apps.get_model("dnsman", "AcmeHubDelegation")
    old_lease = before.apps.get_model("dnsman", "AcmeHubChallengeLease")
    with connection.cursor() as cursor:
        cursor.execute('CREATE SCHEMA "' + schema + '"')
        cursor.execute('SET search_path TO "' + schema + '", public')
    try:
        with connection.schema_editor() as editor:
            editor.create_model(old_allocation)
            editor.create_model(old_lease)
        allocation = old_allocation.objects.create(
            owner_project_uuid=opts.acme_group.get_uuid(), group_id=opts.acme_group.pk, client_ref="historic",
            domain_name="history.example", source_name="_acme-challenge.history.example",
            target_name="history.hub.example.net", allocation_zone_name="hub.example.net", allocation_zone_id="HISTORY")
        deleted = old_allocation.objects.create(
            owner_project_uuid=uuid.uuid4().hex, group_id=None, client_ref="deleted-history",
            domain_name="deleted.example", source_name="_acme-challenge.deleted.example",
            target_name="deleted.hub.example.net", allocation_zone_name="hub.example.net", allocation_zone_id="OLD")
        for index, state in enumerate(("active", "withdrawn", "pending", "expired")):
            old_lease.objects.create(allocation=allocation if index < 3 else deleted,
                challenge_ref=f"history-{index}", record_values=[f"value-{index}"], state=state,
                expires_at=dates.add(seconds=500), reconciled_at=dates.utcnow(), last_error="historical_error")
        allocations_before = list(old_allocation.objects.order_by("pk").values())
        leases_before = list(old_lease.objects.order_by("pk").values())
        for operation in migration.Migration.operations:
            new_state = after.clone()
            operation.state_forwards("dnsman", new_state)
            with connection.schema_editor() as editor:
                operation.database_forwards("dnsman", editor, after, new_state)
            after = new_state
        Allocation = after.apps.get_model("dnsman", "AcmeHubDelegation")
        Lease = after.apps.get_model("dnsman", "AcmeHubChallengeLease")
        Participant = after.apps.get_model("dnsman", "AcmeHubParticipant")
        assert list(Allocation.objects.order_by("pk").values()) == allocations_before, "migration must preserve every allocation PK, field, target and timestamp"
        for old, new in zip(leases_before, Lease.objects.order_by("pk").values()):
            assert new.pop("participant_id") is None, "compatibility migration must leave all leases unbound"
            assert old == new, f"migration changed historical lease data: {old} -> {new}"
        assert Participant.objects.count() == 0, "migration must create no new Group foreign-key owner rows"
        legacy = old_lease.objects.create(allocation=allocation, challenge_ref="after-migration",
                                          record_values=["legacy"], expires_at=dates.add(seconds=50))
        assert Lease.objects.get(pk=legacy.pk).participant_id is None, "actual historical model must still insert after005"
        OldGroup = before.apps.get_model("account", "Group")
        historic_group = OldGroup.objects.create(name="acme_hub_test_historical_" + uuid.uuid4().hex)
        old_allocation.objects.filter(pk=deleted.pk).update(group_id=historic_group.pk)
        historic_group.delete()
        assert Allocation.objects.get(pk=deleted.pk).group_id is None, "published ORM collector must delete Group after new schema without unknown populated Participant FK"
        # Emulate the ORIGINAL, already-recorded005 physical state, then run
        # the new migration's supported schema-editor repair on that state.
        with connection.schema_editor() as editor:
            migration.backfill_owners(after.apps, editor)
        with connection.schema_editor() as editor:
            field = Lease._meta.get_field("participant")
            strict = field.clone()
            strict.set_attributes_from_name("participant")
            strict.model = Lease
            strict.remote_field.model = field.remote_field.model
            strict.null = False
            editor.alter_field(Lease, field, strict)
        original_rows = list(Lease.objects.order_by("pk").values())
        repair = importlib.import_module("mojo.apps.dnsman.migrations.0007_acmehubinstallation")
        with connection.schema_editor() as editor:
            repair.repair_original_005(after.apps, editor)
            repair.repair_original_005(after.apps, editor)
        assert list(Lease.objects.order_by("pk").values()) == original_rows, "physical repair must preserve every original005 binding, timestamp and value"
        with connection.cursor() as cursor:
            columns = connection.introspection.get_table_description(cursor, Lease._meta.db_table)
        assert next(c for c in columns if c.name == "participant_id").null_ok, "already-applied005 column must really become nullable, not only migration state"

    finally:
        with connection.cursor() as cursor:
            cursor.execute('SET search_path TO public')
            cursor.execute('DROP SCHEMA "' + schema + '" CASCADE')


@th.django_unit_test("Shared ACME decorated HTTP endpoints deny unauthenticated and ordinary managers")
def test_shared_http_denials(opts):
    from testit.client import RestClient
    client = RestClient(opts.client.host)
    for path in ("capabilities", "consumer/lookup", "consumer/status", "consumer/join", "allocation/list",
                 "trust/list", "trust/recipient", "trust/grant", "trust/revoke"):
        response = client.post("/api/dnsman/acme/" + path, {})
        assert response.status_code == 403 and response.response.status is False and response.response.error == "acme_not_authorized", f"unauthenticated {path} must fail auth before malformed body: {response}"


@th.django_unit_setup()
def cleanup_acme_hub(opts):
    from mojo.apps.account.models import ApiKey, Group
    from mojo.apps.dnsman.models import AcmeHubChallengeLease, AcmeHubDelegation, AcmeHubParticipant

    AcmeHubChallengeLease.objects.all().delete()
    AcmeHubParticipant.objects.all().delete()
    AcmeHubDelegation.objects.all().delete()
    ApiKey.objects.filter(name__startswith="acme_hub_test_").delete()
    Group.objects.filter(name__startswith="acme_hub_test_").delete()
