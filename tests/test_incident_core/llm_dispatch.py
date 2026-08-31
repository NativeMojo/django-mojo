import uuid
from concurrent.futures import ThreadPoolExecutor

from testit import helpers as th


@th.django_unit_test()
def test_concurrent_claimers_create_one_attempt_and_job(opts):
    from django.db import connections
    from mojo.apps.incident.models import Incident, IncidentLLMAttempt
    from mojo.apps.incident.services import llm_dispatch
    from mojo.apps.jobs.models import Job

    marker = uuid.uuid4().hex
    incident = Incident.objects.create(
        category=f"test:llm-claim:{marker}", status="new", priority=5,
        title="LLM claim test")

    def claim():
        try:
            current = Incident.objects.get(pk=incident.pk)
            attempt, created = llm_dispatch.claim_incident(
                current, event_id=1001, logical_suffix=marker)
            return attempt.pk, created
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))

    attempt_ids = {row[0] for row in results}
    assert len(attempt_ids) == 1, \
        f"concurrent claimers must converge on one attempt, got {results}"
    attempt = IncidentLLMAttempt.objects.get(pk=attempt_ids.pop())
    assert attempt.state == "queued", \
        f"the durable attempt must be queued after claim, got {attempt.state}"
    assert Job.objects.filter(pk=attempt.job_id, status="pending").count() == 1, \
        f"the attempt must own one pending DB outbox row, got job {attempt.job_id!r}"
    assert IncidentLLMAttempt.objects.filter(
        incident=incident, state__in=llm_dispatch.ACTIVE_STATES).count() == 1, \
        "the partial uniqueness boundary must retain one active attempt"


@th.django_unit_test()
def test_repair_requeues_missing_outbox(opts):
    from mojo.apps.incident.models import Incident, IncidentLLMAttempt
    from mojo.apps.incident.services import llm_dispatch
    from mojo.apps.jobs.models import Job

    marker = uuid.uuid4().hex
    incident = Incident.objects.create(
        category=f"test:llm-repair:{marker}", status="new", priority=4,
        title="LLM repair test")
    attempt = IncidentLLMAttempt.objects.create(
        incident=incident, feature="incident_triage",
        logical_key=marker, state="claimed", prior_status="new", event_id=1002)

    repaired = llm_dispatch.repair_attempts(limit=10)
    attempt.refresh_from_db()
    assert repaired >= 1, f"repair must find the stranded attempt, got {repaired}"
    assert attempt.state == "queued", \
        f"repair must move the attempt to queued, got {attempt.state}"
    assert Job.objects.filter(pk=attempt.job_id, status="pending").exists(), \
        f"repair must create a pending DB outbox row, got {attempt.job_id!r}"


@th.django_unit_test()
def test_repair_uses_fresh_delivery_generation_for_terminal_jobs(opts):
    from mojo.apps.incident.models import Incident, IncidentLLMAttempt
    from mojo.apps.incident.services import llm_dispatch
    from mojo.apps.jobs.models import Job

    attempts = []
    for status in ("failed", "canceled", "expired"):
        incident = Incident.objects.create(
            category=f"test:llm-repair-{status}:{uuid.uuid4().hex}",
            status="new", priority=4, title=f"LLM {status} repair")
        attempt, _ = llm_dispatch.claim_incident(
            incident, event_id=3000 + len(attempts),
            logical_suffix=uuid.uuid4().hex)
        old_job_id = attempt.job_id
        Job.objects.filter(pk=old_job_id).update(status=status)
        attempts.append((attempt.pk, old_job_id))

    repaired = llm_dispatch.repair_attempts(limit=20)
    assert repaired >= 3, f"all terminal deliveries must be repaired, got {repaired}"
    for attempt_id, old_job_id in attempts:
        attempt = IncidentLLMAttempt.objects.get(pk=attempt_id)
        assert attempt.job_id != old_job_id and attempt.delivery_generation == 2, \
            f"repair reused terminal delivery {old_job_id}: {attempt.job_id} / {attempt.delivery_generation}"
        assert Job.objects.filter(pk=attempt.job_id, status="pending").count() == 1, \
            f"repair did not create one usable pending delivery for {attempt_id}"
        assert IncidentLLMAttempt.objects.filter(pk=attempt_id).count() == 1, \
            "repair duplicated the logical attempt instead of its delivery"


