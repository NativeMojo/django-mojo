"""
Cache Utilities for Django-MOJO Serializers

Provides utility functions for cache key generation, TTL extraction from RestMeta.GRAPHS,
statistics collection, and cache management operations.

Key Features:
- Generate consistent cache keys for model instances
- Extract cache_ttl from RestMeta.GRAPHS configuration (defaults to 0)
- Collect statistics from all cache backends
- Provide cache management operations
- Thread-safe utility functions
"""

import time
from typing import Any, Optional, Dict, Union
from django.db.models import Model, QuerySet

# Use logit with graceful fallback
try:
    from mojo.helpers import logit
    logger = logit.get_logger("cache_utils", "cache_utils.log")
except Exception:
    import logging
    logger = logging.getLogger("cache_utils")


def get_cache_key(instance: Model, graph: str = "default") -> Optional[str]:
    """
    Generate consistent cache key for model instance and graph.

    Format: "ModelName_pk_graph"
    Example: "Event_123_default", "User_456_list"

    :param instance: Django model instance
    :param graph: Graph configuration name
    :return: Cache key string or None if instance has no PK
    """
    # Fast check - pk should always exist on Django models
    pk = instance.pk
    if pk is None:
        return None

    # Use string concatenation for better performance than f-strings
    return instance.__class__.__name__ + "_" + str(pk) + "_" + graph


def get_model_cache_ttl(instance: Model, graph: str = "default") -> int:
    """
    Extract cache TTL from model's RestMeta.GRAPHS configuration.

    Returns 0 (no caching) by default for safety.

    :param instance: Django model instance
    :param graph: Graph configuration name
    :return: TTL in seconds (0 = no caching)
    """
    # Fast path: check for RestMeta existence
    rest_meta = getattr(instance, 'RestMeta', None)
    if rest_meta is None:
        return 0

    # Fast path: check for GRAPHS
    graphs = getattr(rest_meta, 'GRAPHS', None)
    if graphs is None:
        return 0
    if not isinstance(graphs, dict):
        logit.error("Invalid GRAPHS configuration", f"{graphs}")
        return 0

    # Get specific graph configuration
    graph_config = graphs.get(graph)
    if graph_config is None:
        # Try default graph as fallback
        graph_config = graphs.get('default')
        if graph_config is None:
            return 0

    # Extract cache_ttl, default to 0 for safety
    ttl = graph_config.get('cache_ttl', 0)

    # Ensure TTL is a valid integer
    if not isinstance(ttl, int) or ttl < 0:
        return 0

    return ttl


def get_cache_stats() -> Dict[str, Any]:
    """
    Get comprehensive cache statistics from all backends.

    :return: Dictionary with cache statistics
    """
    try:
        from .backends import get_cache_backend

        backend = get_cache_backend()
        stats = backend.stats()

        # Add timestamp for monitoring
        stats['timestamp'] = time.time()
        stats['timestamp_human'] = time.strftime('%Y-%m-%d %H:%M:%S')

        return stats

    except Exception as e:
        logger.error(f"Error getting cache statistics: {e}")
        return {
            'error': str(e),
            'timestamp': time.time()
        }


def clear_all_caches() -> bool:
    """
    Clear all cached items from the cache backend.

    :return: True if successful
    """
    try:
        from .backends import get_cache_backend, reset_cache_backend

        backend = get_cache_backend()
        result = backend.clear()

        # Reset the backend to clear any in-memory state
        reset_cache_backend()

        logger.info("All serializer caches cleared")
        return result

    except Exception as e:
        logger.error(f"Error clearing caches: {e}")
        return False


def warm_cache(instances: Union[Model, QuerySet, list], graph: str = "default") -> Dict[str, Any]:
    """
    Pre-warm the cache with serialized instances.

    Useful for warming cache after deployments or during low-traffic periods.

    :param instances: Model instance, QuerySet, or list of instances
    :param graph: Graph configuration to use
    :return: Dictionary with warming statistics
    """
    from ..serializer import OptimizedGraphSerializer  # Avoid circular import

    start_time = time.time()
    warmed_count = 0
    error_count = 0

    # Handle different input types
    if isinstance(instances, Model):
        instances = [instances]
    elif isinstance(instances, QuerySet):
        instances = list(instances)

    try:
        # Serialize each instance to warm the cache
        for instance in instances:
            try:
                serializer = OptimizedGraphSerializer(instance, graph=graph)
                serializer.serialize()  # This will cache the result
                warmed_count += 1
            except Exception as e:
                logger.warning(f"Error warming cache for {instance}: {e}")
                error_count += 1

        duration = time.time() - start_time

        stats = {
            'warmed_count': warmed_count,
            'error_count': error_count,
            'duration': duration,
            'objects_per_second': warmed_count / duration if duration > 0 else 0,
            'graph': graph
        }

        logger.info(f"Cache warming completed: {warmed_count} objects in {duration:.2f}s")
        return stats

    except Exception as e:
        logger.error(f"Cache warming failed: {e}")
        return {
            'error': str(e),
            'warmed_count': warmed_count,
            'error_count': error_count
        }


