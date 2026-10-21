"""Internal ACME DNS-01 delegation hub.

The service owns one explicitly configured public Route53 zone.  Tenants can
only write the opaque target permanently allocated to their project/client
reference; the generic dnsman record APIs never expose this writer.
"""

import re
import secrets
import time
import uuid
from contextlib import contextmanager

from django.db import connection, transaction, IntegrityError
from objict import objict

from mojo import errors as me
from mojo.helpers import advisory_locks, dates, logit
from mojo.helpers.aws import route53
from mojo.helpers.dns import probe
from mojo.helpers.settings import settings
from mojo.apps.dnsman.models import AcmeHubDelegation, AcmeHubChallengeLease, AcmeHubParticipant
from mojo.apps.dnsman.models.acme_hub_challenge_lease import (
    STATE_ACTIVE, STATE_EXPIRED, STATE_PENDING, STATE_WITHDRAWN)
from mojo.apps.dnsman.services.naming import normalize_domain
from mojo.apps.dnsman.services.authorization import is_platform_admin
from mojo.apps.dnsman.services import acme_hub_upgrade as upgrade


logger = logit.get_logger("dnsman.acme_hub", "dnsman.log")

_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_VALUES = 20
_MAX_VALUE_LENGTH = 1024
_LOCK_NAMESPACE = advisory_locks.ACME_HUB_ALLOCATION


def _bounded_static(name, default, minimum, maximum):
    value = settings.get_static(name, default, kind="int")
    return max(min(int(value), maximum), minimum)


def ttl():
    return _bounded_static("DNSMAN_ACME_HUB_TTL", 60, 30, 86400)


def lease_seconds():
    return _bounded_static("DNSMAN_ACME_HUB_LEASE_SECONDS", 900, 60, 86400)


def _reference(value, label):
    if not isinstance(value, str) or not _REF_RE.fullmatch(value):
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


def _validate_zone(expected_name, expected_id=None, *, route53_client=None, dns_probe=None):
    """Resolve one exact public Route53 zone and prove its public DNS cut."""
    client = route53_client or route53
    dns = dns_probe or probe
    try:
        expected_name = normalize_domain(expected_name)
        zone_id = expected_id or client.find_zone_id(expected_name)
    except Exception:
        raise me.ValueException("The configured ACME hub zone is invalid", code=503, status=503)
    if not zone_id:
        raise me.ValueException("The configured ACME hub zone is unavailable", code=503, status=503)
    try:
        zone = client.get_zone(zone_id)
        public = dns.find_zone_nameservers(expected_name)
    except Exception:
        raise me.ValueException("The configured ACME hub zone is unavailable", code=503, status=503)
    normalized_id = str(zone_id).split("/")[-1]
    if zone.id != normalized_id or zone.name != expected_name or zone.private:
        raise me.ValueException("The configured ACME hub zone is not the exact public zone", code=503, status=503)

    expected_ns = {dns.normalize_name(value) for value in (zone.name_servers or []) if value}
    public_ns = {dns.normalize_name(value) for value in (public.nameservers or []) if value}
    if public.error or public.zone != expected_name or not expected_ns or public_ns != expected_ns:
        raise me.ValueException("The configured ACME hub zone has no matching public DNS cut", code=503, status=503)
    return objict(name=zone.name, id=zone.id)


def _new_zone(*, route53_client=None, dns_probe=None):
    name = _configured_zone()
    configured_id = settings.get_static("DNSMAN_ACME_HUB_HOSTED_ZONE_ID", None)
    return _validate_zone(
        name, configured_id, route53_client=route53_client, dns_probe=dns_probe)


def _stored_zone(allocation, *, route53_client=None, dns_probe=None):
    # Rotation applies only to new allocations.  Existing rows always use the
    # exact name/id captured when their target was minted.
    return _validate_zone(
        allocation.allocation_zone_name, allocation.allocation_zone_id,
        route53_client=route53_client, dns_probe=dns_probe)


def _fail(token, status=400):
    raise me.ValueException(token, code=status, status=status)


def federation_principal(request):
    """Fresh underlying credential and ORIGINAL project; never an acting user."""
    from mojo.apps.account.models import ApiKey

    original = getattr(request, "api_key", None)
    if not isinstance(original, ApiKey) or getattr(request, "group_token", None) is not None:
        raise me.PermissionDeniedException(
            "An ACME federation API key is required", branch="dnsman.acme_hub.not_api_key")
    key = ApiKey.objects.select_related("group").filter(pk=original.pk).first()
    if (key is None or not key.is_active or key.group is None
            or key.group_id != original.group_id
            or (key.expires_at is not None and key.expires_at <= dates.utcnow())
            or not key.group.is_effectively_active()
            or not key.has_permission("dnsman_acme_federation")):
        raise me.PermissionDeniedException(
            "ACME federation permission denied", branch="dnsman.acme_hub.permission")
    request.group = key.group
    return key


