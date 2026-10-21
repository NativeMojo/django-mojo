"""Local shared-validation setup. Reads never prepare, verify, queue or demote.

The only remote destination is acme_hub_client's deployment-configured hub.
``actor=None`` is reserved for a trusted local management command. Request and
assistant callers must pass their request/identity on every action.
"""
from contextlib import ExitStack

from django.db import transaction
from django.db.models import Q
from django.db.models.functions import Lower
from objict import objict

from mojo import errors as me
from mojo.helpers import dates
from mojo.helpers.request import is_signed_in_user, restricted_identity
from mojo.models.secrets import SecretsUnavailableError
from mojo.apps.dnsman.services import acme_hub_client, delegation, naming

VIEW = ["view_dns", "manage_dns", "security"]
WRITE = ["manage_dns", "security"]
ERRORS = {
    "trust_missing": "The allocation controller must authorize this connection request.",
    "trust_revoked": "The allocation controller revoked this connection.",
    "hub_identity_changed": "The configured hub consumer identity has changed.",
    "allocation_conflict": "The immutable allocation does not match this connection.",
    "alias_mismatch": "The exact one-hop validation CNAME does not match.",
    "alias_lookup_failed": "The validation CNAME could not be checked.",
    "connection_not_verified": "Check this connection before requesting a certificate.",
    "certificate_busy": "A certificate worker is using this domain; retry after it finishes.",
    "configuration_busy": "Shared-validation configuration is busy; refresh before retrying.",
    "cleanup_pending": "An earlier challenge still needs isolated cleanup.",
    "profile_conflict": "An existing certificate has a different name profile.",
    "stale_connection": "This connection is no longer the current request.",
    "unsupported_domain_scope": "Shared setup currently requires a group-managed domain; existing direct-provider operation remains available.",
    "hub_unconfigured": "Configure the deployment's ACME hub access.",
    "hub_misconfigured": "Repair the deployment's ACME hub configuration.",
    "hub_unsupported": "Upgrade the configured ACME hub to shared-validation protocol v2.",
    "hub_unauthorized": "Repair this deployment's protected ACME hub access.",
    "hub_unavailable": "The configured ACME hub is unavailable; retry later.",
    "hub_rate_limited": "The configured ACME hub rate limited this request.",
}


class SetupError(me.ValueException):
    def __init__(self, error_code, data=None, status=None):
        self.error_code = error_code
        self.data = data
        status = status or (429 if error_code == "hub_rate_limited" else 503 if error_code.startswith("hub_") and error_code != "hub_identity_changed" else 409)
        super().__init__(ERRORS[error_code], code=status, status=status)


def _id(value):
    if isinstance(value, bool):
        raise me.ValueException("A positive integer id is required")
    if hasattr(value, "pk"):
        value = value.pk
    if not isinstance(value, (int, str)) or len(str(value)) > 19 or not str(value).isdigit() or not 1 <= int(value) <= 9223372036854775807:
        raise me.ValueException("A positive integer id is required")
    return int(value)


def _identity(actor):
    if actor is None:
        return None
    identity = restricted_identity(actor) or actor.user if hasattr(actor, "user") else actor
    if hasattr(identity, "_meta") and getattr(identity, "pk", None):
        return type(identity).objects.filter(pk=identity.pk).first()
    return identity


def can_access(actor, group, write=False):
    if group is not None and not group.is_effectively_active():
        return False
    if actor is None:
        return True
    identity = _identity(actor)
    if identity is None or not getattr(identity, "is_active", False):
        return False
    perms = WRITE if write else VIEW
    if group is None:
        # House-domain reads follow Domain VIEW_PERMS; mutations are ineligible.
        return not write and bool(identity.has_permission(perms)) and is_signed_in_user(identity)
    if not group.is_effectively_active():
        return False
    if hasattr(identity, "is_group_allowed"):
        return bool(identity.is_group_allowed(group) and identity.has_permission(perms))
    return bool(group.user_has_permission(identity, perms))


def _require(actor, group, write=False):
    if not can_access(actor, group, write):
        raise me.PermissionDeniedException("DNS setup is not available for this target")


