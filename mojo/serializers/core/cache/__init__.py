"""
Django-MOJO Cache System for Serializers

Provides pluggable cache backends for high-performance serialization with intelligent
TTL management based on RestMeta.GRAPHS configuration.

Supported Backends:
- MemoryCacheBackend: In-process LRU cache with TTL support
- RedisCacheBackend: Distributed Redis-based caching (future)
- DisabledCacheBackend: No-op cache for development/testing

Key Features:
- RestMeta.cache_ttl configuration per model/graph
- LRU eviction with configurable size limits
- JSON serialization for Redis compatibility
- Thread-safe operations
- Safe defaults (cache_ttl=0 means no caching)

Usage:
    from mojo.serializers.core.cache import get_cache_backend

    cache = get_cache_backend()

    # Get cached item (returns None if not found/expired)
    data = cache.get("Event_123_default")

    # Set with TTL from RestMeta configuration
    cache.set("Event_123_default", serialized_data, ttl=300)

Configuration:
    MOJO_SERIALIZER_CACHE = {
        'backend': 'memory',  # 'memory', 'redis', 'disabled'
        'memory': {
            'max_size': 5000,
            'enable_stats': True
        },
        'redis': {  # Future implementation
            'host': 'localhost',
            'port': 6379,
            'key_prefix': 'mojo:serializer:'
        }
    }

RestMeta Configuration:
    class MyModel(MojoModel):
        class RestMeta:
            GRAPHS = {
                "default": {
                    "fields": ["id", "name"],
                    "cache_ttl": 300  # 5 minutes
                },
                "realtime": {
                    "fields": ["id", "status"],
                    "cache_ttl": 0  # No caching (default)
                }
            }
"""

from .base import CacheBackend
from .memory import MemoryCacheBackend
from .redis import RedisCacheBackend
from .disabled import DisabledCacheBackend
from .backends import (
    get_cache_backend,
    create_cache_backend,
    reset_cache_backend,
    get_available_backends,
    test_backend_connectivity,
    get_cache_health,
    validate_cache_config
)

from .utils import (
    get_cache_key,
    get_model_cache_ttl,
    get_cache_stats,
    clear_all_caches
)

# Check ujson availability for performance optimization
try:
    import ujson
    HAS_UJSON = True
    UJSON_VERSION = getattr(ujson, '__version__', 'unknown')
except ImportError:
    ujson = None
    HAS_UJSON = False
    UJSON_VERSION = None

# Main exports
__all__ = [
    # Backend classes
    'CacheBackend',
    'MemoryCacheBackend',
    'RedisCacheBackend',
    'DisabledCacheBackend',

    # Factory functions
    'get_cache_backend',
    'create_cache_backend',
    'reset_cache_backend',

    # Backend management
    'get_available_backends',
    'test_backend_connectivity',
    'get_cache_health',
    'validate_cache_config',

    # Utilities
    'get_cache_key',
    'get_model_cache_ttl',
    'get_cache_stats',
    'clear_all_caches',

    # Performance info
    'HAS_UJSON',
    'UJSON_VERSION',
]

# Version info
__version__ = "1.0.0"
