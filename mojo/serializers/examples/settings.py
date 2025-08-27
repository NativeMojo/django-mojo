"""
Django-MOJO Serializer Settings Configuration Examples

This file demonstrates how to configure the MOJO serializer system through Django settings.
Add the desired configurations to your project's settings.py file.

The serializer system supports:
- Multiple serializer backends (simple, optimized, advanced, custom)
- Performance tuning and caching configuration
- Runtime serializer switching
- Custom serializer registration
- Monitoring and debugging options
"""

# =============================================================================
# BASIC SERIALIZER CONFIGURATION
# =============================================================================

# Default serializer to use throughout the application
# Options: 'simple', 'optimized', 'advanced', or any custom registered name
MOJO_DEFAULT_SERIALIZER = 'optimized'

# Enable/disable performance tracking for serializers
MOJO_SERIALIZER_PERFORMANCE_TRACKING = True

# Enable debug logging for serializer operations
MOJO_SERIALIZER_DEBUG = False

# =============================================================================
# CUSTOM SERIALIZER REGISTRATION
# =============================================================================

# Register custom serializers
# Format: {'name': 'import.path.to.SerializerClass'} or {'name': config_dict}
MOJO_CUSTOM_SERIALIZERS = {
    # Simple string path registration
    'my_custom': 'myapp.serializers.CustomGraphSerializer',

    # Detailed configuration with metadata
    'enterprise': {
        'class': 'myapp.serializers.EnterpriseSerializer',
        'description': 'Enterprise-grade serializer with audit logging',
        'is_default': False
    },

    # Redis-backed caching serializer
    'redis_cached': {
        'class': 'myapp.serializers.RedisCachedSerializer',
        'description': 'Serializer with Redis-based distributed caching',
        'is_default': False
    }
}

# =============================================================================
# PERFORMANCE AND CACHING CONFIGURATION
# =============================================================================

# Optimized serializer cache settings
MOJO_OPTIMIZED_SERIALIZER = {
    # Instance cache settings (model+graph+pk -> serialized data)
    'instance_cache_size': 5000,      # Maximum cached instances
    'instance_cache_ttl': 300,        # Time-to-live in seconds (5 minutes)

    # Graph configuration cache (model+graph -> compiled config)
    'graph_cache_size': 500,          # Maximum cached graph configs
    'graph_cache_ttl': 600,           # Time-to-live in seconds (10 minutes)

    # Query optimization cache
    'query_cache_size': 500,          # Maximum cached query optimizations
    'query_cache_ttl': 600,           # Time-to-live in seconds (10 minutes)

    # Performance monitoring
    'enable_performance_stats': True,  # Track performance metrics
    'stats_history_size': 1000,       # Number of operations to track
}

# Advanced serializer settings (if using advanced serializer)
MOJO_ADVANCED_SERIALIZER = {
    # Multi-format support
    'enable_csv_export': True,
    'enable_excel_export': True,
    'enable_html_debug': True,

    # Excel export settings
    'excel_max_rows': 100000,         # Maximum rows for Excel export
    'excel_auto_width': True,         # Auto-adjust column widths
    'excel_freeze_panes': True,       # Freeze header row

    # CSV export settings
    'csv_streaming_threshold': 1000,  # Use streaming for > N rows
    'csv_delimiter': ',',             # CSV field delimiter
    'csv_encoding': 'utf-8',          # CSV file encoding

    # Performance settings
    'enable_caching': True,
    'cache_timeout': 300,             # Cache timeout in seconds
}

# =============================================================================
# DEVELOPMENT AND DEBUGGING
# =============================================================================

# Development-specific settings
if DEBUG:
    # Enable detailed logging in development
    MOJO_SERIALIZER_DEBUG = True

    # Use smaller cache sizes for development
    MOJO_OPTIMIZED_SERIALIZER['instance_cache_size'] = 1000
    MOJO_OPTIMIZED_SERIALIZER['graph_cache_size'] = 100

    # Enable performance benchmarking
    MOJO_ENABLE_SERIALIZER_BENCHMARKS = True

# Production optimizations
else:
    # Disable debug logging in production
    MOJO_SERIALIZER_DEBUG = False

    # Increase cache sizes for production
    MOJO_OPTIMIZED_SERIALIZER['instance_cache_size'] = 10000
    MOJO_OPTIMIZED_SERIALIZER['graph_cache_size'] = 1000

    # Disable benchmarking in production
    MOJO_ENABLE_SERIALIZER_BENCHMARKS = False

# =============================================================================
# REDIS INTEGRATION (Optional)
# =============================================================================

# Redis-backed caching for distributed environments
MOJO_REDIS_SERIALIZER_CACHE = {
    'enabled': False,                  # Enable Redis caching
    'host': 'localhost',
    'port': 6379,
    'db': 2,                          # Use separate DB for serializer cache
    'password': None,
    'key_prefix': 'mojo:serializer:', # Cache key prefix
    'default_timeout': 300,           # Default cache timeout
    'connection_pool_kwargs': {
        'max_connections': 50
    }
}

