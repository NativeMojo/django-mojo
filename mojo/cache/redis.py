import base64
import pickle

from django.core.cache.backends.base import BaseCache
from django.core.cache.backends.base import DEFAULT_TIMEOUT

from mojo.helpers import logit
from mojo.helpers.redis import get_connection


class MojoRedisCache(BaseCache):
    """
    Django cache backend backed by Mojo's shared Redis client.

    LOCATION is accepted for migration compatibility but not used because
    connection details come from mojo.helpers.redis settings.
    """

    def __init__(self, server, params):
        super().__init__(params)
        self._location = server
        self._client = None

    def _get_client(self):
        if self._client is None:
            self._client = get_connection()
        return self._client

    def _serialize(self, value):
        payload = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
        return base64.b64encode(payload).decode("ascii")

    def _deserialize(self, value):
        if value is None:
            return None
        if isinstance(value, str):
            value = value.encode("ascii")
        payload = base64.b64decode(value)
        return pickle.loads(payload)

    def _resolve_timeout(self, timeout):
        if timeout is DEFAULT_TIMEOUT:
            timeout = self.default_timeout
        if timeout is None:
            return None
        timeout = int(timeout)
        if timeout <= 0:
            return 0
        return timeout

    def _log_redis_error(self, action, error):
        logit.error(f"MojoRedisCache {action} failed: {error}")

    def get(self, key, default=None, version=None):
        cache_key = self.make_key(key, version=version)
        try:
            value = self._get_client().get(cache_key)
            if value is None:
                return default
            return self._deserialize(value)
        except Exception as error:
            self._log_redis_error("get", error)
            return default

    def set(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        cache_key = self.make_key(key, version=version)
        backend_timeout = self._resolve_timeout(timeout)

        if backend_timeout == 0:
            self._get_client().delete(cache_key)
            return True

        payload = self._serialize(value)
        try:
            if backend_timeout is None:
                return bool(self._get_client().set(cache_key, payload))
            return bool(self._get_client().set(cache_key, payload, ex=backend_timeout))
        except Exception as error:
            self._log_redis_error("set", error)
            return False

    def add(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        cache_key = self.make_key(key, version=version)
        backend_timeout = self._resolve_timeout(timeout)
        if backend_timeout == 0:
            return False

        payload = self._serialize(value)
        try:
            if backend_timeout is None:
                return bool(self._get_client().set(cache_key, payload, nx=True))
            return bool(self._get_client().set(cache_key, payload, ex=backend_timeout, nx=True))
        except Exception as error:
            self._log_redis_error("add", error)
            return False

    def delete(self, key, version=None):
        cache_key = self.make_key(key, version=version)
        try:
            return bool(self._get_client().delete(cache_key))
        except Exception as error:
            self._log_redis_error("delete", error)
            return False

    def get_many(self, keys, version=None):
        if not keys:
            return {}
        cache_keys = [self.make_key(key, version=version) for key in keys]
        try:
            values = self._get_client().mget(cache_keys)
            results = {}
            for original_key, value in zip(keys, values):
                if value is not None:
                    results[original_key] = self._deserialize(value)
            return results
        except Exception as error:
            self._log_redis_error("get_many", error)
            return {}

    def set_many(self, data, timeout=DEFAULT_TIMEOUT, version=None):
        failed_keys = []
        for key, value in data.items():
            if not self.set(key, value, timeout=timeout, version=version):
                failed_keys.append(key)
        return failed_keys

    def delete_many(self, keys, version=None):
        if not keys:
            return
        cache_keys = [self.make_key(key, version=version) for key in keys]
        try:
            self._get_client().delete(*cache_keys)
        except Exception as error:
            self._log_redis_error("delete_many", error)

    def has_key(self, key, version=None):
        cache_key = self.make_key(key, version=version)
        try:
            return bool(self._get_client().exists(cache_key))
        except Exception as error:
            self._log_redis_error("has_key", error)
            return False

    def clear(self):
        try:
            return bool(self._get_client().flushdb())
        except Exception as error:
            self._log_redis_error("clear", error)
            return False

    def touch(self, key, timeout=DEFAULT_TIMEOUT, version=None):
        cache_key = self.make_key(key, version=version)
        backend_timeout = self._resolve_timeout(timeout)
        try:
            if not self._get_client().exists(cache_key):
                return False
            if backend_timeout is None:
                return bool(self._get_client().persist(cache_key))
            if backend_timeout == 0:
                return bool(self._get_client().delete(cache_key))
            return bool(self._get_client().expire(cache_key, backend_timeout))
        except Exception as error:
            self._log_redis_error("touch", error)
            return False

    def incr(self, key, delta=1, version=None):
        cache_key = self.make_key(key, version=version)
        client = self._get_client()
        pipe = client.pipeline(transaction=True)
        try:
            pipe.watch(cache_key)
            current_raw = pipe.get(cache_key)
            if current_raw is None:
                raise ValueError("Key '%s' not found" % key)

            current_value = self._deserialize(current_raw)
            if not isinstance(current_value, int):
                raise ValueError("Key '%s' value is not an integer" % key)

            new_value = current_value + delta
            ttl = pipe.ttl(cache_key)

            pipe.multi()
            pipe.set(cache_key, self._serialize(new_value))
            if ttl > 0:
                pipe.expire(cache_key, ttl)
            pipe.execute()
            return new_value
        except ValueError:
            raise
        except Exception as error:
            self._log_redis_error("incr", error)
            raise
        finally:
            pipe.reset()

    def decr(self, key, delta=1, version=None):
        return self.incr(key, delta=-delta, version=version)

