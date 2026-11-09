"""Tenant lifecycle for optional delegated ACME DNS-01.

This module uses only the challenge-specific downstream client.  It is not a
DNS provider and intentionally exposes no arbitrary record operations.
"""

from contextlib import contextmanager

from django.db import IntegrityError, transaction

from mojo import errors as me
from mojo.helpers import dates
from mojo.helpers.dns import probe

from mojo.apps.dnsman.models.acme_delegation import (
    STATE_BROKEN, STATE_PENDING, STATE_RETIRED, STATE_VERIFIED)
from mojo.apps.dnsman.services import acme_hub_client, naming


ERROR_GROUP_INACTIVE = "group_inactive"
ERROR_ALIAS_LOOKUP = "alias_lookup_failed"
ERROR_ALIAS_MISMATCH = "alias_mismatch"
ERROR_DOMAIN_CLAIMED = "domain_claimed"
ERROR_HUB_UNAVAILABLE = "hub_unavailable"
ERROR_HUB_MISMATCH = "hub_allocation_mismatch"
ERROR_DOMAIN_OWNER = "domain_owner_changed"


def is_available():
    return acme_hub_client.is_available()


def _group_active(group):
    return group is not None and group.is_effectively_active()


def _safe_hub_error(error):
    return getattr(error, "kind", None) or ERROR_HUB_UNAVAILABLE


def readiness_state(row):
    """Safe, read-only System Setup metadata for one delegation."""
    if row.state == STATE_VERIFIED and row.source_name and row.target_name:
        return {
            "status": "pass",
            "explanation": f"Delegated ACME DNS for {row.domain_name} is verified.",
            "remediation": "",
        }
    if row.state in (STATE_BROKEN, STATE_RETIRED):
        return {
            "status": "fail",
            "explanation": f"Delegated ACME DNS for {row.domain_name} is {row.state}.",
            "remediation": "Repair ownership and re-verify the exact ACME CNAME.",
        }
    return {
        "status": "pending",
        "explanation": f"Delegated ACME DNS for {row.domain_name} is awaiting verification.",
        "remediation": "Publish the displayed CNAME and verify the delegation.",
    }


def _compare_allocation(row, allocation):
    if (
        str(allocation.client_ref) != str(row.client_ref)
        or allocation.domain != row.domain_name
        or allocation.source != f"_acme-challenge.{row.domain_name}"
    ):
        raise me.ValueException(
            "The ACME hub returned a conflicting immutable allocation",
            code=503, status=503)
    if row.source_name and row.source_name != allocation.source:
        raise me.ValueException(
            "The ACME hub changed the immutable delegation source",
            code=503, status=503)
    if row.target_name and row.target_name != allocation.target:
        raise me.ValueException(
            "The ACME hub changed the immutable delegation target",
            code=503, status=503)