def invalidate_model_cache(model_class, instance_pk: Any = None, graph: str = None):
    """
    Invalidate cache entries for a specific model.

    :param model_class: Django model class
    :param instance_pk: Specific instance PK to invalidate (None = all instances)
    :param graph: Specific graph to invalidate (None = all graphs)
    """
    try:
        from .backends import get_cache_backend

        backend = get_cache_backend()
        model_name = model_class.__name__

        # Get current cache stats to see what we're working with
        stats = backend.stats()
        if stats.get('current_size', 0) == 0:
            return  # Nothing to invalidate

        deleted_count = 0

        if instance_pk is not None and graph is not None:
            # Invalidate specific instance + graph
            cache_key = f"{model_name}_{instance_pk}_{graph}"
            if backend.delete(cache_key):
                deleted_count += 1
        else:
            # We need to iterate through cache to find matching keys
            # Note: This is not efficient for large caches, but necessary without key scanning
            logger.warning(f"Bulk invalidation not optimized for {model_name}")
            # For now, just clear all caches
            # TODO: Implement more efficient bulk invalidation when we add Redis
            if backend.clear():
                logger.info(f"Cleared all caches due to {model_name} invalidation")

        if deleted_count > 0:
            logger.info(f"Invalidated {deleted_count} cache entries for {model_name}")

    except Exception as e:
        logger.error(f"Error invalidating cache for {model_class.__name__}: {e}")


def get_cache_info() -> Dict[str, Any]:
    """
    Get comprehensive cache information including configuration and status.

    :return: Dictionary with cache information
    """
    try:
        from django.conf import settings
        from .backends import get_cache_backend

        # Get backend info
        backend = get_cache_backend()
        stats = backend.stats()

        # Get configuration
        cache_config = getattr(settings, 'MOJO_SERIALIZER_CACHE', {})

        return {
            'backend_type': stats.get('backend', 'unknown'),
            'configuration': cache_config,
            'statistics': stats,
            'memory_usage_estimate': _estimate_memory_usage(stats),
            'recommendations': _get_cache_recommendations(stats)
        }

    except Exception as e:
        logger.error(f"Error getting cache info: {e}")
        return {'error': str(e)}


def _estimate_memory_usage(stats: Dict[str, Any]) -> Dict[str, Any]:
    """
    Estimate memory usage based on cache statistics.

    :param stats: Cache statistics
    :return: Memory usage estimates
    """
    current_size = stats.get('current_size', 0)

    if current_size == 0:
        return {'estimated_mb': 0, 'estimated_bytes': 0}

    # Rough estimate: 2KB per cached object (JSON + overhead)
    estimated_bytes = current_size * 2048
    estimated_mb = estimated_bytes / (1024 * 1024)

    return {
        'estimated_bytes': estimated_bytes,
        'estimated_mb': round(estimated_mb, 2),
        'objects': current_size,
        'note': 'Rough estimate assuming ~2KB per cached object'
    }


def _get_cache_recommendations(stats: Dict[str, Any]) -> list:
    """
    Generate cache optimization recommendations based on statistics.

    :param stats: Cache statistics
    :return: List of recommendation strings
    """
    recommendations = []

    # Hit rate recommendations
    hit_rate = stats.get('hit_rate', 0)
    if hit_rate < 0.3:
        recommendations.append("Low cache hit rate (<30%). Consider increasing cache_ttl values.")
    elif hit_rate > 0.9:
        recommendations.append("Excellent cache hit rate (>90%). Cache is working very well.")

    # Eviction recommendations
    evictions = stats.get('evictions', 0)
    sets = stats.get('sets', 0)
    if evictions > 0 and sets > 0:
        eviction_rate = evictions / sets
        if eviction_rate > 0.2:
            recommendations.append(f"High eviction rate ({eviction_rate:.1%}). Consider increasing max_size.")

    # Error recommendations
    errors = stats.get('errors', 0)
    if errors > 0:
        recommendations.append(f"Cache errors detected ({errors}). Check logs for details.")

    # Expired items
    expired_items = stats.get('expired_items', 0)
    total_requests = stats.get('total_requests', 0)
    if expired_items > 0 and total_requests > 0:
        expired_rate = expired_items / total_requests
        if expired_rate > 0.1:
            recommendations.append(f"High expiration rate ({expired_rate:.1%}). Consider longer cache_ttl values.")

    if not recommendations:
        recommendations.append("Cache performance looks good!")

    return recommendations