def _human(request):
    from mojo.apps.account.models import User
    from mojo.helpers.request import is_key_backed_session

    user = getattr(request, "user", None)
    if is_key_backed_session(request) or not isinstance(user, User):
        return None
    # The interactive gate shares require_platform_admin's key-backed exclusion;
    # refresh the user too so an acting/stale identity cannot supply authority.
    return User.objects.filter(pk=user.pk, is_active=True).first()


def authorize(request, consumer=False):
    """New-operation authentication floor, before body parsing and throttles."""
    if getattr(request, "api_key", None) is not None or consumer:
        try:
            return federation_principal(request)
        except me.PermissionDeniedException:
            _fail("acme_not_authorized", 403)
    user = _human(request)
    if user is None:
        _fail("acme_not_authorized", 403)
    return user


def require_controller(request, allocation=None):
    actor = authorize(request)
    if hasattr(actor, "group_id") and getattr(request, "api_key", None) is not None:
        if allocation is None or (allocation.group_id == actor.group_id
                and allocation.owner_project_uuid == actor.group.get_uuid()):
            return actor
    elif is_platform_admin(request, actor):
        return actor
    if allocation is not None:
        _audit(allocation, request, "controller_refused", result="refused")
    _fail("acme_not_authorized", 403)


def _operation_group(group, request=None):
    if request is not None:
        return federation_principal(request).group
    from mojo.apps.account.models import Group

    fresh = Group.objects.filter(pk=getattr(group, "pk", None)).first()
    if fresh is None or not fresh.is_effectively_active():
        raise me.PermissionDeniedException("An active project is required")
    return fresh


def _project_uuid(group):
    return _operation_group(group).get_uuid()


def _participant_for(group, client_ref, request=None, new=False):
    group = _operation_group(group, request)
    client_ref = _reference(client_ref, "client_ref")
    participant = AcmeHubParticipant.objects.select_related("allocation").filter(
        consumer_project_uuid=group.get_uuid(), group=group, client_ref=client_ref).first()
    if participant is None:
        if new:
            _fail("acme_not_authorized", 404)
        raise me.ValueException("Unknown ACME delegation")
    return participant


def _allocation_for(group, client_ref):
    # Compatibility for internal callers; all mutations resolve a participant.
    return _participant_for(group, client_ref).allocation


def _audit(allocation, request, kind, count=0, level="info", participant=None, result="accepted"):
    # Identifiers/counts only: never digests, provider ids, or credentials.
    message = (
        f"ACME hub {kind}: project={allocation.owner_project_uuid} "
        f"client_ref={getattr(participant, 'client_ref', allocation.client_ref)} "
        f"consumer={getattr(participant, 'consumer_project_uuid', None)} "
        f"participant={getattr(participant, 'participant_ref', None)} "
        f"role={getattr(participant, 'role', None)} result={result} count={int(count)} "
        f"key={getattr(getattr(request, 'api_key', None), 'pk', None)} "
        f"user={getattr(getattr(request, 'user', None), 'pk', None)}")
    try:
        allocation.model_logit(request, message, f"dnsman_acme:{kind}", level=level)
    except Exception:
        logger.exception(f"ACME hub audit write failed for allocation {allocation.pk}")


def _existing_owner(participant, group, domain, request, **seams):
    allocation = participant.allocation
    if (participant.role != "owner" or participant.revoked_at is not None
            or participant.group_id != group.pk):
        _audit(allocation, request, "allocation_refused", participant=participant, result="refused")
        _fail("acme_reference_conflict", 409)
    if allocation.domain_name != domain:
        _audit(allocation, request, "allocation_refused", participant=participant, result="refused")
        raise me.ValueException("client_ref is already bound to a different domain")
    _stored_zone(allocation, **seams)
    return allocation


