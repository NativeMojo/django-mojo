import time

from testit import helpers as th


def _cleanup_prefixed_keys(redis_conn, prefix):
    keys = list(redis_conn.scan_iter(match=f"{prefix}:*", count=200))
    if keys:
        redis_conn.delete(*keys)


@th.django_unit_setup()
def setup_redis_cache_backend(opts):
    from mojo.cache import MojoRedisCache
    from mojo.helpers.redis import get_connection

    opts.redis = get_connection()
    assert opts.redis.ping() == True, "Redis must be running for cache backend integration tests"

    opts.prefix = "djcachetest"
    opts.MojoRedisCache = MojoRedisCache
    opts.backend_path = "mojo.cache.MojoRedisCache"

    _cleanup_prefixed_keys(opts.redis, opts.prefix)


@th.django_unit_test()
def test_django_cache_uses_mojo_backend_first(opts):
    from django.conf import settings
    from django.core.cache import caches

    configured_backend = settings.CACHES.get("default", {}).get("BACKEND")
    assert configured_backend == opts.backend_path, (
        f"settings.CACHES['default']['BACKEND'] must be '{opts.backend_path}', got '{configured_backend}'"
    )

    caches.close_all()
    backend = caches["default"]
    assert isinstance(backend, opts.MojoRedisCache), (
        f"default cache backend must be MojoRedisCache at runtime, got {backend.__class__.__module__}.{backend.__class__.__name__}"
    )


@th.django_unit_test()
def test_django_core_cache_basic_flow(opts):
    from django.core.cache import cache
    from django.core.cache import caches

    _cleanup_prefixed_keys(opts.redis, opts.prefix)
    caches.close_all()
    backend = caches["default"]
    assert isinstance(backend, opts.MojoRedisCache), "Cache backend must be MojoRedisCache before running flow tests"

    assert cache.set(f"{opts.prefix}:k1", {"ok": True}, timeout=30) == True, "cache.set should succeed"
    assert cache.get(f"{opts.prefix}:k1") == {"ok": True}, "cache.get should return stored dict"
    assert cache.add(f"{opts.prefix}:k1", "new") == False, "cache.add should not overwrite existing key"
    assert cache.delete(f"{opts.prefix}:k1") == True, "cache.delete should return True for existing key"
    assert cache.get(f"{opts.prefix}:k1") is None, "cache.get should return None after delete"


@th.django_unit_test()
def test_django_core_cache_many_operations(opts):
    from django.core.cache import cache
    from django.core.cache import caches

    _cleanup_prefixed_keys(opts.redis, opts.prefix)
    caches.close_all()
    backend = caches["default"]
    assert isinstance(backend, opts.MojoRedisCache), "Cache backend must be MojoRedisCache before running flow tests"

    failed = cache.set_many({f"{opts.prefix}:a": 1, f"{opts.prefix}:b": 2}, timeout=60)
    assert failed == [], f"cache.set_many should return empty failed list, got {failed}"
    values = cache.get_many([f"{opts.prefix}:a", f"{opts.prefix}:b", f"{opts.prefix}:missing"])
    assert values == {f"{opts.prefix}:a": 1, f"{opts.prefix}:b": 2}, f"cache.get_many mismatch: {values}"
    cache.delete_many([f"{opts.prefix}:a", f"{opts.prefix}:b"])
    assert cache.get_many([f"{opts.prefix}:a", f"{opts.prefix}:b"]) == {}, "cache.delete_many should remove keys"


