"""
Cache Backend Factory for Django-MOJO Serializers

Provides factory functions to create appropriate cache backends based on configuration.
Handles backend selection, configuration validation, and graceful fallback behavior.

Supported Backends:
- memory: In-memory LRU cache with TTL support
- redis: Distributed Redis-based caching
- disabled: No-op cache for development/testing

Factory Functions:
- get_cache_backend(): Get configured cache backend instance
- create_cache_backend(): Create specific backend type
- validate_cache_config(): Validate configuration parameters

Configuration via Django Settings:
    MOJO_SERIALIZER_CACHE = {
        'backend': 'memory',  # 'memory', 'redis', 'disabled'
        'memory': {
            'max_size': 5000,
            'enable_stats': True
        },
        'redis': {
            'host': 'localhost',
            'port': 6379,
            'db': 2,
            'key_prefix': 'mojo:serializer:'
        }
    }
"""

import threading
from typing import Dict, Any, Optional
from django.conf import settings

# Use logit with graceful fallback
try:
    from mojo.helpers import logit
    logger = logit.get_logger("cache_backends", "cache_backends.log")
except Exception:
    import logging
    logger = logging.getLogger("cache_backends")

from .base import CacheBackend
from .memory import MemoryCacheBackend
from .disabled import DisabledCacheBackend

# Check ujson availability for optimal JSON performance
try:
    import ujson
    HAS_UJSON = True
    UJSON_VERSION = getattr(ujson, '__version__', 'unknown')
except ImportError:
    ujson = None
    HAS_UJSON = False
    UJSON_VERSION = None

# Redis backend with graceful import fallback
try:
    from .redis import RedisCacheBackend
    HAS_REDIS_BACKEND = True
except ImportError:
    RedisCacheBackend = None
    HAS_REDIS_BACKEND = False



# Global cache backend instance (lazy initialization)
_cache_backend_instance = None
_backend_lock = threading.RLock()

# Default configuration
DEFAULT_CACHE_CONFIG = {
    'backend': 'memory',
    'memory': {
        'max_size': 5000,
        'enable_stats': True
    },
    'redis': {
        'host': 'localhost',
        'port': 6379,
        'db': 0,
        'key_prefix': 'mojo:serializer:',
        'socket_timeout': 1.0,
        'socket_connect_timeout': 1.0,
        'enable_stats': True
    },
    'disabled': {}
}


def get_cache_backend() -> CacheBackend:
    """
    Get the configured cache backend instance.

    Uses lazy initialization and returns the same instance across calls.
    Configuration is read from Django settings with sensible defaults.
    Thread-safe singleton pattern.

    :return: Configured cache backend instance
    """
    global _cache_backend_instance

    with _backend_lock:
        if _cache_backend_instance is None:
            _cache_backend_instance = create_cache_backend()

        return _cache_backend_instance


def create_cache_backend(backend_type: Optional[str] = None, **kwargs) -> CacheBackend:
    """
    Create cache backend instance based on type and configuration.

    :param backend_type: Backend type ('memory', 'redis', 'disabled') or None for auto-detection
    :param kwargs: Override configuration parameters
    :return: Cache backend instance
    """
    # Get configuration from Django settings
    cache_config = getattr(settings, 'MOJO_SERIALIZER_CACHE', {})
    config = {**DEFAULT_CACHE_CONFIG, **cache_config}

    # Determine backend type
    if backend_type is None:
        backend_type = config.get('backend', 'memory').lower()

    # Override with any provided kwargs
    if kwargs:
        config.update(kwargs)

    # Validate configuration
    validated_config = validate_cache_config(config, backend_type)

    # Create appropriate backend
    if backend_type == 'disabled':
        logger.info("Creating disabled cache backend")
        return DisabledCacheBackend()

    elif backend_type == 'redis':
        return _create_redis_backend(validated_config)

    elif backend_type == 'memory':
        return _create_memory_backend(validated_config)

    else:
        logger.warning(f"Unknown cache backend type '{backend_type}', falling back to memory")
        return _create_memory_backend(validated_config)


