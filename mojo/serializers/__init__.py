"""
Django-MOJO Serializers Package

Provides high-performance serialization for Django models with RestMeta.GRAPHS support.

Usage:
    from mojo.serializers import serialize, to_json, to_response

    # Quick serialization
    data = serialize(instance, graph="detail")
    json_str = to_json(queryset, graph="list")
    response = to_response(instance, request, graph="default")

For direct serializer access, import from full paths:
    from mojo.serializers.simple import GraphSerializer
    from mojo.serializers.core.serializer import OptimizedGraphSerializer
    from mojo.serializers.advanced import AdvancedGraphSerializer
"""

# Import only manager functions - serializer classes are lazy-loaded by the manager
from .core.manager import (
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

# Version and metadata
__version__ = "2.0.0"
__author__ = "Django-MOJO Team"

# Default exports
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

# Initialize default manager on import
_manager = get_serializer_manager()

# Convenience shortcuts at package level
def get_serializer(instance, graph="default", many=None, serializer_type=None, **kwargs):
    """Get configured serializer instance."""
    return _manager.get_serializer(instance, graph, many, serializer_type, **kwargs)

def get_default_serializer():
    """Get the current default serializer name."""
    return _manager.registry.get_default()

# Add shortcuts to exports
__all__.extend([
    'get_serializer',
    'get_default_serializer'
])