def resolve_target(domain=None, group=None, name=None, actor=None, write=False):
    from mojo.apps.account.models import Group
    from mojo.apps.dnsman.models import Domain

    if domain is not None:
        if group is not None or name is not None:
            raise me.ValueException("Provide either domain, or group and name")
        row = Domain.objects.select_related("group").filter(pk=_id(domain)).first()
        if row is None or not can_access(actor, row.group, False):
            raise me.ValueException("DNS target not found", code=404, status=404)
        target = objict(domain=row, group=row.group, name=naming.normalize_domain(row.name))
    else:
        if group is None or not isinstance(name, str):
            raise me.ValueException("Provide either domain, or group and name")
        group = Group.get_active(_id(group))
        if group is None:
            raise me.ValueException("DNS target not found", code=404, status=404)
        _require(actor, group, write)
        normalized = naming.normalize_domain(name)
        if len(normalized) > 253:
            raise me.ValueException("A normalized apex name of at most 253 characters is required")
        row = Domain.objects.select_related("group").filter(name=normalized).first()
        if row is not None and row.group_id != group.pk:
            raise me.PermissionDeniedException("DNS setup is not available for this target")
        target = objict(domain=row, group=group, name=normalized)
    if write:
        if target.group is None:
            raise SetupError("unsupported_domain_scope")
        _require(actor, target.group, True)
    elif target.group is not None:
        _require(actor, target.group)
    return target


def _refresh(target, actor=None, write=False):
    if target.domain is not None:
        fresh = resolve_target(domain=target.domain.pk, actor=actor, write=write)
        if fresh.name != target.name or (fresh.group.pk if fresh.group else None) != (target.group.pk if target.group else None):
            raise SetupError("stale_connection")
        return fresh
    return resolve_target(group=target.group.pk, name=target.name, actor=actor, write=write)


def resolve_connection(value, actor=None, write=False):
    from mojo.apps.dnsman.models import AcmeDelegation
    row = AcmeDelegation.objects.select_related("domain", "group").filter(pk=_id(value)).first()
    if row is None or not can_access(actor, row.group, write):
        raise me.ValueException("DNS connection not found", code=404, status=404)
    if row.group is None:
        raise SetupError("unsupported_domain_scope")
    target = resolve_target(domain=row.domain_id, actor=actor, write=write) if row.domain_id else resolve_target(
        group=row.group_id, name=row.domain_name, actor=actor, write=write)
    if row.tenant_uuid != row.group.uuid or row.domain_name != target.name or row.group_id != target.group.pk:
        raise SetupError("stale_connection")
    return row


def _current(target):
    from mojo.apps.dnsman.models import AcmeDelegation
    rows = AcmeDelegation.objects.filter(domain_name=target.name)
    if target.group is not None:
        rows = rows.filter(group_id=target.group.pk)
    elif target.domain is not None:
        rows = rows.filter(domain_id=target.domain.pk)
    else:
        return None
    live = rows.exclude(state="retired").order_by("-created", "-pk").first()
    return live or rows.filter(verified_at__isnull=False).order_by("-created", "-pk").first()


def _hub_error(error, capabilities=False):
    kind = getattr(error, "kind", None)
    status = getattr(error, "http_status", None)
    if kind == "acme_trust_revoked":
        return "trust_revoked"
    if kind == "acme_not_authorized" and not capabilities:
        return "trust_missing"
    if kind == "acme_reference_conflict":
        return "allocation_conflict"
    if kind == "acme_alias_mismatch":
        return "alias_mismatch"
    if kind == "acme_alias_lookup_failed":
        return "alias_lookup_failed"
    if kind == "configuration":
        return "hub_misconfigured"
    if status == 404 and capabilities:
        return "hub_unsupported"
    if status in (401, 403):
        return "hub_unauthorized"
    if status == 429:
        return "hub_rate_limited"
    return "hub_unavailable"


def _preflight(target, client, allocation_ref=None):
    try:
        if client is acme_hub_client and not client.is_available():
            raise SetupError("hub_unconfigured", status=503)
        caps = client.capabilities()
    except SetupError:
        raise
    except Exception as error:
        raise SetupError(_hub_error(error, capabilities=True), status=429 if getattr(error, "http_status", None) == 429 else 503) from None
    if caps.get("version") == 2 and caps.get("shared_validation") is False:
        raise SetupError("hub_unconfigured", status=503)
    if caps.get("version") != 2 or caps.get("shared_validation") is not True:
        raise SetupError("hub_unsupported", status=503)
    if not caps.get("can_consume") or not caps.get("consumer_project_uuid"):
        raise SetupError("hub_unauthorized", status=503)
    try:
        # Authenticated own-participant lookup is deliberately before prepare.
        # A page is bounded; an explicit target need not enumerate every grant.
        page = client.lookup(target.name, allocation_ref=allocation_ref)
    except Exception as error:
        raise SetupError(_hub_error(error), status=429 if getattr(error, "http_status", None) == 429 else 503) from None
    return caps, page["items"]


