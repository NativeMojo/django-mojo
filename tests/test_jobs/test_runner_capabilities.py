"""Provider cache must keep heartbeat publication free of slow probes."""
from types import SimpleNamespace
import uuid
from testit import helpers as th


@th.django_unit_test()
def test_default_runner_consumes_firewall(opts):
    from mojo.apps.jobs import DEFAULT_CHANNELS
    th.assert_in("firewall", DEFAULT_CHANNELS, "normal API engine must consume firewall work")
    from mojo.apps.jobs.capabilities import validate_firewall_runner
    th.assert_true(validate_firewall_runner(DEFAULT_CHANNELS, True), "default API runner must satisfy firewall contract")
    for channels, direct in ((["default"], True), (["firewall"], False)):
        with th.assert_raises(ValueError):
            validate_firewall_runner(channels, direct)


@th.django_unit_test()
def test_capability_cache_transitions_and_expires(opts):
    from mojo.apps.jobs.capabilities import CapabilityCache
    calls, transitions = [], []
    now = [1.0]
    engine = SimpleNamespace(channels=["firewall", "host-engine"], runner_id="host-engine")
    def probe(engine):
        calls.append(True)
        return {"ready": True}
    cache = CapabilityCache(engine, providers={"firewall_reconcile": (probe, lambda e: transitions.append(e))},
                            clock=lambda: now[0], ttl=10)
    th.assert_eq(cache.snapshot(), {}, "unprobed capability must stay absent")
    cache.refresh()
    th.assert_eq(cache.snapshot(), {"firewall_reconcile": 1}, "ready capability must be advertised")
    cache.refresh()
    th.assert_eq(len(transitions), 1, "ready transition recovery must fire once")
    cache.snapshot()
    th.assert_eq(len(calls), 2, "heartbeat cache reads must never run probes")
    now[0] = 12
    th.assert_eq(cache.snapshot(), {}, "expired proof must stop advertisement")


@th.django_unit_test()
def test_engine_marks_structural_failure_terminal_with_retries_remaining(opts):
    from mojo.apps.jobs.models import Job
    from mojo.apps.jobs.job_engine import JobEngine
    from mojo.apps.incident.asyncjobs import FirewallSyncRetry
    row = Job.objects.create(id=uuid.uuid4().hex, channel="testit_firewall_terminal",
                             func="example.never", status="running", attempt=1, max_retries=8)
    removed = []
    engine = JobEngine.__new__(JobEngine)
    engine.runner_id = "testit-firewall-engine"
    engine._remove_from_processing = lambda channel, job_id: removed.append(job_id)
    try:
        engine._handle_job_failure(row.pk, row.channel, FirewallSyncRetry("broker_wrong_user"))
        row.refresh_from_db()
        th.assert_eq(row.status, "failed", "structural failure must terminate despite retries remaining")
        th.assert_true(row.finished_at is not None, "terminal failure needs durable completion time")
        th.assert_eq(removed, [row.pk], "terminal failure must remove processing membership")
    finally:
        row.delete()
