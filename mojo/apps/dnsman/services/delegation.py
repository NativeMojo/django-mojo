"""Tenant lifecycle for optional delegated ACME DNS-01.

This module uses only the challenge-specific downstream client.  It is not a
DNS provider and intentionally exposes no arbitrary record operations.
"""

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


def is_available():
    return acme_hub_client.is_available()


def _group_active(group):
    return group is not None and group.is_effectively_active()


def _safe_hub_error(error):
    return getattr(error, "kind", None) or ERROR_HUB_UNAVAILABLE


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


def initiate(group, user, domain=None, name=None):
    """Persist ownership/client_ref, then allocate the permanent hub target."""
    from mojo.apps.account.models import Group
    from mojo.apps.dnsman.models import AcmeDelegation

    if not is_available():
        raise me.ValueException(
            "Delegated ACME is not configured", code=503, status=503)
    if group is None:
        raise me.ValueException("An active group is required")
    domain_name = naming.normalize_domain(domain.name if domain is not None else name)
    if domain is not None and domain.group_id != group.pk:
        raise me.ValueException("The domain does not belong to this group")

    tenant_uuid = group.get_uuid()
    with transaction.atomic():
        locked_group = Group.objects.select_for_update().filter(pk=group.pk).first()
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
    try:
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

    state = STATE_BROKEN if row.is_sticky else STATE_PENDING
    AcmeDelegation.objects.filter(pk=row.pk).update(
        state=state, last_error_code=code, modified=dates.utcnow())
    row.state = state
    row.last_error_code = code


def prove_alias(row):
    """Authoritatively prove the exact stored, one-hop CNAME allocation."""
    if not row.source_name or not row.target_name:
        _proof_failure(row, ERROR_HUB_MISMATCH)
        raise me.ValueException("The delegated ACME allocation is incomplete")
    proof = probe.verify_one_hop_cname(row.source_name, row.target_name)
    if not proof.ok:
        code = ERROR_ALIAS_LOOKUP if "lookup" in (proof.error or "") else ERROR_ALIAS_MISMATCH
        _proof_failure(row, code)
        raise me.ValueException(
            "The required ACME CNAME is not authoritatively verified")
    return True


def verify(row):
    """Verify the alias and atomically bind/create the certificate Domain."""
    from mojo.apps.account.models import Group
    from mojo.apps.dnsman.models import AcmeDelegation, Domain
    from mojo.apps.dnsman.models.domain import PROVIDER_MOJO, STATUS_ACTIVE

    if row.state == STATE_RETIRED:
        raise me.ValueException("This ACME delegation is retired")
    prove_alias(row)

    refusal = None
    with transaction.atomic():
        # Do not select_related nullable relations into this FOR UPDATE query:
        # PostgreSQL refuses locking the nullable side of an outer join.
        row = AcmeDelegation.objects.select_for_update().get(pk=row.pk)
        group = Group.objects.select_for_update().filter(pk=row.group_id).first()
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
            domain = row.domain
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


def for_domain(domain):
    """Return sticky delegation routing for a Domain, or None for direct DNS."""
    from mojo.apps.dnsman.models import AcmeDelegation

    row = AcmeDelegation.objects.filter(
        domain=domain, state__in=(STATE_VERIFIED, STATE_BROKEN)
    ).order_by("-verified_at").first()
    return row


def compare_challenge_result(row, result, challenge_ref):
    """Treat every downstream publish/withdraw reply as untrusted input."""
    _compare_allocation(row, result)
    if str(result.challenge_ref) != str(challenge_ref):
        raise me.ValueException(
            "The ACME hub returned a conflicting challenge reference",
            code=503, status=503)
    return result