def _shared_allocate(group, client_ref, domain, request=None, *, route53_client=None, dns_probe=None, zone_resolver=None):
    group = _operation_group(group, request)
    owner_uuid = group.get_uuid()
    client_ref = _reference(client_ref, "client_ref")
    domain = normalize_domain(domain)
    seams = dict(route53_client=route53_client, dns_probe=dns_probe)

    def existing():
        return AcmeHubParticipant.objects.select_related("allocation").filter(
            consumer_project_uuid=owner_uuid, client_ref=client_ref).first()

    participant = existing()
    if participant is not None:
        return _existing_owner(participant, group, domain, request, **seams)
    zone = (zone_resolver or _new_zone)(**seams)
    for unused in range(5):
        try:
            # The savepoint rolls BOTH rows back if the canonical participant
            # namespace loses an allocate/grant race. No orphan target remains.
            with transaction.atomic():
                group = _operation_group(group, request)
                allocation = AcmeHubDelegation.objects.create(
                    owner_project_uuid=owner_uuid, client_ref=client_ref,
                    domain_name=domain, source_name=f"_acme-challenge.{domain}",
                    target_name=f"{secrets.token_hex(16)}.{zone.name}", group=group,
                    allocation_zone_name=zone.name, allocation_zone_id=zone.id)
                participant = AcmeHubParticipant.objects.create(
                    allocation=allocation, consumer_project_uuid=owner_uuid,
                    client_ref=client_ref, group=group, role="owner")
            _audit(allocation, request, "allocated", participant=participant)
            return allocation
        except IntegrityError:
            participant = existing()
            if participant is not None:
                return _existing_owner(participant, group, domain, request, **seams)
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


def _retire_elapsed(allocation):
    return AcmeHubChallengeLease.objects.filter(
        allocation=allocation, state__in=[STATE_PENDING, STATE_ACTIVE],
        expires_at__lte=dates.utcnow()).update(
            state=STATE_EXPIRED, reconciled_at=None, last_error=None, modified=dates.utcnow())


def _desired_snapshot(allocation):
    # Every caller holds the allocation lock. Elapsed/revoked intent must never
    # enter the union just because the periodic sweep has not run yet.
    _retire_elapsed(allocation)
    desired = set()
    snapshot_ids = []
    pending_ids = []
    leases = list(AcmeHubChallengeLease.objects.filter(allocation=allocation)
        .select_related("participant").only("pk", "state", "record_values", "participant__revoked_at", "participant__allocation_id"))
    shared = upgrade.enabled()
    for lease in leases:
        if shared and (lease.participant_id is None or lease.participant.allocation_id != allocation.pk):
            upgrade.refuse("participant_state_unavailable")
        live = lease.participant_id is None or lease.participant.revoked_at is None
        snapshot_ids.append(lease.pk)
        if lease.state in (STATE_PENDING, STATE_ACTIVE) and live:
            desired.update(lease.record_values or [])
        if lease.state == STATE_PENDING and live:
            pending_ids.append(lease.pk)
    return sorted(desired), snapshot_ids, pending_ids


def _desired_values(allocation):
    return _desired_snapshot(allocation)[0]


def _confirm_exact(allocation, values, *, dns_probe=None):
    """Tri-state read of what the authority currently serves at the target.

    Deliberately NOT a boolean.  `probe.query_txt` reports NXDOMAIN and NoAnswer
    as `error=None` with an empty value list, so an empty desired set confirms
    cleanly and only a genuine lookup failure sets `error`.  Folding that
    failure into "mismatch" would make a resolver outage issue a Route53 write
    for EVERY candidate allocation on every five-minute sweep — a self-inflicted
    write storm against an API throttled at roughly five requests a second.
    """
    dns = dns_probe or probe
    try:
        result = dns.query_txt(allocation.target_name)
    except Exception:
        return "unknown"
    if result.error is not None:
        return "unknown"
    if set(result.txt_values or []) == set(values):
        return "match"
    return "mismatch"


def _write_exact(allocation, values, *, route53_client=None, dns_probe=None):
    """Submit the exact RRset to Route53 and return once the API accepts it.

    There is deliberately no convergence wait here.  Issuance never runs on an
    HTTP request — the downstream calls this through a 30s-read-timeout wire —
    so a hub-side propagation wait could never be satisfied before the caller
    gave up.  The downstream owns the propagation gate on its own job worker.
    """
    upgrade.check_session()
    client = route53_client or route53
    _stored_zone(allocation, route53_client=route53_client, dns_probe=dns_probe)
    if values:
        client.upsert_record(
            allocation.allocation_zone_id, "TXT", allocation.target_name,
            values, ttl=ttl(), zone_name=allocation.allocation_zone_name,
            comment="django-mojo ACME delegation hub reconciliation")
        return
    current = None
    for record in client.list_records(allocation.allocation_zone_id):
        if record.type == "TXT" and record.name == allocation.target_name:
            current = record
            break
    if current is not None:
        client.delete_record(
            allocation.allocation_zone_id, "TXT", allocation.target_name,
            values=current.record_values, ttl=current.ttl,
            zone_name=allocation.allocation_zone_name,
            comment="django-mojo ACME delegation hub reconciliation")


