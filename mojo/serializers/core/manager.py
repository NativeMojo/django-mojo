"""
Serializer Manager for Django-MOJO

Provides a unified interface for switching between different serializers and managing
serialization strategies across the application. Supports:

- Multiple serializer backends (simple, advanced, optimized, custom)
- Runtime serializer switching
- Performance monitoring and comparison
- Configuration via Django settings
- Fallback mechanisms for robustness
- Easy integration with MojoModel REST operations

Usage:
    # Use default configured serializer
    manager = SerializerManager()
    serializer = manager.get_serializer(instance, graph="list")

    # Force specific serializer
    serializer = manager.get_serializer(instance, serializer_type="optimized")

    # Performance comparison
    results = manager.benchmark_serializers(queryset)
"""

import time
import importlib
from typing import Dict, Any, Optional, Type, Union, List
from threading import RLock

from django.conf import settings
from django.db.models import QuerySet, Model
from django.http import HttpResponse

from mojo.helpers import logit

logger = logit.get_logger("serializer_manager", "serializer_manager.log")

# Thread-safe lock for serializer registry operations
_registry_lock = RLock()

# Default serializer configurations
# DEFAULT_SERIALIZERS = {
#     'simple': 'mojo.serializers.simple.GraphSerializer',
#     'optimized': 'mojo.serializers.core.serializer.OptimizedGraphSerializer',
#     'advanced': 'mojo.serializers.advanced.AdvancedGraphSerializer',
# }

DEFAULT_SERIALIZERS = {
    'optimized': 'mojo.serializers.core.serializer.OptimizedGraphSerializer'
}

FORMAT_SERIALIZERS = {
    'csv': 'mojo.serializers.formats.csv.CsvFormatter'
}

# Global serializer registry
_SERIALIZER_REGISTRY = {}

# Performance tracking
_PERFORMANCE_DATA = {
    'serializer_usage': {},
    'performance_history': [],
    'benchmark_results': {}
}


class SerializerRegistry:
    """
    Registry for managing available serializers.

    This registry supports lazy loading of serializers. When a serializer is
    registered using an import path string, it is only imported when the
    `register` method is called. The default serializers are registered
    on the first call to `get_serializer_manager()`, avoiding imports at
    application startup.
    """

    def __init__(self):
        self.serializers = {}
        self.default_serializer = None
        self.lock = RLock()

    def register(self, name: str, serializer_class_or_path: Union[str, Type],
                 description: str = None, is_default: bool = False):
        """
        Register a serializer.

        :param name: Unique name for the serializer
        :param serializer_class_or_path: Serializer class or import path string
        :param description: Optional description
        :param is_default: Set as default serializer
        """
        with self.lock:
            # Handle string import path
            if isinstance(serializer_class_or_path, str):
                try:
                    module_path, class_name = serializer_class_or_path.rsplit('.', 1)
                    module = importlib.import_module(module_path)
                    serializer_class = getattr(module, class_name)
                except (ImportError, AttributeError) as e:
                    logger.error(f"Failed to import serializer '{serializer_class_or_path}': {e}")
                    return False
            else:
                serializer_class = serializer_class_or_path

            self.serializers[name] = {
                'class': serializer_class,
                'description': description or f"{name} serializer",
                'registered_at': time.time()
            }

            if is_default or self.default_serializer is None:
                self.default_serializer = name

            # logger.info(f"Registered serializer: {name}")
            return True

    def get(self, name: str):
        """Get serializer class by name."""
        with self.lock:
            serializer_info = self.serializers.get(name)
            return serializer_info['class'] if serializer_info else None

    def list_serializers(self):
        """List all registered serializers."""
        with self.lock:
            return {
                name: {
                    'description': info['description'],
                    'class_name': info['class'].__name__,
                    'is_default': name == self.default_serializer
                }
                for name, info in self.serializers.items()
            }

    def get_default(self):
        """Get the default serializer name."""
        with self.lock:
            return self.default_serializer

    def set_default(self, name: str):
        """Set default serializer."""
        with self.lock:
            if name in self.serializers:
                self.default_serializer = name
                logger.info(f"Default serializer set to: {name}")
                return True
            return False


