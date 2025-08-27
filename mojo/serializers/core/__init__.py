"""
Django-MOJO Core Serializers

High-performance serialization system with intelligent caching and RestMeta.GRAPHS support.
This is the optimized serializer implementation with pluggable cache backends.

Main Components:
- OptimizedGraphSerializer: Ultra-fast serializer with smart caching
- SerializerManager: Unified interface for all serializer backends
- Cache backends: Memory and Redis-based caching systems

Usage:
    from mojo.serializers.core import OptimizedGraphSerializer, SerializerManager

    # Direct usage
    serializer = OptimizedGraphSerializer(instance, graph="detail")
    data = serializer.serialize()

    # Via manager
    manager = SerializerManager()
    serializer = manager.get_serializer(instance, graph="list")
"""

# Import core components
from .serializer import OptimizedGraphSerializer
from .manager import (
    SerializerManager,
    get_serializer_manager,
    register_serializer,
    set_default_serializer,
    serialize,
    to_json,
    to_response,
    get_performance_stats,
    clear_serializer_caches,
    benchmark_serializers,
    list_serializers,
    HAS_UJSON,
    UJSON_VERSION
)

# Import cache system
from .cache import (
    get_cache_backend,
    get_cache_key,
    get_model_cache_ttl,
    get_cache_stats,
    clear_all_caches,
    HAS_UJSON,
    UJSON_VERSION
)

# Core serializer exports
__all__ = [
    # Main serializer
    'OptimizedGraphSerializer',

    # Manager
    'SerializerManager',
    'get_serializer_manager',

    # Registration functions
    'register_serializer',
    'set_default_serializer',

    # Convenience functions
    'serialize',
    'to_json',
    'to_response',

    # Performance monitoring
    'get_performance_stats',
    'clear_serializer_caches',
    'benchmark_serializers',
    'list_serializers',

    # Cache system
    'get_cache_backend',
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
