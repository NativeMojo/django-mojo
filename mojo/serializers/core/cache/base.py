"""
Abstract Base Cache Backend for Django-MOJO Serializers

Defines the interface that all cache backends must implement to ensure consistency
between memory-based, Redis-based, and other caching systems.

All cache backends must implement this interface for plug-and-play compatibility.
"""

from abc import ABC, abstractmethod
from typing import Any, Optional, Dict


class CacheBackend(ABC):
    """
    Abstract base class for serializer cache backends.

    All cache backends must implement this interface to ensure consistency
    between memory-based and distributed caching systems. This allows for
    seamless switching between different cache implementations.

    Implementation Requirements:
    - Thread-safe operations
    - TTL support (0 = no expiration)
    - JSON-serializable values only
    - Graceful error handling
    - Performance statistics tracking
    """

    @abstractmethod
    def get(self, key: str) -> Optional[Any]:
        """
        Retrieve item from cache.

        Must check TTL expiration and return None for expired items.
        Should update access patterns for LRU backends.

        :param key: Cache key string
        :return: Cached value or None if not found/expired
        """
        pass

    @abstractmethod
    def set(self, key: str, value: Any, ttl: int = 0) -> bool:
        """
        Store item in cache with optional TTL.

        Value must be JSON-serializable. TTL of 0 should mean no caching
        (safe default behavior).

        :param key: Cache key string
        :param value: Value to cache (must be JSON serializable)
        :param ttl: Time-to-live in seconds (0 = no caching/expiration)
        :return: True if successfully cached, False otherwise
        """
        pass

    @abstractmethod
    def delete(self, key: str) -> bool:
        """
        Remove specific item from cache.

        :param key: Cache key string
        :return: True if item was found and removed, False if not found
        """
        pass

    @abstractmethod
    def clear(self) -> bool:
        """
        Clear all items from cache.

        Should remove all cached items and reset any internal state.

        :return: True if cache was successfully cleared
        """
        pass

    @abstractmethod
    def stats(self) -> Dict[str, Any]:
        """
        Get cache performance statistics.

        Should return a dictionary with standardized keys for monitoring
        and debugging purposes. Recommended keys:

        - backend: str (backend type name)
        - current_size: int (number of cached items)
        - max_size: int (maximum capacity, 0 if unlimited)
        - hit_rate: float (0.0-1.0, cache hit percentage)
        - total_requests: int (hits + misses)
        - hits: int (cache hits)
        - misses: int (cache misses)
        - sets: int (items added to cache)
        - deletes: int (items removed from cache)
        - evictions: int (items evicted due to capacity)
        - expired_items: int (items that expired)
        - errors: int (operation errors)

        :return: Dictionary with cache statistics
        """
        pass