def _reconcile_locked(allocation, request=None, audit_kind="reconciled",
                      confirm=False, *, route53_client=None, dns_probe=None, participant=None):
    """Reconcile one snapshot while the caller holds the allocation lock.

    `confirm=True` (the sweeper) probes the authority first and writes only on a
    real mismatch.  `confirm=False` (publish/withdraw) always writes: the caller
    just changed durable intent and has nothing to confirm against.
    """
    values, snapshot_ids, pending_ids = _desired_snapshot(allocation)
    verdict = "mismatch"
    if confirm:
        verdict = _confirm_exact(allocation, values, dns_probe=dns_probe)
        if verdict == "unknown":
            # Could not read the authority.  Leave reconciled_at NULL so the
            # next pass retries, and write nothing.
            _audit(allocation, request, "reconcile_deferred", count=len(values))
            return values

    if verdict == "mismatch":
        AcmeHubChallengeLease.objects.filter(pk__in=snapshot_ids).update(reconciled_at=None)
        try:
            _write_exact(
                allocation, values,
                route53_client=route53_client, dns_probe=dns_probe)
        except Exception as exc:
            AcmeHubChallengeLease.objects.filter(pk__in=snapshot_ids).update(
                last_error=type(exc).__name__, reconciled_at=None,
                modified=dates.utcnow())
            _audit(allocation, request, "provider_failure", count=len(values), level="error", participant=participant, result="failed")
            if isinstance(exc, me.MojoException):
                raise
            raise me.ValueException("ACME hub provider reconciliation failed", code=503, status=503)

    AcmeHubChallengeLease.objects.filter(pk__in=pending_ids).update(
        state=STATE_ACTIVE, last_error=None)
    AcmeHubChallengeLease.objects.filter(pk__in=snapshot_ids).update(
        last_error=None)
    # INVARIANT: reconciled_at is stamped ONLY on a confirmed probe match.
    # Stamping it after a successful write would degrade the field to "a write
    # was submitted once", which is exactly the signal the sweeper needs to
    # distinguish from "the authority actually serves this".
    if verdict == "match":
        AcmeHubChallengeLease.objects.filter(pk__in=snapshot_ids).update(
            reconciled_at=dates.utcnow())
    _audit(allocation, request, audit_kind, count=len(values), participant=participant)
    return values


@upgrade.admitted
def reconcile(allocation, request=None, audit_kind="reconciled",
              *, route53_client=None, dns_probe=None):
    """Replace the target RRset with the union of all durable live leases."""
    with _allocation_lock(allocation.pk):
        return _reconcile_locked(
            allocation, request=request, audit_kind=audit_kind, confirm=True,
            route53_client=route53_client, dns_probe=dns_probe)


def _persist_publish_lease(participant, challenge_ref, values):
    """Commit idempotent desired state, retrying a concurrent first insert."""
    allocation = participant.allocation
    for attempt in range(2):
        try:
            with transaction.atomic():
                lease = AcmeHubChallengeLease.objects.select_for_update().filter(
                    participant=participant, allocation=allocation, challenge_ref=challenge_ref).first()
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
                        allocation=allocation, participant=participant,
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


def _shared_publish(group, client_ref, challenge_ref, values, request=None,
            *, route53_client=None, dns_probe=None):
    started = time.monotonic()
    dns = dns_probe or probe
    participant = _participant_for(group, client_ref, request)
    allocation = participant.allocation
    challenge_ref = _reference(challenge_ref, "challenge_ref")
    values = _digest_values(values)

    if participant.revoked_at is not None:
        _audit(allocation, request, "publish_refused", participant=participant, result="revoked")
        _fail("acme_trust_revoked", 403)
    proof = dns.verify_one_hop_cname(allocation.source_name, allocation.target_name)
    if not proof.ok:
        _audit(allocation, request, "publish_refused", count=len(values), participant=participant, result="refused")
        raise me.ValueException(proof.error or "CNAME delegation proof failed")

    with _allocation_lock(allocation.pk):
        # Revoke may have won while the DNS proof was in flight.
        participant = _participant_for(group, client_ref, request)
        if participant.revoked_at is not None:
            _audit(allocation, request, "publish_refused", participant=participant, result="revoked")
            _fail("acme_trust_revoked", 403)
        _retire_elapsed(allocation)
        lease = _persist_publish_lease(participant, challenge_ref, values)
        desired = _reconcile_locked(
            allocation, request=request, audit_kind="published", confirm=False, participant=participant,
            route53_client=route53_client, dns_probe=dns_probe)
    lease.refresh_from_db()
    # The request path no longer waits for propagation, but it is not free: a
    # one-hop CNAME proof, a zone revalidation and one Route53 call, each probe
    # query bounded at 5s with no total budget. Log the real number so a
    # downstream read-timeout regression is visible from the hub side.
    logger.info(
        f"ACME hub publish allocation={allocation.pk} "
        f"values={len(desired)} elapsed={time.monotonic() - started:.3f}s")
    return objict(allocation=allocation, participant=participant, lease=lease, active_value_count=len(desired))