def _create_redis_backend(config: Dict[str, Any]) -> CacheBackend:
    """
    Create Redis cache backend with configuration validation and fallback.
    """
    if not HAS_REDIS_BACKEND:
        logger.warning(
            "Redis backend requested but redis package not available. "
            "Install with: pip install redis. Falling back to memory cache."
        )
        return _create_memory_backend(config)

    redis_config = config.get('redis', {})

    try:
        logger.info(f"Creating Redis cache backend: {redis_config.get('host', 'localhost')}:{redis_config.get('port', 6379)}")
        if HAS_UJSON:
            logger.info(f"Using ujson {UJSON_VERSION} for optimal JSON performance")
        else:
            logger.warning("ujson not available - using standard json (slower performance)")

        backend = RedisCacheBackend(**redis_config)

        # Test connection
        if not backend.ping():
            raise ConnectionError("Redis ping failed")

        logger.info("Redis cache backend successfully initialized")
        return backend

    except Exception as e:
        logger.error(f"Failed to create Redis cache backend: {e}")
        logger.info("Falling back to memory cache backend")
        return _create_memory_backend(config)


def _create_memory_backend(config: Dict[str, Any]) -> CacheBackend:
    """
    Create memory cache backend with configuration.
    """
    memory_config = config.get('memory', {})
    max_size = memory_config.get('max_size', 5000)
    enable_stats = memory_config.get('enable_stats', True)

    logger.info(f"Creating memory cache backend with max_size={max_size}")
    if HAS_UJSON:
        logger.info(f"Using ujson {UJSON_VERSION} for optimal JSON performance")
    else:
        logger.warning("ujson not available - using standard json (slower performance)")

    return MemoryCacheBackend(max_size=max_size, enable_stats=enable_stats)


def validate_cache_config(config: Dict[str, Any], backend_type: str) -> Dict[str, Any]:
    """
    Validate and normalize cache configuration.

    :param config: Raw configuration dictionary
    :param backend_type: Backend type to validate for
    :return: Validated configuration with defaults applied
    """
    validated = config.copy()

    # Validate backend type
    valid_backends = ['memory', 'redis', 'disabled']
    if backend_type not in valid_backends:
        logger.warning(f"Invalid backend type '{backend_type}', using 'memory'")
        validated['backend'] = 'memory'
        backend_type = 'memory'

    # Backend-specific validation
    if backend_type == 'memory':
        memory_config = validated.setdefault('memory', {})
        memory_config.setdefault('max_size', 5000)
        memory_config.setdefault('enable_stats', True)

        # Validate max_size
        max_size = memory_config.get('max_size')
        if not isinstance(max_size, int) or max_size <= 0:
            logger.warning(f"Invalid memory max_size '{max_size}', using 5000")
            memory_config['max_size'] = 5000

    elif backend_type == 'redis':
        redis_config = validated.setdefault('redis', {})

        # Set defaults
        redis_config.setdefault('host', 'localhost')
        redis_config.setdefault('port', 6379)
        redis_config.setdefault('db', 0)
        redis_config.setdefault('key_prefix', 'mojo:serializer:')
        redis_config.setdefault('socket_timeout', 1.0)
        redis_config.setdefault('socket_connect_timeout', 1.0)
        redis_config.setdefault('enable_stats', True)

        # Validate port
        port = redis_config.get('port')
        if not isinstance(port, int) or port <= 0 or port > 65535:
            logger.warning(f"Invalid Redis port '{port}', using 6379")
            redis_config['port'] = 6379

        # Validate db
        db = redis_config.get('db')
        if not isinstance(db, int) or db < 0:
            logger.warning(f"Invalid Redis db '{db}', using 0")
            redis_config['db'] = 0

        # Validate timeouts
        for timeout_key in ['socket_timeout', 'socket_connect_timeout']:
            timeout = redis_config.get(timeout_key)
            if not isinstance(timeout, (int, float)) or timeout <= 0:
                logger.warning(f"Invalid Redis {timeout_key} '{timeout}', using 1.0")
                redis_config[timeout_key] = 1.0

        # Ensure key_prefix ends with colon
        key_prefix = redis_config.get('key_prefix', '')
        if key_prefix and not key_prefix.endswith(':'):
            redis_config['key_prefix'] = f"{key_prefix}:"

    return validated


