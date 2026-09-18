"""Fleet retirement must drain work without losing its visibility lease."""
import json
import threading
from unittest import mock

from testit import helpers as th


@th.django_unit_test()
def test_engine_drain_keeps_visibility_until_jobs_and_hooks_finish(opts):
    import signal
    from mojo.apps.jobs import job_engine
    from mojo.helpers import dates

    redis = mock.MagicMock()
    with mock.patch.object(job_engine, "get_adapter", return_value=redis):
        engine = job_engine.JobEngine(channels=["fleet-drain"], max_workers=2)
    engine.running = True
    engine.start_time = dates.utcnow()
    engine.heartbeat_interval = 0.01
    handlers = {}
    with mock.patch.object(job_engine.signal, "signal", side_effect=lambda sig, fn: handlers.update({sig: fn})):
        engine._setup_signal_handlers()
    job_started, hook_started = threading.Event(), threading.Event()
    job_release, hook_release = threading.Event(), threading.Event()
    draining, renewed = threading.Event(), threading.Event()
    errors = []

    def job_work():
        job_started.set()
        job_release.wait(5)

    def hook_work(unused):
        hook_started.set()
        hook_release.wait(5)

    def lease_touch(key, mapping):
        if draining.is_set() and "active-fleet-job" in mapping:
            renewed.set()

    redis.zadd.side_effect = lease_touch
    future = engine.executor.submit(job_work)
    engine.active_jobs["active-fleet-job"] = {
        "future": future, "channel": "fleet-drain"}
    future.add_done_callback(lambda f: engine._job_completed("active-fleet-job"))
    with mock.patch.object(job_engine, "load_job_function", return_value=hook_work):
        engine._run_startup_hooks(["test.fleet_hook"])
        assert hook_started.wait(1), "startup hook did not enter its active work"
    assert job_started.wait(1), "job did not enter its active work"
    original_shutdown = engine.executor.shutdown

    def shutdown(*args, **kwargs):
        draining.set()
        return original_shutdown(*args, **kwargs)

    engine.executor.shutdown = shutdown
    engine.heartbeat_thread = threading.Thread(target=engine._heartbeat_loop)
    engine.heartbeat_thread.start()

    def stop():
        try:
            engine.stop(timeout=0)
        except Exception as error:
            errors.append(error)

    stopper = threading.Thread(target=stop)
    stopper.start()
    try:
        assert draining.wait(1), "engine never began draining its executor"
        assert renewed.wait(1), "draining engine stopped renewing its active job lease"
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        renewed.clear()
        assert renewed.wait(1), "repeated TERM ended the active job visibility lease"
        assert engine.running and not engine.stop_event.is_set(), (
            "heartbeat/control lifecycle ended before active work drained")
        job_release.set()
        future.result(timeout=1)
        assert stopper.is_alive(), "engine exited before its startup hook finished"
    finally:
        job_release.set()
        hook_release.set()
        stopper.join(3)
        engine.stop_event.set()
        engine.heartbeat_thread.join(1)
    assert not stopper.is_alive(), "engine did not finish after work drained"
    assert not errors, f"engine drain failed: {errors}"


@th.django_unit_test()
def test_shutdown_control_only_requests_drain(opts):
    from mojo.apps.jobs import job_engine

    with mock.patch.object(job_engine, "get_adapter", return_value=mock.MagicMock()):
        engine = job_engine.JobEngine(channels=["fleet-control"])
    engine.running = True
    engine.control_thread = threading.Thread(
        target=engine._handle_control_message,
        args=(json.dumps({"command": "shutdown"}),))
    engine.control_thread.start()
    engine.control_thread.join(1)
    try:
        assert not engine.control_thread.is_alive(), "shutdown control blocked on teardown"
        assert engine.running, "control callback performed teardown instead of requesting it"
        assert engine.shutdown_requested.is_set(), "shutdown control did not request drain"
        assert not engine.stop_event.is_set(), "control callback stopped active job visibility"
    finally:
        engine.stop()


