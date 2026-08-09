"""
Engine startup hooks (maestro #1772).

The property under test is placement as much as mechanism: hooks fire from
start() — the entry point unique to a real runner daemon — and never from
initialize(), which in-process tooling and tests also reach. That is what
keeps a converge-on-boot handler from firing on every `manage.py shell` or
test run.

No test here touches the process-global hook registry: test modules run in
parallel threads, the registry carries other apps' real hooks (edge's
converge), and patching it would race any module reading it. Wiring is
asserted with instance-level patches; execution goes through the `hooks=`
injection seam.
"""
import concurrent.futures

from unittest import mock

from testit import helpers as th


HOOK_CALLS = []


def record_hook(engine):
    HOOK_CALLS.append(engine.runner_id)
    return "recorded"


def broken_hook(engine):
    raise RuntimeError("deliberately broken startup hook")


@th.django_unit_test("initialize() alone never fires startup hooks")
def test_initialize_does_not_fire_hooks(opts):
    from mojo.apps.jobs.job_engine import JobEngine

    engine = JobEngine(channels=["default"], runner_id="hooks-init-engine")
    try:
        with mock.patch.object(engine, "_run_startup_hooks") as run_hooks:
            engine.initialize()
        assert run_hooks.call_count == 0, (
            "initialize() ran startup hooks — shells, management commands "
            "and tests reach initialize(), so hooks there would converge "
            "from laptops and CI")
    finally:
        engine.stop()


@th.django_unit_test("start() runs the startup hooks exactly once")
def test_start_fires_hooks_once(opts):
    from mojo.apps.jobs.job_engine import JobEngine

    engine = JobEngine(channels=["default"], runner_id="hooks-start-engine")
    with mock.patch.object(engine, "_run_startup_hooks") as run_hooks, \
            mock.patch.object(engine, "_main_loop"):
        # start() blocks in _main_loop for a real daemon; a no-op loop makes
        # it run the full startup path and then its finally-stop().
        engine.start()

    assert run_hooks.call_count == 1, (
        f"start() must run the startup hooks exactly once, "
        f"got {run_hooks.call_count} calls")
    assert not engine.running, (
        "the engine should have completed its start/stop cycle cleanly")


@th.django_unit_test("a hook runs on the worker pool with the engine")
def test_hook_receives_engine(opts):
    from mojo.apps.jobs.job_engine import JobEngine

    HOOK_CALLS.clear()
    engine = JobEngine(channels=["default"], runner_id="hooks-exec-engine")
    try:
        futures = engine._run_startup_hooks(
            hooks=[f"{__name__}.record_hook"])
        concurrent.futures.wait(futures, timeout=10)
        assert HOOK_CALLS == ["hooks-exec-engine"], (
            f"the hook must be called once with the engine, got {HOOK_CALLS}")
    finally:
        engine.executor.shutdown(wait=True)


@th.django_unit_test("broken and unloadable hooks are contained")
def test_broken_hook_is_contained(opts):
    from mojo.apps.jobs.job_engine import JobEngine

    HOOK_CALLS.clear()
    engine = JobEngine(channels=["default"], runner_id="hooks-broken-engine")
    try:
        futures = engine._run_startup_hooks(hooks=[
            f"{__name__}.broken_hook",
            "tests.no.such.module.no_such_hook",
            f"{__name__}.record_hook",
        ])
        done, not_done = concurrent.futures.wait(futures, timeout=10)
        assert not not_done, "startup hooks did not finish within 10s"
        for future in done:
            assert future.exception() is None, (
                "a hook failure escaped _run_startup_hook — it would have "
                f"taken the engine down: {future.exception()!r}")
        assert HOOK_CALLS == ["hooks-broken-engine"], (
            "a raising hook and an unloadable hook must be logged and "
            f"skipped, with later hooks still running — got {HOOK_CALLS}")
    finally:
        engine.executor.shutdown(wait=True)
