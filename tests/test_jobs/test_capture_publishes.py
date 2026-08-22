"""capture_publishes concurrency + hook contracts (maestro item #2558).

The old wrap-based capture rebound mojo.apps.jobs.publish: import-time
binders never saw the wrapper, and two overlapping captures could restore
each other out of order and strand a stale wrapper process-wide. The
registry/_capture_router design must compose concurrent captures and leave
production publish untouched when no capture is active.
"""
import threading

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

FUNC_A = "testit_2558.capture_a"
FUNC_B = "testit_2558.capture_b"


@th.django_unit_test("two concurrent captures each see only their own jobs")
def test_concurrent_captures_are_scoped(opts):
    from mojo.apps import jobs

    results = {}
    barrier = threading.Barrier(2, timeout=10)

    def capture_one(func_name, tag):
        with th.capture_publishes(lambda c: c.get("func") == func_name) as calls:
            barrier.wait()  # both captures active simultaneously
            jobs.publish(func=func_name, payload={"tag": tag}, channel="cleanup")
            barrier.wait()  # neither exits before the other published
        results[tag] = list(calls)

    t1 = threading.Thread(target=capture_one, args=(FUNC_A, "a"))
    t2 = threading.Thread(target=capture_one, args=(FUNC_B, "b"))
    t1.start(); t2.start()
    t1.join(timeout=15); t2.join(timeout=15)

    assert_eq(len(results.get("a", [])), 1,
              "capture A must record exactly its own publish")
    assert_eq(len(results.get("b", [])), 1,
              "capture B must record exactly its own publish")
    assert_eq(results["a"][0]["func"], FUNC_A,
              "capture A must not have absorbed capture B's job")
    assert_eq(results["b"][0]["func"], FUNC_B,
              "capture B must not have absorbed capture A's job")


@th.django_unit_test("the router uninstalls when the last capture exits")
def test_router_uninstalls_cleanly(opts):
    from mojo.apps import jobs

    with th.capture_publishes(lambda c: c.get("func") == FUNC_A):
        with th.capture_publishes(lambda c: c.get("func") == FUNC_B):
            assert_true(jobs._capture_router is not None,
                        "the router must be installed while captures are active")
        assert_true(jobs._capture_router is not None,
                    "the outer capture must keep the router installed when the "
                    "inner one exits")
    assert_true(jobs._capture_router is None,
                "the router must uninstall when the last capture exits — a "
                "stranded router is the stale-wrapper bug in new clothes")


@th.django_unit_test("a captured publish returns the capture result and queues nothing")
def test_capture_result_and_side_effect(opts):
    from mojo.apps import jobs

    with th.capture_publishes(lambda c: c.get("func") == FUNC_A,
                              result="job-fixed") as calls:
        job_id = jobs.publish(func=FUNC_A, payload={}, channel="cleanup")
    assert_eq(job_id, "job-fixed", "a matching publish must return the capture result")
    assert_eq(len(calls), 1, "the matching publish must be recorded")
    assert_eq(calls[0]["channel"], "cleanup",
              "the captured call dict must carry the publish kwargs")

    boom = RuntimeError("no runners")
    raised = None
    with th.capture_publishes(lambda c: c.get("func") == FUNC_A,
                              side_effect=boom):
        try:
            jobs.publish(func=FUNC_A, payload={}, channel="cleanup")
        except RuntimeError as err:
            raised = err
    assert_true(raised is boom,
                "side_effect must raise for matching publishes — dispatch-"
                "failure paths depend on it")