def initiate(group, user, domain=None, name=None, *, available=None):
    """Persist ownership/client_ref, then allocate the permanent hub target.

    ``available`` is an injection seam for tests, overriding the hub
    availability probe (None means ``is_available()``).
    """
    from mojo.apps.account.models import Group
    from mojo.apps.account.services import group_hierarchy
    from mojo.apps.dnsman.models import AcmeDelegation, Domain

    if available is None:
        available = is_available()
    if not available:
        raise me.ValueException(
            "Delegated ACME is not configured", code=503, status=503)
    if group is None:
        raise me.ValueException("An active group is required")
    domain_name = naming.normalize_domain(domain.name if domain is not None else name)
    if domain is not None and domain.group_id != group.pk:
        raise me.ValueException("The domain does not belong to this group")

    tenant_uuid = group.get_uuid()
    with transaction.atomic():
        # The FK insert needs a Domain KEY SHARE lock too. Take the same
        # Domain -> Group order as shared Connect before holding either FK.
        # The Group lock is NO KEY (#9740): it excludes other writers of the
        # group row, not the inserts of the tenant's other rows.
        if domain is not None:
            domain = Domain.objects.select_for_update().filter(pk=domain.pk).first()
            if domain is None or domain.group_id != group.pk or domain.name != domain_name:
                raise me.ValueException("The domain does not belong to this group")
        locked_group = group_hierarchy.lock_rows(Group.objects.filter(pk=group.pk)).first()
        if not _group_active(locked_group) or locked_group.uuid != tenant_uuid:
            raise me.ValueException("An active group is required")
        row = AcmeDelegation.objects.select_for_update().filter(
            tenant_uuid=tenant_uuid, domain_name=domain_name
        ).exclude(state=STATE_RETIRED).first()
        if row is None:
            try:
                with transaction.atomic():
                    row = AcmeDelegation.objects.create(
                        domain=domain,
                        group=locked_group,
                        user=user if getattr(user, "is_request_user", False) else None,
                        tenant_uuid=tenant_uuid,
                        tenant_name=locked_group.name,
                        user_email=getattr(user, "email", None),
                        domain_name=domain_name,
                        state=STATE_PENDING,
                    )
            except IntegrityError:
                row = AcmeDelegation.objects.select_for_update().get(
                    tenant_uuid=tenant_uuid, domain_name=domain_name,
                    state__in=(STATE_PENDING, STATE_VERIFIED, STATE_BROKEN))

    # The row (and therefore client_ref) is committed before the first remote
    # allocation.  A retry replays the same immutable reference.
    if row.connection_mode == "shared":
        raise me.ValueException("Use shared connection setup for this domain")
    try:
        row = require_current_owner(row, require_domain=row.domain_id is not None)
        allocation = acme_hub_client.allocate(domain_name, str(row.client_ref))
        _compare_allocation(row, allocation)
    except Exception as error:
        AcmeDelegation.objects.filter(pk=row.pk).update(
            last_error_code=_safe_hub_error(error), modified=dates.utcnow())
        if isinstance(error, me.ValueException):
            raise
        raise me.ValueException(
            "Could not allocate the delegated ACME target",
            code=503, status=503)

    with transaction.atomic():
        row = AcmeDelegation.objects.select_for_update().get(pk=row.pk)
        _compare_allocation(row, allocation)
        if not row.source_name:
            row.source_name = allocation.source
            row.target_name = allocation.target
        row.last_error_code = None
        row.save()
    return row


def _proof_failure(row, code):
    from mojo.apps.dnsman.models import AcmeDelegation

    # The caller may have fetched this row before another verifier made the
    # delegation sticky.  Derive the failure state from the locked persisted
    # row so stale objects can never demote verified -> pending.
    with transaction.atomic():
        stored = AcmeDelegation.objects.select_for_update().get(pk=row.pk)
        state = (STATE_RETIRED if stored.state == STATE_RETIRED else
                 STATE_BROKEN if stored.verified_at is not None else STATE_PENDING)
        stored.state = state
        stored.last_error_code = code
        stored.save(update_fields=["state", "last_error_code", "modified"])
    row.state = state
    row.last_error_code = code


class _BindingChanged(Exception):
    pass


@contextmanager
def _lifecycle_locks(row):
    """Lock Domain, Group, Delegation, retrying an intervening first binding.

    A savepoint rollback releases acquired locks even when our caller owns an
    outer transaction; a normal continue would retain them until its commit.
    """
    from mojo.apps.account.models import Group
    from mojo.apps.account.services import group_hierarchy
    from mojo.apps.dnsman.models import AcmeDelegation, Domain
    for unused in range(3):
        try:
            with transaction.atomic():
                snapshot = AcmeDelegation.objects.values(
                    "domain_id", "domain_name", "group_id").get(pk=row.pk)
                domains = Domain.objects.select_for_update()
                domain = domains.filter(pk=snapshot["domain_id"]).first() if snapshot["domain_id"] else domains.filter(name=snapshot["domain_name"]).first()
                group = group_hierarchy.lock_rows(Group.objects.filter(pk=snapshot["group_id"])).first()
                stored = AcmeDelegation.objects.select_for_update().get(pk=row.pk)
                if stored.domain_id != snapshot["domain_id"]:
                    raise _BindingChanged()
                if stored.domain_id is None:
                    current_id = Domain.objects.filter(name=stored.domain_name).values_list("pk", flat=True).first()
                    if current_id != (domain.pk if domain else None):
                        raise _BindingChanged()
                yield stored, group, domain
                return
        except _BindingChanged:
            continue
    raise me.ValueException("The ACME connection changed; retry setup", code=409, status=409)