@th.django_unit_test()
def test_django_core_cache_timeout_behaviors(opts):
    from django.core.cache import cache
    from django.core.cache import caches

    _cleanup_prefixed_keys(opts.redis, opts.prefix)
    caches.close_all()
    backend = caches["default"]
    assert isinstance(backend, opts.MojoRedisCache), "Cache backend must be MojoRedisCache before running flow tests"

    assert cache.set(f"{opts.prefix}:persist", "v1", timeout=None) == True, "timeout=None set should succeed"
    time.sleep(1.1)
    assert cache.get(f"{opts.prefix}:persist") == "v1", "timeout=None should keep key without expiry"

    assert cache.set(f"{opts.prefix}:zero", "v2", timeout=0) == True, "timeout=0 set should succeed"
    assert cache.get(f"{opts.prefix}:zero") is None, "timeout=0 should immediately expire/no-store key"

    assert cache.set(f"{opts.prefix}:short", "v3", timeout=1) == True, "positive timeout set should succeed"
    assert cache.get(f"{opts.prefix}:short") == "v3", "key should exist before positive timeout expires"
    time.sleep(1.1)
    assert cache.get(f"{opts.prefix}:short") is None, "key should expire after positive timeout"


@th.django_unit_test()
def test_django_core_cache_serialization_round_trip(opts):
    from django.core.cache import cache
    from django.core.cache import caches

    _cleanup_prefixed_keys(opts.redis, opts.prefix)
    caches.close_all()
    backend = caches["default"]
    assert isinstance(backend, opts.MojoRedisCache), "Cache backend must be MojoRedisCache before running flow tests"

    samples = {
        f"{opts.prefix}:dict": {"a": 1, "b": [1, 2]},
        f"{opts.prefix}:list": [1, 2, "x"],
        f"{opts.prefix}:str": "hello",
        f"{opts.prefix}:int": 99,
        f"{opts.prefix}:bytes": b"\x00\xffdata",
    }
    for key, value in samples.items():
        assert cache.set(key, value, timeout=30) == True, f"cache.set should succeed for '{key}'"
        assert cache.get(key) == value, f"Round-trip mismatch for '{key}'"


@th.django_unit_test()
def test_django_core_cache_incr_decr_and_error_contract(opts):
    from django.core.cache import cache
    from django.core.cache import caches

    _cleanup_prefixed_keys(opts.redis, opts.prefix)
    caches.close_all()
    backend = caches["default"]
    assert isinstance(backend, opts.MojoRedisCache), "Cache backend must be MojoRedisCache before running flow tests"

    cache.set(f"{opts.prefix}:counter", 10, timeout=30)
    assert cache.incr(f"{opts.prefix}:counter", 2) == 12, "cache.incr should increment integer value"
    assert cache.decr(f"{opts.prefix}:counter", 1) == 11, "cache.decr should decrement integer value"

    cache.set(f"{opts.prefix}:not-int", "abc", timeout=30)
    did_raise = False
    try:
        cache.incr(f"{opts.prefix}:not-int")
    except ValueError:
        did_raise = True
    assert did_raise == True, "cache.incr should raise ValueError for non-integer key values"

    did_raise = False
    try:
        cache.incr(f"{opts.prefix}:missing")
    except ValueError:
        did_raise = True
    assert did_raise == True, "cache.incr should raise ValueError for missing keys"


@th.django_unit_test()
def test_django_core_cache_key_prefix_version_touch_and_clear(opts):
    from django.core.cache import cache
    from django.core.cache import caches

    _cleanup_prefixed_keys(opts.redis, opts.prefix)
    caches.close_all()
    backend = caches["default"]
    assert isinstance(backend, opts.MojoRedisCache), "Cache backend must be MojoRedisCache before running flow tests"

    cache.set(f"{opts.prefix}:versioned", "value", version=7, timeout=30)
    internal_key = backend.make_key(f"{opts.prefix}:versioned", version=7)
    assert opts.redis.exists(internal_key) == 1, f"Expected internal key '{internal_key}' to exist in Redis"

    assert cache.has_key(f"{opts.prefix}:versioned", version=7) == True, "cache.has_key should return true for existing key"
    assert cache.touch(f"{opts.prefix}:versioned", timeout=120, version=7) == True, "cache.touch should update timeout"
    assert cache.get(f"{opts.prefix}:versioned", version=7) == "value", "Touched key should remain readable"

    cache.delete(f"{opts.prefix}:versioned", version=7)
    assert cache.get(f"{opts.prefix}:versioned", version=7) is None, "Key should be deleted"
