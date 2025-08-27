"""
Serializer Manager - Backward Compatibility Layer

This module provides backward compatibility for imports from mojo.serializers.manager.
All functionality has been moved to mojo.serializers.core.manager but this layer
ensures existing code continues to work.

Usage:
    from mojo.serializers.manager import get_serializer_manager  # Still works
    from mojo.serializers import get_serializer_manager         # Preferred
"""

# Import everything from the new core manager
from .core.manager import (
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

# Re-export everything for backward compatibility
__all__ = [
    'SerializerManager',
    'get_serializer_manager',
    'register_serializer',
    'set_default_serializer',
    'serialize',
    'to_json',
    'to_response',
    'get_performance_stats',
    'clear_serializer_caches',
    'benchmark_serializers',
    'list_serializers',
    'HAS_UJSON',
    'UJSON_VERSION'
]

# Deprecation warning (optional - you can remove this if you don't want warnings)
import warnings
warnings.warn(
    "Importing from mojo.serializers.manager is deprecated. "
    "Use 'from mojo.serializers import ...' instead.",
    DeprecationWarning,
    stacklevel=2
)
