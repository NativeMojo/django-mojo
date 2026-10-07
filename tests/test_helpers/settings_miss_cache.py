"""settings.get remembers a miss (#6561).

An unset key used to cost one SELECT per scope on every read, because only
found values were cached. Setting.resolve now caches the database's answer
either way — the value, or CACHE_MISS when the scope has no row — so after
the first read an unset key costs Redis reads and zero SQL.

Everything here is test-owned: DM6561_* keys, two dm6561_* groups, and the
`redis=` seam on Setting.resolve for the Redis-down cases. Nothing is patched.
"""
from testit import helpers as th

TESTIT_TIER = "core"

UNSET_KEY = "DM6561_UNSET"
LATE_KEY = "DM6561_SET_AFTER_MISS"
CHAIN_KEY = "DM6561_CHAIN_MISS"
PARENT_KEY = "DM6561_PARENT_LATE"
RACE_KEY = "DM6561_READER_RACE"
DOWN_KEY = "DM6561_REDIS_DOWN"
OWNED_KEYS = (UNSET_KEY, LATE_KEY, CHAIN_KEY, PARENT_KEY, RACE_KEY, DOWN_KEY)
PARENT_NAME = "dm6561_parent"
CHILD_NAME = "dm6561_child"


class _CountingRedis:
    """The real client, recording which commands the resolver sends."""

    def __init__(self, real):
        self.real = real
        self.calls = []

    def hget(self, key, field):
        self.calls.append(("hget", key))
        return self.real.hget(key, field)

    def pipeline(self, transaction=False):
        self.calls.append(("pipeline", None))
        return self.real.pipeline(transaction=transaction)

    def expire(self, key, ttl):
        self.calls.append(("expire", key))
        return self.real.expire(key, ttl)


class _DownRedis:
    """A client whose every command fails, like a Redis that went away."""

    def __init__(self):
        self.calls = 0

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            import redis
            self.calls += 1
            raise redis.exceptions.ConnectionError("redis is down")
        return fail


def _clean(parent_id=None, child_id=None):
    from mojo.apps.account.models.setting import Setting
    Setting.objects.filter(key__in=OWNED_KEYS).delete()
    r = Setting._redis()
    if r:
        for group_id in (None, parent_id, child_id):
            r.hdel(Setting._redis_key(group_id), *OWNED_KEYS)


@th.django_unit_setup()
def setup_settings_miss_cache(opts):
    from mojo.apps.account.models import Group

    parent = Group.objects.filter(name=PARENT_NAME).last()
    if parent is None:
        parent = Group(name=PARENT_NAME, kind="organization")
        parent.save()
    child = Group.objects.filter(name=CHILD_NAME).last()
    if child is None:
        child = Group(name=CHILD_NAME, kind="team", parent=parent)
        child.save()
    elif child.parent_id != parent.pk:
        child.parent = parent
        child.save(update_fields=["parent"])
    opts.parent_id = parent.pk
    opts.child_id = child.pk
    _clean(parent.pk, child.pk)


@th.django_unit_test("an unset key costs zero SQL after its first read")
def test_unset_key_second_read_issues_no_sql(opts):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from mojo.apps.account.models.setting import Setting
    from mojo.helpers.settings import settings

    with CaptureQueriesContext(connection) as first:
        value = settings.get(UNSET_KEY, "fallback")
    assert value == "fallback", f"an unset key did not return its default: {value!r}"
    assert len(first.captured_queries) >= 1, \
        "the first read of an unset key never asked the database"

    for attempt in range(3):
        with CaptureQueriesContext(connection) as again:
            value = settings.get(UNSET_KEY, "fallback")
        assert value == "fallback", \
            f"read {attempt + 2} of an unset key changed value: {value!r}"
        assert len(again.captured_queries) == 0, (
            f"read {attempt + 2} of an unset key still ran SQL: "
            f"{[q['sql'] for q in again.captured_queries]}")

    value, found = Setting.get_cached(UNSET_KEY)
    assert not found and value is None, \
        f"get_cached exposed the cached miss as a value: {value!r}"


@th.django_unit_test("a key set after its miss was cached is read at once, and falls back when removed")
def test_set_after_cached_miss_is_visible(opts):
    from mojo.apps.account.models.setting import Setting
    from mojo.helpers.settings import settings

    assert settings.get(LATE_KEY, "file-default") == "file-default", \
        "the key was unexpectedly set before the test set it"
    try:
        Setting.set(LATE_KEY, "now-set")
        value = settings.get(LATE_KEY, "file-default")
        assert value == "now-set", \
            f"a key set after its miss was cached still read as unset: {value!r}"
    finally:
        Setting.remove(LATE_KEY)
    value = settings.get(LATE_KEY, "file-default")
    assert value == "file-default", \
        f"a removed key did not fall back to its default: {value!r}"


