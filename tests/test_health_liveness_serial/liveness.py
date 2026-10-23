import json
from datetime import datetime, timedelta, timezone
from unittest import mock

from testit import helpers as th

NOW = datetime(2026, 10, 23, 14, 0, tzinfo=timezone.utc)


class Redis:
    def __init__(self):
        self.data, self.writes = {}, []
    def get(self, key):
        return self.data.get(key)
    def set(self, key, value, **kwargs):
        if kwargs.get("nx") and key in self.data:
            return False
        self.data[key] = value
        self.writes.append((key, kwargs))
        return True
    def delete(self, key):
        self.data.pop(key, None)


def _runner(host="node-a", runner_id="node-a-100", channels=None):
    return dict(hostname=host, runner_id=runner_id, channels=channels or ["edge"],
                alive=True, last_heartbeat=NOW.isoformat())


def _collect(client, rows, *, expected=None, now=NOW, reporter=None, channels=None):
    from mojo.apps.incident.services import health_liveness as hl
    def roster(channel, **kwargs):
        if isinstance(rows, Exception):
            raise rows
        return [row for row in rows if channel in row["channels"]]
    return hl.collect(client=client, roster=roster, expected=expected,
                      channels=channels or ["edge"], now=now, report_fn=reporter)


@th.django_unit_test()
def test_cron_checks_liveness_without_publishing_a_detector_job(opts):
    from mojo.apps.incident import cronjobs
    with mock.patch.object(cronjobs, "HEALTH_MONITORING_ENABLED", True), \
            mock.patch.object(cronjobs, "_ensure_health_defaults"), \
            mock.patch.object(cronjobs.jobs, "publish") as publish:
        # The collector's bounded production reads may return Unknown here;
        # the regression is that OS cron itself must execute that read.
        result = cronjobs.check_system_health(now=NOW)
    queued_detectors = [call for call in publish.call_args_list
                       if call.kwargs.get("func") == "mojo.apps.incident.asyncjobs.check_system_health"]
    assert not queued_detectors, "the stopped scheduler must not be responsible for starting its detector"
    assert isinstance(result, dict) and "scheduler" in result, "cron did not return independently collected liveness evidence"


@th.django_unit_test()
def test_disappeared_runner_outlives_heartbeat_and_pid_is_not_identity(opts):
    client = Redis()
    _collect(client, [_runner()])
    gone = _collect(client, [], now=NOW + timedelta(seconds=180))
    assert gone["runners"]["missing"] == [{"hostname": "node-a", "channels": ["edge"]}], "expired heartbeat erased an established expected runner"
    restarted = _collect(client, [_runner(runner_id="node-a-200")], now=NOW + timedelta(seconds=360))
    assert restarted["runners"]["healthy"] is True, "a legitimate PID replacement retained an alarm for the old process"


@th.django_unit_test()
def test_configured_missing_and_intentionally_retired_hosts(opts):
    client = Redis()
    absent = _collect(client, [], expected=["node-a"])
    assert absent["runners"]["healthy"] is False and absent["runners"]["missing"], "an empty live roster hid configured missing runners"
    _collect(client, [_runner(), _runner("node-b", "node-b-100")], expected=["node-a", "node-b"])
    retired = _collect(client, [_runner()], expected=["node-a"], now=NOW + timedelta(seconds=180))
    assert retired["runners"]["healthy"] is True and not retired["runners"]["missing"], "current declarations did not retire the removed host"


@th.django_unit_test()
def test_sibling_runner_does_not_hide_a_missing_role_and_declared_retirement_clears_it(opts):
    client = Redis()
    _collect(client, [_runner(channels=["edge", "email"])], channels=["edge", "email"])
    partial = [_runner(), _runner("node-b", "node-b-200", ["email"])]
    lost = _collect(client, partial, channels=["edge", "email"], now=NOW+timedelta(seconds=180))
    assert lost["runners"]["missing"] == [{"hostname": "node-a", "channels": ["email"]}], "a responding sibling engine erased an established missing role"
    retired = _collect(client, partial, channels=["edge"], now=NOW+timedelta(seconds=360))
    assert retired["runners"]["healthy"] is True, "current declared channel retirement retained a stale role alarm"