@th.django_unit_test()
def test_concurrent_standalone_ticket_claims_converge(opts):
    from django.db import connections
    from mojo.apps.incident.models import IncidentLLMAttempt, Ticket
    from mojo.apps.incident.services import llm_dispatch

    ticket = Ticket.objects.create(title=f"Standalone {uuid.uuid4().hex}")

    def claim():
        try:
            current = Ticket.objects.get(pk=ticket.pk)
            attempt, created = llm_dispatch.claim_ticket(current, note_id=99)
            return attempt.pk, created
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert len({row[0] for row in results}) == 1, \
        f"standalone ticket claimers must converge, got {results}"
    assert IncidentLLMAttempt.objects.filter(
        ticket_id=ticket.pk, state__in=llm_dispatch.ACTIVE_STATES).count() == 1, \
        "the database constraint must retain one active standalone ticket attempt"


@th.django_unit_test()
def test_execute_attempt_records_guard_failure_retry_and_terminal(opts):
    from mojo.apps.incident.models import Incident, IncidentLLMAttempt
    from mojo.apps.incident.services import llm_dispatch
    from mojo.helpers.llm import LLMExecutionError
    from mojo.apps.jobs.models import Job

    incident = Incident.objects.create(
        category=f"test:llm-execute:{uuid.uuid4().hex}", status="new", priority=4,
        title="LLM execute test")
    attempt, _ = llm_dispatch.claim_incident(
        incident, event_id=2001, logical_suffix=uuid.uuid4().hex, max_attempts=2)
    job = Job.objects.get(pk=attempt.job_id)

    def failing_handler(current_job):
        raise LLMExecutionError("emergency_stopped")

    llm_dispatch.execute_attempt(job, handler_loader=lambda path: failing_handler)
    attempt.refresh_from_db()
    assert attempt.state == "queued" and attempt.attempt_count == 1, \
        f"first guard failure must become a queued retry, got {attempt.state} / {attempt.attempt_count}"
    assert attempt.error_code == "emergency_stopped", \
        f"attempt must persist only the safe guard code, got {attempt.error_code!r}"
    assert attempt.retry_at and (attempt.retry_at - attempt.modified).total_seconds() >= 55, \
        "temporary guard failures must not burn retries in two seconds"
    job.refresh_from_db()
    assert job.last_error == "emergency_stopped", \
        f"generic Job visibility must retain only the safe code, got {job.last_error!r}"
    assert job.metadata.get("llm_attempt") == {
        "status": "failed", "code": "emergency_stopped"}, \
        f"generic Job result metadata must expose only safe failure state: {job.metadata}"

    retry_job = Job.objects.get(pk=attempt.job_id)
    llm_dispatch.execute_attempt(retry_job, handler_loader=lambda path: failing_handler)
    attempt.refresh_from_db()
    incident.refresh_from_db()
    assert attempt.state == "terminal" and attempt.attempt_count == 2, \
        f"retry exhaustion must terminalize the attempt, got {attempt.state}"
    assert incident.status == "new", \
        f"terminal failure must restore the prior incident state, got {incident.status}"

@th.django_unit_test()
def test_execute_attempt_success_and_lease_ownership(opts):
    from mojo.apps.incident.models import Incident, IncidentLLMAttempt
    from mojo.apps.incident.services import llm_dispatch
    from mojo.apps.jobs.models import Job
    from django.utils import timezone

    incident = Incident.objects.create(
        category=f"test:llm-success:{uuid.uuid4().hex}", status="new", priority=4,
        title="LLM success test")
    attempt, _ = llm_dispatch.claim_incident(
        incident, event_id=2002, logical_suffix=uuid.uuid4().hex)
    job = Job.objects.get(pk=attempt.job_id)
    seen = {}

    def successful_handler(current_job):
        seen.update(current_job.payload)

    llm_dispatch.execute_attempt(job, handler_loader=lambda path: successful_handler)
    attempt.refresh_from_db()
    assert attempt.state == "succeeded", \
        f"a successful managed handler must finish the attempt, got {attempt.state}"
    incident.refresh_from_db()
    assert incident.status == "new", \
        f"triage success without an update must restore prior status, got {incident.status}"
    assert seen.get("_llm_attempt_id") == attempt.pk and seen.get("_llm_lease_owner"), \
        f"managed payload must carry the owner-token lease, got {seen}"

    attempt.state = "running"
    attempt.lease_owner = "current-owner"
    attempt.lease_expires_at = timezone.now() + timezone.timedelta(seconds=30)
    attempt.save(update_fields=["state", "lease_owner", "lease_expires_at"])
    assert llm_dispatch.renew_lease(attempt.pk, "wrong-owner") is False, \
        "a non-owner heartbeat must be refused"
    old_expiry = attempt.lease_expires_at
    assert llm_dispatch.renew_lease(attempt.pk, "current-owner") is True, \
        "the current worker must be able to heartbeat its lease"
    attempt.refresh_from_db()
    assert attempt.lease_expires_at > old_expiry, \
        "an owner heartbeat must extend the active attempt lease"
    assert llm_dispatch.repair_attempts(limit=100) >= 0, \
        "repair should inspect active work without failing"
    attempt.refresh_from_db()
    assert attempt.state == "running" and attempt.lease_owner == "current-owner", \
        "repair must not requeue a legitimately active worker"
    attempt.lease_owner = "replacement-owner"
    attempt.lease_expires_at = timezone.now() + timezone.timedelta(seconds=60)
    attempt.save(update_fields=["lease_owner", "lease_expires_at"])
    assert llm_dispatch._finish(
        attempt.pk, "current-owner", False, "provider_timeout") is False, \
        "a stale owner must not fail work after a concurrent renewal/ownership change"
    attempt.refresh_from_db()
    assert attempt.state == "running" and attempt.lease_owner == "replacement-owner", \
        "stale lease completion changed the renewed worker state"