def _compare(row, participant):
    if (str(participant.get("client_ref")) != str(row.client_ref)
            or participant.get("domain") != row.domain_name
            or participant.get("allocation_ref") != row.hub_allocation_ref
            or participant.get("target") != row.hub_allocation_ref
            or participant.get("source") != f"_acme-challenge.{row.domain_name}"
            or participant.get("scope") != [row.domain_name, f"*.{row.domain_name}"]
            or row.hub_participant_ref and participant.get("participant_ref") != row.hub_participant_ref
            or row.target_name and participant.get("target") != row.target_name
            or row.source_name and participant.get("source") != row.source_name):
        raise SetupError("allocation_conflict")


def _shared_status(row, client, caps=None, require_active=True):
    if caps is None:
        try:
            caps = client.capabilities()
        except Exception as error:
            raise SetupError(_hub_error(error, capabilities=True), status=503) from None
    if caps.get("consumer_project_uuid") != row.hub_consumer_project_uuid:
        raise SetupError("hub_identity_changed")
    # A published connection can renew during the compatible rollout before
    # new setup/grants are enabled. Its exact pinned participant must still
    # authenticate and pass all identity, scope and active-trust checks below.
    existing = bool(row.hub_participant_ref) and caps.get("shared_validation") is False
    if caps.get("version") != 2 or not caps.get("can_consume") and not existing:
        raise SetupError("hub_unsupported", status=503)
    try:
        participant = client.status(str(row.client_ref))
    except Exception as error:
        raise SetupError(_hub_error(error)) from None
    _compare(row, participant)
    if not require_active:
        return participant
    if participant.get("trust_state") == "revoked":
        raise SetupError("trust_revoked")
    if participant.get("trust_state") != "active" or not participant.get("can_publish"):
        raise SetupError("trust_missing")
    return participant


def validate_shared(row, client=None):
    """Runtime check; safe failures persist only bounded tokens, never remote text."""
    client = client or acme_hub_client
    row = delegation.require_current_owner(row)
    try:
        participant = _shared_status(row, client)
        _alias_required(participant.get("alias", {}))
        return participant
    except SetupError as error:
        delegation._proof_failure(row, error.error_code)
        raise


def _alias_required(alias):
    if alias.get("state") == "lookup_failed":
        raise SetupError("alias_lookup_failed")
    if alias.get("state") != "match" or alias.get("one_hop") is not True:
        raise SetupError("alias_mismatch")


def _iso(value):
    return value.isoformat() if value is not None else None


def _selected_certificate(target):
    from mojo.apps.dnsman.models import Certificate
    if target.domain is None:
        return None
    wanted = sorted([target.name, f"*.{target.name}"])
    rows = list(Certificate.objects.filter(domain_id=target.domain.pk).order_by("-created", "-pk"))
    return next((row for row in rows if sorted(row.sans or [row.common_name]) == wanted and row.status in ("active", "issuing", "pending")), None) or next(
        (row for row in rows if sorted(row.sans or [row.common_name]) == wanted), None)


def _certificate_status(cert, custody=None):
    if cert is None:
        return None
    try:
        available = custody(cert) if custody else cert.has_material
        material = "available" if available else "missing"
        serving = bool(available and cert.not_after and cert.not_after > dates.utcnow())
    except SecretsUnavailableError:
        material, serving = "unavailable", None
    return dict(id=cert.pk, state=cert.status, names=list(cert.sans or [cert.common_name]),
                not_after=_iso(cert.not_after), renew_after=_iso(cert.renew_after),
                serving_valid=serving, material_state=material)


def _certificate_job(cert):
    from mojo.apps.jobs.models import Job
    from mojo.apps.dnsman.services import certs
    return Job.objects.filter(func__in=(certs.ISSUE_JOB, certs.RENEW_JOB),
        payload={"certificate": cert.pk}, broadcast=False).order_by("-created", "-pk").first()


