"""#8352: count_online counts many ids with one multi-key EXISTS per chunk.

These tests patch nothing: the default tier forbids patching production code,
so the chunking, error and round-trip checks drive `_count_existing` with a
client built here, and the semantics checks seed this module's own presence
keys (user type "t8352") in the checkout's Redis. The checks that need
`get_redis` replaced (empty input touches no Redis, one call per chunk end to
end) live in tests/test_realtime_extended_serial/count_online_cluster.py.
"""
from testit import helpers as th

USER_TYPE = "t8352"


def _redis():
    from mojo.helpers.redis import get_connection
    return get_connection()


def _key(uid):
    return f"realtime:online:{USER_TYPE}:{uid}"


def _clear(*uids):
    _redis().delete(*[_key(uid) for uid in uids])


class _RecordingClient:
    """Records each exists() call's keys; answers from `replies` in order (an
    exception instance is raised), else from `inner`, else len(keys)."""

    def __init__(self, inner=None, replies=None):
        self.inner = inner
        self.replies = list(replies or [])
        self.calls = []

    def exists(self, *keys):
        self.calls.append(keys)
        if self.replies:
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        if self.inner is not None:
            return self.inner.exists(*keys)
        return len(keys)

    def __getattr__(self, name):
        if name in ("keys", "scan", "scan_iter"):
            raise AssertionError(f"count_online must not call {name}")
        raise AttributeError(name)


@th.django_unit_test("count_online: exported from mojo.apps.realtime like is_online")
def test_count_online_exported(opts):
    import mojo.apps.realtime as realtime
    from mojo.apps.realtime import manager

    assert realtime.count_online is manager.count_online
    assert realtime.is_online is manager.is_online


@th.django_unit_test("count_online: empty input returns 0")
def test_count_online_empty(opts):
    from mojo.apps.realtime import count_online

    assert count_online(USER_TYPE, []) == 0
    assert count_online(USER_TYPE, ()) == 0
    assert count_online(USER_TYPE, iter([])) == 0


@th.django_unit_test("count_online: chunk_size must be a non-bool int >= 1, checked before empty input")
def test_count_online_chunk_size_validation(opts):
    from mojo.apps.realtime import count_online

    for bad in (0, -1, True, False, 2.5, "10", None):
        for ids in ([], [1, 2]):
            try:
                count_online(USER_TYPE, ids, chunk_size=bad)
            except ValueError:
                continue
            raise AssertionError(f"chunk_size={bad!r} with ids={ids} must raise ValueError")


@th.django_unit_test("count_online: a bare str or bytes is refused, not split into characters")
def test_count_online_refuses_bare_string(opts):
    from mojo.apps.realtime import count_online

    for bad in ("123", b"123", ""):
        try:
            count_online(USER_TYPE, bad)
        except TypeError:
            continue
        raise AssertionError(f"user_ids={bad!r} must raise TypeError")


@th.django_unit_test("count_online: string and set presence keys count, as is_online sees them")
def test_count_online_matches_is_online(opts):
    from mojo.apps.realtime import count_online, is_online

    redis = _redis()
    _clear(8352001, 8352002, 8352003)
    try:
        # Legacy JSON string form and the current set form.
        redis.set(_key(8352001), '{"connection_ids": ["c1"]}')
        redis.sadd(_key(8352002), "c2")
        ids = [8352001, 8352002, 8352003]
        assert count_online(USER_TYPE, ids) == 2
        assert [is_online(USER_TYPE, uid) for uid in ids] == [True, True, False]
        assert count_online(USER_TYPE, [8352003]) == 0
    finally:
        _clear(8352001, 8352002, 8352003)


@th.django_unit_test("count_online: 5 and '5' are one id; repeats count once")
def test_count_online_dedups_mixed_ids(opts):
    from mojo.apps.realtime import count_online

    redis = _redis()
    _clear(5, 6)
    try:
        redis.sadd(_key(5), "c5")
        # Undeduplicated, EXISTS would count key 5 three times.
        assert count_online(USER_TYPE, [5, "5", 5, "6"]) == 1
        assert count_online(USER_TYPE, [5, "5", 5, "6"], chunk_size=1) == 1
    finally:
        _clear(5, 6)


@th.django_unit_test("count_online: one EXISTS per chunk, chunk+1 boundary")
def test_count_existing_chunks(opts):
    from mojo.apps.realtime.manager import _count_existing

    def sizes(count, chunk_size):
        client = _RecordingClient()
        keys = [_key(i) for i in range(count)]
        assert _count_existing(client, keys, chunk_size) == count
        return [len(call) for call in client.calls]

    assert sizes(3, 3) == [3]
    assert sizes(4, 3) == [3, 1]
    assert sizes(1000, 1000) == [1000]
    assert sizes(1001, 1000) == [1000, 1]
    assert sizes(10000, 1000) == [1000] * 10


@th.django_unit_test("count_online: a Redis error in a later chunk propagates, no partial sum")
def test_count_existing_error_propagates(opts):
    import redis as redis_lib
    from mojo.apps.realtime.manager import _count_existing

    client = _RecordingClient(replies=[3, redis_lib.exceptions.ConnectionError("chunk 2 down")])
    keys = [_key(i) for i in range(6)]
    try:
        result = _count_existing(client, keys, 3)
    except redis_lib.exceptions.ConnectionError:
        pass
    else:
        raise AssertionError(f"the error must reach the caller, got {result!r}")
    assert len(client.calls) == 2


@th.django_unit_test("count_online: ceil(unique/chunk) EXISTS calls on the real connection, no KEYS or SCAN")
def test_count_existing_round_trips_real_redis(opts):
    from mojo.apps.realtime.manager import _count_existing

    uids = list(range(8352100, 8352125))
    online = uids[::3]
    redis = _redis()
    _clear(*uids)
    try:
        for uid in online:
            redis.sadd(_key(uid), f"c{uid}")
        client = _RecordingClient(inner=redis)
        assert _count_existing(client, [_key(uid) for uid in uids], 10) == len(online)
        assert [len(call) for call in client.calls] == [10, 10, 5]
    finally:
        _clear(*uids)
