"""The regression: every production publish path must land on the
active-prefix channel and NOTHING may land on the legacy literal.

Deliberately imports none of the new channel builders — expected names are
constructed inline from the documented contract ({prefix}:{legacy_name}),
so this file runs unmodified against pre-feature code and fails there:
old publishers hit the legacy names, the prefixed subscribers stay silent.
"""
import json
import time
import uuid

from testit import helpers as th

# Harmless if some same-prefix engine ever executes a stray broadcast:
# returns a timestamp, mutates nothing.
NOOP_FUNC = "mojo.helpers.dates.utcnow"


def _active_prefix():
    from mojo.helpers.settings import settings
    return settings.get_static("REDIS_PUBSUB_PREFIX", "") or ""


def _subscribe(channel):
    from mojo.helpers.redis import get_connection
    pubsub = get_connection().pubsub()
    pubsub.subscribe(channel)
    end = time.time() + 0.3
    while time.time() < end:
        pubsub.get_message(timeout=0.05)
    return pubsub


def _collect(pubsub, seconds=1.0):
    end = time.time() + seconds
    out = []
    while time.time() < end:
        msg = pubsub.get_message(timeout=0.05)
        if msg and msg.get("type") == "message":
            out.append(msg)
    return out


@th.django_unit_test()
def test_active_prefix_is_configured(opts):
    prefix = _active_prefix()
    assert prefix, (
        "REDIS_PUBSUB_PREFIX is empty in the test environment — the "
        "testproject predates Pub/Sub isolation; run bin/create_testproject"
    )


@th.django_unit_test()
def test_realtime_publishes_land_on_prefixed_channels_only(opts):
    from mojo.apps.realtime import manager as rt

    prefix = _active_prefix()
    assert prefix, "REDIS_PUBSUB_PREFIX must be set for this test (see create_testproject)"

    # broadcast
    prefixed = _subscribe(f"{prefix}:realtime:broadcast")
    legacy = _subscribe("realtime:broadcast")
    try:
        rt.broadcast({"title": "iso-check"})
        got = _collect(prefixed, seconds=1.5)
        assert len(got) == 1, f"prefixed broadcast channel got {len(got)} messages, wanted 1"
        payload = json.loads(got[0]["data"])
        assert payload.get("data", {}).get("title") == "iso-check", \
            f"unexpected broadcast payload: {payload}"
        stray = _collect(legacy, seconds=0.8)
        assert not stray, f"legacy realtime:broadcast still receives traffic: {stray}"
    finally:
        prefixed.close()
        legacy.close()

    # topic
    topic = f"iso-topic-{uuid.uuid4().hex[:8]}"
    prefixed = _subscribe(f"{prefix}:realtime:topic:{topic}")
    legacy = _subscribe(f"realtime:topic:{topic}")
    try:
        rt.publish_topic(topic, {"n": 1})
        got = _collect(prefixed, seconds=1.5)
        assert len(got) == 1, f"prefixed topic channel got {len(got)} messages, wanted 1"
        payload = json.loads(got[0]["data"])
        assert payload.get("topic") == topic, \
            f"client-visible topic changed on the wire payload: {payload}"
        stray = _collect(legacy, seconds=0.8)
        assert not stray, f"legacy topic channel still receives traffic: {stray}"
    finally:
        prefixed.close()
        legacy.close()

    # direct message
    conn_id = f"iso-conn-{uuid.uuid4().hex[:8]}"
    prefixed = _subscribe(f"{prefix}:realtime:messages:{conn_id}")
    legacy = _subscribe(f"realtime:messages:{conn_id}")
    try:
        rt.send_to_connection(conn_id, {"n": 2})
        got = _collect(prefixed, seconds=1.5)
        assert len(got) == 1, f"prefixed messages channel got {len(got)} messages, wanted 1"
        stray = _collect(legacy, seconds=0.8)
        assert not stray, f"legacy messages channel still receives traffic: {stray}"
    finally:
        prefixed.close()
        legacy.close()


@th.django_unit_test()
def test_jobs_control_lands_on_prefixed_channels_only(opts):
    from mojo.apps.jobs.manager import JobManager

    prefix = _active_prefix()
    assert prefix, "REDIS_PUBSUB_PREFIX must be set for this test (see create_testproject)"
    manager = JobManager()

    # Global broadcast: fire-and-forget execute.
    prefixed = _subscribe(f"{prefix}:mojo:jobs:runners:broadcast")
    legacy = _subscribe("mojo:jobs:runners:broadcast")
    try:
        manager.broadcast_execute(NOOP_FUNC, collect_replies=False)
        got = _collect(prefixed, seconds=1.5)
        assert len(got) == 1, f"prefixed broadcast got {len(got)} messages, wanted 1"
        message = json.loads(got[0]["data"])
        assert message.get("func") == NOOP_FUNC, f"unexpected broadcast message: {message}"

        # Reply channels are minted under the prefix too.
        manager.broadcast_command("status", timeout=0.3, expected_runners=1)
        got = _collect(prefixed, seconds=1.5)
        assert len(got) == 1, f"prefixed broadcast got {len(got)} command messages, wanted 1"
        message = json.loads(got[0]["data"])
        reply_channel = message.get("reply_channel", "")
        assert reply_channel.startswith(f"{prefix}:mojo:jobs:replies:"), \
            f"reply channel minted outside the prefix: {reply_channel}"

        stray = _collect(legacy, seconds=0.8)
        assert not stray, f"legacy jobs broadcast channel still receives traffic: {stray}"
    finally:
        prefixed.close()
        legacy.close()

    # Individual runner control + ping reply root.
    runner_id = f"iso-runner-{uuid.uuid4().hex[:8]}"
    prefixed = _subscribe(f"{prefix}:mojo:jobs:runner:{runner_id}:ctl")
    legacy = _subscribe(f"mojo:jobs:runner:{runner_id}:ctl")
    try:
        manager.execute_on_runner(runner_id, NOOP_FUNC, wait_for_reply=False)
        got = _collect(prefixed, seconds=1.5)
        assert len(got) == 1, f"prefixed runner ctl got {len(got)} messages, wanted 1"

        manager.ping(runner_id, timeout=0.3)
        got = _collect(prefixed, seconds=1.5)
        assert len(got) == 1, f"prefixed runner ctl got {len(got)} ping messages, wanted 1"
        message = json.loads(got[0]["data"])
        reply_channel = message.get("reply_channel", "")
        assert reply_channel.startswith(f"{prefix}:mojo:jobs:ping:"), \
            f"ping reply channel minted outside the prefix: {reply_channel}"

        stray = _collect(legacy, seconds=0.8)
        assert not stray, f"legacy runner ctl channel still receives traffic: {stray}"
    finally:
        prefixed.close()
        legacy.close()
