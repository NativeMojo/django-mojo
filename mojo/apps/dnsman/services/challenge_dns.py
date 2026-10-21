"""Owner-aware ACME producer boundary; qualified recovery is a separate action.

Inventory/mirror diagnostics remain compatibility facts. Only exact journal
receipts, producer coverage and permanent retirement can establish finality.
"""
import uuid

from mojo.apps.dnsman.services import challenge_attempts as attempts


def reservation_identity(reservation):
    return {"kind": "inline_reservation", "domain": reservation.domain_id,
            "owner_ref": str(reservation.owner_ref), "name": reservation.record_name}


def _scope(domain, name):
    return {"provider": domain.provider, "zone_id": domain.hosted_zone_id or "",
            "account_id": "", "name": name.lower().rstrip("."), "type": "TXT",
            "scope_class": "inline_acme_txt"}


def reservation_owner(reservation, journal=None):
    journal = journal or attempts.Journal()
    identity = reservation_identity(reservation)
    rows = journal.project(owner={"identity": identity}, kinds=["origin"])
    if len(rows) != 1:
        raise attempts.JournalError("legacy_history_unknown")
    owner = rows[0]["owner"]
    domain = reservation.domain
    expected = {"domain": domain.pk, "certificate": reservation.certificate_id,
                "reservation": reservation.pk, "record_name": reservation.record_name,
                "provider": domain.provider, "zone_id": domain.hosted_zone_id or ""}
    if any(owner["binding"].get(key) != value for key, value in expected.items()):
        raise attempts.JournalError("stale_owner", 409)
    return owner, rows[0]


def record_origin(reservation, *, fresh=False, owner_snapshot=None, journal=None):
    journal = journal or attempts.Journal()
    identity = reservation_identity(reservation)
    existing = journal.project(owner={"identity": identity}, kinds=["origin"])
    if existing:
        return reservation_owner(reservation, journal)[1]
    domain = reservation.domain
    scope = _scope(domain, reservation.record_name)
    owner = {"identity": identity, "binding": {
        "domain": domain.pk, "certificate": reservation.certificate_id,
        "reservation": reservation.pk, "record_name": reservation.record_name,
        "provider": domain.provider, "zone_id": domain.hosted_zone_id or "",
        "tenant_uuid": getattr(domain.group, "uuid", None) if domain.group_id else None,
        "scope": scope, "application": owner_snapshot}}
    anchor = attempts.BackendFence()
    return journal.append("origin", scope, owner, domain.provider,
                          {"legacy_history": not fresh, "owned_values": list(reservation.record_values)}, anchor.identity)


def require_reusable(reservation, desired):
    journal = attempts.Journal()
    identity = {"identity": reservation_identity(reservation)}
    rows = journal.project(owner=identity)
    if (rows and not attempts.all_final(rows)
            and set(reservation.record_values or []) != set(desired)):
        raise attempts.JournalError("prior_attempt_unknown", 409)
    return attempts.all_final(rows) if rows else False


def retired_owner_digests(scope, journal=None):
    journal = journal or attempts.Journal()
    return {row["owner_digest"] for row in journal.project(scope=scope, kinds=["retirement_barrier"])}


def _receipt(journal, intent, result, provider):
    if isinstance(result, dict):
        change_id = result.get("change_id")
        request_id = result.get("request_id", "")
    else:
        change_id, request_id = result, ""
    if provider == "route53" and isinstance(change_id, str) and 0 < len(change_id) <= 256:
        intent.row = journal.receipt(intent.row, "accepted", change_id=change_id, request_id=request_id)
    else:
        intent.row = journal.receipt(intent.row, "unknown", reason="provider_receipt_unavailable")
    return change_id