def _renewal(cert, row, certificate, blocked, issuer_active=False):
    if cert is None:
        return dict(state="not_requested", next_attempt_at=None, attempts=0, error_code=None,
                    cleanup_pending=bool(row and row.cleanup_challenge_ref), job=None, worker_version=None)
    from mojo.apps.dnsman.services import certs
    job = _certificate_job(cert)
    if blocked:
        state = "blocked"
    elif issuer_active:
        state = "issuing"
    elif job and job.status in ("pending", "running"):
        state = "queued" if job.status == "pending" else "issuing"
    elif cert.status in ("pending", "issuing"):
        state = "failed" if job and job.status == "failed" else "blocked"
    elif job and job.status == "failed" and job.last_error == certs.ISSUE_BUSY_ERROR:
        state = "failed"
    elif job and job.status in ("canceled", "expired") and cert.renew_after and cert.renew_after <= dates.utcnow():
        state = "blocked"
    elif cert.not_after and cert.not_after <= dates.utcnow():
        state = "expired"
    elif cert.last_error and cert.status == "active":
        state = "retrying"
    elif cert.status == "failed":
        state = "failed"
    elif cert.status == "active":
        state = "scheduled"
    else:
        state = "unknown"
    return dict(state=state, next_attempt_at=_iso(cert.renew_after), attempts=cert.attempts,
                error_code="certificate_busy" if job and job.last_error == certs.ISSUE_BUSY_ERROR else (
                    (row.last_error_code if row and row.last_error_code in ERRORS else "issuance_failed") if cert.last_error else None),
                cleanup_pending=bool(row and row.cleanup_challenge_ref),
                job=dict(id=job.pk, state=job.status) if job else None, worker_version=None)


def _descriptor(row):
    return dict(version=1, consumer_project_uuid=row.hub_consumer_project_uuid,
                client_ref=str(row.client_ref), domain=row.domain_name, allocation_ref=row.hub_allocation_ref)