# Global serializer registry
_registry = SerializerRegistry()


class SerializerManager:
    """
    Main serializer manager providing unified interface for all serialization operations.
    """

    def __init__(self, default_serializer: str = None, enable_performance_tracking: bool = True):
        """
        Initialize serializer manager.

        :param default_serializer: Override default serializer
        :param enable_performance_tracking: Enable performance monitoring
        """
        self.default_serializer = default_serializer
        self.performance_tracking = enable_performance_tracking
        self.registry = _registry
        self.serializer_class = None

        # Initialize default serializers if not already done
        self._ensure_default_serializers()

        # Load configuration from Django settings
        self._load_configuration()

    def _ensure_default_serializers(self):
        """Ensure default serializers are registered."""
        if not self.registry.serializers:
            for name, import_path in DEFAULT_SERIALIZERS.items():
                self.registry.register(
                    name=name,
                    serializer_class_or_path=import_path,
                    is_default=(name == 'optimized')  # Set optimized as default
                )
            for format, import_path in FORMAT_SERIALIZERS.items():
                _registry.register(
                    name=format,
                    serializer_class_or_path=import_path
                )


    def _load_configuration(self):
        """Load configuration from Django settings."""
        # Get default serializer from settings
        default_from_settings = getattr(settings, 'MOJO_DEFAULT_SERIALIZER', None)
        if default_from_settings and not self.default_serializer:
            self.default_serializer = default_from_settings

        # Register custom serializers from settings
        custom_serializers = getattr(settings, 'MOJO_CUSTOM_SERIALIZERS', {})
        for name, config in custom_serializers.items():
            if isinstance(config, str):
                # Simple string path
                self.registry.register(name, config)
            elif isinstance(config, dict):
                # Detailed configuration
                self.registry.register(
                    name=name,
                    serializer_class_or_path=config.get('class'),
                    description=config.get('description'),
                    is_default=config.get('is_default', False)
                )

    def get_serializer(self, instance, graph: str = "default", many: bool = None,
                      serializer_type: str = None, **kwargs):
        if not self.serializer_class:
            self.serializer_class = self.registry.get("optimized")
        return self.serializer_class(instance, graph=graph, many=many, **kwargs)


    def get_serializer_old(self, instance, graph: str = "default", many: bool = None,
                      serializer_type: str = None, **kwargs):
        """
        Get appropriate serializer for the given instance and parameters.

        :param instance: Model instance, QuerySet, or list of objects
        :param graph: Graph configuration name
        :param many: Force many=True for list serialization
        :param serializer_type: Override serializer type
        :param kwargs: Additional serializer arguments
        :return: Configured serializer instance
        """
        # Determine serializer type
        if serializer_type is None:
            serializer_type = self.default_serializer or self.registry.get_default()

        # Get serializer class
        serializer_class = self.registry.get(serializer_type)
        if serializer_class is None:
            logger.warning(f"Serializer '{serializer_type}' not found, using default")
            serializer_type = self.registry.get_default()
            serializer_class = self.registry.get(serializer_type)

        if serializer_class is None:
            raise ValueError("No serializer available")

        # Auto-detect many parameter for QuerySets
        if many is None and isinstance(instance, QuerySet):
            many = True

        # Track usage for performance monitoring
        if self.performance_tracking:
            self._track_usage(serializer_type, instance)

        # Create and return serializer instance
        try:
            return serializer_class(instance, graph=graph, many=many, **kwargs)
        except Exception as e:
            logger.error(f"Failed to create serializer '{serializer_type}': {e}")
            # Fallback to simple serializer
            fallback_class = self.registry.get('simple')
            if fallback_class and fallback_class != serializer_class:
                logger.info("Falling back to simple serializer")
                return fallback_class(instance, graph=graph, many=many)
            raise

    def get_format_serializer(self, format: str):
        SerializerClass = self.registry.get(format)
        if SerializerClass:
            return SerializerClass()
        raise ValueError(f"Serializer for format '{format}' not found")

    def serialize(self, instance, graph: str = "default", many: bool = None,
                 serializer_type: str = None, **kwargs):
        """
        Direct serialization method.

        :param instance: Object(s) to serialize
        :param graph: Graph configuration
        :param many: Force many=True
        :param serializer_type: Override serializer type
        :param kwargs: Additional arguments
        :return: Serialized data
        """
        serializer = self.get_serializer(
            instance=instance,
            graph=graph,
            many=many,
            serializer_type=serializer_type,
            **kwargs
        )
        return serializer.serialize()

    def to_json(self, instance, graph: str = "default", many: bool = None,
               serializer_type: str = None, **kwargs):
        """
        Serialize to JSON string.

        :param instance: Object(s) to serialize
        :param graph: Graph configuration
        :param many: Force many=True
        :param serializer_type: Override serializer type
        :param kwargs: Additional JSON arguments
        :return: JSON string
        """
        serializer = self.get_serializer(
            instance=instance,
            graph=graph,
            many=many,
            serializer_type=serializer_type
        )
        return serializer.to_json(**kwargs)

    def to_response(self, instance, request, graph: str = "default", many: bool = None,
                   serializer_type: str = None, **kwargs):
        """
        Serialize to HTTP response.

        :param instance: Object(s) to serialize
        :param request: Django request object
        :param graph: Graph configuration
        :param many: Force many=True
        :param serializer_type: Override serializer type
        :param kwargs: Additional response arguments
        :return: HttpResponse
        """
        serializer = self.get_serializer(
            instance=instance,
            graph=graph,
            many=many,
            serializer_type=serializer_type
        )
        return serializer.to_response(request, **kwargs)

    def benchmark_serializers(self, instance, graph: str = "default",
                            serializer_types: List[str] = None, iterations: int = 10):
        """
        Benchmark multiple serializers for performance comparison.

        :param instance: Test instance or queryset
        :param graph: Graph configuration to test
        :param serializer_types: List of serializers to test (default: all)
        :param iterations: Number of iterations per serializer
        :return: Benchmark results
        """
        if serializer_types is None:
            serializer_types = list(self.registry.list_serializers().keys())

        results = {}

        for serializer_type in serializer_types:
            logger.info(f"Benchmarking {serializer_type} serializer...")

            times = []
            errors = 0

            for i in range(iterations):
                try:
                    start_time = time.perf_counter()

                    serializer = self.get_serializer(
                        instance=instance,
                        graph=graph,
                        serializer_type=serializer_type
                    )
                    data = serializer.serialize()
                    json_output = serializer.to_json()

                    end_time = time.perf_counter()
                    times.append(end_time - start_time)

                except Exception as e:
                    logger.error(f"Benchmark error for {serializer_type}: {e}")
                    errors += 1

            if times:
                results[serializer_type] = {
                    'min_time': min(times),
                    'max_time': max(times),
                    'avg_time': sum(times) / len(times),
                    'total_time': sum(times),
                    'iterations': len(times),
                    'errors': errors,
                    'objects_per_second': len(times) / sum(times) if sum(times) > 0 else 0
                }
            else:
                results[serializer_type] = {
                    'error': 'All iterations failed',
                    'errors': errors
                }

        # Store benchmark results
        if self.performance_tracking:
            _PERFORMANCE_DATA['benchmark_results'][time.time()] = results

        return results

    def get_performance_stats(self):
        """Get performance statistics for all serializers."""
        stats = {
            'usage_stats': _PERFORMANCE_DATA['serializer_usage'].copy(),
            'registered_serializers': self.registry.list_serializers(),
            'default_serializer': self.registry.get_default()
        }

        # Add serializer-specific stats
        for name in self.registry.serializers.keys():
            serializer_class = self.registry.get(name)
            if hasattr(serializer_class, 'get_performance_stats'):
                try:
                    stats[f'{name}_stats'] = serializer_class.get_performance_stats()
                except Exception as e:
                    logger.warning(f"Failed to get stats for {name}: {e}")

        return stats

    def clear_caches(self, serializer_type: str = None):
        """
        Clear caches for specified serializer or all serializers.

        :param serializer_type: Specific serializer to clear, or None for all
        """
        if serializer_type:
            serializer_class = self.registry.get(serializer_type)
            if serializer_class and hasattr(serializer_class, 'clear_caches'):
                serializer_class.clear_caches()
                logger.info(f"Cleared caches for {serializer_type}")
        else:
            # Clear all serializer caches
            for name in self.registry.serializers.keys():
                serializer_class = self.registry.get(name)
                if serializer_class and hasattr(serializer_class, 'clear_caches'):
                    try:
                        serializer_class.clear_caches()
                    except Exception as e:
                        logger.warning(f"Failed to clear cache for {name}: {e}")
            logger.info("Cleared all serializer caches")

    def register_serializer(self, name: str, serializer_class_or_path: Union[str, Type],
                          description: str = None, is_default: bool = False):
        """
        Register a new serializer.

        :param name: Unique serializer name
        :param serializer_class_or_path: Serializer class or import path
        :param description: Optional description
        :param is_default: Set as default serializer
        :return: True if successful
        """
        return self.registry.register(name, serializer_class_or_path, description, is_default)

    def set_default_serializer(self, name: str):
        """
        Set the default serializer.

        :param name: Serializer name
        :return: True if successful
        """
        success = self.registry.set_default(name)
        if success:
            self.default_serializer = name
        return success

    def _track_usage(self, serializer_type: str, instance):
        """Track serializer usage for performance monitoring."""
        if not self.performance_tracking:
            return

        usage_key = f"{serializer_type}"
        if usage_key not in _PERFORMANCE_DATA['serializer_usage']:
            _PERFORMANCE_DATA['serializer_usage'][usage_key] = {
                'count': 0,
                'last_used': None,
                'total_objects': 0
            }

        stats = _PERFORMANCE_DATA['serializer_usage'][usage_key]
        stats['count'] += 1
        stats['last_used'] = time.time()

        # Count objects being serialized
        if isinstance(instance, QuerySet):
            try:
                stats['total_objects'] += instance.count()
            except Exception:
                stats['total_objects'] += 1
        elif isinstance(instance, (list, tuple)):
            stats['total_objects'] += len(instance)
        else:
            stats['total_objects'] += 1


