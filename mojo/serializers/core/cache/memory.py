"""
Memory Cache Backend for Django-MOJO Serializers

In-memory LRU cache implementation with TTL support and thread-safe operations.
Provides high-performance caching for serialized model instances with automatic
eviction and expiration handling.

Key Features:
- LRU (Least Recently Used) eviction when capacity is reached
- TTL (Time To Live) expiration checking on access
- Thread-safe operations using RLock
- JSON serialization for Redis compatibility
- Performance statistics tracking
- Configurable size limits with safe defaults
- TTL of 0 means no caching (safe default behavior)

Usage:
    cache = MemoryCacheBackend(max_size=5000, enable_stats=True)

    # Set with TTL (0 = no caching)
    cache.set("Event_123_default", serialized_data, ttl=300)

    # Get (returns None if not found/expired)
    data = cache.get("Event_123_default")

    # Statistics
    stats = cache.stats()
"""

# Use ujson for optimal performance, fallback to standard json
try:
    import ujson as json
except ImportError:
    import json
import time
import threading
from collections import OrderedDict
from typing import Any, Optional, Dict

# Use logit with graceful fallback
try:
    from mojo.helpers import logit
    logger = logit.get_logger("memory_cache", "memory_cache.log")
except Exception:
    import logging
    logger = logging.getLogger("memory_cache")

from .base import CacheBackend


