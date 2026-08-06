"""Internal ACME DNS-01 delegation hub.

The service owns one explicitly configured public Route53 zone.  Tenants can
only write the opaque target permanently allocated to their project/client
reference; the generic dnsman record APIs never expose this writer.
"""

import re
import secrets
import time
from contextlib import contextmanager

from django.db import connection, transaction, IntegrityError
from django.db.models import Q
from objict import objict

from mojo import errors as me
from mojo.helpers import dates, logit
from mojo.helpers.aws import route53
from mojo.helpers.dns import probe
from mojo.helpers.settings import settings
from mojo.apps.dnsman.models import AcmeHubDelegation, AcmeHubChallengeLease
from mojo.apps.dnsman.models.acme_hub_challenge_lease import (
    STATE_ACTIVE, STATE_EXPIRED, STATE_PENDING, STATE_WITHDRAWN)
from mojo.apps.dnsman.services.naming import normalize_domain


logger = logit.get_logger("dnsman.acme_hub", "dnsman.log")

_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_VALUES = 20
_MAX_VALUE_LENGTH = 1024
_LOCK_NAMESPACE = 1422


def _bounded_static(name, default, minimum, maximum):
    value = settings.get_static(name, default, kind="int")
    return max(min(int(value), maximum), minimum)


def ttl():
    return _bounded_static("DNSMAN_ACME_HUB_TTL", 60, 30, 86400)


def lease_seconds():
    return _bounded_static("DNSMAN_ACME_HUB_LEASE_SECONDS", 900, 60, 86400)


def propagation_timeout():
    return _bounded_static("DNSMAN_ACME_HUB_PROPAGATION_TIMEOUT", 300, 5, 900)


def propagation_interval():
    return _bounded_static("DNSMAN_ACME_HUB_PROPAGATION_INTERVAL", 5, 1, 30)


def _reference(value, label):
    if not isinstance(value, str) or not _REF_RE.match(value):
        raise me.ValueException(
            f"{label} must be 1-128 characters using letters, digits, '.', '_', ':' or '-'")
    return value


def _digest_values(values):
    if not isinstance(values, list) or not values or len(values) > _MAX_VALUES:
        raise me.ValueException(f"values must be a non-empty list of at most {_MAX_VALUES} strings")
    normalized = []
    for value in values:
        if not isinstance(value, str) or not value or len(value) > _MAX_VALUE_LENGTH:
            raise me.ValueException(
                f"each challenge value must be 1-{_MAX_VALUE_LENGTH} characters")
        if "\x00" in value or "\r" in value or "\n" in value:
            raise me.ValueException("challenge values cannot contain control characters")
        if value not in normalized:
            normalized.append(value)
    return sorted(normalized)


def _configured_zone():
    raw_name = settings.get_static("DNSMAN_ACME_HUB_ZONE", None)
    if not raw_name:
        raise me.ValueException("ACME delegation hub is not configured", code=503, status=503)
    return normalize_domain(raw_name)


def _validate_zone(expected_name, expected_id=None):
    """Resolve one exact public Route53 zone and prove its public DNS cut."""
    try:
        expected_name = normalize_domain(expected_name)
        zone_id = expected_id or route53.find_zone_id(expected_name)
    except Exception:
        raise me.ValueException("The configured ACME hub zone is invalid", code=503, status=503)
    if not zone_id:
        raise me.ValueException("The configured ACME hub zone is unavailable", code=503, status=503)
    try:
        zone = route53.get_zone(zone_id)
        public = probe.find_zone_nameservers(expected_name)
    except Exception:
        raise me.ValueException("The configured ACME hub zone is unavailable", code=503, status=503)
    normalized_id = str(zone_id).split("/")[-1]
    if zone.id != normalized_id or zone.name != expected_name or zone.private:
        raise me.ValueException("The configured ACME hub zone is not the exact public zone", code=503, status=503)

    expected_ns = {probe.normalize_name(value) for value in (zone.name_servers or []) if value}
    public_ns = {probe.normalize_name(value) for value in (public.nameservers or []) if value}
    if public.error or public.zone != expected_name or not expected_ns or public_ns != expected_ns:
        raise me.ValueException("The configured ACME hub zone has no matching public DNS cut", code=503, status=503)
    return objict(name=zone.name, id=zone.id)