# Global manager instance
_default_manager = None

def get_serializer_manager():
    """Get the global serializer manager instance."""
    global _default_manager
    if _default_manager is None:
        _default_manager = SerializerManager()
    return _default_manager

def register_serializer(name: str, serializer_class_or_path: Union[str, Type],
                       description: str = None, is_default: bool = False):
    """Register a serializer globally."""
    return get_serializer_manager().register_serializer(name, serializer_class_or_path, description, is_default)

def set_default_serializer(name: str):
    """Set the global default serializer."""
    return get_serializer_manager().set_default_serializer(name)

def serialize(instance, graph: str = "default", many: bool = None, serializer_type: str = None, **kwargs):
    """Global serialize function."""
    return get_serializer_manager().serialize(instance, graph, many, serializer_type, **kwargs)

def to_json(instance, graph: str = "default", many: bool = None, serializer_type: str = None, **kwargs):
    """Global to_json function."""
    return get_serializer_manager().to_json(instance, graph, many, serializer_type, **kwargs)

def to_response(instance, request, graph: str = "default", many: bool = None, serializer_type: str = None, **kwargs):
    """Global to_response function."""
    return get_serializer_manager().to_response(instance, request, graph, many, serializer_type, **kwargs)

def get_performance_stats():
    """Get global performance statistics."""
    return get_serializer_manager().get_performance_stats()

def clear_serializer_caches(serializer_type: str = None):
    """Clear serializer caches globally."""
    return get_serializer_manager().clear_caches(serializer_type)

def benchmark_serializers(instance, graph: str = "default", serializer_types: List[str] = None, iterations: int = 10):
    """Benchmark serializers globally."""
    return get_serializer_manager().benchmark_serializers(instance, graph, serializer_types, iterations)

def list_serializers():
    """List all registered serializers globally."""
    return get_serializer_manager().registry.list_serializers()

# Import ujson availability info
try:
    import ujson
    HAS_UJSON = True
    UJSON_VERSION = getattr(ujson, '__version__', 'unknown')
except ImportError:
    HAS_UJSON = False
    UJSON_VERSION = None
