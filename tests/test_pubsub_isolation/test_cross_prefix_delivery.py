"""Cross-prefix non-delivery on one Redis server, including across database
indexes — the mechanism this feature relies on, proven with owned raw
connections. Prefixes are made-up uuid-suffixed values no real checkout
derives; the cross-index client only publishes on those channels and never
touches stored keys, so no other checkout's data or traffic is reachable."""
import time
import uuid

from testit import helpers as th


def _drain(pubsub, seconds=0.3):
    end = time.time() + seconds
    while time.time() < end:
        pubsub.get_message(timeout=0.05)


def _collect(pubsub, seconds=1.0):
    end = time.time() + seconds
    out = []
    while time.time() < end:
        msg = pubsub.get_message(timeout=0.05)
        if msg and msg.get("type") == "message":
            out.append(msg)
    return out


@th.django_unit_test()
def test_cross_prefix_non_delivery_same_topic(opts):
    from mojo.helpers.redis import get_connection
    from mojo.apps.realtime.channels import topic_channel

    prefix_a = f"tiso_{uuid.uuid4().hex[:8]}"
    prefix_b = f"tiso_{uuid.uuid4().hex[:8]}"
    topic = "orders:42"  # identical logical topic in both environments

    conn = get_connection()
    pubsub = conn.pubsub()
    pubsub.subscribe(topic_channel(topic, prefix=prefix_a))
    _drain(pubsub)

    try:
        conn.publish(topic_channel(topic, prefix=prefix_b), '{"env": "b"}')
        stray = _collect(pubsub, seconds=0.8)
        assert not stray, \
            f"environment A received environment B's publish on the same logical topic: {stray}"

        conn.publish(topic_channel(topic, prefix=prefix_a), '{"env": "a"}')
        own = _collect(pubsub, seconds=1.5)
        assert len(own) == 1, \
            f"same-prefix delivery broken (positive control): got {len(own)} messages"
    finally:
        pubsub.close()


@th.django_unit_test()
def test_pubsub_crosses_db_indexes_and_only_the_prefix_isolates(opts):
    import redis as redis_lib
    from mojo.helpers.redis import get_connection
    from mojo.apps.realtime.channels import topic_channel

    conn = get_connection()
    kwargs = conn.connection_pool.connection_kwargs
    own_db = kwargs.get("db", 0)
    other_db = (own_db + 1) % 16
    # Publish-only client on a DIFFERENT database index: Pub/Sub writes no
    # keys, so this touches no other checkout's data.
    other = redis_lib.Redis(
        host=kwargs.get("host", "localhost"),
        port=kwargs.get("port", 6379),
        db=other_db,
    )

    prefix_a = f"tiso_{uuid.uuid4().hex[:8]}"
    prefix_b = f"tiso_{uuid.uuid4().hex[:8]}"
    topic = "audit:7"

    pubsub = conn.pubsub()
    pubsub.subscribe(topic_channel(topic, prefix=prefix_a))
    _drain(pubsub)

    try:
        # The premise: a different database index alone does NOT isolate
        # Pub/Sub — the same channel name crosses indexes.
        other.publish(topic_channel(topic, prefix=prefix_a), '{"across": "indexes"}')
        crossed = _collect(pubsub, seconds=1.5)
        assert len(crossed) == 1, \
            "expected Pub/Sub to ignore database numbers (same name, different db) — " \
            f"got {len(crossed)} messages; the isolation premise changed"

        # The fix: differing prefixes DO isolate, database indexes regardless.
        other.publish(topic_channel(topic, prefix=prefix_b), '{"env": "b"}')
        stray = _collect(pubsub, seconds=0.8)
        assert not stray, \
            f"prefix isolation failed across database indexes: {stray}"
    finally:
        pubsub.close()
        other.close()