def plan(target, allocation_ref=None, actor=None, client=None, custody=None, operation=None, _issuer_active=False):
    target = _refresh(target, actor)
    client = client or acme_hub_client
    if allocation_ref is not None:
        allocation_ref = naming.normalize_domain(allocation_ref)
        if len(allocation_ref) > 253:
            raise me.ValueException("An allocation target of at most 253 characters is required")
    row = _current(target)
    caps, participant, failure = None, None, None
    trust = dict(state="not_requested" if row is None else "unknown", can_publish=False, can_withdraw=False)
    alias = dict(state="unknown", source=f"_acme-challenge.{target.name}", target=allocation_ref,
                 observed_target=None, one_hop=None, checked_at=None)
    if row:
        alias.update(source=row.source_name or alias["source"], target=row.target_name or row.hub_allocation_ref)
    # House-domain rollout is local/read-only and does not expose descriptors.
    if target.group is not None:
        try:
            caps, participants = _preflight(target, client, allocation_ref or (row.hub_allocation_ref if row else None))
            if row and row.connection_mode == "shared":
                participant = _shared_status(row, client, caps=caps, require_active=False)
                trust.update(state=participant.trust_state, can_publish=bool(participant.can_publish), can_withdraw=bool(participant.can_withdraw))
                alias.update(participant.get("alias", {}))
                alias.update(source=participant.source, target=participant.target)
            elif not row and not allocation_ref:
                choices = {p["allocation_ref"] for p in participants}
                if len(choices) == 1:
                    alias["target"] = next(iter(choices))
        except SetupError as error:
            failure = error.error_code
            if failure in ("trust_missing", "trust_revoked", "hub_identity_changed"):
                trust["state"] = {"trust_missing": "missing", "trust_revoked": "revoked", "hub_identity_changed": "identity_changed"}[failure]
        except acme_hub_client.AcmeHubClientError as error:
            failure = _hub_error(error, capabilities=True)
    hub_state = failure[4:] if failure and failure.startswith("hub_") and failure != "hub_identity_changed" else "ready"
    if hub_state == "rate_limited":
        hub_state = "unavailable"
    if target.group is None:
        hub_state = "unconfigured"
    cert = _selected_certificate(target)
    certificate = _certificate_status(cert, custody)
    write = target.group is not None and can_access(actor, target.group, True)
    verified = bool(row and row.state == "verified" and trust["state"] == "active" and alias["state"] == "match" and alias["one_hop"] is True)
    renewal = _renewal(cert, row, certificate, bool(row and row.connection_mode == "shared" and not verified),
                       issuer_active=_issuer_active)
    next_code, next_actor = "none", "none"
    if target.group is None:
        next_code, next_actor = "unsupported_domain_scope", "deployment_operator"
    elif hub_state != "ready":
        next_code = {"unconfigured": "configure_hub", "misconfigured": "configure_hub", "unsupported": "upgrade_hub", "unauthorized": "repair_hub_access"}.get(hub_state, "retry_hub")
        next_actor = "deployment_operator"
    elif failure == "hub_identity_changed":
        next_code, next_actor = "repair_hub_access", "deployment_operator"
    elif row is None:
        next_code, next_actor = ("prepare_connection" if alias["target"] else "choose_allocation"), "project_operator"
    elif trust["state"] == "revoked":
        next_code, next_actor = "restart_connection", "project_operator"
    elif trust["state"] == "missing":
        next_code, next_actor = "grant_trust", "allocation_controller"
    elif row.connection_mode != "shared" or failure == "allocation_conflict":
        next_code, next_actor = "inspect_failure", "project_operator"
    elif not row.hub_participant_ref:
        next_code, next_actor = "connect", "project_operator"
    elif alias["state"] != "match" or alias["one_hop"] is not True:
        next_code, next_actor = ("connect" if not row.hub_participant_ref else "repair_alias"), "domain_owner"
    elif row.state != "verified":
        next_code, next_actor = "check", "project_operator"
    elif certificate and certificate["material_state"] != "available" and cert.status == "active":
        next_code, next_actor = "restore_custody", "deployment_operator"
    elif renewal["state"] in ("queued", "issuing"):
        next_code, next_actor = "wait_for_job", "project_operator"
    elif renewal["state"] == "blocked":
        next_code, next_actor = "inspect_failure", "project_operator"
    elif renewal["state"] in ("failed", "expired", "retrying"):
        next_code, next_actor = ("repair_worker", "deployment_operator") if renewal["error_code"] == "certificate_busy" else ("inspect_failure", "project_operator")
    elif cert is None:
        next_code, next_actor = "request", "project_operator"
    connection = dict(id=row.pk, mode=row.connection_mode, state=row.state, client_ref=str(row.client_ref),
                      allocation_ref=row.hub_allocation_ref, participant_ref=row.hub_participant_ref,
                      verified_at=_iso(row.verified_at)) if row else None
    return dict(version=1, checked_at=_iso(dates.utcnow()),
                domain=dict(id=target.domain.pk if target.domain else None, name=target.name,
                            provider=target.domain.provider if target.domain else None,
                            group=dict(id=target.group.pk, name=target.group.name) if target.group else None),
                profile=dict(name="apex_wildcard", names=[target.name, f"*.{target.name}"]),
                hub=dict(state=hub_state, protocol_version=caps.version if caps else None,
                         consumer_project_uuid=caps.consumer_project_uuid if caps else None),
                connection=connection, trust=trust, alias=alias, certificate=certificate, renewal=renewal,
                capabilities=dict(group_choices=bool(write),
                                  can_connect=bool(write and hub_state == "ready" and alias["target"] and failure != "hub_identity_changed"
                                                   and (row is None or row.connection_mode == "shared") and trust["state"] != "revoked"),
                                  can_restart=bool(write and row and row.connection_mode == "shared" and (trust["state"] == "revoked" or row.state == "pending" and not row.hub_participant_ref)),
                                  can_check=bool(write and row and row.hub_participant_ref and row.connection_mode == "shared" and trust["state"] == "active"),
                                  can_request=bool(write and verified and not row.cleanup_challenge_ref and not (
                                      certificate and cert.status == "active" and certificate["material_state"] != "available")
                                      and not (cert and cert.status in ("pending", "issuing") and renewal["state"] in ("blocked", "failed"))
                                      and not (renewal["job"] and renewal["job"]["state"] == "canceled"))),
                next_action=dict(code=next_code, actor=next_actor),
                connection_request=_descriptor(row) if write and row and row.connection_mode == "shared" else None,
                operation=operation or dict(action="plan", outcome="observed", changed=False))


def _error_with_plan(error, target, actor, client, custody=None):
    if error.data is None:
        error.data = plan(target, actor=actor, client=client, custody=custody)
    return error


def _settle_cleanup(domain, certificates, old, cleanup=None):
    from mojo.apps.dnsman.models import DnsRecordReservation
    from mojo.apps.dnsman.services import certs, record_reservations
    if cleanup:
        cleanup(domain, certificates, old)
        return
    if old and old.cleanup_challenge_ref:
        try:
            certs._withdraw_pending_delegated_cleanup(old)
        except Exception:
            raise SetupError("cleanup_pending") from None
        old.refresh_from_db()
        if old.cleanup_challenge_ref:
            raise SetupError("cleanup_pending")
    for reservation in DnsRecordReservation.objects.select_for_update().filter(domain=domain).exclude(state="released"):
        if reservation.certificate_id is None:
            raise SetupError("cleanup_pending")
        if reservation.mutation_attempted:
            if old and old.connection_mode == "shared":
                raise SetupError("cleanup_pending")
            reservation.state = "cleanup_pending"
            reservation.save(update_fields=["state", "modified"])
        else:
            record_reservations.release(reservation)
    for cert in certificates:
        try:
            record_reservations.cleanup_for_certificate(cert)
        except Exception:
            raise SetupError("cleanup_pending") from None