@th.django_unit_test()
def test_roster_failure_is_unknown_visible_and_does_not_erase_baseline(opts):
    client, events = Redis(), []
    _collect(client, [_runner()])
    failed = _collect(client, RuntimeError("runner_roster_invalid"), reporter=lambda *args, **kwargs: events.append(kwargs))
    assert failed["runners"]["status"] == "unavailable" and failed["runners"]["healthy"] is None, "a failed roster read became an empty healthy roster"
    assert any(event["category"] == "system:health:runner" for event in events), "a roster read failure did not raise a visible health event"
    after = _collect(client, [], now=NOW + timedelta(seconds=180))
    assert after["runners"]["missing"], "a failed read erased established expectations"


@th.django_unit_test()
def test_stopped_scheduler_and_progress_recovery_without_any_jobs(opts):
    from mojo.apps.incident.services import health_liveness as hl
    from mojo.apps.jobs.keys import JobKeys
    client = Redis()
    stopped = _collect(client, [], expected=["node-a"])
    assert stopped["scheduler"]["healthy"] is False, "no queued jobs and no scheduler incorrectly read as healthy"
    lock = "test-leader"
    client.data[JobKeys().scheduler_lock()] = lock
    client.data[hl.progress_key()] = json.dumps(hl.progress_sample(lock, "scheduler-a", NOW))
    recovered = _collect(client, [_runner()], now=NOW + timedelta(seconds=1))
    assert recovered["scheduler"]["healthy"] is True, "an independent observation of current loop progress did not recover"
    stale = _collect(client, [_runner()], now=NOW + timedelta(seconds=60))
    assert stale["scheduler"]["healthy"] is False, "an old loop progress sample was renewed by the detector's read"


@th.django_unit_test()
def test_cached_detector_reads_never_renew_observation_or_expiry(opts):
    from mojo.apps.incident.services import health_liveness as hl
    client = Redis()
    _collect(client, [_runner()])
    before = len(client.writes)
    first = hl.read_cached(client=client, now=NOW + timedelta(seconds=1))
    second = hl.read_cached(client=client, now=NOW + timedelta(seconds=181))
    stale = hl.read_cached(client=client, now=NOW + timedelta(seconds=421))
    assert first["observed_at"] == second["observed_at"] == NOW.isoformat(), "cached collection time became a detector observation"
    assert len(client.writes) == before, "reading cached detector evidence renewed Redis expiry"
    assert stale["status"] == "stale" and stale["scheduler"]["healthy"] is False, "detector expiry was hidden or old measured results were discarded"


@th.django_unit_test()
def test_repeated_ticks_do_not_fan_out_and_recovery_rearms_event(opts):
    client, events = Redis(), []
    report = lambda *args, **kwargs: events.append(kwargs)
    _collect(client, [], expected=["node-a"], reporter=report)
    count = len(events)
    _collect(client, [], expected=["node-a"], now=NOW + timedelta(seconds=180), reporter=report)
    assert len(events) == count, "an unchanged independent detector tick repeated broad incident fanout"
    _collect(client, [_runner()], expected=["node-a"], now=NOW + timedelta(seconds=360), reporter=report)
    _collect(client, [], expected=["node-a"], now=NOW + timedelta(seconds=540), reporter=report)
    assert len(events) == count + 1, "runner recovery did not rearm its next outage event"


@th.django_unit_test()
def test_disabled_health_monitoring_neither_collects_nor_publishes(opts):
    from mojo.apps.incident import cronjobs
    from mojo.apps.account.services import health_snapshot as hs
    with mock.patch.object(cronjobs, "HEALTH_MONITORING_ENABLED", False), \
            mock.patch.object(hs, "resolve_identity", side_effect=hs.SnapshotError("source_identity_unresolved")), \
            mock.patch.object(cronjobs.jobs, "publish") as publish:
        assert cronjobs.check_system_health(now=NOW) is None, "disabled health monitoring unexpectedly ran the detector"
    assert not publish.called, "disabled health monitoring queued a health job"