def require_current_owner(row, require_domain=True):
    """Lock and re-prove the immutable tenant/domain binding before remote I/O."""
    from mojo.apps.account.models import Group
    from mojo.apps.dnsman.models import AcmeDelegation, Domain

    refusal = None
    with _lifecycle_locks(row) as (stored, group, domain):
        if (
            not _group_active(group)
            or group.uuid != stored.tenant_uuid
        ):
            stored.state = STATE_RETIRED
            stored.retired_at = stored.retired_at or dates.utcnow()
            stored.last_error_code = ERROR_GROUP_INACTIVE
            refusal = "The delegation tenant is no longer active"
        elif require_domain and (
            domain is None
            or domain.group_id != group.pk
            or domain.name != stored.domain_name
        ):
            stored.state = (STATE_RETIRED if stored.state == STATE_RETIRED else
                            STATE_BROKEN if stored.verified_at is not None else STATE_RETIRED)
            stored.retired_at = (
                stored.retired_at or dates.utcnow()
                if stored.state == STATE_RETIRED else None)
            stored.last_error_code = ERROR_DOMAIN_OWNER
            refusal = "The delegated domain is no longer owned by this tenant"
        if refusal:
            stored.save(update_fields=[
                "state", "retired_at", "last_error_code", "modified"])

    if refusal:
        row.state = stored.state
        row.retired_at = stored.retired_at
        row.last_error_code = stored.last_error_code
        raise me.ValueException(refusal)
    return stored


def prove_alias(row, resolve_cname=None):
    """Authoritatively prove the exact stored, one-hop CNAME allocation."""
    if not row.source_name or not row.target_name:
        _proof_failure(row, ERROR_HUB_MISMATCH)
        raise me.ValueException("The delegated ACME allocation is incomplete")
    proof = (resolve_cname or probe.verify_one_hop_cname)(row.source_name, row.target_name)
    if not proof.ok:
        code = ERROR_ALIAS_LOOKUP if "lookup" in (proof.error or "") else ERROR_ALIAS_MISMATCH
        _proof_failure(row, code)
        raise me.ValueException(
            "The required ACME CNAME is not authoritatively verified")
    return True


