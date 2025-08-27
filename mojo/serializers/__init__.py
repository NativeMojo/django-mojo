"""
Django-MOJO Serializers Package

Provides high-performance serialization for Django models with RestMeta.GRAPHS support.
Includes multiple serializer backends optimized for different use cases:

- OptimizedGraphSerializer: Ultra-fast with intelligent caching (default)
- GraphSerializer: Simple and reliable fallback
- AdvancedGraphSerializer: Feature-rich with multiple format support

Usage:
    from mojo.serializers import serialize, to_json, to_response

    # Quick serialization
    data = serialize(instance, graph="detail")
    json_str = to_json(queryset, graph="list")
    response = to_response(instance, request, graph="default")

    # Direct serializer access
    from mojo.serializers import OptimizedGraphSerializer
    serializer = OptimizedGraphSerializer(instance, graph="detail")
    data = serializer.serialize()
"""

# Core serializer classes
from .simple import GraphSerializer

# New optimized core system (default)
from .core import (
    OptimizedGraphSerializer,
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
    HAS_UJSON,
    UJSON_VERSION
)

# Advanced serializer (optional - may not be available)
try:
    from .advanced import AdvancedGraphSerializer
except ImportError:
    AdvancedGraphSerializer = None

# Version and metadata
__version__ = "2.0.0"
__author__ = "Django-MOJO Team"

# Default exports
__all__ = [
    # Core serializer classes
    'GraphSerializer',
    'OptimizedGraphSerializer',
    'AdvancedGraphSerializer',

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

    # Performance info
    'HAS_UJSON',
    'UJSON_VERSION',
]

# Initialize default manager on import
_manager = get_serializer_manager()

# Convenience shortcuts at package level
def get_serializer(instance, graph="default", many=None, serializer_type=None, **kwargs):
    """Get configured serializer instance."""
    return _manager.get_serializer(instance, graph, many, serializer_type, **kwargs)

def list_serializers():
    """List all registered serializers."""
    return _manager.registry.list_serializers()

def get_default_serializer():
    """Get the current default serializer name."""
    return _manager.registry.get_default()

# Add shortcuts to exports
__all__.extend([
    'get_serializer',
    'list_serializers',
    'get_default_serializer'
])