@th.django_unit_test()
def test_brpop_result_is_restored_if_drain_arrives_while_waiting(opts):
    from mojo.apps.jobs import job_engine

    redis = mock.MagicMock()
    with mock.patch.object(job_engine, "get_adapter", return_value=redis):
        engine = job_engine.JobEngine(channels=["fleet-pop"])
    engine.running = True
    queue = engine.keys.queue("fleet-pop")
    ran = []

    def pop(*args, **kwargs):
        request = getattr(engine, "request_shutdown", engine.stop_event.set)
        request()
        return queue, "unclaimed-job"

    redis.brpop.side_effect = pop
    engine.execute_job = lambda *args: ran.append(args)
    try:
        engine._main_loop()
    finally:
        engine.stop(timeout=0)
    assert not ran, "engine started a popped job after drain was requested"
    redis.rpush.assert_called_once_with(queue, "unclaimed-job")
    assert not redis.zadd.called, "unclaimed job was entered into in-flight processing"


@th.django_unit_test()
def test_external_stop_waits_for_claim_to_finish_submission(opts):
    from mojo.apps.jobs import job_engine

    redis = mock.MagicMock()
    with mock.patch.object(job_engine, "get_adapter", return_value=redis):
        engine = job_engine.JobEngine(channels=["fleet-claim-race"])
    engine.running = True
    admitted, release, closing = threading.Event(), threading.Event(), threading.Event()
    ran = []

    def processing(*args, **kwargs):
        admitted.set()
        release.wait(5)

    redis.brpop.return_value = (engine.keys.queue("fleet-claim-race"), "claimed-job")
    redis.zadd.side_effect = processing
    engine.execute_job = lambda *args: ran.append(args)
    original_shutdown = engine.executor.shutdown

    def shutdown(*args, **kwargs):
        closing.set()
        return original_shutdown(*args, **kwargs)

    engine.executor.shutdown = shutdown
    consumer = threading.Thread(target=engine._main_loop)
    consumer.start()
    stopper = None
    try:
        assert admitted.wait(1), "consumer did not cross the claim admission boundary"
        stopper = threading.Thread(target=engine.stop)
        stopper.start()
        assert engine.shutdown_requested.wait(1), "external stop did not request shutdown"
        assert not closing.wait(0.05), "executor closed while an admitted claim was still submitting"
    finally:
        engine.request_shutdown()
        release.set()
        consumer.join(2)
        if stopper is not None:
            stopper.join(2)
        else:
            engine.stop()
    assert not consumer.is_alive(), "claim consumer failed to stop"
    assert stopper is None or not stopper.is_alive(), "external stop failed to drain"
    assert ran == [("fleet-claim-race", "claimed-job")], "admitted job was lost or duplicated"


@th.django_unit_test()
def test_scheduler_signal_defers_lock_release_until_batch_finishes(opts):
    import signal
    from mojo.apps.jobs import scheduler

    with mock.patch.object(scheduler, "get_adapter", return_value=mock.MagicMock()):
        worker = scheduler.Scheduler(channels=["fleet-scheduler"])
    worker.running = True
    worker.has_lock = True
    worker._release_lock = mock.Mock()
    handlers = {}
    with mock.patch.object(scheduler.signal, "signal", side_effect=lambda sig, fn: handlers.update({sig: fn})):
        worker._setup_signal_handlers()
    exited = False
    try:
        handlers[signal.SIGTERM](signal.SIGTERM, None)
    except SystemExit:
        exited = True
    assert not exited, "TERM interrupted an in-progress scheduler batch"
    assert worker.stop_event.is_set(), "TERM did not request scheduler shutdown"
    worker._release_lock.assert_not_called()
    worker.stop()
    worker._release_lock.assert_called_once()


