"""``clear_rate_limits`` deletes the right keys, cheaply.

The sweeps here used to run through redis-py's ``scan_iter`` with its default
``COUNT 10`` — one network round trip per ten keys — and one ``DEL`` per
match. On a database with tens of thousands of keys that is thousands of
round trips to delete (usually) nothing: measured at 0.885s for the four
sweeps one login performs over a 74,734-key db, against 0.023s once COUNT is
a real batch. A consumer's test client calls this on every login, so the
whole suite paid it.

These tests pin the BEHAVIOUR that must survive that change — literal
patterns delete without scanning, glob patterns still sweep, neighbouring
keys are untouched — plus the round-trip count itself, which is the point of
the fix and is otherwise invisible.
"""
import uuid as _uuid

from testit import helpers as th


def _redis():
    from mojo.helpers.redis import get_connection
    return get_connection()


@th.django_unit_test()
def test_literal_pattern_deletes_without_scanning(opts):
    """``srl:<key>:ip:<ip>`` has no glob character — it IS the key name, so
    SCAN would walk the keyspace to rediscover what the caller passed in."""
    from mojo.decorators.limits import _delete_matching
    r = _redis()
    if r is None:
        return
    key = f"srl:testit-literal-{_uuid.uuid4().hex[:8]}"
    r.set(key, 1)
    calls = []
    real_scan = r.scan_iter

    def spy(*a, **kw):
        calls.append((a, kw))
        return real_scan(*a, **kw)

    r.scan_iter = spy
    try:
        assert _delete_matching(r, key) == 1, "the literal key must be deleted"
    finally:
        r.scan_iter = real_scan
    assert calls == [], f"a literal pattern must not SCAN, got {calls}"
    assert not r.exists(key), "the key must be gone"
    assert _delete_matching(r, key) == 0, "a second clear reports nothing deleted"


@th.django_unit_test()
def test_glob_pattern_sweeps_and_spares_neighbours(opts):
    from mojo.decorators.limits import _delete_matching
    r = _redis()
    if r is None:
        return
    tag = _uuid.uuid4().hex[:8]
    mine = [f"rl:login:ip:testit-{tag}:{w}" for w in (100, 200, 300)]
    other = f"rl:login:ip:testit-{tag}-neighbour:100"
    for k in mine:
        r.set(k, 1)
    r.set(other, 1)
    try:
        deleted = _delete_matching(r, f"rl:login:ip:testit-{tag}:*")
        assert deleted == 3, f"every window key must be swept, got {deleted}"
        assert not any(r.exists(k) for k in mine)
        assert r.exists(other), "a neighbouring identity must be untouched"
    finally:
        r.delete(other, *mine)


@th.django_unit_test()
def test_glob_sweep_asks_redis_for_a_real_batch(opts):
    """The regression that matters. Without an explicit COUNT this is one
    round trip per ten keys — the defect this test exists to prevent."""
    from mojo.decorators.limits import _delete_matching, _SCAN_COUNT
    r = _redis()
    if r is None:
        return
    seen = {}
    real_scan = r.scan_iter

    def spy(*a, **kw):
        seen.update(kw)
        return real_scan(*a, **kw)

    r.scan_iter = spy
    try:
        _delete_matching(r, f"rl:login:ip:testit-{_uuid.uuid4().hex[:8]}:*")
    finally:
        r.scan_iter = real_scan
    assert seen.get("count") == _SCAN_COUNT, (
        f"the sweep must pass an explicit COUNT (redis-py defaults to 10 — "
        f"thousands of round trips on a large db), got {seen}")
    assert _SCAN_COUNT >= 100, f"_SCAN_COUNT={_SCAN_COUNT} is not a real batch"


@th.django_unit_test()
def test_clear_rate_limits_removes_ip_and_account_counters(opts):
    """End to end through the public entry point, in the shape the consumer
    test clients call it: an IP + key sweep and a per-account clear."""
    from mojo.decorators.limits import clear_rate_limits
    r = _redis()
    if r is None:
        return
    tag = _uuid.uuid4().hex[:8]
    ip = f"testit-{tag}"
    srl = f"srl:login:ip:{ip}"
    rl = f"rl:login:ip:{ip}:100"
    acct_srl = "srl:login:account:987654321"
    survivor = f"srl:otherkey:ip:{ip}"
    for k in (srl, rl, acct_srl, survivor):
        r.set(k, 1)
    try:
        clear_rate_limits(ip=ip, key="login")
        assert not r.exists(srl) and not r.exists(rl), "both login IP keys go"
        assert r.exists(survivor), "a different limit bucket must survive"
        clear_rate_limits(key="login", account_id=987654321)
        assert not r.exists(acct_srl), "the per-account counter goes"
    finally:
        r.delete(srl, rl, acct_srl, survivor)