class MemoryCacheBackend(CacheBackend):
    """
    In-memory LRU cache backend with TTL support.

    Implements thread-safe LRU eviction with TTL-based expiration.
    Uses OrderedDict for efficient LRU operations and JSON serialization
    for compatibility with distributed cache backends.

    Cache Structure:
    - OrderedDict maintains insertion/access order for LRU
    - Each entry: {key: (json_value, expires_at_timestamp)}
    - Thread-safe with RLock for concurrent access
    - Automatic cleanup on access (lazy expiration)
    """

    def __init__(self, max_size: int = 5000, enable_stats: bool = True):
        """
        Initialize memory cache backend.

        :param max_size: Maximum number of items to cache before LRU eviction
        :param enable_stats: Enable performance statistics tracking
        """
        self.max_size = max_size
        self.enable_stats = enable_stats

        # Thread-safe LRU cache storage
        # Format: {key: (json_serialized_value, expires_at_timestamp)}
        self._cache = OrderedDict()
        self._lock = threading.RLock()

        # Performance statistics tracking
        self._stats = {
            'hits': 0,              # Successful cache retrievals
            'misses': 0,            # Cache misses (not found/expired)
            'sets': 0,              # Items added to cache
            'deletes': 0,           # Items explicitly deleted
            'evictions': 0,         # Items evicted due to capacity
            'expired_items': 0,     # Items that expired on access
            'errors': 0             # JSON or other operation errors
        }

        logger.debug(f"Memory cache initialized: max_size={max_size}, stats_enabled={enable_stats}")

    def get(self, key: str) -> Optional[Any]:
        """
        Retrieve item from cache with TTL checking and LRU updating.

        Checks expiration, updates LRU order, and deserializes JSON.
        Returns None for cache misses or expired items.
        """
        with self._lock:
            if key not in self._cache:
                self._increment_stat('misses')
                return None

            try:
                # Remove from current position for LRU update
                json_value, expires_at = self._cache.pop(key)

                # Check TTL expiration
                if expires_at != 0 and time.time() > expires_at:
                    # Item expired - don't re-add to cache
                    self._increment_stat('expired_items')
                    self._increment_stat('misses')
                    return None

                # Re-add to end (most recently used position)
                self._cache[key] = (json_value, expires_at)
                self._increment_stat('hits')

                # Deserialize from JSON
                return json.loads(json_value) if json_value else None

            except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
                logger.warning(f"Cache get error for key '{key}': {e}")
                # Remove corrupted entry
                self._cache.pop(key, None)
                self._increment_stat('errors')
                self._increment_stat('misses')
                return None

    def set(self, key: str, value: Any, ttl: int = 0) -> bool:
        """
        Store item in cache with LRU eviction and JSON serialization.

        TTL of 0 means no caching (safe default). Automatically evicts
        LRU items when capacity is reached.
        """
        if ttl == 0:
            # TTL of 0 means no caching - safe default behavior
            return True

        with self._lock:
            try:
                # Serialize to JSON for Redis compatibility
                json_value = json.dumps(value, default=str)  # default=str handles dates/decimals

                # Calculate expiration timestamp
                expires_at = time.time() + ttl if ttl > 0 else 0

                # Remove existing entry if present (updates position)
                if key in self._cache:
                    self._cache.pop(key)

                # Evict LRU items if at capacity
                while len(self._cache) >= self.max_size:
                    self._evict_lru()

                # Add new item (becomes most recently used)
                self._cache[key] = (json_value, expires_at)
                self._increment_stat('sets')

                return True

            except (json.JSONEncodeError, TypeError, ValueError) as e:
                logger.warning(f"Cache set error for key '{key}': {e}")
                self._increment_stat('errors')
                return False

    def delete(self, key: str) -> bool:
        """
        Remove specific item from cache.

        :param key: Cache key to remove
        :return: True if item was found and removed
        """
        with self._lock:
            if key in self._cache:
                self._cache.pop(key)
                self._increment_stat('deletes')
                return True
            return False

    def clear(self) -> bool:
        """
        Clear all cached items and reset statistics.

        Thread-safe operation that removes all items from cache.
        """
        with self._lock:
            cleared_count = len(self._cache)
            self._cache.clear()

            if cleared_count > 0:
                logger.info(f"Cleared {cleared_count} items from memory cache")

            return True

    def stats(self) -> Dict[str, Any]:
        """
        Get comprehensive cache statistics.

        Returns performance metrics, capacity info, and calculated ratios
        for monitoring and optimization purposes.
        """
        with self._lock:
            total_requests = self._stats['hits'] + self._stats['misses']
            hit_rate = (self._stats['hits'] / total_requests) if total_requests > 0 else 0.0

            return {
                # Backend identification
                'backend': 'memory',

                # Capacity information
                'current_size': len(self._cache),
                'max_size': self.max_size,
                'utilization': len(self._cache) / self.max_size if self.max_size > 0 else 0.0,

                # Performance metrics
                'hit_rate': hit_rate,
                'total_requests': total_requests,

                # Raw statistics
                **self._stats.copy()
            }

    def _evict_lru(self):
        """
        Evict least recently used item from cache.

        OrderedDict maintains insertion order - first item is LRU.
        Thread-safe operation that removes oldest item.
        """
        if self._cache:
            # OrderedDict: first item (index 0) is least recently used
            evicted_key, _ = self._cache.popitem(last=False)
            self._increment_stat('evictions')
            logger.debug(f"Evicted LRU item: {evicted_key}")

    def _increment_stat(self, stat_name: str):
        """
        Thread-safe statistics increment.

        Only increments if statistics tracking is enabled.
        """
        if self.enable_stats:
            self._stats[stat_name] += 1

    def get_memory_info(self) -> Dict[str, Any]:
        """
        Get memory usage estimation and cache health information.

        Provides rough estimates for memory usage and recommendations
        for cache configuration optimization.
        """
        with self._lock:
            current_size = len(self._cache)

            if current_size == 0:
                return {
                    'estimated_memory_mb': 0,
                    'estimated_memory_bytes': 0,
                    'objects': 0,
                    'health': 'empty'
                }

            # Rough estimate: 2KB per cached object (JSON + overhead)
            estimated_bytes = current_size * 2048
            estimated_mb = estimated_bytes / (1024 * 1024)

            # Health assessment
            utilization = current_size / self.max_size if self.max_size > 0 else 0
            if utilization > 0.9:
                health = 'high_utilization'
            elif utilization > 0.7:
                health = 'moderate_utilization'
            else:
                health = 'good'

            return {
                'estimated_memory_bytes': estimated_bytes,
                'estimated_memory_mb': round(estimated_mb, 2),
                'objects': current_size,
                'utilization': utilization,
                'health': health,
                'note': 'Rough estimate assuming ~2KB per cached object'
            }
