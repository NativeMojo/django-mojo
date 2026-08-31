"""Durable claim, DB-outbox publication, execution, and repair for incident LLM work."""

import hashlib
import secrets

from django.db import IntegrityError, transaction
from django.utils import timezone


FEATURE_HANDLERS = {
    "incident_triage": "mojo.apps.incident.handlers.llm_agent.execute_llm_handler",
    "incident_analysis": "mojo.apps.incident.handlers.llm_agent.execute_llm_analysis",
    "incident_ticket": "mojo.apps.incident.handlers.llm_agent.execute_llm_ticket_reply",
}
ACTIVE_STATES = ("claimed", "queued", "running", "retryable")
LEASE_SECONDS = 180


def _lease_seconds(feature, policy=None):
    """Cover the largest policy-permitted loop, with heartbeats as defense."""
    try:
        from mojo.apps.account.services import llm_safety
        policy = policy or llm_safety.parse_policy()
        limits = policy["features"][feature]
        calls = min(policy["shared"]["max_loop_calls"], limits["max_loop_calls"])
        timeout = min(policy["shared"]["timeout_seconds"], limits["timeout_seconds"])
        return max(LEASE_SECONDS, calls * timeout + 60)
    except Exception:
        return LEASE_SECONDS


def _logical_key(incident_id, feature, event_id=None, ticket_id=None, suffix=""):
    value = f"{incident_id}:{feature}:{event_id or ''}:{ticket_id or ''}:{suffix}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _queue_locked(attempt, delay=None):
    from mojo.apps import jobs
    payload = {"attempt_id": attempt.pk}
    attempt.delivery_generation += 1
    job_id = jobs.publish(
        "mojo.apps.incident.services.llm_dispatch.execute_attempt", payload,
        channel="incident_handlers", delay=delay, max_retries=0,
        idempotency_key=(
            f"llm-attempt:{attempt.pk}:{attempt.delivery_generation}")[:64])
    attempt.job_id = job_id
    attempt.state = "queued"
    if delay is None:
        attempt.retry_at = None
    attempt.save(update_fields=[
        "job_id", "state", "retry_at", "delivery_generation", "modified"])
    return attempt


def claim_incident(incident, *, feature="incident_triage", event_id=None,
                   ruleset_id=None, ticket_id=None, logical_suffix="",
                   max_attempts=3, fresh=False):
    """Create one active attempt and its Job row in the same transaction."""
    from mojo.apps.incident.models import Incident, IncidentLLMAttempt
    if feature not in FEATURE_HANDLERS:
        raise ValueError("Unsupported incident LLM feature")
    with transaction.atomic():
        locked = Incident.objects.select_for_update().get(pk=incident.pk)
        active = IncidentLLMAttempt.objects.filter(
            incident=locked, feature=feature, state__in=ACTIVE_STATES).first()
        if active:
            return active, False
        suffix = secrets.token_hex(16) if fresh else logical_suffix
        logical_key = _logical_key(
            incident.pk, feature, event_id=event_id, ticket_id=ticket_id,
            suffix=suffix)
        existing = IncidentLLMAttempt.objects.filter(logical_key=logical_key).first()
        if existing:
            if existing.state == "terminal" and _is_transient(existing.error_code):
                if existing.retry_at and existing.retry_at > timezone.now():
                    return existing, False
                existing.state = "claimed"
                existing.attempt_count = 0
                existing.error_code = ""
                existing.finished_at = None
                existing.retry_at = None
                existing.save()
                if feature == "incident_triage" and locked.status != "investigating":
                    existing.prior_status = locked.status
                    existing.save(update_fields=["prior_status", "modified"])
                    locked.status = "investigating"
                    locked.save(update_fields=["status"])
                _queue_locked(existing)
                return existing, True
            return existing, False
        try:
            attempt = IncidentLLMAttempt.objects.create(
                incident=locked, feature=feature, logical_key=logical_key,
                prior_status=locked.status, event_id=event_id,
                ruleset_id=ruleset_id, ticket_id=ticket_id,
                max_attempts=max(1, min(int(max_attempts), 10)))
        except IntegrityError:
            return IncidentLLMAttempt.objects.get(logical_key=logical_key), False
        if feature == "incident_triage":
            locked.status = "investigating"
            locked.save(update_fields=["status"])
        _queue_locked(attempt)
        return attempt, True