def _shared_withdraw(group, client_ref, challenge_ref, request=None,
             *, route53_client=None, dns_probe=None):
    started = time.monotonic()
    participant = _participant_for(group, client_ref, request)
    allocation = participant.allocation
    challenge_ref = _reference(challenge_ref, "challenge_ref")
    with _allocation_lock(allocation.pk):
        participant = _participant_for(group, client_ref, request)
        _retire_elapsed(allocation)
        changed = False
        with transaction.atomic():
            lease = AcmeHubChallengeLease.objects.select_for_update().filter(
                participant=participant, allocation=allocation, challenge_ref=challenge_ref).first()
            if lease is not None and lease.state not in (STATE_WITHDRAWN, STATE_EXPIRED):
                lease.state = STATE_WITHDRAWN
                lease.reconciled_at = None
                lease.last_error = None
                lease.save(update_fields=[
                    "state", "reconciled_at", "last_error", "modified"])
                changed = True

        owed = AcmeHubChallengeLease.objects.filter(
            allocation=allocation, reconciled_at__isnull=True).exists()
        if changed or owed:
            desired = _reconcile_locked(
                allocation, request=request, audit_kind="withdrawn", confirm=False, participant=participant,
                route53_client=route53_client, dns_probe=dns_probe)
        else:
            desired = _desired_values(allocation)
            _audit(allocation, request, "withdrawn", count=len(desired), participant=participant)
    logger.info(
        f"ACME hub withdraw allocation={allocation.pk} "
        f"values={len(desired)} elapsed={time.monotonic() - started:.3f}s")
    return objict(allocation=allocation, participant=participant, active_value_count=len(desired))


def _sweep_candidates(limit, now):
    """Allocation ids owed a sweep, most urgent first, bounded by `limit`.

    `.order_by()` is load-bearing.  `AcmeHubChallengeLease.Meta.ordering` puts
    `created` in the SELECT, which makes a bare `.distinct()` a no-op — the
    limit would then bound LEASES, not allocations.  That was tolerable while
    only rare failures reached this queue; now that every publish and every
    withdrawal lands here unconfirmed, it is not.

    Three arms, each taking only what the previous left of the budget:

      1. leases past `expires_at` — still published, so they are retired first
         and a burst of freshly published allocations can never crowd them out;
      2. unconfirmed intent that has not failed;
      3. unconfirmed intent whose last attempt failed — still retried, but only
         behind every healthy allocation, so an allocation whose hosted zone has
         been invalidated (it raises 503 on every attempt) cannot hold the head
         of the queue forever.
    """
    leases = AcmeHubChallengeLease.objects.order_by()

    def take(queryset, budget, seen):
        if budget <= 0:
            return []
        return list(queryset.exclude(allocation_id__in=seen).values_list(
            "allocation_id", flat=True).distinct()[:budget])

    seen = set()
    ids = take(leases.filter(
        state__in=[STATE_PENDING, STATE_ACTIVE], expires_at__lte=now),
        limit, seen)
    seen.update(ids)
    unconfirmed = leases.filter(reconciled_at__isnull=True)
    for arm in (unconfirmed.filter(last_error__isnull=True),
                unconfirmed.filter(last_error__isnull=False)):
        found = take(arm, limit - len(ids), seen)
        ids.extend(found)
        seen.update(found)
    return ids


@upgrade.admitted
def sweep(limit=None, *, route53_client=None, dns_probe=None):
    """Expire stale leases and confirm or repair every allocation's RRset."""
    if limit is None:
        limit = _bounded_static("DNSMAN_ACME_HUB_SWEEP_LIMIT", 100, 1, 1000)
    now = dates.utcnow()
    allocation_ids = _sweep_candidates(limit, now)
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
                _reconcile_locked(
                    allocation, audit_kind=kind, confirm=True,
                    route53_client=route53_client, dns_probe=dns_probe)
                reconciled += 1
        except Exception:
            errors += 1
            logger.exception(f"ACME hub sweep failed for allocation {allocation.pk}")
    # Allocations this pass touched whose authority still does not serve their
    # exact desired RRset.  Nothing raises for these any more, so this count is
    # the only signal that propagation is stuck somewhere.
    unconfirmed = AcmeHubChallengeLease.objects.filter(
        allocation_id__in=allocation_ids,
        state__in=[STATE_PENDING, STATE_ACTIVE],
        reconciled_at__isnull=True).order_by().values_list(
            "allocation_id", flat=True).distinct().count()
    return objict(
        expired=expired, reconciled=reconciled, errors=errors,
        unconfirmed=unconfirmed)