def is_cacheable(instance: Model, graph: str = "default") -> bool:
    """
    Check if an instance is cacheable based on its RestMeta configuration.

    :param instance: Django model instance
    :param graph: Graph configuration name
    :return: True if cacheable (TTL > 0)
    """
    return get_model_cache_ttl(instance, graph) > 0


def debug_cache_key(instance: Model, graph: str = "default") -> Dict[str, Any]:
    """
    Debug information for cache key and configuration.

    Useful for troubleshooting caching issues.

    :param instance: Django model instance
    :param graph: Graph configuration name
    :return: Debug information dictionary
    """
    try:
        cache_key = get_cache_key(instance, graph)
        ttl = get_model_cache_ttl(instance, graph)

        # Check if already cached
        from .backends import get_cache_backend
        backend = get_cache_backend()
        cached_value = backend.get(cache_key) if cache_key else None

        return {
            'instance': f"{instance.__class__.__name__}(pk={instance.pk})",
            'graph': graph,
            'cache_key': cache_key,
            'ttl': ttl,
            'is_cacheable': ttl > 0,
            'is_cached': cached_value is not None,
            'has_rest_meta': hasattr(instance, 'RestMeta'),
            'has_graphs': hasattr(instance, 'RestMeta') and hasattr(instance.RestMeta, 'GRAPHS'),
            'available_graphs': list(instance.RestMeta.GRAPHS.keys()) if hasattr(instance, 'RestMeta') and hasattr(instance.RestMeta, 'GRAPHS') else []
        }

    except Exception as e:
        return {
            'error': str(e),
            'instance': str(instance),
            'graph': graph
        }


def test_json_performance(test_data: Any = None, iterations: int = 1000) -> Dict[str, Any]:
    """
    Test JSON serialization performance comparing ujson vs standard json.

    :param test_data: Data to serialize (uses default test data if None)
    :param iterations: Number of iterations to run
    :return: Performance comparison results
    """
    import time

    # Default test data if none provided
    if test_data is None:
        test_data = {
            'id': 123,
            'name': 'Test Object',
            'created': time.time(),
            'active': True,
            'metadata': {
                'type': 'performance_test',
                'values': list(range(100)),
                'nested': {
                    'deep': {
                        'data': 'test_value' * 10
                    }
                }
            }
        }

    results = {
        'test_data_size': len(str(test_data)),
        'iterations': iterations,
        'ujson_available': False,
        'standard_json_time': 0.0,
        'ujson_time': 0.0,
        'speedup_ratio': 1.0,
        'recommendation': ''
    }

    # Test standard json
    import json as std_json
    start_time = time.perf_counter()
    for _ in range(iterations):
        json_str = std_json.dumps(test_data, default=str)
        std_json.loads(json_str)
    std_time = time.perf_counter() - start_time
    results['standard_json_time'] = std_time

    # Test ujson if available
    try:
        import ujson
        results['ujson_available'] = True

        start_time = time.perf_counter()
        for _ in range(iterations):
            json_str = ujson.dumps(test_data)
            ujson.loads(json_str)
        ujson_time = time.perf_counter() - start_time
        results['ujson_time'] = ujson_time

        # Calculate speedup
        if ujson_time > 0:
            results['speedup_ratio'] = std_time / ujson_time
            if results['speedup_ratio'] > 2.0:
                results['recommendation'] = f"ujson is {results['speedup_ratio']:.1f}x faster - consider using ujson for better performance"
            else:
                results['recommendation'] = f"ujson is {results['speedup_ratio']:.1f}x faster - moderate improvement"
        else:
            results['recommendation'] = "ujson time measurement failed"

    except ImportError:
        results['ujson_available'] = False
        results['ujson_time'] = 0
        results['speedup_ratio'] = 1.0
        results['recommendation'] = "ujson not available - install with 'pip install ujson' for better performance"

    return results