@th.django_unit_test()
def test_enrolled_snapshot_source_collects_quietly_with_legacy_alarms_disabled(opts):
    from mojo.apps.incident import cronjobs
    from mojo.apps.incident.services import health_liveness as hl
    from mojo.apps.account.services import health_snapshot as hs
    result = {"scheduler": {}, "runners": {"live_count": 2}}
    with mock.patch.object(cronjobs, "HEALTH_MONITORING_ENABLED", False), \
            mock.patch.object(hs, "resolve_identity", return_value={"source_id": "a" * 64}), \
            mock.patch.object(hl, "collect", return_value=result) as collect, \
            mock.patch.object(cronjobs, "_ensure_health_defaults") as defaults, \
            mock.patch.object(cronjobs.jobs, "publish") as publish:
        assert cronjobs.check_system_health(now=NOW) == result, "enrolled snapshots did not get independent evidence"
    assert collect.call_args.kwargs["report_fn"] is None, "snapshot enrollment enabled health incident reporting"
    assert not defaults.called and not publish.called, "quiet collection installed notify rules or caused resource fanout"


@th.django_unit_test()
def test_scheduler_loop_writes_real_progress_and_bounds_write_cadence(opts):
    from mojo.apps.jobs.scheduler import Scheduler
    from mojo.apps.jobs.services import liveness
    scheduler = object.__new__(Scheduler)
    scheduler.has_lock, scheduler._last_liveness = True, 0
    scheduler.redis, scheduler.lock_value, scheduler.scheduler_id = Redis(), "leader", "node-a-scheduler"
    scheduler._publish_loop_progress()
    raw = scheduler.redis.data[liveness.progress_key()]
    sample = json.loads(raw)
    assert sample["lease"] == liveness.lease_digest("leader") and sample["observed_at"], "scheduler loop did not bind its actual progress to its leader incarnation"
    scheduler._publish_loop_progress()
    assert len(scheduler.redis.writes) == 1 and scheduler.redis.writes[0][1]["ex"] == 30, "loop progress writes were not throttled or expiring"


@th.django_unit_test()
def test_fleet_and_snapshot_consume_cached_detector_without_creating_samples(opts):
    import time
    from mojo.apps.aws.services import fleet_health as fh
    from mojo.apps.account.services import health_evidence as he
    from test_account.health_evidence import _assembly_configuration
    client = Redis()
    _collect(client, [], expected=["node-a"])
    class Context:
        def __enter__(self):
            return client
        def __exit__(self, *args):
            pass
    first = fh._read_scheduler(time.monotonic()+2, connect=lambda **kwargs: Context(), now=NOW)
    reread = fh._read_scheduler(time.monotonic()+2, connect=lambda **kwargs: Context(), now=NOW+timedelta(seconds=300))
    assert first["observed_at"] is None and reread["detector"]["observed_at"] == first["detector"]["observed_at"], "fleet lock reads fabricated or renewed detector observation time"
    fleet = {"rows": [], "evidence": {}, "sources": {"scheduler": first}, "expected": {"complete": True}, "roster_complete": True}
    def project(source, now):
        fleet["sources"]["scheduler"] = source
        return next(row for row in he.build_components(_assembly_configuration(), {}, {}, fleet, {}, now=now)
                    if row["kind"] == "jobman")
    initial = project(first, NOW)
    repeated = project(reread, NOW+timedelta(seconds=300))
    slot = lambda row, name: next(value for value in row["evidence"] if value["slot_id"] == name)
    assert not slot(initial, "scheduler_lock")["required"], "noncausal scheduler lease metadata blocked complete coverage"
    assert slot(initial, "scheduler_detector")["sample_id"] == slot(repeated, "scheduler_detector")["sample_id"], "snapshot collection invented a second independent detector sample"
    assert next(row for row in initial["conditions"] if row["rule"] == "scheduler_available")["outcome"] == "fail", "known independent scheduler failure was not a causal Jobman condition"
    stale = project(first, NOW+timedelta(seconds=421))
    assert next(row for row in stale["conditions"] if row["rule"] == "scheduler_available")["outcome"] == "unknown", "stale detector evidence established a current scheduler failure"