def _input_domain(value):
    if not isinstance(value, str) or not value or len(value) > 254:
        _fail("acme_invalid_request")
    try:
        normalized = normalize_domain(value)
        if len(normalized) > 253:
            _fail("acme_invalid_request")
        return normalized
    except Exception:
        _fail("acme_invalid_request")


def _input_ref(value):
    try:
        return _reference(value, "client_ref")
    except me.ValueException:
        _fail("acme_invalid_request")


def _uuid(value):
    if not isinstance(value, str) or len(value) not in (32, 36):
        _fail("acme_invalid_request")
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        _fail("acme_invalid_request")
    if value not in (str(parsed), parsed.hex):
        _fail("acme_invalid_request")
    return value


def _project_identity(value):
    from mojo.apps.dnsman.fleet_settings import project_uuid
    try:
        project_uuid(value)
    except ValueError:
        _fail("acme_invalid_request")
    return value


def _cursor(value):
    if value is None:
        return None
    if (not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", value)
            or int(value) > 9223372036854775807):
        _fail("acme_invalid_request")
    return int(value)


def _page(query, cursor, serialize):
    after = _cursor(cursor)
    if after is not None:
        query = query.filter(pk__gt=after)
    rows = list(query.order_by("pk")[:21])
    return dict(items=[serialize(row) for row in rows[:20]],
                next_cursor=str(rows[19].pk) if len(rows) > 20 else None)


def allocation_payload(allocation, participant=None):
    # This EXACT four-key legacy shape is a strict downstream contract.
    return dict(client_ref=participant.client_ref if participant else allocation.client_ref,
                domain=allocation.domain_name, source=allocation.source_name, target=allocation.target_name)


def participant_payload(participant, request, controller=False):
    allocation = participant.allocation
    key = getattr(request, "api_key", None)
    own = (key is not None and key.group_id == participant.group_id
           and key.group.get_uuid() == participant.consumer_project_uuid)
    data = dict(
        allocation_ref=allocation.target_name, participant_ref=str(participant.participant_ref),
        client_ref=participant.client_ref, domain=allocation.domain_name,
        source=allocation.source_name, target=allocation.target_name, role=participant.role,
        trust_state="revoked" if participant.revoked_at else "active",
        scope=[allocation.domain_name, f"*.{allocation.domain_name}"],
        can_publish=bool(own and participant.revoked_at is None), can_withdraw=bool(own),
        revoked_at=participant.revoked_at.isoformat() if participant.revoked_at else None)
    if controller:
        data.update(consumer_project_uuid=participant.consumer_project_uuid,
                    consumer_label=(participant.group.name or "")[:200] if participant.group else "")
    return data


@upgrade.admitted
def capabilities(request, *, route53_client=None, dns_probe=None, zone_resolver=None):
    actor = authorize(request)
    try:
        (zone_resolver or _new_zone)(route53_client=route53_client, dns_probe=dns_probe)
    except Exception:
        _fail("acme_hub_unavailable", 503)
    machine = getattr(request, "api_key", None) is not None
    shared = upgrade.enabled()
    return dict(version=2, shared_validation=shared,
                consumer_project_uuid=actor.group.get_uuid() if machine else None,
                can_consume=shared and machine, can_manage_trust=shared and (machine or bool(actor.is_superuser)),
                trust_scope="apex_and_wildcard")


@upgrade.admitted(shared=True)
def lookup_participants(request, domain, allocation_ref=None, cursor=None):
    key = authorize(request, consumer=True)
    query = AcmeHubParticipant.objects.select_related("allocation").filter(
        consumer_project_uuid=key.group.get_uuid(), group_id=key.group_id,
        allocation__domain_name=_input_domain(domain))
    if allocation_ref is not None:
        query = query.filter(allocation__target_name=_input_domain(allocation_ref))
    return _page(query, cursor, lambda row: participant_payload(row, request))


