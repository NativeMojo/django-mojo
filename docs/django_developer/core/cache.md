# Django Cache Backend (Redis)

Use the Mojo Redis-backed Django cache backend to replace `redis_cache.RedisCache`.

## Recommended settings

```python
CACHES = {
    "default": {
        "BACKEND": "mojo.cache.MojoRedisCache",
        "TIMEOUT": 300,
        "KEY_PREFIX": "mojoc",
    }
}
```

## Migration notes

- Remove `redis_cache` from your project dependencies.
- Existing usage of `from django.core.cache import cache` stays the same.
- `LOCATION` is accepted for migration compatibility but ignored by `MojoRedisCache`.
- Redis connection settings come from Mojo Redis helpers (`mojo.helpers.redis.get_connection`) and your framework Redis settings.
