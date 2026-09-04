"""Capability-negotiated checked execution protocol (Security #2685)."""

import json
from unittest import mock

from testit import helpers as th


class _PubSub:
    def __init__(self, messages):
        self.messages = list(messages)
        self.subscribed = []
        self.closed = False

    def subscribe(self, channel):
        self.subscribed.append(channel)

    def get_message(self, timeout=None):
        if self.messages:
            return self.messages.pop(0)
        return None

    def close(self):
        self.closed = True


class _Redis:
    def __init__(self, messages=()):
        self.messages = list(messages)
        self.published = []
        self.pubsub_value = None

    def pubsub(self):
        self.pubsub_value = _PubSub(self.messages)
        return self.pubsub_value

    def publish(self, channel, payload):
        self.published.append((channel, json.loads(payload)))
        return 1


def _manager(rows, replies=()):
    from mojo.apps.jobs.keys import JobKeys
    from mojo.apps.jobs.manager import JobManager

    manager = JobManager.__new__(JobManager)
    manager.redis = _Redis(replies)
    manager.keys = JobKeys(pubsub_prefix="")
    manager.get_runners_bounded = lambda *args, **kwargs: rows
    return manager


def _runner(runner_id, host, compatible=True, started="2026-09-04T12:00:00+00:00"):
    row = {
        "runner_id": runner_id,
        "hostname": host,
        "channels": ["default"],
        "alive": True,
        "started": started,
    }
    if compatible:
        row["capabilities"] = {"execute_checked": 2}
    return row


def _reply(correlation, runner_id, host, result=None, status="success",
           started="2026-09-04T12:00:00+00:00"):
    row = {
        "schema": "mojo.jobs.execute-checked-reply",
        "version": 2,
        "correlation_id": correlation,
        "runner_id": runner_id,
        "hostname": host,
        "started": started,
        "func": "example.checked",
        "status": status,
    }
    if status == "success":
        row["result"] = result or {"ok": True}
    else:
        row["error"] = "execution_failed"
    return {"type": "message", "data": json.dumps(row)}


@th.django_unit_test("checked execution chooses one compatible runner per host")
def test_checked_selects_one_runner_per_host(opts):
    correlation = "0123456789abcdef" * 2
    rows = [
        _runner("z-runner", "WEB-1"),
        _runner("a-runner", "web-1"),
        _runner("old-runner", "web-1", compatible=False),
        _runner("b-runner", "web-2"),
    ]
    replies = [
        _reply(correlation, "a-runner", "web-1"),
        _reply(correlation, "b-runner", "web-2"),
    ]
    manager = _manager(rows, replies)

    result = manager.broadcast_execute_checked(
        "example.checked", {}, timeout=0.1, channel="default",
        correlation_id=correlation)

    assert result["status"] == "verified", (
        f"one valid reply per exact host should verify, got {result!r}")
    targets = [channel for channel, unused in manager.redis.published]
    assert targets == [
        manager.keys.runner_ctl("a-runner"),
        manager.keys.runner_ctl("b-runner"),
    ], f"checked execution targeted the wrong runner set: {targets!r}"
    payloads = [payload for unused, payload in manager.redis.published]
    assert [payload["target"] for payload in payloads] == [
        {"runner_id": "a-runner", "hostname": "web-1",
         "started": "2026-09-04T12:00:00+00:00"},
        {"runner_id": "b-runner", "hostname": "web-2",
         "started": "2026-09-04T12:00:00+00:00"},
    ], "checked dispatch was not bound to the heartbeat incarnation"
    assert result["expected_roster"] == [
        {"host": "web-1", "started": "2026-09-04T12:00:00+00:00"},
        {"host": "web-2", "started": "2026-09-04T12:00:00+00:00"},
    ]


@th.django_unit_test("a reply from a restarted selected runner is rejected")
def test_checked_reply_requires_selected_incarnation(opts):
    correlation = "0fedcba987654321" * 2
    manager = _manager(
        [_runner("runner-1", "web-1", started="2026-09-04T12:00:00+00:00")],
        [_reply(correlation, "runner-1", "web-1",
                started="2026-09-04T12:01:00+00:00")])

    result = manager.broadcast_execute_checked(
        "example.checked", {}, timeout=0.02, channel="default",
        correlation_id=correlation)

    assert result["status"] == "partial", result
    assert result["missing_hosts"] == ["web-1"], result
    assert result["anomalies"] == ["identity_mismatch"], result


