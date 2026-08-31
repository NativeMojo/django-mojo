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