def _prepare(target, allocation_ref, consumer, actor, restart, cleanup=None, identity_check=None, revoked_ref=None):
    from mojo.apps.account.models import Group
    from mojo.apps.dnsman.models import AcmeDelegation, Certificate, Domain
    from mojo.apps.dnsman.services import certs
    with transaction.atomic():
        with ExitStack() as stack:
            from mojo.apps.dnsman.services import connection_config
            try:
                stack.enter_context(connection_config.identity_lock())
                (identity_check or connection_config.require_current_identity)(consumer)
            except connection_config.ConfigurationError as error:
                raise SetupError("configuration_busy" if error.error_code == "configuration_busy"
                                 else "hub_identity_changed") from None
            # Domain first, NO Certificate row locks before trying all advisory
            # locks. An issuer owns its advisory lock before joined row locks.
            domain = Domain.objects.select_for_update().filter(name=target.name).first()
            if target.domain is not None and (domain is None or target.domain.pk != domain.pk):
                raise SetupError("stale_connection")
            ids = list(Certificate.objects.filter(domain=domain).order_by("pk").values_list("pk", flat=True)) if domain else []
            for pk in ids:
                if not stack.enter_context(certs._issue_lock(pk)):
                    raise SetupError("certificate_busy")
            group = Group.objects.select_for_update().filter(pk=target.group.pk).first()
            if group is None or not group.is_effectively_active() or (domain and domain.group_id != group.pk):
                raise SetupError("stale_connection")
            _require(actor, group, True)
            certificates = list(Certificate.objects.select_for_update().filter(pk__in=ids).order_by("pk"))
            target = objict(domain=domain, group=group, name=target.name)
            old = _current(target)
            if old:
                old = AcmeDelegation.objects.select_for_update().get(pk=old.pk)
                if old.tenant_uuid != group.uuid:
                    raise SetupError("stale_connection")
                if old.connection_mode != "shared" or old.hub_allocation_ref != allocation_ref:
                    raise SetupError("allocation_conflict")
                if old.hub_consumer_project_uuid != consumer:
                    raise SetupError("hub_identity_changed")
                if not restart:
                    if old.state == "retired":
                        raise SetupError("stale_connection")
                    return old
                observed_revoked = revoked_ref == (old.pk, str(old.client_ref), old.hub_participant_ref)
                if revoked_ref is not None and not observed_revoked:
                    raise SetupError("stale_connection")
                if old.hub_participant_ref and old.last_error_code != "trust_revoked" and not observed_revoked:
                    raise SetupError("stale_connection")
                if not old.hub_participant_ref and old.state != "pending":
                    raise SetupError("stale_connection")
                if observed_revoked:
                    # Only admitted preparation may persist an explicit restart
                    # observation, bound to this freshly locked predecessor.
                    old.last_error_code = "trust_revoked"
                    old.save(update_fields=["last_error_code", "modified"])
            elif restart:
                raise SetupError("stale_connection")
            if domain:
                _settle_cleanup(domain, certificates, old, cleanup)
            elif old and old.cleanup_challenge_ref:
                raise SetupError("cleanup_pending")
            if old:
                old.state, old.retired_at = "retired", dates.utcnow()
                old.save(update_fields=["state", "retired_at", "modified"])
            # get_uuid can write; preparation is the first mutating boundary.
            return AcmeDelegation.objects.create(
                domain=domain, group=group, tenant_uuid=group.get_uuid(), tenant_name=group.name,
                user=_identity(actor) if getattr(_identity(actor), "is_request_user", False) else None,
                user_email=getattr(_identity(actor), "email", None), domain_name=target.name,
                connection_mode="shared", hub_allocation_ref=allocation_ref,
                hub_consumer_project_uuid=consumer, state="pending")