# =============================================================================
# MONITORING AND ALERTS
# =============================================================================

# Performance monitoring thresholds
MOJO_SERIALIZER_MONITORING = {
    'slow_serialization_threshold': 1.0,    # Log if serialization takes > 1 second
    'memory_usage_threshold': 100 * 1024 * 1024,  # Alert if using > 100MB memory
    'cache_hit_rate_threshold': 0.80,       # Alert if cache hit rate < 80%
    'error_rate_threshold': 0.05,           # Alert if error rate > 5%
}

# Integration with external monitoring systems
MOJO_EXTERNAL_MONITORING = {
    # StatsD integration
    'statsd': {
        'enabled': False,
        'host': 'localhost',
        'port': 8125,
        'prefix': 'mojo.serializer'
    },

    # Sentry integration for error tracking
    'sentry': {
        'enabled': False,
        'track_performance': True,
        'sample_rate': 0.1  # Sample 10% of operations
    }
}

# =============================================================================
# FEATURE FLAGS
# =============================================================================

# Feature flags for gradual rollout of new serializer features
MOJO_SERIALIZER_FEATURES = {
    'optimized_serializer_default': True,     # Use optimized serializer by default
    'automatic_query_optimization': True,     # Enable automatic select_related/prefetch_related
    'advanced_caching': True,                 # Enable advanced caching strategies
    'performance_monitoring': True,           # Enable detailed performance monitoring
    'async_serialization': False,             # Enable async serialization (experimental)
    'distributed_caching': False,             # Enable distributed caching (experimental)
}

# =============================================================================
# MODEL-SPECIFIC CONFIGURATIONS
# =============================================================================

# Override serializer settings for specific models
MOJO_MODEL_SERIALIZER_OVERRIDES = {
    'auth.User': {
        'default_serializer': 'simple',       # Use simple serializer for User model
        'cache_ttl': 60,                      # Shorter cache TTL for user data
        'enable_caching': True
    },

    'myapp.LargeDataModel': {
        'default_serializer': 'optimized',    # Use optimized serializer for large models
        'cache_ttl': 1800,                    # Longer cache TTL for stable data
        'enable_query_optimization': True
    },

    'reports.AnalyticsData': {
        'default_serializer': 'advanced',     # Use advanced serializer for reports
        'enable_csv_export': True,
        'enable_excel_export': True
    }
}

# =============================================================================
# LOGGING CONFIGURATION
# =============================================================================

# Add serializer-specific logging configuration to Django's LOGGING setting
SERIALIZER_LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'serializer': {
            'format': '[{levelname}] {asctime} {name} - {message}',
            'style': '{',
        },
    },
    'handlers': {
        'serializer_file': {
            'level': 'INFO',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': 'logs/serializer.log',
            'maxBytes': 10 * 1024 * 1024,  # 10MB
            'backupCount': 5,
            'formatter': 'serializer',
        },
        'performance_file': {
            'level': 'INFO',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': 'logs/serializer_performance.log',
            'maxBytes': 10 * 1024 * 1024,  # 10MB
            'backupCount': 3,
            'formatter': 'serializer',
        },
    },
    'loggers': {
        'optimized_serializer': {
            'handlers': ['serializer_file'],
            'level': 'INFO',
            'propagate': True,
        },
        'serializer_manager': {
            'handlers': ['serializer_file'],
            'level': 'INFO',
            'propagate': True,
        },
        'serializer_performance': {
            'handlers': ['performance_file'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}

# Merge serializer logging into main LOGGING configuration
# Note: In your actual settings.py, you would merge this into your existing LOGGING dict

# =============================================================================
# MIGRATION AND COMPATIBILITY
# =============================================================================

# Backwards compatibility settings
MOJO_SERIALIZER_COMPATIBILITY = {
    # Support legacy GraphSerializer API
    'legacy_graph_serializer_support': True,

    # Automatically migrate from simple to optimized serializer
    'auto_migrate_simple_to_optimized': True,

    # Warn about deprecated serializer features
    'deprecation_warnings': True,

    # Fallback to simple serializer on errors
    'fallback_on_errors': True
}

# =============================================================================
# EXAMPLE USAGE IN VIEWS/MODELS
# =============================================================================

"""
Example usage in your Django code:

# In views.py
from mojo.serializers import serialize, to_response

def my_api_view(request):
    queryset = MyModel.objects.all()

    # Use default configured serializer
    return to_response(queryset, request, graph="list")

    # Force specific serializer
    return to_response(queryset, request, graph="list", serializer_type="optimized")

# In models.py
class MyModel(MojoModel):
    name = models.CharField(max_length=100)

    class RestMeta:
        GRAPHS = {
            "default": {"fields": ["id", "name"]},
            "list": {"fields": ["id", "name"]},
            "detail": {"fields": ["id", "name", "created", "modified"]}
        }

# Management commands
# python manage.py serializer_admin list
# python manage.py serializer_admin benchmark --model MyModel --count 1000
# python manage.py serializer_admin stats
# python manage.py serializer_admin clear-cache
"""