def _new_zone():
    name = _configured_zone()
    configured_id = settings.get_static("DNSMAN_ACME_HUB_HOSTED_ZONE_ID", None)
    return _validate_zone(name, configured_id)


def _stored_zone(allocation):
    # Rotation applies only to new allocations.  Existing rows always use the
    # exact name/id captured when their target was minted.
    return _validate_zone(allocation.allocation_zone_name, allocation.allocation_zone_id)


def _project_uuid(group):
    if group is None or not group.is_effectively_active():
        raise me.PermissionDeniedException("An active project is required")
    return group.get_uuid()


def _allocation_for(group, client_ref):
    owner_uuid = _project_uuid(group)
    client_ref = _reference(client_ref, "client_ref")
    allocation = AcmeHubDelegation.objects.filter(
        owner_project_uuid=owner_uuid, client_ref=client_ref).first()
    if allocation is None:
        raise me.ValueException("Unknown ACME delegation")
    return allocation


def _audit(allocation, request, kind, count=0, level="info"):
    # Counts and identifiers only.  Never pass record_values, AWS change ids,
    # credentials or hosted-zone ids into this trail.
    message = (
        f"ACME hub {kind}: project={allocation.owner_project_uuid} "
        f"client_ref={allocation.client_ref} count={int(count)} "
        f"key={getattr(getattr(request, 'api_key', None), 'pk', None)}")
    try:
        allocation.model_logit(request, message, f"dnsman_acme:{kind}", level=level)
    except Exception:
        logger.exception(f"ACME hub audit write failed for allocation {allocation.pk}")


def allocate(group, client_ref, domain, request=None):
    owner_uuid = _project_uuid(group)
    client_ref = _reference(client_ref, "client_ref")
    domain = normalize_domain(domain)
    source = f"_acme-challenge.{domain}"

    existing = AcmeHubDelegation.objects.filter(
        owner_project_uuid=owner_uuid, client_ref=client_ref).first()
    if existing is not None:
        if existing.domain_name != domain:
            _audit(existing, request, "allocation_refused")
            raise me.ValueException("client_ref is already bound to a different domain")
        _stored_zone(existing)
        return existing

    zone = _new_zone()
    for unused in range(5):
        target = f"{secrets.token_hex(16)}.{zone.name}"
        try:
            with transaction.atomic():
                allocation = AcmeHubDelegation.objects.create(
                    owner_project_uuid=owner_uuid,
                    client_ref=client_ref,
                    domain_name=domain,
                    source_name=source,
                    target_name=target,
                    group=group,
                    allocation_zone_name=zone.name,
                    allocation_zone_id=zone.id,
                )
            _audit(allocation, request, "allocated")
            return allocation
        except IntegrityError:
            existing = AcmeHubDelegation.objects.filter(
                owner_project_uuid=owner_uuid, client_ref=client_ref).first()
            if existing is not None:
                if existing.domain_name != domain:
                    _audit(existing, request, "allocation_refused")
                    raise me.ValueException("client_ref is already bound to a different domain")
                _stored_zone(existing)
                return existing
    raise me.ValueException("Could not allocate an ACME delegation target", code=503, status=503)


@contextmanager
def _allocation_lock(allocation_id):
    """Cross-process session advisory lock held through the Route53 write."""
    if connection.vendor != "postgresql":
        # django-mojo supports PostgreSQL in production.  This keeps isolated
        # unit-test databases functional without pretending to provide a
        # cross-process guarantee on unsupported engines.
        yield
        return
    lock_id = int(allocation_id) % 2147483647
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s, %s)", [_LOCK_NAMESPACE, lock_id])
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [_LOCK_NAMESPACE, lock_id])