def _send(adapter, action, scope, owner, values, ttl, journal):
    with attempts.scope_lock(scope) as anchor:
        if journal.project(owner=owner, kinds=["retirement_barrier"]):
            raise attempts.JournalError("cleanup_pending", 409)
        # Real adapters prepare the exact provider request before this append.
        # An injected compatibility adapter is a test/legacy mechanism seam;
        # its logical request is explicitly not finality qualification.
        if hasattr(adapter, "prepare_challenge_mutation"):
            request, callback = adapter.prepare_challenge_mutation(action, scope["name"], values, ttl)
        else:
            request = {"action": action, "name": scope["name"], "type": "TXT", "record_values": list(values or []), "ttl": ttl}
            if action == "publish":
                callback = lambda: adapter.upsert_record("TXT", scope["name"], values, ttl=ttl)
            else:
                method = adapter.clear_record if action == "clear" else adapter.delete_record
                callback = lambda: method("TXT", scope["name"], record_values=values)
        if request is None:
            return None
        intent = None
        def current():
            if journal.project(owner=owner, kinds=["retirement_barrier"]):
                raise attempts.JournalError("cleanup_pending", 409)
        def no_change(error):
            from mojo.helpers.aws.route53_challenge import ChallengeError
            return isinstance(error, ChallengeError) and error.no_change is True
        try:
            intent = journal.prepare("provider_mutation", scope, owner, adapter.name, request, anchor)
            result = intent.send(callback, guard=current, no_change_if=no_change)
            return _receipt(journal, intent, result, adapter.name)
        finally:
            close = getattr(callback, "close", None)
            if callable(close):
                close()


def reservation_mutation(reservation, adapter, action, values, ttl=60):
    journal = attempts.Journal()
    record_origin(reservation, journal=journal)
    owner, origin = reservation_owner(reservation, journal)
    return _send(adapter, action, owner["binding"]["scope"], owner, values, ttl, journal)


def compatibility_mutation(domain, adapter, action, name, values, ttl=60):
    """Record an unowned old producer without manufacturing qualified history."""
    journal = attempts.Journal()
    scope = _scope(domain, name)
    owner = {"identity": {"kind": "legacy_unowned", "operation": uuid.uuid4().hex},
             "binding": {"domain": domain.pk, "scope": scope}}
    anchor = attempts.BackendFence()
    journal.append("origin", scope, owner, adapter.name,
                   {"legacy_history": True, "owned_values": list(values or [])}, anchor.identity)
    return _send(adapter, action, scope, owner, values, ttl, journal)


def hub_scope(allocation):
    return {"provider": "route53", "zone_id": allocation.allocation_zone_id,
            "account_id": "", "name": allocation.target_name, "type": "TXT",
            "scope_class": "hub_target_txt"}


def hub_owner(allocation, participant, challenge_ref):
    """The permanent reference identity is independent of a rollbackable lease."""
    return {"identity": {"kind": "hub_challenge", "allocation": allocation.pk,
                "participant": str(participant.participant_ref) if participant else None,
                "consumer": participant.consumer_project_uuid if participant else allocation.owner_project_uuid,
                "client_ref": participant.client_ref if participant else allocation.client_ref,
                "challenge_ref": challenge_ref},
            "binding": {"scope": hub_scope(allocation), "domain": allocation.domain_name,
                        "allocation_created": allocation.created.isoformat()}}


def hub_origin(allocation, participant, challenge_ref, values, *, fresh=False, journal=None):
    journal = journal or attempts.Journal()
    owner = hub_owner(allocation, participant, challenge_ref)
    existing = journal.project(owner=owner, kinds=["origin"])
    if existing:
        if existing[0]["request"].get("owned_values") != list(values):
            raise attempts.JournalError("journal_intent_conflict", 409)
        return owner, existing[0]
    row = journal.append("origin", hub_scope(allocation), owner, "route53",
        {"legacy_history": not fresh, "owned_values": list(values)}, attempts.BackendFence().identity)
    return owner, row


def _hub_identities(allocation, participant, challenge_ref):
    owners = [hub_owner(allocation, participant, challenge_ref)]
    if participant is None:
        from mojo.apps.dnsman.models import AcmeHubParticipant
        current = AcmeHubParticipant.objects.filter(allocation=allocation, role="owner",
            consumer_project_uuid=allocation.owner_project_uuid, client_ref=allocation.client_ref).first()
        if current is not None:
            owners.append(hub_owner(allocation, current, challenge_ref))
    elif (participant.role == "owner" and participant.consumer_project_uuid == allocation.owner_project_uuid
            and participant.client_ref == allocation.client_ref):
        owners.append(hub_owner(allocation, None, challenge_ref))
    return owners