def _alias(allocation, *, dns_probe=None):
    dns = dns_probe or probe
    failed = dict(state="lookup_failed", observed_target=None, one_hop=None,
                  checked_at=dates.utcnow().isoformat())
    mismatch = dict(failed, state="mismatch")
    try:
        first = dns.query_cname(allocation.source_name)
        if first.error:
            return failed
        if len(first.targets) != 1:
            return mismatch
        observed = normalize_domain(first.targets[0])
        if observed == allocation.source_name:
            return mismatch
        second = dns.query_cname(observed)
        if second.error:
            return failed
        one_hop = not bool(second.targets)
        return dict(state="match" if observed == allocation.target_name and one_hop else "mismatch",
                    observed_target=observed, one_hop=one_hop, checked_at=dates.utcnow().isoformat())
    except Exception:
        return failed


@upgrade.admitted
def participant_status(request, client_ref, *, dns_probe=None):
    key = authorize(request, consumer=True)
    participant = _participant_for(key.group, _input_ref(client_ref), request, new=True)
    return dict(participant_payload(participant, request),
                alias=_alias(participant.allocation, dns_probe=dns_probe))


def _proof(allocation, *, route53_client=None, dns_probe=None):
    try:
        _stored_zone(allocation, route53_client=route53_client, dns_probe=dns_probe)
    except Exception:
        _fail("acme_hub_unavailable", 503)
    observation = _alias(allocation, dns_probe=dns_probe)
    if observation["state"] == "lookup_failed":
        _fail("acme_alias_lookup_failed", 503)
    if observation["state"] != "match":
        _fail("acme_alias_mismatch")


@upgrade.admitted(shared=True)
def join(request, domain, client_ref, allocation_ref, *, route53_client=None, dns_probe=None):
    key = authorize(request, consumer=True)
    participant = _participant_for(key.group, _input_ref(client_ref), request, new=True)
    allocation = participant.allocation
    if allocation.domain_name != _input_domain(domain) or allocation.target_name != _input_domain(allocation_ref):
        _fail("acme_reference_conflict", 409)
    if participant.revoked_at:
        _fail("acme_trust_revoked", 403)
    _proof(allocation, route53_client=route53_client, dns_probe=dns_probe)
    with _allocation_lock(allocation.pk):
        participant = _participant_for(key.group, client_ref, request, new=True)
        if participant.revoked_at:
            _fail("acme_trust_revoked", 403)
    return allocation_payload(allocation, participant)


def _controlled_allocation(request, allocation_ref):
    # Fail at the controller boundary BEFORE resolving a recipient or DNS.
    require_controller(request)
    allocation = AcmeHubDelegation.objects.filter(target_name=_input_domain(allocation_ref)).first()
    if allocation is None:
        _fail("acme_not_authorized", 403)
    require_controller(request, allocation)
    return allocation


@upgrade.admitted(shared=True)
def list_allocations(request, domain, cursor=None):
    actor = require_controller(request)
    query = AcmeHubDelegation.objects.filter(domain_name=_input_domain(domain))
    if getattr(request, "api_key", None) is not None:
        query = query.filter(owner_project_uuid=actor.group.get_uuid(), group_id=actor.group_id)

    def serialize(row):
        require_controller(request, row)
        return dict(allocation_ref=row.target_name, domain=row.domain_name, source=row.source_name,
                    target=row.target_name, owner_project_uuid=row.owner_project_uuid, can_manage_trust=True)
    return _page(query, cursor, serialize)


@upgrade.admitted(shared=True)
def list_participants(request, allocation_ref, cursor=None):
    allocation = _controlled_allocation(request, allocation_ref)
    query = AcmeHubParticipant.objects.select_related("allocation", "group").filter(allocation=allocation)
    return _page(query, cursor, lambda row: participant_payload(row, request, controller=True))


def _recipient(consumer_project_uuid):
    from mojo.apps.account.models import Group

    group = Group.objects.filter(uuid=_project_identity(consumer_project_uuid)).first()
    if group is None or not group.is_effectively_active():
        _fail("acme_not_authorized", 403)
    return group


@upgrade.admitted(shared=True)
def resolve_recipient(request, allocation_ref, consumer_project_uuid):
    _controlled_allocation(request, allocation_ref)
    group = _recipient(consumer_project_uuid)
    return dict(consumer_project_uuid=group.get_uuid(), label=(group.name or "")[:200], active=True)


def _connection_request(value):
    if not isinstance(value, dict) or set(value) != {
            "version", "consumer_project_uuid", "client_ref", "domain", "allocation_ref"}:
        _fail("acme_invalid_request")
    if type(value["version"]) is not int or value["version"] != 1:
        _fail("acme_invalid_request")
    return dict(version=1, consumer_project_uuid=_project_identity(value["consumer_project_uuid"]),
                client_ref=_input_ref(value["client_ref"]), domain=_input_domain(value["domain"]),
                allocation_ref=_input_domain(value["allocation_ref"]))