def reset_cache_backend():
    """
    Reset the global cache backend instance.

    Forces recreation of the backend on next access. Useful for testing
    or when configuration changes require a fresh backend instance.
    """
    global _cache_backend_instance

    with _backend_lock:
        if _cache_backend_instance:
            try:
                _cache_backend_instance.clear()
                if hasattr(_cache_backend_instance, 'close'):
                    _cache_backend_instance.close()
            except Exception as e:
                logger.warning(f"Error cleaning up old cache backend: {e}")

        _cache_backend_instance = None
        logger.info("Cache backend reset - will be recreated on next access")


def get_available_backends() -> Dict[str, Dict[str, Any]]:
    """
    Get information about available cache backends.

    :return: Dictionary with backend availability and capabilities
    """
    backends = {
        'memory': {
            'available': True,
            'description': 'In-memory LRU cache with TTL support',
            'features': ['LRU eviction', 'TTL support', 'Thread-safe', 'Statistics'],
            'suitable_for': ['development', 'single-server', 'testing']
        },
        'disabled': {
            'available': True,
            'description': 'No-op cache that disables caching',
            'features': ['Zero overhead', 'Debug-friendly'],
            'suitable_for': ['development', 'testing', 'debugging']
        },
        'redis': {
            'available': HAS_REDIS_BACKEND,
            'description': 'Distributed Redis-based caching',
            'features': ['Distributed', 'Persistent', 'Native TTL', 'High performance'],
            'suitable_for': ['production', 'multi-server', 'high-availability'],
            'requirements': ['redis package', 'Redis server'],
            'ujson_available': HAS_UJSON,
            'ujson_version': UJSON_VERSION
        }
    }

    if not HAS_REDIS_BACKEND:
        backends['redis']['error'] = 'Redis package not installed'

    # Add ujson info to memory backend as well
    backends['memory']['ujson_available'] = HAS_UJSON
    backends['memory']['ujson_version'] = UJSON_VERSION

    # Add performance note
    if not HAS_UJSON:
        performance_note = 'Install ujson for 2-5x faster JSON serialization: pip install ujson'
        backends['memory']['performance_note'] = performance_note
        backends['redis']['performance_note'] = performance_note

    return backends


