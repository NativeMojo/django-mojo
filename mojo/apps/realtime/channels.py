"""
Realtime Pub/Sub channel names, with the opt-in isolation prefix.

Every realtime CHANNEL (broadcast, per-topic, per-connection messages) is
built here so REDIS_PUBSUB_PREFIX namespaces all of them consistently on
publish, subscribe, and unsubscribe — see mojo/helpers/redis/channels.py.
Storage keys (realtime:online:*, the realtime:topic:* member sets,
realtime:connections:*, realtime:response:*, realtime:waiter*) deliberately
do NOT come through here: they are isolated by the per-checkout Redis
database index. Client-visible topic names and payloads are unaffected —
the prefix exists only on the Redis wire.
"""
def _channel_name(name, prefix):
    # Imported lazily: realtime.manager is imported by realtime/__init__
    # before Django/paths are configured (the asgi entrypoint), and pulling
    # the mojo.helpers.redis package at module import time boots logit too
    # early. Same reason manager.get_redis() defers its import.
    from mojo.helpers.redis.channels import channel_name
    return channel_name(name, prefix)


def broadcast_channel(prefix=None):
    """The all-connections broadcast channel."""
    return _channel_name("realtime:broadcast", prefix)


def topic_channel(topic, prefix=None):
    """The channel for one logical topic (topic stays client-visible as-is)."""
    return _channel_name(f"realtime:topic:{topic}", prefix)


def messages_channel(connection_id, prefix=None):
    """The direct-message channel for one connection."""
    return _channel_name(f"realtime:messages:{connection_id}", prefix)