def verify(row, resolve_cname=None, shared_checked=False):
    """Verify the alias and atomically bind/create the certificate Domain."""
    from mojo.apps.account.models import Group
    from mojo.apps.dnsman.models import AcmeDelegation, Domain
    from mojo.apps.dnsman.models.domain import PROVIDER_MOJO, STATUS_ACTIVE

    if row.state == STATE_RETIRED:
        raise me.ValueException("This ACME delegation is retired")
    if row.connection_mode == "shared" and not shared_checked:
        raise me.ValueException("Use shared connection Check for this domain")
    prove_alias(row, resolve_cname=resolve_cname)

    refusal = None
    with _lifecycle_locks(row) as (row, group, locked_domain):
        # Proof is network I/O. A restart may have retired this exact ref while
        # it ran; never revive it or let the partial uniqueness constraint turn
        # a stale Check into an unhandled IntegrityError.
        if row.state == STATE_RETIRED:
            raise me.ValueException("This ACME connection is stale", code=409, status=409)
        if row.connection_mode == "shared":
            current_id = AcmeDelegation.objects.filter(
                tenant_uuid=row.tenant_uuid, domain_name=row.domain_name).exclude(
                    state=STATE_RETIRED).order_by("-created", "-pk").values_list("pk", flat=True).first()
            if current_id != row.pk:
                raise me.ValueException("This ACME connection is stale", code=409, status=409)
        if (
            not _group_active(group)
            or group.uuid != row.tenant_uuid
        ):
            row.state = STATE_RETIRED
            row.retired_at = dates.utcnow()
            row.last_error_code = ERROR_GROUP_INACTIVE
            row.save(update_fields=[
                "state", "retired_at", "last_error_code", "modified"])
            refusal = "The delegation tenant is no longer active"
        else:
            domain = locked_domain if row.domain_id else None
            if domain is not None:
                # The Domain can be deleted or transferred while the tenant is
                # publishing its CNAME.  Re-check the locked owner now.
                domain = Domain.objects.select_for_update().filter(pk=domain.pk).first()
                if domain is None or domain.group_id != group.pk:
                    refusal = "The delegated domain is no longer owned by this tenant"
            else:
                existing = Domain.objects.select_for_update().filter(
                    name=row.domain_name).first()
                if row.is_sticky:
                    refusal = "The verified delegated domain no longer exists"
                    row.state = STATE_BROKEN
                    row.last_error_code = ERROR_DOMAIN_CLAIMED
                    row.save(update_fields=["state", "last_error_code", "modified"])
                elif existing is not None:
                    refusal = "This domain name is already claimed"
                    row.last_error_code = ERROR_DOMAIN_CLAIMED
                    row.save(update_fields=["last_error_code", "modified"])
                else:
                    try:
                        # Nested savepoint keeps a unique-name race from
                        # poisoning the outer transaction before we can record
                        # the bounded losing-claim state.
                        with transaction.atomic():
                            domain = Domain.objects.create(
                                group=group,
                                user=row.user,
                                name=row.domain_name,
                                provider=PROVIDER_MOJO,
                                status=STATUS_ACTIVE,
                                verified=True,
                                metadata={"certificate_only": True},
                            )
                    except IntegrityError:
                        refusal = "This domain name is already claimed"
                        row.last_error_code = ERROR_DOMAIN_CLAIMED
                        row.save(update_fields=["last_error_code", "modified"])

            if refusal is None:
                row.domain = domain
                row.state = STATE_VERIFIED
                row.verified_at = row.verified_at or dates.utcnow()
                row.last_error_code = None
                row.save()

    if refusal:
        raise me.ValueException(refusal)
    return row


def _routing_rows():
    """Shared pending intent and every once-verified tombstone are deny guards.

    Newest live shared intent wins over history. Legacy pending allocations
    remain inert. All consumers use this selector and ordering.
    """
    from django.db.models import Case, IntegerField, Q, Value, When
    from mojo.apps.dnsman.models import AcmeDelegation

    live_shared = Q(connection_mode="shared") & ~Q(state=STATE_RETIRED)
    return AcmeDelegation.objects.filter(
        live_shared | Q(verified_at__isnull=False)
    ).annotate(_route_priority=Case(
        When(live_shared, then=Value(0)), default=Value(1),
        output_field=IntegerField())).order_by("domain_id", "_route_priority", "-created", "-pk")


def routing_for_domain(domain):
    return _routing_rows().filter(domain_id=domain.pk).first()


def routing_for_domains(domain_ids):
    rows = {}
    for row in _routing_rows().filter(domain_id__in=domain_ids):
        rows.setdefault(row.domain_id, row)
    return rows


def routed_domain_ids():
    return _routing_rows().filter(domain_id__isnull=False).order_by().values_list("domain_id", flat=True)


def for_domain(domain):
    """Compatibility wrapper: a route is not proof of readiness."""
    return routing_for_domain(domain)


def require_shared_authority(row, client=None):
    """Refresh the authenticated consumer and exact participant before CA I/O."""
    if row.connection_mode != "shared":
        return row
    from mojo.apps.dnsman.services import delegation_setup
    delegation_setup.validate_shared(row, client=client)
    return row


def compare_challenge_result(row, result, challenge_ref):
    """Treat every downstream publish/withdraw reply as untrusted input."""
    _compare_allocation(row, result)
    if str(result.challenge_ref) != str(challenge_ref):
        raise me.ValueException(
            "The ACME hub returned a conflicting challenge reference",
            code=503, status=503)
    return result
