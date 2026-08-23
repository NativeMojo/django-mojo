"""Startup firewall recovery (item #2716).

A node reconciles its own kernel firewall because its engine started, not
because a broadcast happened to arrive while it was listening — fan-out
resolves the roster at publish time, so a broadcast sent while this node's
engine was restarting quietly skips it.

The hook publishes rather than reconciling inline: every firewall write goes
through the root-owned broker, which refuses outside a JobEngine execution
context, and a startup hook has none.
"""
from unittest import mock

from testit import helpers as th

SYNC_JOB = "mojo.apps.incident.asyncjobs.sync_firewall"


def _engine(runner_id="test-node-engine", channels=None):
    if channels is None:
        channels = ["default", runner_id]
    return mock.Mock(runner_id=runner_id, channels=channels)


def _mock_redis():
    store = {}
    r = mock.MagicMock()
    r.store = store
    r.get = lambda key: store.get(key)
    r.set = lambda key, val, **kwargs: store.__setitem__(key, val) or True
    r.delete = lambda *keys: sum(store.pop(k, None) is not None for k in keys)
    return r


@th.django_unit_test("the startup hook queues a forced reconcile on this engine")
def test_startup_hook_publishes_box_direct_force_sync(opts):
    from mojo.apps.incident import asyncjobs

    redis_client = _mock_redis()
    _, force_key, _ = asyncjobs._sync_firewall_keys()

    with mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=redis_client), \
         th.capture_publishes(lambda c: c.get("func") == SYNC_JOB) as calls:
        result = asyncjobs.on_engine_start(_engine())

    assert len(calls) == 1, f"expected exactly one startup publish, got {calls}"
    assert calls[0].get("channel") == "test-node-engine", \
        f"recovery must be addressed to this engine only, got {calls[0]}"
    assert calls[0].get("payload") == {"force": True}, \
        f"an unforced startup reconcile would skip on the surviving marker: {calls[0]}"
    assert not calls[0].get("broadcast"), \
        f"the startup hook must publish locally, never fan out: {calls[0]}"
    assert redis_client.get(force_key) == "1", \
        "the force flag must be set before publishing so a lost job still converges"
    assert "queued" in result, f"the hook should report what it queued, got {result!r}"


@th.django_unit_test("the startup hook skips when no engine consumes the box channel")
def test_startup_hook_skips_without_box_direct_channel(opts):
    """With JOBS_HOSTNAME_CHANNEL off the job would strand on a channel
    nobody reads; say so loudly instead of queueing into the void."""
    from mojo.apps.incident import asyncjobs

    redis_client = _mock_redis()

    with mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=redis_client), \
         th.capture_publishes(lambda c: c.get("func") == SYNC_JOB) as calls:
        result = asyncjobs.on_engine_start(
            _engine(channels=["default"]))

    assert calls == [], f"nothing should be published into an unconsumed channel: {calls}"
    assert redis_client.store == {}, \
        f"no force flag should be left behind for a reconcile that cannot run: {redis_client.store}"
    assert "skipped" in result, f"the hook should report the skip, got {result!r}"