def _desired_snapshot(allocation):
    desired = set()
    snapshot_ids = []
    pending_ids = []
    leases = list(AcmeHubChallengeLease.objects.filter(allocation=allocation)
        .only("pk", "state", "record_values"))
    for lease in leases:
        snapshot_ids.append(lease.pk)
        if lease.state in (STATE_PENDING, STATE_ACTIVE):
            desired.update(lease.record_values or [])
        if lease.state == STATE_PENDING:
            pending_ids.append(lease.pk)
    return sorted(desired), snapshot_ids, pending_ids


def _desired_values(allocation):
    return _desired_snapshot(allocation)[0]


def _wait_change(change_id, deadline):
    if not change_id:
        return
    while True:
        if route53.get_change(change_id).insync:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise me.ValueException("ACME hub Route53 reconciliation timed out", code=503, status=503)
        time.sleep(min(propagation_interval(), remaining))


def _wait_exact_txt(name, expected, deadline):
    expected = set(expected)
    while True:
        result = probe.query_txt(name)
        if result.error is None and set(result.txt_values) == expected:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise me.ValueException("ACME hub authoritative visibility timed out", code=503, status=503)
        time.sleep(min(propagation_interval(), remaining))


def _write_exact(allocation, values):
    _stored_zone(allocation)
    deadline = time.monotonic() + propagation_timeout()
    change_id = None
    if values:
        change_id = route53.upsert_record(
            allocation.allocation_zone_id, "TXT", allocation.target_name,
            values, ttl=ttl(), zone_name=allocation.allocation_zone_name,
            comment="django-mojo ACME delegation hub reconciliation")
    else:
        current = None
        for record in route53.list_records(allocation.allocation_zone_id):
            if record.type == "TXT" and record.name == allocation.target_name:
                current = record
                break
        if current is not None:
            change_id = route53.delete_record(
                allocation.allocation_zone_id, "TXT", allocation.target_name,
                values=current.record_values, ttl=current.ttl,
                zone_name=allocation.allocation_zone_name,
                comment="django-mojo ACME delegation hub reconciliation")
    _wait_change(change_id, deadline)
    _wait_exact_txt(allocation.target_name, values, deadline)


def _reconcile_locked(allocation, request=None, audit_kind="reconciled"):
    """Reconcile one snapshot while the caller holds the allocation lock."""
    values, snapshot_ids, pending_ids = _desired_snapshot(allocation)
    try:
        _write_exact(allocation, values)
    except Exception as exc:
        AcmeHubChallengeLease.objects.filter(pk__in=snapshot_ids).update(
            last_error=type(exc).__name__, reconciled_at=None)
        _audit(allocation, request, "provider_failure", count=len(values), level="error")
        if isinstance(exc, me.MojoException):
            raise
        raise me.ValueException("ACME hub provider reconciliation failed", code=503, status=503)

    now = dates.utcnow()
    AcmeHubChallengeLease.objects.filter(pk__in=pending_ids).update(
        state=STATE_ACTIVE, reconciled_at=now, last_error=None)
    AcmeHubChallengeLease.objects.filter(pk__in=snapshot_ids).update(
        reconciled_at=now, last_error=None)
    _audit(allocation, request, audit_kind, count=len(values))
    return values


def reconcile(allocation, request=None, audit_kind="reconciled"):
    """Replace the target RRset with the union of all durable live leases."""
    with _allocation_lock(allocation.pk):
        return _reconcile_locked(
            allocation, request=request, audit_kind=audit_kind)