def hub_lease_retired(allocation, lease, retired):
    return any(attempts.owner_digest(owner) in retired
        for owner in _hub_identities(allocation, lease.participant, lease.challenge_ref))


def require_hub_publish(allocation, participant, challenge_ref):
    owners = _hub_identities(allocation, participant, challenge_ref)
    journal = attempts.Journal()
    if any(journal.project(owner=owner, kinds=["retirement_barrier"]) for owner in owners):
        raise attempts.JournalError("cleanup_pending", 409)
    return owners[0]


def hub_retired(allocation, journal=None):
    return retired_owner_digests(hub_scope(allocation), journal)


def hub_write(allocation, values, *, compatibility=None, ttl=60):
    """All hub writes enter this scope fence, including legacy reconciliation.

    Exact raw deletion removes only journal/lease-owned contributions. External
    raw values and an identical value still desired by a live peer survive.
    """
    from mojo.apps.dnsman.models import AcmeHubChallengeLease
    from mojo.helpers.aws import route53_challenge as raw
    journal = attempts.Journal()
    scope = hub_scope(allocation)
    with attempts.scope_lock(scope) as anchor:
        retired = hub_retired(allocation, journal)
        from mojo.apps.dnsman.services import acme_hub
        fresh = acme_hub._desired_snapshot(allocation)[0]
        if sorted(values) != fresh:
            raise attempts.JournalError("generation_changed", 409)
        owner = {"identity": {"kind": "hub_reconcile", "allocation": allocation.pk,
                    "operation": uuid.uuid4().hex}, "binding": {"scope": scope}}
        journal.append("origin", scope, owner, "route53", {"legacy_history": True}, anchor.identity)
        close = None
        if compatibility is not None:
            request, callback = compatibility()
        else:
            client = raw.mutation_client()
            close = client.close
            try:
                snapshot = raw.snapshot(client, allocation.allocation_zone_id, allocation.target_name)
                owned = set()
                for lease in AcmeHubChallengeLease.objects.filter(allocation=allocation):
                    owned.update(lease.record_values or [])
                for origin in journal.project(scope=scope, kinds=["origin"]):
                    owned.update(origin["request"].get("owned_values", []))
                request = raw.batch(allocation.allocation_zone_id, allocation.target_name,
                    snapshot, sorted(owned), values, ttl=ttl, comment="mojo-acme:" + uuid.uuid4().hex)
                callback = lambda: raw.submit(client, request)
            except Exception:
                close()
                raise
        try:
            if request is None:
                return None
            intent = journal.prepare("provider_mutation", scope, owner, "route53", request, anchor)
            def current():
                if hub_retired(allocation, journal) != retired or acme_hub._desired_snapshot(allocation)[0] != values:
                    raise attempts.JournalError("generation_changed", 409)
            result = intent.send(callback, guard=current,
                no_change_if=lambda error: isinstance(error, raw.ChallengeError) and error.no_change is True)
            return _receipt(journal, intent, result, "route53")
        finally:
            if close:
                close()


def outgoing_hub(operation, payload, config, send):
    """Instrument the actual outgoing mutation; old acknowledgements stay unknown."""
    journal = attempts.Journal()
    scope = {"provider": "hub", "origin": config.base_url,
             "client_ref": payload["client_ref"], "scope_class": "hub_consumer"}
    owner = {"identity": {"kind": "hub_consumer", "origin": config.base_url,
                "client_ref": payload["client_ref"], "challenge_ref": payload["challenge_ref"]},
             "binding": {"scope": scope}}
    with attempts.scope_lock(scope) as anchor:
        origins = journal.project(owner=owner, kinds=["origin"])
        if not origins:
            journal.append("origin", scope, owner, "hub",
                {"legacy_history": operation != "publish", "values": payload.get("values", [])}, anchor.identity)
        if operation == "publish" and journal.project(owner=owner, kinds=["retirement_barrier"]):
            raise attempts.JournalError("cleanup_pending", 409)
        request = {"operation": operation, "payload": payload, "origin": config.base_url}
        intent = journal.prepare("hub_mutation", scope, owner, "hub", request, anchor)
        result = intent.send(send)
        # Ordinary v1/v2 publish/withdraw acks are not finality evidence.
        intent.row = journal.receipt(intent.row, "unknown", reason="legacy_acknowledgement")
        return result

