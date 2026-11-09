"""Owner-only compatibility semantics while old hub writers may overlap."""
import secrets

from django.db import transaction, IntegrityError
from objict import objict

from mojo import errors as me
from mojo.helpers import dates
from mojo.apps.dnsman.models import AcmeHubDelegation, AcmeHubChallengeLease


def allocation_for(group, client_ref, request=None):
    from . import acme_hub as hub
    group = hub._operation_group(group, request)
    row = AcmeHubDelegation.objects.filter(
        owner_project_uuid=group.get_uuid(), group=group,
        client_ref=hub._reference(client_ref, "client_ref")).first()
    if row is None:
        raise me.ValueException("Unknown ACME delegation")
    return row


def allocate(group, client_ref, domain, request=None, *, route53_client=None, dns_probe=None, zone_resolver=None):
    from . import acme_hub as hub
    group = hub._operation_group(group, request)
    owner = group.get_uuid()
    client_ref = hub._reference(client_ref, "client_ref")
    domain = hub.normalize_domain(domain)
    seams = dict(route53_client=route53_client, dns_probe=dns_probe)
    query = AcmeHubDelegation.objects.filter(owner_project_uuid=owner, client_ref=client_ref)

    def existing():
        row = query.first()
        if row is not None:
            if row.domain_name != domain or row.group_id != group.pk:
                raise me.ValueException("client_ref is already bound to a different domain")
            hub._stored_zone(row, **seams)
        return row

    row = existing()
    if row is not None:
        return row
    zone = (zone_resolver or hub._new_zone)(**seams)
    for unused in range(5):
        try:
            with transaction.atomic():
                hub._operation_group(group, request)
                row = AcmeHubDelegation.objects.create(
                    owner_project_uuid=owner, client_ref=client_ref, group=group,
                    domain_name=domain, source_name=f"_acme-challenge.{domain}",
                    target_name=f"{secrets.token_hex(16)}.{zone.name}",
                    allocation_zone_name=zone.name, allocation_zone_id=zone.id)
            hub._audit(row, request, "allocated")
            return row
        except IntegrityError:
            row = existing()
            if row is not None:
                return row
    raise me.ValueException("Could not allocate an ACME delegation target", code=503, status=503)


def persist(allocation, challenge_ref, values):
    from . import acme_hub as hub
    # Every caller holds the SAME allocation advisory lock as published code.
    with transaction.atomic():
        row = AcmeHubChallengeLease.objects.select_for_update().filter(
            allocation=allocation, challenge_ref=challenge_ref).first()
        if row is not None:
            if row.state in (hub.STATE_WITHDRAWN, hub.STATE_EXPIRED):
                raise me.ValueException("challenge_ref has already been retired")
            if sorted(row.record_values or []) != values:
                raise me.ValueException("challenge_ref is already bound to different values")
            row.state = hub.STATE_PENDING
            row.expires_at = dates.add(seconds=hub.lease_seconds())
            row.reconciled_at = None
            row.last_error = None
            row.save(update_fields=["state", "expires_at", "reconciled_at", "last_error", "modified"])
        else:
            row = AcmeHubChallengeLease.objects.create(
                allocation=allocation, challenge_ref=challenge_ref, record_values=values,
                state=hub.STATE_PENDING, expires_at=dates.add(seconds=hub.lease_seconds()))
        return row


def publish(group, client_ref, challenge_ref, values, request=None, *, route53_client=None, dns_probe=None):
    from . import acme_hub as hub
    row = allocation_for(group, client_ref, request)
    challenge_ref = hub._reference(challenge_ref, "challenge_ref")
    values = hub._digest_values(values)
    proof = (dns_probe or hub.probe).verify_one_hop_cname(row.source_name, row.target_name)
    if not proof.ok:
        raise me.ValueException(proof.error or "CNAME delegation proof failed")
    with hub._allocation_lock(row.pk):
        allocation_for(group, client_ref, request)
        lease = persist(row, challenge_ref, values)
        desired = hub._reconcile_locked(row, request=request, audit_kind="published",
                                       route53_client=route53_client, dns_probe=dns_probe)
    lease.refresh_from_db()
    return objict(allocation=row, participant=None, lease=lease, active_value_count=len(desired))


def withdraw(group, client_ref, challenge_ref, request=None, *, route53_client=None, dns_probe=None):
    from . import acme_hub as hub
    row = allocation_for(group, client_ref, request)
    challenge_ref = hub._reference(challenge_ref, "challenge_ref")
    with hub._allocation_lock(row.pk):
        allocation_for(group, client_ref, request)
        lease = AcmeHubChallengeLease.objects.filter(allocation=row, challenge_ref=challenge_ref).first()
        changed = lease is not None and lease.state not in (hub.STATE_WITHDRAWN, hub.STATE_EXPIRED)
        if changed:
            lease.state, lease.reconciled_at, lease.last_error = hub.STATE_WITHDRAWN, None, None
            lease.save(update_fields=["state", "reconciled_at", "last_error", "modified"])
        desired = hub._reconcile_locked(row, request=request, audit_kind="withdrawn",
                                        route53_client=route53_client, dns_probe=dns_probe) if changed else hub._desired_values(row)
    return objict(allocation=row, participant=None, active_value_count=len(desired))
