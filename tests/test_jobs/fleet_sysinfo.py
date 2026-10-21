"""Legacy dispatch and checked diagnostics share no singleton client mutation."""
from types import SimpleNamespace
from testit import helpers as th


@th.django_unit_test()
def test_legacy_and_diagnostic_dispatch(opts):
    from mojo.apps.jobs import get_sysinfo
    calls = []
    class Client:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            calls.append("closed")
    class Manager:
        def broadcast_execute(self, path, **kwargs):
            calls.append((path, kwargs))
            return [{"legacy": True}]
        def execute_on_runner(self, runner, path, **kwargs):
            return {"runner_id": runner}
        def broadcast_execute_checked(self, path, data, **kwargs):
            calls.append((path, kwargs))
            return {"status": "verified", "results": []}
    manager = Manager()
    assert get_sysinfo(manager=manager) == [{"legacy": True}], "legacy get_sysinfo must retain list dispatch"
    assert get_sysinfo(runner_id="runner", manager=manager) == [{"runner_id": "runner"}], "targeted legacy dispatch must remain compatible"
    result = get_sysinfo(diagnostic=True, roster=[], channel="edge", manager=manager,
                         connect=lambda **kwargs: Client())
    assert result["status"] == "verified", "diagnostic mode must return the checked envelope"
    assert calls[-1] == "closed", "diagnostic client must always close"
    assert calls[-2][1]["deadline"] > 0, "checked dispatch must receive the absolute deadline"
    assert calls[-2][0].endswith("collect_diagnostics"), "diagnostics must use the fixed collector path"


@th.django_unit_test()
def test_checked_deadline_before_subscribe_publish_poll_cleanup(opts):
    import time
    from mojo.apps.jobs.manager import JobManager
    from mojo.apps.jobs.keys import JobKeys
    roster = [{"hostname": "node-one", "runner_id": "node-engine", "started": "2026-10-20T00:00:00+00:00",
               "channels": ["edge"], "capabilities": {"execute_checked": 2}}]
    # Real monotonic deadlines, exhausted in a chosen fake operation; no sleeps.
    for phase in ("subscribe", "publish", "poll", "cleanup"):
        state = {"expired": False, "closed": False, "published": 0}
        class Pubsub:
            def subscribe(self, channel):
                if phase == "subscribe":
                    state["expired"] = True
                    raise TimeoutError("subscribe budget")
            def get_message(self, **kwargs):
                state["expired"] = True
                raise TimeoutError("poll budget")
            def close(self):
                state["closed"] = True
                if phase == "cleanup":
                    raise TimeoutError("cleanup budget")
        class Transport:
            def pubsub(self):
                return Pubsub()
            def publish(self, *args):
                state["published"] += 1
                if phase in ("publish", "cleanup"):
                    raise TimeoutError("publish budget")
        manager = object.__new__(JobManager)
        manager.keys = JobKeys()
        manager.redis = SimpleNamespace()
        result = manager.broadcast_execute_checked("fixed.collector", channel="edge", roster=roster,
                                                    client=Transport(), deadline=time.monotonic() + 1)
        assert result["status"] != "verified", f"{phase} exhaustion cannot establish checked proof"
        assert state["closed"], f"{phase} exhaustion must always clean up the pubsub"
        if phase == "subscribe":
            assert state["published"] == 0, "failed subscription must prevent publication"