def connect(target, allocation_ref, actor=None, restart=False, client=None, custody=None, cleanup=None, identity_check=None):
    from mojo.apps.dnsman.models import AcmeDelegation
    target = _refresh(target, actor)
    if target.group is None:
        raise SetupError("unsupported_domain_scope", data=plan(target, actor=actor, client=client, custody=custody))
    _require(actor, target.group, True)
    if type(restart) is not bool or not isinstance(allocation_ref, str):
        raise me.ValueException("An allocation_ref and boolean restart are required")
    allocation_ref = naming.normalize_domain(allocation_ref)
    if len(allocation_ref) > 253:
        raise me.ValueException("An allocation target of at most 253 characters is required")
    client = client or acme_hub_client
    try:
        caps, unused = _preflight(target, client, allocation_ref)
        old = _current(target)
        revoked_ref = None
        if restart and old and old.hub_participant_ref:
            try:
                _shared_status(old, client, caps)
            except SetupError as error:
                if error.error_code != "trust_revoked":
                    raise
                # Keep remote observation read-only until identity admission and
                # the current row/tenant/immutable-reference checks succeed.
                revoked_ref = (old.pk, str(old.client_ref), old.hub_participant_ref)
            else:
                raise SetupError("stale_connection")
        row = _prepare(target, allocation_ref, caps.consumer_project_uuid, actor, restart, cleanup,
                       identity_check, revoked_ref=revoked_ref)
        # First potentially ambiguous join is AFTER durable immutable intent.
        try:
            _refresh(target, actor, write=True)
            row = delegation.require_current_owner(row, require_domain=row.domain_id is not None)
            participant = _shared_status(row, client)
        except SetupError as error:
            if error.error_code == "trust_missing":
                return plan(target, actor=actor, client=client, custody=custody,
                            operation=dict(action="connect", outcome="awaiting_trust", changed=old is None or restart))
            raise
        _alias_required(participant.alias)
        _refresh(target, actor, write=True)
        try:
            result = client.join(row.domain_name, str(row.client_ref), row.hub_allocation_ref)
            delegation._compare_allocation(row, result)
            if result.target != row.hub_allocation_ref:
                raise SetupError("allocation_conflict")
        except SetupError:
            raise
        except Exception as error:
            raise SetupError(_hub_error(error)) from None
        with transaction.atomic():
            _refresh(target, actor, write=True)
            current = AcmeDelegation.objects.select_for_update().get(pk=row.pk)
            if current.state == "retired" or _current(target).pk != current.pk:
                raise SetupError("stale_connection")
            _compare(current, participant)
            current.hub_participant_ref = participant.participant_ref
            current.source_name, current.target_name = result.source, result.target
            current.last_error_code = None
            current.save()
        return plan(target, actor=actor, client=client, custody=custody,
                    operation=dict(action="connect", outcome="connected", changed=not row.hub_participant_ref))
    except SetupError as error:
        raise _error_with_plan(error, target, actor, client, custody)


def _row_target(row, actor):
    return resolve_target(domain=row.domain_id, actor=actor, write=True) if row.domain_id else resolve_target(
        group=row.group_id, name=row.domain_name, actor=actor, write=True)


def check(row, actor=None, client=None, custody=None, probe=None):
    from mojo.apps.dnsman.models import AcmeDelegation
    row = resolve_connection(row, actor=actor, write=True)
    target = _row_target(row, actor)
    client = client or acme_hub_client
    try:
        if row.connection_mode != "shared" or _current(target).pk != row.pk or row.state == "retired":
            raise SetupError("stale_connection")
        if not row.hub_participant_ref:
            raise SetupError("connection_not_verified")
        participant = _shared_status(row, client)
        _alias_required(participant.alias)
        # The proven hub observation is not the local persisted ownership proof.
        row = delegation.verify(row, resolve_cname=probe, shared_checked=True)
        target = resolve_target(domain=row.domain_id, actor=actor)
        return plan(target, actor=actor, client=client, custody=custody,
                    operation=dict(action="check", outcome="verified", changed=True))
    except SetupError as error:
        delegation._proof_failure(row, error.error_code)
        raise _error_with_plan(error, target, actor, client, custody)
    except me.MojoException:
        row.refresh_from_db()
        current = _current(target)
        code = "stale_connection" if row.state == "retired" or current is None or current.pk != row.pk else (
            row.last_error_code if row.last_error_code in ERRORS else "alias_mismatch")
        raise _error_with_plan(SetupError(code), target, actor, client, custody) from None