@th.django_unit_test()
def test_draining_engine_rejects_new_checked_work(opts):
    from mojo.apps.jobs import job_engine
    from mojo.helpers import dates

    redis = mock.MagicMock()
    with mock.patch.object(job_engine, "get_adapter", return_value=redis):
        engine = job_engine.JobEngine(channels=["fleet-checked"])
    engine.is_initialized = True
    engine.start_time = dates.utcnow()
    engine.request_shutdown()
    correlation = "1234567890abcdef" * 2
    message = {
        "protocol": 2, "correlation_id": correlation,
        "reply_channel": engine.keys.reply_channel(correlation),
        "func": "test.must_not_run", "channel": "fleet-checked", "data": {},
        "target": {"runner_id": engine.runner_id,
                   "hostname": job_engine.host_channel(),
                   "started": engine.start_time.isoformat()},
    }
    with mock.patch.object(job_engine, "load_job_function") as loader:
        engine._handle_checked_execute(message, engine.keys.runner_ctl(engine.runner_id))
    loader.assert_not_called()
    reply = json.loads(redis.publish.call_args.args[1])
    assert reply["error"] == "runner_draining", "new control work was accepted during drain"
    engine.executor.shutdown(wait=True)


@th.django_unit_test()
def test_active_control_callback_finishes_before_engine_exit(opts):
    from mojo.apps.jobs import job_engine
    from mojo.helpers import dates

    with mock.patch.object(job_engine, "get_adapter", return_value=mock.MagicMock()):
        engine = job_engine.JobEngine(channels=["fleet-control-active"])
    engine.running = True
    engine.start_time = dates.utcnow()
    started, release = threading.Event(), threading.Event()

    def work(data):
        started.set()
        release.wait(5)
        return {}

    with mock.patch.object(job_engine, "load_job_function", return_value=work):
        engine.control_thread = threading.Thread(
            target=engine._handle_control_message,
            args=(json.dumps({"command": "execute", "func": "test.active"}),))
        engine.control_thread.start()
        assert started.wait(1), "control callback did not start"
        stopper = threading.Thread(target=engine.stop)
        stopper.start()
        try:
            assert engine._control_stop.wait(1), "engine did not enter control callback drain"
            assert stopper.is_alive(), "engine retired an active control callback"
            assert engine.running and not engine.stop_event.is_set(), (
                "heartbeat ended while a control callback still held work")
        finally:
            release.set()
            stopper.join(2)
            engine.control_thread.join(1)
    assert not stopper.is_alive(), "engine failed to exit after control work returned"


@th.django_unit_test()
def test_scheduler_finishes_and_restores_current_popped_batch(opts):
    import time
    from mojo.apps.jobs import scheduler
    from django.utils import timezone

    redis = mock.MagicMock()
    redis.get.return_value = None
    with mock.patch.object(scheduler, "get_adapter", return_value=redis):
        worker = scheduler.Scheduler(channels=["fleet-batch"])
    future = time.time() * 1000 + 100000

    def pop(*args, **kwargs):
        worker.stop_event.set()
        return [("future-job", future)]

    redis.zpopmin.side_effect = pop
    worker._process_channel("fleet-batch", timezone.now(), time.time() * 1000)
    redis.zadd.assert_called_once_with(worker.keys.sched("fleet-batch"), {"future-job": future})
    assert redis.zpopmin.call_count == 1, "draining scheduler popped another batch"