@upgrade.admitted(shared=True)
def grant(request, connection_request, *, route53_client=None, dns_probe=None):
    require_controller(request)
    descriptor = _connection_request(connection_request)
    allocation = _controlled_allocation(request, descriptor["allocation_ref"])
    if allocation.domain_name != descriptor["domain"]:
        _fail("acme_reference_conflict", 409)
    _proof(allocation, route53_client=route53_client, dns_probe=dns_probe)
    group = _recipient(descriptor["consumer_project_uuid"])
    with _allocation_lock(allocation.pk):
        require_controller(request, allocation)
        group = _recipient(descriptor["consumer_project_uuid"])
        query = AcmeHubParticipant.objects.select_related("allocation", "group").filter(
            consumer_project_uuid=descriptor["consumer_project_uuid"], client_ref=descriptor["client_ref"])
        row = query.first()
        if row is None:
            try:
                with transaction.atomic():
                    row = AcmeHubParticipant.objects.create(
                        allocation=allocation, group=group, role="trusted",
                        consumer_project_uuid=group.get_uuid(), client_ref=descriptor["client_ref"])
            except IntegrityError:
                row = query.first()
        if (row is None or row.allocation_id != allocation.pk or row.group_id != group.pk
                or row.role != "trusted" or row.revoked_at is not None):
            _audit(allocation, request, "grant_refused", participant=row, result="conflict")
            _fail("acme_reference_conflict", 409)
        _audit(allocation, request, "granted", participant=row)
        return participant_payload(row, request, controller=True)


@upgrade.admitted(shared=True)
def revoke(request, allocation_ref, participant_ref, *, route53_client=None, dns_probe=None):
    allocation = _controlled_allocation(request, allocation_ref)
    participant_ref = _uuid(participant_ref)
    with _allocation_lock(allocation.pk):
        require_controller(request, allocation)
        row = AcmeHubParticipant.objects.select_related("allocation", "group").filter(
            allocation=allocation, participant_ref=participant_ref).first()
        if row is None or row.role == "owner":
            _fail("acme_not_authorized", 403)
        # Commit revocation and lease retirement BEFORE remote cleanup. A 503
        # never rolls this decision back, and repeat revokes retry the union.
        with transaction.atomic():
            if row.revoked_at is None:
                row.revoked_at = dates.utcnow()
                row.save(update_fields=["revoked_at", "modified"])
            AcmeHubChallengeLease.objects.filter(
                participant=row, allocation=allocation, state__in=[STATE_PENDING, STATE_ACTIVE]).update(
                    state=STATE_WITHDRAWN, reconciled_at=None, last_error=None, modified=dates.utcnow())
        _audit(allocation, request, "revoked", participant=row)
        try:
            _reconcile_locked(allocation, request=request, audit_kind="revocation_cleanup", participant=row,
                              route53_client=route53_client, dns_probe=dns_probe)
        except Exception:
            _fail("acme_hub_unavailable", 503)
        return participant_payload(row, request, controller=True)


def _existing_participant(group, client_ref, request):
    client_ref = _reference(client_ref, "client_ref")
    group = _operation_group(group, request)
    return AcmeHubParticipant.objects.filter(
        consumer_project_uuid=group.get_uuid(), group=group,
        client_ref=client_ref).exists()


@upgrade.admitted
def allocate(group, client_ref, domain, request=None, **kwargs):
    args = (group, client_ref, domain)
    kwargs["request"] = request
    if upgrade.enabled() or _existing_participant(group, client_ref, request):
        return _shared_allocate(*args, **kwargs)
    from . import acme_hub_legacy
    return acme_hub_legacy.allocate(*args, **kwargs)


@upgrade.admitted
def publish(group, client_ref, challenge_ref, values, request=None, **kwargs):
    args = (group, client_ref, challenge_ref, values)
    kwargs["request"] = request
    if upgrade.enabled() or _existing_participant(group, client_ref, request):
        return _shared_publish(*args, **kwargs)
    from . import acme_hub_legacy
    return acme_hub_legacy.publish(*args, **kwargs)


@upgrade.admitted
def withdraw(group, client_ref, challenge_ref, request=None, **kwargs):
    args = (group, client_ref, challenge_ref)
    kwargs["request"] = request
    if upgrade.enabled() or _existing_participant(group, client_ref, request):
        return _shared_withdraw(*args, **kwargs)
    from . import acme_hub_legacy
    return acme_hub_legacy.withdraw(*args, **kwargs)