def claim_ticket(ticket, note_id=None):
    from mojo.apps.incident.models import IncidentLLMAttempt, Ticket
    logical_key = _logical_key(
        ticket.incident_id or 0, "incident_ticket", ticket_id=ticket.pk,
        suffix=str(note_id or "initial"))
    with transaction.atomic():
        locked = Ticket.objects.select_for_update().get(pk=ticket.pk)
        existing = IncidentLLMAttempt.objects.filter(logical_key=logical_key).first()
        if existing:
            return existing, False
        active = IncidentLLMAttempt.objects.filter(
            ticket_id=ticket.pk, feature="incident_ticket",
            state__in=ACTIVE_STATES).first()
        if active:
            return active, False
        try:
            with transaction.atomic():
                attempt = IncidentLLMAttempt.objects.create(
                    incident_id=locked.incident_id, feature="incident_ticket",
                    logical_key=logical_key, ticket_id=locked.pk, note_id=note_id)
        except IntegrityError:
            attempt = IncidentLLMAttempt.objects.filter(
                ticket_id=locked.pk, feature="incident_ticket",
                state__in=ACTIVE_STATES).first()
            if attempt is None:
                attempt = IncidentLLMAttempt.objects.get(logical_key=logical_key)
            return attempt, False
        _queue_locked(attempt)
        return attempt, True


def _payload(attempt, owner):
    lease = {"_llm_attempt_adopted": True, "_llm_attempt_id": attempt.pk,
             "_llm_lease_owner": owner}
    if attempt.feature == "incident_triage":
        return dict(lease, **{
            "event_id": attempt.event_id, "incident_id": attempt.incident_id,
            "ruleset_id": attempt.ruleset_id,
        })
    if attempt.feature == "incident_analysis":
        return dict(lease, incident_id=attempt.incident_id)
    return dict(lease, ticket_id=attempt.ticket_id, note_id=attempt.note_id)


def execute_attempt(job, handler_loader=None):
    from mojo.apps.incident.models import IncidentLLMAttempt
    from mojo.apps.jobs.job_engine import load_job_function

    attempt_id = job.payload.get("attempt_id")
    owner = secrets.token_hex(16)
    with transaction.atomic():
        attempt = IncidentLLMAttempt.objects.select_for_update().get(pk=attempt_id)
        if attempt.state not in ("queued", "retryable"):
            return
        attempt.state = "running"
        attempt.attempt_count += 1
        attempt.lease_owner = owner
        attempt.lease_expires_at = timezone.now() + timezone.timedelta(
            seconds=_lease_seconds(attempt.feature))
        attempt.save(update_fields=[
            "state", "attempt_count", "lease_owner", "lease_expires_at", "modified"])
    original_payload = job.payload
    job.payload = _payload(attempt, owner)
    try:
        handler = (handler_loader or load_job_function)(FEATURE_HANDLERS[attempt.feature])
        handler(job)
    except Exception as err:
        code = _safe_code(getattr(err, "code", "provider_failed"))
        retry_after = getattr(err, "retry_after", None)
        try:
            job.add_log(f"LLM attempt failed safely: {code}", kind="error")
            job.last_error = code
            job.metadata = dict(job.metadata or {})
            job.metadata["llm_attempt"] = {"status": "failed", "code": code}
            job.save(update_fields=["last_error", "metadata", "modified"])
        except Exception:
            pass
        _finish(attempt.pk, owner, False, code, retry_after=retry_after)
        return
    finally:
        job.payload = original_payload
    if _finish(attempt.pk, owner, True, ""):
        try:
            job.metadata = dict(job.metadata or {})
            job.metadata["llm_attempt"] = {"status": "succeeded", "code": ""}
            job.save(update_fields=["metadata", "modified"])
        except Exception:
            pass


def renew_lease(attempt_id, owner):
    """Heartbeat only the running attempt owned by this exact worker token."""
    from mojo.apps.incident.models import IncidentLLMAttempt
    attempt = IncidentLLMAttempt.objects.filter(
        pk=attempt_id, state="running", lease_owner=owner).first()
    if attempt is None:
        return False
    expires = timezone.now() + timezone.timedelta(
        seconds=_lease_seconds(attempt.feature))
    return IncidentLLMAttempt.objects.filter(
        pk=attempt_id, state="running", lease_owner=owner).update(
            lease_expires_at=expires, modified=timezone.now()) == 1


PERMANENT_CODES = frozenset({
    "capability_unsupported", "context_invalid", "input_too_large",
    "model_mismatch", "operation_invalid", "output_too_large",
    "provider_unsupported", "route_missing",
})


def _safe_code(code):
    from mojo.apps.account.services import llm_safety
    return code if code in llm_safety.SAFE_CODES else "provider_failed"


def _is_transient(code):
    return _safe_code(code) not in PERMANENT_CODES


def _retry_delay(error_code, retry_after, attempt_count):
    try:
        supplied = int(retry_after or 0)
    except (TypeError, ValueError):
        supplied = 0
    supplied = max(0, min(supplied, 86400))
    if _is_transient(error_code):
        return max(60, supplied)
    return min(300, max(2, 2 ** attempt_count))