@th.django_unit_test()
def test_fleet_proof_captures_revision_and_rejects_stale_or_reused_pid(opts):
    import tempfile
    import time
    from pathlib import Path
    from mojo.apps.jobs import fleet_state
    from mojo.helpers.settings import settings

    with tempfile.TemporaryDirectory() as root, \
            mock.patch.object(fleet_state, "_started_at", return_value=time.time() - 60):
        with mock.patch.object(settings, "get_static", return_value="a" * 32), \
                mock.patch.object(fleet_state, "_start_ticks", return_value=1234):
            state = fleet_state.begin("engine", root=root)
            assert state is not None, "configured process did not capture its loaded revision"
            assert fleet_state.publish(state), "process proof did not publish"
            proof = fleet_state.read("engine", state["pid"], root=root)
            assert proof and proof["loaded_revision"] == "a" * 32, "loaded revision proof was missing"
        with mock.patch.object(settings, "get_static", return_value="b" * 32), \
                mock.patch.object(fleet_state, "_start_ticks", return_value=1234):
            fleet_state.publish(state, draining=True)
            proof = fleet_state.read("engine", state["pid"], root=root)
            assert proof["loaded_revision"] == "a" * 32, "publication re-read a newer disk/settings revision"
            assert proof["draining"] is True, "draining process claimed ready-to-run work"
        with mock.patch.object(fleet_state, "_start_ticks", return_value=1235):
            assert fleet_state.read("engine", state["pid"], root=root) is None, (
                "proof survived kernel PID reuse")
        path = Path(root) / "job_processes" / f"engine-{state['pid']}.json"
        old = json.loads(path.read_text())
        old.update(started_at=time.time() - 100, updated_at=time.time() - 30)
        path.write_text(json.dumps(old))
        with mock.patch.object(fleet_state, "_start_ticks", return_value=1234):
            assert fleet_state.read("engine", state["pid"], root=root) is None, "stale proof was accepted"
            old["updated_at"] = 10 ** 1000
            path.write_text(json.dumps(old))
            assert fleet_state.read("engine", state["pid"], root=root) is None, (
                "oversized numeric timestamp escaped fail-closed proof parsing")
        fleet_state.remove(state)
        assert not path.exists(), "orderly shutdown left process proof behind"


@th.django_unit_test()
def test_fleet_proof_refuses_symlinks_and_unconfigured_writes(opts):
    import os
    import tempfile
    from pathlib import Path
    from mojo.apps.jobs import fleet_state
    from mojo.helpers.settings import settings

    with tempfile.TemporaryDirectory() as root:
        with mock.patch.object(settings, "get_static", return_value=None):
            assert fleet_state.begin("engine", root=root) is None, "unconfigured tooling created fleet state"
        assert not fleet_state.publish(None), "empty proof state produced a file"
        assert list(Path(root).iterdir()) == [], "unconfigured tooling wrote to disk"
        directory = Path(root) / "job_processes"
        directory.mkdir()
        target = Path(root) / "other.json"
        target.write_text("{}")
        path = directory / f"engine-{os.getpid()}.json"
        path.symlink_to(target)
        assert fleet_state.read("engine", os.getpid(), root=root) is None, "reader followed a proof symlink"


@th.django_unit_test()
def test_failed_engine_startup_never_publishes_ready_and_cleans_up(opts):
    from mojo.apps.jobs import job_engine, fleet_state
    from mojo.helpers import dates

    for failed_stage in ("capabilities", "hooks"):
        with mock.patch.object(job_engine, "get_adapter", return_value=mock.MagicMock()):
            engine = job_engine.JobEngine(channels=["fleet-startup"])

        def initialize():
            engine.running = True
            engine.start_time = dates.utcnow()

        engine.initialize = initialize
        engine.capability_cache.start = mock.Mock()
        engine._run_startup_hooks = mock.Mock()
        engine._main_loop = mock.Mock()
        if failed_stage == "capabilities":
            engine.capability_cache.start.side_effect = RuntimeError("startup refused")
        else:
            engine._run_startup_hooks.side_effect = RuntimeError("startup refused")
        with mock.patch.object(fleet_state, "begin", return_value={"test": True}), \
                mock.patch.object(fleet_state, "publish") as publish, \
                mock.patch.object(fleet_state, "remove") as remove:
            failed = False
            try:
                engine.start()
            except RuntimeError:
                failed = True
            assert failed, "startup error was swallowed"
            publish.assert_not_called()
            remove.assert_called_once_with({"test": True})
        engine._main_loop.assert_not_called()
        assert not engine.running and engine.stop_event.is_set(), "failed startup leaked engine lifecycle"
        assert not engine._fleet_ready, "failed startup advertised fleet readiness"


