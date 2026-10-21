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


@th.django_unit_test()
def test_deadline_client_bounds_every_phase_and_closes_locally(opts):
    from mojo.helpers.redis.client import _DeadlineClient
    for phase in ("subscribe", "publish", "get_message", "execute", "cleanup"):
        clock, calls, socket_limits = [0], [], []
        connection = SimpleNamespace(socket_timeout=60, socket_connect_timeout=60,
                                     _sock=SimpleNamespace(settimeout=socket_limits.append))
        class Pool:
            connection_kwargs = {}
            _available_connections = [connection]
            _in_use_connections = []
            def disconnect(self):
                calls.append("disconnect")
        class Transport:
            connection_pool = Pool()
            def pubsub(self):
                return self
            def pipeline(self, **kwargs):
                return self
            def close(self):
                calls.append("close")
            def __getattr__(self, name):
                def operation(*args, **kwargs):
                    calls.append(name)
                    if name == phase:
                        clock[0] = 10
                    return None
                return operation
        target = Transport()
        bounded = _DeadlineClient(target, 10, clock=lambda: clock[0], max_timeout=1)
        pubsub = bounded.pubsub()
        pipeline = bounded.pipeline(transaction=False)
        assert isinstance(pubsub, _DeadlineClient) and isinstance(pipeline, _DeadlineClient), "pubsub and pipeline must retain the same absolute deadline"
        try:
            if phase == "cleanup":
                clock[0] = 10
            elif phase == "execute":
                pipeline.execute()
            elif phase == "publish":
                bounded.publish("channel", "message")
            else:
                getattr(pubsub, phase)("channel") if phase == "subscribe" else pubsub.get_message(timeout=5)
        except TimeoutError:
            pass
        else:
            assert phase == "cleanup", f"a late {phase} result must be rejected"
        before = len(calls)
        try:
            bounded.publish("after", "expiry")
        except TimeoutError:
            pass
        else:
            raise AssertionError(f"{phase} expiry must prevent subsequent network operations")
        assert len(calls) == before, f"{phase} expiry cannot start another network call"
        assert connection.socket_timeout <= 1 and connection.socket_connect_timeout <= 1, "remaining socket/connect limits must replace the inherited 60s timeout"
        assert socket_limits and all(value <= 1 for value in socket_limits), "live pubsub sockets must receive remaining-budget limits"
        bounded.close()
        assert calls[-2:] == ["close", "disconnect"], "cleanup after expiry must disconnect locally without another network operation"
