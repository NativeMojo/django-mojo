"""
Disabled Cache Backend for Django-MOJO Serializers

No-op cache backend that disables all caching functionality while maintaining
the same interface as other cache backends. Useful for development, testing,
or production environments where caching should be completely disabled.

Key Features:
- All operations succeed but no data is actually cached
- Zero memory footprint for caching
- Minimal performance overhead
- Same interface as other backends for easy switching
- Safe for all environments including production

Usage:
    cache = DisabledCacheBackend()

    # All operations succeed but nothing is cached
    cache.set("key", "value", ttl=300)  # Returns True, but doesn't cache
    result = cache.get("key")           # Always returns None
    cache.clear()                       # Returns True

    # Statistics show zero activity
    stats = cache.stats()  # All zeros
"""

from typing import Any, Optional, Dict

# Use logit with graceful fallback
try:
    from mojo.helpers import logit
    logger = logit.get_logger("disabled_cache", "disabled_cache.log")
except Exception:
    import logging
    logger = logging.getLogger("disabled_cache")

from .base import CacheBackend


class DisabledCacheBackend(CacheBackend):
    """
    No-op cache backend that disables all caching functionality.

    This backend implements the full CacheBackend interface but performs
    no actual caching operations. All methods succeed but no data is stored
    or retrieved. This is useful when you want to disable caching without
    changing application code.

    Benefits:
    - Zero memory usage for caching
    - Minimal CPU overhead
    - Same interface as other backends
    - Safe fallback option
    - Useful for debugging caching issues
    """

    def __init__(self):
        """
        Initialize disabled cache backend.

        No configuration options needed since no caching is performed.
        Logs initialization for debugging purposes.
        """
        logger.info("Disabled cache backend initialized - no caching will be performed")

    def get(self, key: str) -> Optional[Any]:
        """
        Always returns None (cache miss).

        Since no caching is performed, all get operations result in cache misses.
        This ensures the application always fetches fresh data.

        :param key: Cache key (ignored)
        :return: Always None (cache miss)
        """
        return None

    def set(self, key: str, value: Any, ttl: int = 0) -> bool:
        """
        Always returns True but doesn't cache anything.

        Accepts all parameters to maintain interface compatibility but
        performs no caching operation. This allows the application to
        continue functioning normally without caching.

        :param key: Cache key (ignored)
        :param value: Value to cache (ignored)
        :param ttl: Time-to-live in seconds (ignored)
        :return: Always True (operation "succeeded")
        """
        return True

    def delete(self, key: str) -> bool:
        """
        Always returns True but doesn't delete anything.

        Since nothing is cached, there's nothing to delete, but we return
        True to indicate the "operation" succeeded.

        :param key: Cache key (ignored)
        :return: Always True (operation "succeeded")
        """
        return True

    def clear(self) -> bool:
        """
        Always returns True but doesn't clear anything.

        Since no cache exists, there's nothing to clear, but we return
        True to indicate the "operation" succeeded.

        :return: Always True (operation "succeeded")
        """
        return True

    def stats(self) -> Dict[str, Any]:
        """
        Returns statistics showing no cache activity.

        All statistics are zero since no caching operations are performed.
        This maintains compatibility with monitoring systems that expect
        cache statistics.

        :return: Dictionary with zero statistics
        """
        return {
            # Backend identification
            'backend': 'disabled',

            # Capacity information (all zero/disabled)
            'current_size': 0,
            'max_size': 0,
            'utilization': 0.0,

            # Performance metrics (all zero)
            'hit_rate': 0.0,
            'total_requests': 0,

            # Operation counts (all zero)
            'hits': 0,
            'misses': 0,
            'sets': 0,
            'deletes': 0,
            'evictions': 0,
            'expired_items': 0,
            'errors': 0,

            # Status information
            'status': 'disabled',
            'note': 'Caching is completely disabled'
        }

    def is_enabled(self) -> bool:
        """
        Always returns False since caching is disabled.

        Utility method to check if caching is actually enabled.
        Other backends would return True.

        :return: Always False
        """
        return False

    def get_config_info(self) -> Dict[str, Any]:
        """
        Return configuration information for this backend.

        Useful for debugging and monitoring to understand
        what cache backend is currently active.

        :return: Configuration information
        """
        return {
            'backend_type': 'disabled',
            'description': 'No-op cache backend with caching completely disabled',
            'memory_usage': 0,
            'thread_safe': True,
            'supports_ttl': False,  # TTL is ignored
            'supports_persistence': False,
            'recommended_for': ['development', 'testing', 'debugging']
        }