@th.django_unit_test()
def test_retry_after_lease_ceiling_and_transient_reclaim(opts):
    from django.utils import timezone
    from mojo.apps.incident.models import Incident
    from mojo.apps.incident.services import llm_dispatch
    from mojo.apps.jobs.models import Job
    from mojo.helpers.llm import LLMExecutionError

    policy = {
        "shared": {"max_loop_calls": 5, "timeout_seconds": 1000},
        "features": {"incident_triage": {
            "max_loop_calls": 5, "timeout_seconds": 1000}},
    }
    assert llm_dispatch._lease_seconds("incident_triage", policy=policy) == 5060, \
        "valid policy work above one hour must not be truncated to 3600 seconds"

    incident = Incident.objects.create(
        category=f"test:llm-retry-after:{uuid.uuid4().hex}", status="new",
        priority=4, title="LLM retry-after test")
    suffix = uuid.uuid4().hex
    attempt, _ = llm_dispatch.claim_incident(
        incident, event_id=4001, logical_suffix=suffix, max_attempts=1)
    job = Job.objects.get(pk=attempt.job_id)

    def limited(current_job):
        raise LLMExecutionError("provider_rate_limited", retry_after=127)

    llm_dispatch.execute_attempt(job, handler_loader=lambda path: limited)
    attempt.refresh_from_db()
    assert attempt.state == "terminal" and attempt.error_code == "provider_rate_limited", \
        f"one-cycle exhaustion must preserve the transient code, got {attempt.state}/{attempt.error_code}"
    blocked, blocked_created = llm_dispatch.claim_incident(
        incident, event_id=4001, logical_suffix=suffix, max_attempts=1)
    assert blocked.pk == attempt.pk and blocked_created is False, \
        "retry_after must prevent an immediate reclaim from burning another cycle"
    attempt.retry_at = timezone.now() - timezone.timedelta(seconds=1)
    attempt.save(update_fields=["retry_at"])
    reclaimed, created = llm_dispatch.claim_incident(
        incident, event_id=4001, logical_suffix=suffix, max_attempts=1)
    assert created is True and reclaimed.pk == attempt.pk and reclaimed.state == "queued", \
        f"later sweep/claim must re-arm the same transient logical work: {created}/{reclaimed.state}"
    assert reclaimed.attempt_count == 0, \
        f"a reclaimed retry cycle must reset its bounded attempt budget, got {reclaimed.attempt_count}"


@th.django_unit_test()
def test_manual_analysis_is_fresh_coalesced_and_status_neutral(opts):
    from mojo.apps.incident.models import Incident
    from mojo.apps.incident.services import llm_dispatch

    incident = Incident.objects.create(
        category=f"test:llm-manual:{uuid.uuid4().hex}", status="open",
        priority=4, title="LLM manual analysis")
    first, created = llm_dispatch.claim_incident(
        incident, feature="incident_analysis", fresh=True)
    duplicate, duplicate_created = llm_dispatch.claim_incident(
        incident, feature="incident_analysis", fresh=True)
    incident.refresh_from_db()
    assert created is True and duplicate_created is False and duplicate.pk == first.pk, \
        "concurrent manual requests must coalesce on the active attempt"
    assert incident.status == "open", \
        f"manual analysis must not force investigating, got {incident.status}"
    first.state = "succeeded"
    first.save(update_fields=["state"])
    second, second_created = llm_dispatch.claim_incident(
        incident, feature="incident_analysis", fresh=True)
    assert second_created is True and second.pk != first.pk, \
        "a later manual request after success must receive a fresh logical attempt"
