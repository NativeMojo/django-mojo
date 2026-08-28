"""
Pub/Sub channel naming with an opt-in isolation prefix.

REDIS_PUBSUB_PREFIX namespaces every framework Pub/Sub CHANNEL (jobs
runner control/broadcast/replies/ping, realtime broadcast/topic/messages)
so cooperating environments sharing one Redis server do not hear each
other. Redis Pub/Sub ignores logical database numbers, so the per-checkout
db index that isolates stored keys does nothing for messages — the prefix
closes that gap. Default is "" and the channel names are then byte-identical
to the legacy literals.

The setting is file-static (settings.get_static): channel naming cannot
depend on a Redis-backed settings store, for the same circularity reason as
the connection settings in client.py. It is cooperative segregation for
test checkouts — bin/create_testproject derives a per-checkout value — not
a security boundary, and no substitute for Redis ACLs.

Storage keys are deliberately NOT routed through here; they are isolated by
the per-checkout database index already.
"""
from mojo.helpers.settings import settings


def isolation_prefix():
    """The process-wide Pub/Sub isolation prefix ("" when not configured)."""
    return settings.get_static("REDIS_PUBSUB_PREFIX", "") or ""


def channel_name(name, prefix=None):
    """Build a Pub/Sub channel name.

    prefix=None means "use the configured isolation prefix"; pass "" to
    build the bare legacy name explicitly (tests inject prefixes here).
    """
    if prefix is None:
        prefix = isolation_prefix()
    if prefix:
        return f"{prefix}:{name}"
    return name
