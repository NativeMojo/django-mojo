"""Capability-negotiated checked execution protocol (Security #2685)."""

import json

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


def _runner(runner_id, host, compatible=True):
    row = {
        "runner_id": runner_id,
        "hostname": host,
        "channels": ["default"],
        "alive": True,
    }
    if compatible:
        row["capabilities"] = {"execute_checked": 1}
    return row


def _reply(correlation, runner_id, host, result=None, status="success"):
    row = {
        "schema": "mojo.jobs.execute-checked-reply",
        "version": 1,
        "correlation_id": correlation,
        "runner_id": runner_id,
        "hostname": host,
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