@th.django_unit_test("a restarted engine ignores commands for its prior incarnation")
def test_checked_engine_requires_current_target_incarnation(opts):
    from mojo.apps.jobs.job_engine import JobEngine, host_channel
    from mojo.apps.jobs.keys import JobKeys

    engine = JobEngine.__new__(JobEngine)
    engine.runner_id = "runner-1"
    engine.channels = ["default"]
    engine.keys = JobKeys(pubsub_prefix="")
    engine.redis = _Redis()
    engine.start_time = mock.Mock()
    engine.start_time.isoformat.return_value = "2026-09-04T12:01:00+00:00"
    message = {
        "protocol": 2,
        "correlation_id": "abcdef0123456789" * 2,
        "reply_channel": engine.keys.reply_channel("abcdef0123456789" * 2),
        "func": "example.checked",
        "channel": "default",
        "data": {},
        "target": {
            "runner_id": "runner-1", "hostname": host_channel(),
            "started": "2026-09-04T12:00:00+00:00",
        },
    }
    engine._handle_checked_execute(
        message, engine.keys.runner_ctl(engine.runner_id))
    assert engine.redis.published == [], \
        "a pre-restart command executed or replied from the new incarnation"


@th.django_unit_test("an incompatible host refuses before mutation")
def test_checked_mixed_versions_refuse_before_publish(opts):
    manager = _manager([
        _runner("new", "web-1"),
        _runner("old", "web-2", compatible=False),
    ])

    result = manager.broadcast_execute_checked(
        "example.checked", {}, timeout=0.1, channel="default",
        correlation_id="1234567890abcdef" * 2)

    assert result["status"] == "unknown", (
        f"a host with no compatible runner must refuse, got {result!r}")
    assert result["missing_hosts"] == ["web-1", "web-2"], (
        f"no host was dispatched, so every expected host is missing: {result!r}")
    assert manager.redis.published == [], (
        f"mixed-version refusal published mutations: {manager.redis.published!r}")


@th.django_unit_test("missing and anomalous replies poison verification")
def test_checked_anomaly_is_partial(opts):
    correlation = "fedcba0987654321" * 2
    manager = _manager(
        [_runner("one", "web-1"), _runner("two", "web-2")],
        [
            _reply(correlation, "one", "web-1"),
            _reply("wrong" * 8, "two", "web-2"),
        ])

    result = manager.broadcast_execute_checked(
        "example.checked", {}, timeout=0.02, channel="default",
        correlation_id=correlation)

    assert result["status"] == "partial", (
        f"identity anomalies and a missing host must be partial: {result!r}")
    assert result["missing_hosts"] == ["web-2"], (
        f"missing host evidence was lost: {result!r}")
    assert result["anomalies"] == ["identity_mismatch"], (
        f"bounded anomaly evidence was not preserved: {result!r}")


@th.django_unit_test("checked execution requires a concrete channel and strong correlation")
def test_checked_rejects_ambiguous_identity(opts):
    manager = _manager([_runner("one", "web-1")])

    no_channel = manager.broadcast_execute_checked(
        "example.checked", {}, timeout=0.1, channel=None)
    weak_id = manager.broadcast_execute_checked(
        "example.checked", {}, timeout=0.1, channel="default",
        correlation_id="short")

    assert no_channel["status"] == "unknown", (
        f"missing channel should be unknown, got {no_channel!r}")
    assert weak_id["status"] == "unknown", (
        f"weak correlation should be unknown, got {weak_id!r}")
    assert manager.redis.published == [], (
        f"invalid checked calls published work: {manager.redis.published!r}")


@th.django_unit_test("checked wire rejects non-finite and unsafe bounded identity")
def test_checked_rejects_nonfinite_payload_and_unsafe_channel(opts):
    manager = _manager([_runner("one", "web-1")])

    nonfinite = manager.broadcast_execute_checked(
        "example.checked", {"value": float("nan")}, timeout=0.1,
        channel="default", correlation_id="89abcdef01234567" * 2)
    unsafe_channel = manager.broadcast_execute_checked(
        "example.checked", {}, timeout=0.1, channel="bad:channel",
        correlation_id="76543210fedcba98" * 2)

    assert nonfinite["status"] == "unknown", nonfinite
    assert unsafe_channel["status"] == "unknown", unsafe_channel
    assert manager.redis.published == [], \
        "invalid checked wire values reached a runner control channel"


@th.django_unit_test("falsey non-object payloads remain invalid")
def test_checked_rejects_empty_array_payload(opts):
    manager = _manager([_runner("one", "web-1")])

    result = manager.broadcast_execute_checked(
        "example.checked", [], timeout=0.1, channel="default",
        correlation_id="abcdef0123456789" * 2)

    assert result["status"] == "unknown", result
    assert result["anomalies"] == ["payload_must_be_object"], result
    assert manager.redis.published == []
