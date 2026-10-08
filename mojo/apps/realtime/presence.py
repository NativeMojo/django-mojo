"""
Presence bookkeeping for the per-identity connection cap (#4567).

An identity's online set (`realtime:online:<type>:<id>`) holds one member per
connection. A member is alive while its `realtime:connections:<id>` record
exists; the record expires on its own, the set is kept alive by every sibling.
So the cap counts members that still have a record, and the rest are removed
at admission, on every live sibling's heartbeat and at cleanup.

Plain functions taking a Redis client, so the handler runs them in its
executor and tests can pass their own client.
"""

CONNECTION_KEY_PREFIX = "realtime:connections:"

# One key, so it is legal on clustered Redis. Already a member: admitted (the
# add is idempotent). At or over the maximum: refused. A maximum <= 0 disables
# the cap and just adds.
ADMIT_LUA = """
if redis.call('SISMEMBER', KEYS[1], ARGV[1]) == 0 then
    local maximum = tonumber(ARGV[2])
    if maximum > 0 and redis.call('SCARD', KEYS[1]) >= maximum then
        return 0
    end
    redis.call('SADD', KEYS[1], ARGV[1])
end
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
return 1
"""


def _text(value):
    return value.decode() if isinstance(value, (bytes, bytearray)) else value


def prune(redis, online_key, keep=None):
    """Remove the members of `online_key` that have no connection record and
    return how many members are left. `keep` is never removed.

    Not atomic, and it does not need to be: it only removes an id with no
    record, and a live connection wrongly removed (a lagging replica read)
    adds itself back on its next heartbeat. A key that is not a set is left
    untouched and counts as 0.
    """
    key_type = _text(redis.type(online_key))
    if key_type != "set":
        return 0
    members = [_text(member) for member in redis.smembers(online_key)]
    if not members:
        return 0
    pipe = redis.pipeline(transaction=False)
    for member in members:
        pipe.exists(f"{CONNECTION_KEY_PREFIX}{member}")
    alive = pipe.execute()
    stale = [member for member, exists in zip(members, alive)
             if not exists and member != keep]
    if stale:
        redis.srem(online_key, *stale)
    return len(members) - len(stale)


def admit(redis, online_key, connection_id, maximum, ttl):
    """Atomically add `connection_id` to `online_key` unless the set already
    holds `maximum` members. Returns True when the connection is a member."""
    allowed = redis.eval(ADMIT_LUA, 1, online_key, connection_id, str(int(maximum)), str(int(ttl)))
    return int(allowed) == 1