def _clear_analysis_flag(incident_id):
    from mojo.apps.incident.models import Incident
    incident = Incident.objects.select_for_update().filter(pk=incident_id).first()
    if incident is None:
        return
    metadata = dict(incident.metadata or {})
    if metadata.get("analysis_in_progress"):
        metadata["analysis_in_progress"] = False
        incident.metadata = metadata
        incident.save(update_fields=["metadata"])


def _finish_locked(attempt, owner, succeeded, error_code, retry_after=None):
    from mojo.apps.incident.models import Incident, IncidentLLMAttempt
    if attempt.state != "running" or attempt.lease_owner != owner:
        return False
    error_code = _safe_code(error_code)
    if succeeded:
        attempt.state = "succeeded"
        attempt.finished_at = timezone.now()
        attempt.error_code = ""
        if attempt.feature == "incident_analysis":
            _clear_analysis_flag(attempt.incident_id)
    elif attempt.attempt_count < attempt.max_attempts:
        attempt.state = "retryable"
        attempt.error_code = error_code
        delay = _retry_delay(error_code, retry_after, attempt.attempt_count)
        attempt.retry_at = timezone.now() + timezone.timedelta(seconds=delay)
        attempt.lease_owner = ""
        attempt.lease_expires_at = None
        attempt.save()
        _queue_locked(attempt, delay=delay)
        return True
    else:
        attempt.state = "terminal"
        attempt.finished_at = timezone.now()
        attempt.error_code = error_code
        if _is_transient(error_code):
            delay = _retry_delay(error_code, retry_after, attempt.attempt_count)
            attempt.retry_at = timezone.now() + timezone.timedelta(seconds=delay)
        else:
            attempt.retry_at = None
        if attempt.feature == "incident_triage":
            Incident.objects.filter(
                pk=attempt.incident_id, status="investigating").update(
                    status=attempt.prior_status or "open")
        if attempt.feature == "incident_analysis":
            _clear_analysis_flag(attempt.incident_id)
    attempt.lease_owner = ""
    attempt.lease_expires_at = None
    attempt.save()
    return True


def _finish(attempt_id, owner, succeeded, error_code, retry_after=None):
    from mojo.apps.incident.models import IncidentLLMAttempt
    with transaction.atomic():
        attempt = IncidentLLMAttempt.objects.select_for_update().get(pk=attempt_id)
        return _finish_locked(
            attempt, owner, succeeded, error_code, retry_after=retry_after)


def adopt_legacy_job(job, feature, incident_id, event_id=None, ruleset_id=None,
                     ticket_id=None):
    from mojo.apps.incident.models import Incident
    incident = Incident.objects.get(pk=incident_id)
    return claim_incident(
        incident, feature=feature, event_id=event_id, ruleset_id=ruleset_id,
        ticket_id=ticket_id, logical_suffix=f"legacy:{job.pk}")


def repair_attempts(limit=100):
    """Requeue stranded attempts and terminalize expired worker leases."""
    from mojo.apps.incident.models import IncidentLLMAttempt
    from mojo.apps.jobs.models import Job
    now = timezone.now()
    repaired = 0
    rows = list(IncidentLLMAttempt.objects.filter(
        state__in=ACTIVE_STATES).order_by("created")[:max(1, min(int(limit), 500))])
    for row in rows:
        with transaction.atomic():
            attempt = IncidentLLMAttempt.objects.select_for_update().get(pk=row.pk)
            if attempt.state == "running":
                if attempt.lease_expires_at and attempt.lease_expires_at <= now:
                    if _finish_locked(
                            attempt, attempt.lease_owner, False,
                            "provider_timeout"):
                        repaired += 1
            else:
                job = Job.objects.filter(pk=attempt.job_id).first() \
                    if attempt.job_id else None
                if attempt.state in ("claimed", "queued", "retryable") and (
                        job is None or job.status in ("failed", "canceled", "expired")):
                    _queue_locked(attempt)
                    repaired += 1
    return repaired


def repair_attempts_job(job):
    repaired = repair_attempts()
    job.add_log(f"Repaired {repaired} incident LLM attempt(s)")


def start_historical_backlog(before, limit, actor):
    """Owner-only bounded opt-in for incidents older than the activation watermark."""
    from mojo.apps.account.services import system_settings
    from mojo.apps.incident.models import Event, Incident
    system_settings.require_system_admin(actor)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100")
    incidents = list(Incident.objects.filter(
        status="new", created__lt=before).order_by("created", "pk")[:limit])
    count = 0
    for incident in incidents:
        event_id = Event.objects.filter(incident=incident).order_by("created").values_list(
            "pk", flat=True).first()
        if event_id:
            _, created = claim_incident(
                incident, event_id=event_id, ruleset_id=incident.rule_set_id,
                logical_suffix=f"historical:{before.isoformat()}")
            count += int(created)
    return count