@th.django_unit_test()
def test_engine_readiness_begins_at_queue_consumption(opts):
    from mojo.apps.jobs import job_engine, fleet_state
    from mojo.helpers import dates

    with mock.patch.object(job_engine, "get_adapter", return_value=mock.MagicMock()):
        engine = job_engine.JobEngine(channels=["fleet-startup-ready"])
    observed = []

    def initialize():
        engine.running = True
        engine.start_time = dates.utcnow()

    engine.initialize = initialize
    engine.capability_cache.start = lambda: observed.append(("capabilities", engine._fleet_ready))
    engine._run_startup_hooks = lambda: observed.append(("hooks", engine._fleet_ready))
    engine._main_loop = lambda: observed.append(("consume", engine._fleet_ready))
    with mock.patch.object(fleet_state, "begin", return_value={"test": True}), \
            mock.patch.object(fleet_state, "publish") as publish, \
            mock.patch.object(fleet_state, "remove"):
        engine.start()
    assert observed == [("capabilities", False), ("hooks", False), ("consume", True)], (
        "readiness did not follow successful startup dispatch")
    assert publish.call_count == 2, "ready/draining proof was not published at lifecycle boundaries"


@th.django_unit_test()
def test_kernel_birth_is_independent_of_proof_publication(opts):
    from mojo.apps.jobs import fleet_state

    with mock.patch.object(fleet_state.os, "sysconf", return_value=100), \
            mock.patch.object(fleet_state.time, "time", return_value=10000), \
            mock.patch.object(fleet_state.time, "CLOCK_BOOTTIME", 7, create=True), \
            mock.patch.object(fleet_state.time, "clock_gettime", return_value=1000):
        assert fleet_state._started_at(90000) == 9900, "kernel birth was replaced with observation time"
        assert fleet_state._started_at(100001) is None, "future kernel birth was accepted"
    with mock.patch.object(fleet_state.time, "clock_gettime", side_effect=OSError("unsupported clock")):
        assert fleet_state._started_at(90000) is None, "unsupported boot clock did not fail closed"


@th.django_unit_test()
def test_late_proof_cannot_hide_process_born_before_install(opts):
    import os
    import tempfile
    import time
    from mojo.apps.jobs import fleet_state, execution_context
    from mojo.deploy import fleet_config_node, jobman
    from mojo.helpers.settings import settings

    revision = "c" * 32
    now = time.time()
    original_read = fleet_state.read
    with tempfile.TemporaryDirectory() as root, \
            mock.patch.object(settings, "get_static", return_value=revision), \
            mock.patch.object(fleet_state, "_start_ticks", return_value=1234), \
            mock.patch.object(fleet_state, "_started_at", return_value=now - 60), \
            mock.patch.object(execution_context, "current_runner_incarnation", return_value={"runner_id": "node-engine", "started": "now"}), \
            mock.patch.object(jobman, "exact_processes", return_value=[str(os.getpid())]):
        state = fleet_state.begin("engine", root=root)
        assert state["started_at"] == now - 60, "begin did not capture the actual process birth"
        # Simulate initialization/publication after installation while the
        # process had already loaded the previous base configuration. The
        # override revision is deliberately unchanged across that update.
        state["started_at"] = now - 1
        assert fleet_state.publish(state), "late proof publication failed"
        proof = original_read("engine", os.getpid(), root=root)
        assert proof["started_at"] == now - 60, "reader trusted a later claimed process birth"
        with mock.patch.object(fleet_state, "read", side_effect=lambda component, pid: original_read(component, pid, root=root)):
            result = fleet_config_node._jobs_proof(revision, now - 10)
        assert result["error_code"] == "engine_revision_pending", (
            "process born before installation passed on late publication with the same override revision")
        assert not result["healthy"], "old process configuration was declared healthy"