def test_backend_connectivity(backend_type: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Test connectivity and functionality of a cache backend.

    :param backend_type: Backend type to test
    :param config: Optional configuration override
    :return: Test results dictionary
    """
    test_results = {
        'backend': backend_type,
        'available': False,
        'connectivity': False,
        'functionality': False,
        'performance': {},
        'errors': []
    }

    try:
        # Create temporary backend instance
        if config:
            backend = create_cache_backend(backend_type, **config)
        else:
            # Use existing backend if it matches type
            existing_backend = get_cache_backend()
            if existing_backend.stats().get('backend') == backend_type:
                backend = existing_backend
            else:
                backend = create_cache_backend(backend_type)

        test_results['available'] = True

        # Test connectivity
        if hasattr(backend, 'ping'):
            test_results['connectivity'] = backend.ping()
        else:
            test_results['connectivity'] = True

        if test_results['connectivity']:
            # Test basic functionality
            import time
            test_key = f"test_key_{int(time.time())}"
            test_value = {'test': True, 'timestamp': time.time()}

            # Test set/get cycle
            start_time = time.perf_counter()
            set_success = backend.set(test_key, test_value, ttl=60)
            set_time = time.perf_counter() - start_time

            if set_success:
                start_time = time.perf_counter()
                retrieved_value = backend.get(test_key)
                get_time = time.perf_counter() - start_time

                if retrieved_value == test_value:
                    test_results['functionality'] = True
                    test_results['performance'] = {
                        'set_time_ms': round(set_time * 1000, 2),
                        'get_time_ms': round(get_time * 1000, 2)
                    }
                else:
                    test_results['errors'].append("Retrieved value doesn't match set value")

                # Cleanup
                backend.delete(test_key)
            else:
                test_results['errors'].append("Failed to set test value")
        else:
            test_results['errors'].append("Backend connectivity test failed")

    except Exception as e:
        test_results['errors'].append(f"Backend test failed: {str(e)}")
        logger.error(f"Error testing {backend_type} backend: {e}")

    return test_results


def get_cache_health() -> Dict[str, Any]:
    """
    Get comprehensive cache system health information.

    :return: Health status dictionary
    """
    try:
        backend = get_cache_backend()
        stats = backend.stats()

        # Calculate health score based on various metrics
        health_score = 100
        issues = []

        # Check hit rate
        hit_rate = stats.get('hit_rate', 0)
        if hit_rate < 0.5:
            health_score -= 20
            issues.append(f"Low cache hit rate: {hit_rate:.1%}")

        # Check error rate
        errors = stats.get('errors', 0)
        total_ops = stats.get('sets', 0) + stats.get('hits', 0) + stats.get('misses', 0)
        if total_ops > 0:
            error_rate = errors / total_ops
            if error_rate > 0.05:  # 5% error rate
                health_score -= 30
                issues.append(f"High error rate: {error_rate:.1%}")

        # Check connectivity for Redis
        if stats.get('backend') == 'redis':
            if hasattr(backend, 'ping') and not backend.ping():
                health_score -= 50
                issues.append("Redis connectivity issues")

        # Determine overall health status
        if health_score >= 90:
            status = 'excellent'
        elif health_score >= 70:
            status = 'good'
        elif health_score >= 50:
            status = 'fair'
        else:
            status = 'poor'

        return {
            'status': status,
            'health_score': health_score,
            'backend_type': stats.get('backend', 'unknown'),
            'statistics': stats,
            'issues': issues,
            'recommendations': _get_health_recommendations(stats, issues)
        }

    except Exception as e:
        logger.error(f"Error getting cache health: {e}")
        return {
            'status': 'error',
            'health_score': 0,
            'error': str(e)
        }


def _get_health_recommendations(stats: Dict[str, Any], issues: list) -> list:
    """
    Generate health recommendations based on statistics and issues.
    """
    recommendations = []

    # Hit rate recommendations
    hit_rate = stats.get('hit_rate', 0)
    if hit_rate < 0.3:
        recommendations.append("Consider increasing cache TTL values in RestMeta.GRAPHS")
    elif hit_rate < 0.5:
        recommendations.append("Review caching strategy - some models may benefit from longer TTL")

    # Memory recommendations for memory backend
    if stats.get('backend') == 'memory':
        utilization = stats.get('utilization', 0)
        if utilization > 0.9:
            recommendations.append("Memory cache is near capacity - consider increasing max_size")

        evictions = stats.get('evictions', 0)
        if evictions > 100:
            recommendations.append("High eviction count - consider increasing cache size")

    # Redis-specific recommendations
    elif stats.get('backend') == 'redis':
        if 'Redis connectivity issues' in issues:
            recommendations.append("Check Redis server status and network connectivity")

        redis_evictions = stats.get('redis_evicted_keys', 0)
        if redis_evictions > 0:
            recommendations.append("Redis is evicting keys - consider increasing Redis maxmemory")

    # General recommendations
    errors = stats.get('errors', 0)
    if errors > 10:
        recommendations.append("Multiple cache errors detected - check logs for details")

    if not recommendations:
        recommendations.append("Cache system is performing well!")

    return recommendations