def _persist_publish_lease(allocation, challenge_ref, values):
    """Commit idempotent desired state, retrying a concurrent first insert."""
    for attempt in range(2):
        try:
            with transaction.atomic():
                lease = AcmeHubChallengeLease.objects.select_for_update().filter(
                    allocation=allocation, challenge_ref=challenge_ref).first()
                if lease is not None:
                    if lease.state in (STATE_WITHDRAWN, STATE_EXPIRED):
                        raise me.ValueException("challenge_ref has already been retired")
                    if sorted(lease.record_values or []) != values:
                        raise me.ValueException("challenge_ref is already bound to different values")
                    lease.state = STATE_PENDING
                    lease.expires_at = dates.add(seconds=lease_seconds())
                    lease.reconciled_at = None
                    lease.last_error = None
                    lease.save(update_fields=[
                        "state", "expires_at", "reconciled_at", "last_error", "modified"])
                else:
                    lease = AcmeHubChallengeLease.objects.create(
                        allocation=allocation,
                        challenge_ref=challenge_ref,
                        record_values=values,
                        state=STATE_PENDING,
                        expires_at=dates.add(seconds=lease_seconds()),
                    )
            return lease
        except IntegrityError:
            if attempt:
                raise me.ValueException(
                    "Could not persist the ACME challenge lease", code=503, status=503)


def publish(group, client_ref, challenge_ref, values, request=None):
    allocation = _allocation_for(group, client_ref)
    challenge_ref = _reference(challenge_ref, "challenge_ref")
    values = _digest_values(values)

    proof = probe.verify_one_hop_cname(allocation.source_name, allocation.target_name)
    if not proof.ok:
        _audit(allocation, request, "publish_refused", count=len(values))
        raise me.ValueException(proof.error or "CNAME delegation proof failed")

    with _allocation_lock(allocation.pk):
        lease = _persist_publish_lease(allocation, challenge_ref, values)
        desired = _reconcile_locked(
            allocation, request=request, audit_kind="published")
    lease.refresh_from_db()
    return objict(allocation=allocation, lease=lease, active_value_count=len(desired))


def withdraw(group, client_ref, challenge_ref, request=None):
    allocation = _allocation_for(group, client_ref)
    challenge_ref = _reference(challenge_ref, "challenge_ref")
    with _allocation_lock(allocation.pk):
        changed = False
        with transaction.atomic():
            lease = AcmeHubChallengeLease.objects.select_for_update().filter(
                allocation=allocation, challenge_ref=challenge_ref).first()
            if lease is not None and lease.state not in (STATE_WITHDRAWN, STATE_EXPIRED):
                lease.state = STATE_WITHDRAWN
                lease.reconciled_at = None
                lease.last_error = None
                lease.save(update_fields=[
                    "state", "reconciled_at", "last_error", "modified"])
                changed = True

        if changed:
            desired = _reconcile_locked(
                allocation, request=request, audit_kind="withdrawn")
        else:
            desired = _desired_values(allocation)
            _audit(allocation, request, "withdrawn", count=len(desired))
    return objict(allocation=allocation, active_value_count=len(desired))


def sweep(limit=None):
    """Expire stale leases and retry every allocation with uncommitted intent."""
    if limit is None:
        limit = _bounded_static("DNSMAN_ACME_HUB_SWEEP_LIMIT", 100, 1, 1000)
    now = dates.utcnow()
    allocation_ids = list(AcmeHubChallengeLease.objects.filter(
        Q(
            state__in=[STATE_PENDING, STATE_ACTIVE],
            expires_at__lte=now)
        | Q(reconciled_at__isnull=True)
    ).values_list("allocation_id", flat=True).distinct()[:limit])
    expired = 0
    reconciled = 0
    errors = 0
    for allocation in AcmeHubDelegation.objects.filter(pk__in=allocation_ids):
        try:
            with _allocation_lock(allocation.pk):
                expired_here = AcmeHubChallengeLease.objects.filter(
                    allocation=allocation,
                    state__in=[STATE_PENDING, STATE_ACTIVE],
                    expires_at__lte=now).update(
                        state=STATE_EXPIRED,
                        reconciled_at=None,
                        last_error=None)
                expired += expired_here
                kind = "expired" if expired_here else "reconciled"
                _reconcile_locked(allocation, audit_kind=kind)
                reconciled += 1
        except Exception:
            errors += 1
            logger.exception(f"ACME hub sweep failed for allocation {allocation.pk}")
    return objict(expired=expired, reconciled=reconciled, errors=errors)