@th.django_unit_test("setting a parent after a child cached its miss is visible to the child")
def test_parent_set_after_child_cached_miss(opts):
    from mojo.apps.account.models import Group
    from mojo.apps.account.models.setting import Setting, CACHE_MISS

    parent = Group.objects.get(pk=opts.parent_id)
    child = Group.objects.get(pk=opts.child_id)
    assert Setting.resolve(PARENT_KEY, group=child, default="none") == "none", \
        "the key was unexpectedly set before the test set it"
    cached = Setting._cache_read(
        Setting._redis(), Setting._redis_key(child.pk), PARENT_KEY)
    assert cached == CACHE_MISS, \
        f"the child's miss was not cached, so this test proves nothing: {cached!r}"
    try:
        Setting.set(PARENT_KEY, "from-parent", group=parent)
        value = Setting.resolve(PARENT_KEY, group=child, default="none")
        assert value == "from-parent", \
            f"the child's cached miss shadowed the parent's new value: {value!r}"

        Setting.set(PARENT_KEY, "from-global")
        Setting.remove(PARENT_KEY, group=parent)
        value = Setting.resolve(PARENT_KEY, group=child, default="none")
        assert value == "from-global", \
            f"cached misses on the chain shadowed the new global value: {value!r}"
    finally:
        Setting.remove(PARENT_KEY, group=parent)
        Setting.remove(PARENT_KEY)


@th.django_unit_test("a cached-miss group chain is one Redis read per level and no SQL")
def test_group_chain_of_cached_misses_issues_no_sql(opts):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from mojo.apps.account.models import Group
    from mojo.apps.account.models.setting import Setting, CACHE_TTL

    child = Group.objects.get(pk=opts.child_id)
    assert Setting.resolve(CHAIN_KEY, group=child, default="none") == "none", \
        "the key was unexpectedly set before the test read it"

    client = _CountingRedis(Setting._redis())
    with CaptureQueriesContext(connection) as again:
        value = Setting.resolve(CHAIN_KEY, group=child, default="none", redis=client)
    assert value == "none", f"a chain of cached misses returned a value: {value!r}"
    assert len(again.captured_queries) == 0, (
        "a chain of cached misses still ran SQL: "
        f"{[q['sql'] for q in again.captured_queries]}")
    expected = [
        ("hget", Setting._redis_key(opts.child_id)),
        ("hget", Setting._redis_key(opts.parent_id)),
        ("hget", Setting._redis_key(None)),
    ]
    assert client.calls == expected, \
        f"expected one Redis read per level and no writes, got {client.calls}"

    ttl = Setting._redis().ttl(Setting._redis_key(opts.child_id))
    assert 0 < ttl <= CACHE_TTL, \
        f"the settings hash has no expiry backstop after a cached miss (ttl={ttl})"


@th.django_unit_test("a reader's late miss never overwrites the value a writer pushed")
def test_reader_miss_never_overwrites_writer_value(opts):
    from mojo.apps.account.models.setting import Setting, CACHE_MISS

    try:
        # The race: a reader's SELECT saw no row, a writer then saved and
        # pushed, and only now does the reader record its miss.
        Setting.set(RACE_KEY, "written")
        r = Setting._redis()
        Setting._cache_write(r, Setting._redis_key(), RACE_KEY, CACHE_MISS,
                             only_if_absent=True)
        value = Setting.resolve(RACE_KEY, default="none")
        assert value == "written", \
            f"a reader's late miss overwrote the writer's value: {value!r}"
    finally:
        Setting.remove(RACE_KEY)


@th.django_unit_test("Redis unavailable falls back to the database, never an exception")
def test_redis_down_reads_the_database(opts):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from mojo.apps.account.models import Group
    from mojo.apps.account.models.setting import Setting

    child = Group.objects.get(pk=opts.child_id)
    try:
        Setting.set(DOWN_KEY, "from-db")
        with CaptureQueriesContext(connection) as no_client:
            value = Setting.resolve(DOWN_KEY, group=child, default="none", redis=None)
        assert value == "from-db", \
            f"with no Redis client the database value was not returned: {value!r}"
        assert len(no_client.captured_queries) >= 1, \
            "with no Redis client the read did not go to the database"

        down = _DownRedis()
        value = Setting.resolve(DOWN_KEY, group=child, default="none", redis=down)
        assert value == "from-db", \
            f"with Redis failing the database value was not returned: {value!r}"
        assert down.calls == 1, \
            f"the walk kept asking a Redis that had already failed ({down.calls} calls)"

        value = Setting.resolve(UNSET_KEY + "_DOWN", default="none", redis=_DownRedis())
        assert value == "none", \
            f"an unset key with Redis failing did not return its default: {value!r}"
    finally:
        Setting.remove(DOWN_KEY)
        _clean(opts.parent_id, opts.child_id)
