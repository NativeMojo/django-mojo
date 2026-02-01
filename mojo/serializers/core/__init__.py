"""
Django-MOJO Core Serializers

High-performance serialization system with intelligent caching and RestMeta.GRAPHS support.
This is the optimized serializer implementation with pluggable cache backends.

Usage:
    # For direct serializer access, use full import paths:
    from mojo.serializers.core.serializer import OptimizedGraphSerializer
    from mojo.serializers.core.manager import SerializerManager

    # Via manager (recommended)
    from mojo.serializers import get_serializer_manager
    manager = get_serializer_manager()
    serializer = manager.get_serializer(instance, graph="list")
"""

# Import only manager functions - serializers and cache are lazy-loaded when needed
from .manager import (
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

# Core exports
__all__ = [
    # Manager
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

    # Performance info
    'HAS_UJSON',
    'UJSON_VERSION',
]

# Version info
__version__ = "1.0.0"
