from testit import helpers as th
import threading


@th.unit_test("job execution context is immutable nested-safe and cleared")
def test_execution_context_lifecycle(opts):
    from mojo.apps.jobs.execution_context import current, execution

    th.assert_eq(current(), None, "worker threads must begin without inherited job identity")
    with execution("job-1", "pkg.func", 2, "priority", "runner-1", broadcast=False) as value:
        th.assert_eq(current(), value, "the active job must be visible to trusted call sites")
        with th.assert_raises(RuntimeError):
            with execution("job-2", "pkg.other", 1, "priority", "runner-1"):
                pass
    th.assert_eq(current(), None, "job identity must clear in finally")


@th.unit_test("execution identity is generated rather than caller supplied")
def test_execution_identity_is_generated(opts):
    from mojo.apps.jobs.execution_context import execution

    with execution("job-1", "pkg.func", 1, "default", "runner-1") as value:
        th.assert_true(len(value["execution_id"]) == 32,
                       "each execution needs a fresh bounded correlation identity")


@th.unit_test("job execution context does not leak into a fresh worker thread")
def test_execution_context_thread_isolation(opts):
    from mojo.apps.jobs.execution_context import current, execution

    observed = []
    with execution("job-1", "pkg.func", 1, "default", "runner-1"):
        thread = threading.Thread(target=lambda: observed.append(current()))
        thread.start()
        thread.join(timeout=2)
    th.assert_eq(observed, [None], "a new worker thread must not inherit job attestation")