def request(row, actor=None, client=None, custody=None, publish=None):
    from mojo.apps.dnsman.models import Certificate, Domain
    from mojo.apps.jobs.models import Job
    from mojo.apps.dnsman.services import certs
    row = resolve_connection(row, actor=actor, write=True)
    target = _row_target(row, actor)
    client = client or acme_hub_client
    try:
        if row.connection_mode != "shared" or _current(target).pk != row.pk:
            raise SetupError("connection_not_verified")
        validate_shared(row, client)
        if row.state != "verified":
            raise SetupError("connection_not_verified")
        if row.cleanup_challenge_ref:
            raise SetupError("cleanup_pending")
        names = [target.name, f"*.{target.name}"]
        with transaction.atomic():
            with ExitStack() as stack:
                Domain.objects.select_for_update().get(pk=target.domain.pk)
                target = _refresh(target, actor, write=True)
                current_route = delegation.routing_for_domain(target.domain)
                if current_route is None or current_route.pk != row.pk or current_route.state != "verified":
                    raise SetupError("stale_connection")
                rows = list(Certificate.objects.filter(domain=target.domain).exclude(status="revoked").order_by("pk"))
                if any(sorted(cert.sans or [cert.common_name]) != sorted(names) and cert.status in ("pending", "issuing", "active") for cert in rows):
                    raise SetupError("profile_conflict")
                # request_certificate takes Certificate row locks. As in
                # Connect, try every advisory owner first so an issuer paused
                # before its joined Domain lock cannot create an inverse wait.
                busy = False
                for cert in rows:
                    if not stack.enter_context(certs._issue_lock(cert.pk)):
                        busy = True
                        break
                current = certs.find_covering_certificate(target.domain, names)
                selected = _selected_certificate(target)
                material = _certificate_status(selected, custody)
                if busy:
                    outcome = "already_queued"
                elif selected and selected.status == "active" and material["material_state"] != "available":
                    outcome = "custody_unavailable"
                elif current and current.not_after and current.not_after > dates.utcnow():
                    outcome = "already_current"
                elif any(cert.status in ("pending", "issuing") for cert in rows):
                    job = _certificate_job(selected) if selected else None
                    outcome = "already_queued" if job and job.status in ("pending", "running") else "job_held"
                else:
                    publication = {}
                    def publish_requested(func, certificate):
                        job_id = (publish or certs.publish_job)(func, certificate)
                        # Inspect the returned id before commit exposes new
                        # work. A preserved terminal id did not enqueue work;
                        # a new job may legitimately finish during on_commit.
                        publication["held"] = isinstance(job_id, str) and Job.objects.filter(
                            pk=job_id, status__in=("canceled", "expired", "failed", "completed")).exists()
                        return job_id
                    certs.request_certificate(target.domain, names=names, publish=publish_requested)
                    outcome = "job_held" if publication.get("held") else "queued"
        result = plan(target, actor=actor, client=client, custody=custody,
                      operation=dict(action="request", outcome=outcome, changed=outcome == "queued"),
                      _issuer_active=busy)
        return result
    except SetupError as error:
        raise _error_with_plan(error, target, actor, client, custody)


def group_choices(actor, id=None, search="", start=0, size=25):
    from mojo.apps.account.models import Group
    if not isinstance(search, str) or len(search) > 100:
        raise me.ValueException("Invalid DNS group query")
    for value, lower, upper in ((start, 0, 100000), (size, 1, 50)):
        if isinstance(value, bool) or not isinstance(value, (str, int)) or len(str(value)) > 6 or not str(value).isdigit() or not lower <= int(value) <= upper:
            raise me.ValueException("Invalid DNS group query")
    start, size = int(start), int(size)
    query = Group.objects.filter(is_active=True)
    path = "parent"
    for unused in range(8):
        query = query.exclude(**{f"{path}__is_active": False})
        path = f"{path}__parent"
    query = query.filter(**{f"{path}__isnull": True})
    identity = _identity(actor)
    if actor is not None and (identity is None or not getattr(identity, "is_active", False)):
        raise me.PermissionDeniedException("DNS setup group choices are not available")
    if identity is not None:
        if hasattr(identity, "is_group_allowed"):
            if not identity.has_permission(WRITE):
                query = query.none()
            else:
                query = query.filter(pk__in=identity.get_groups().values_list("pk", flat=True))
        elif not identity.has_permission(WRITE):
            query = query.filter(pk__in=identity.get_groups_with_permission(WRITE).values_list("pk", flat=True))
    if id is not None:
        query = query.filter(pk=_id(id))
        start, size = 0, 1
    elif search:
        query = query.filter(name__icontains=search.strip())
    # Recheck exact inherited authority; the chooser is a convenience only.
    rows = [dict(id=g.pk, name=g.name) for g in query.order_by(Lower("name"), "pk")[start:start + size] if can_access(actor, g, True)]
    return dict(status=True, data=rows, start=start, size=size, count=query.count())
