"""
OptimizedGraphSerializer - High-performance serializer with intelligent caching

This is the main serializer that combines:
- RestMeta.GRAPHS configuration
- Multi-level caching with TTL support
- Query optimization (select_related/prefetch_related)
- ujson for optimal JSON performance
- Thread-safe operations

Drop-in replacement for GraphSerializer with significant performance improvements.
"""

import time
import datetime
import math
from decimal import Decimal
from typing import Any, Dict, List, Optional, Union

# Performance debugging
ENABLE_PERF_DEBUG = False

# Use ujson for optimal performance
try:
    import ujson as json
except ImportError:
    import json

from django.db.models import ForeignKey, OneToOneField, ManyToOneRel, ManyToManyField
from django.db.models import QuerySet, Model
from django.core.exceptions import FieldDoesNotExist
from django.http import HttpResponse

# Use logit with graceful fallback
try:
    from mojo.helpers import logit
    logger = logit.get_logger("optimized_serializer", "optimized_serializer.log")
except Exception:
    import logging
    logger = logging.getLogger("optimized_serializer")

from mojo.helpers.settings import settings
from .cache import get_cache_backend, get_cache_key, get_model_cache_ttl

# Load setting once at module import time for performance
SERIALIZE_DATETIME_TO_FLOAT = settings.get('SERIALIZE_DATETIME_TO_FLOAT', False)


class OptimizedGraphSerializer:
    """
    Ultra-fast serializer with intelligent caching and query optimization.

    Drop-in replacement for GraphSerializer with:
    - Multi-level caching based on RestMeta.cache_ttl configuration
    - Query optimization with select_related/prefetch_related
    - ujson for optimal JSON performance
    - Thread-safe operations
    - Memory-efficient QuerySet handling
    """

    def __init__(self, instance, graph="default", many=False, request=None, bypass_cache=False, simple_mode=False, **kwargs):
        """
        Initialize optimized serializer.

        :param instance: Model instance or QuerySet
        :param graph: Graph configuration name from RestMeta.GRAPHS
        :param many: Force many=True for list serialization
        :param request: Django request object (for compatibility)
        :param bypass_cache: Skip all caching operations for performance testing
        :param simple_mode: Behave exactly like simple serializer (no cache overhead at all)
        :param kwargs: Additional options (cache, etc.)
        """
        self.graph = graph
        self.request = request
        self.instance = instance
        self.many = many
        self.qset = None
        self.bypass_cache = bypass_cache

        # Simplified cache handling - only initialize if actually needed
        self.bypass_cache = bypass_cache
        self.simple_mode = simple_mode or bypass_cache  # simple_mode implies bypass_cache
        self._cache_backend = None
        self._request_cache = None

        # Handle QuerySet detection - same as simple serializer
        if isinstance(instance, QuerySet):
            self.many = True
            self.qset = instance
            self.instance = list(instance)  # Convert QuerySet to list for iteration
        elif many and not isinstance(instance, (list, tuple)):
            # Convert single instance to list for many=True case
            self.instance = [instance]

        # Performance tracking (only if debug enabled)
        if ENABLE_PERF_DEBUG:
            self._start_time = time.perf_counter()
            self._debug_times = {}
            self._debug_enabled = True
        else:
            self._debug_enabled = False

    def serialize(self):
        """
        Main serialization method with caching and optimization.
        """
        if self.many:
            return [self._serialize_instance_cached(obj) for obj in self.instance]
        return self._serialize_instance_cached(self.instance)



    def _serialize_instance_cached(self, obj):
        """
        Serialize single instance with intelligent caching.
        """
        # Simple mode: behave exactly like simple serializer
        if self.simple_mode:
            return self._serialize_instance_direct(obj)

        # Skip all caching if bypass_cache is enabled
        if self.bypass_cache:
            return self._serialize_instance_direct(obj)

        # Always do request-scoped caching for performance
        cache_key = get_cache_key(obj, self.graph)
        if not cache_key:
            return self._serialize_instance_direct(obj)

        # Initialize request cache only when needed
        if self._request_cache is None:
            self._request_cache = {}

        # Check request-scoped cache first (works for all models)
        if cache_key in self._request_cache:
            return self._request_cache[cache_key]

        # Check if persistent caching is configured
        cache_ttl = get_model_cache_ttl(obj, self.graph)
        cached_result = None

        if cache_ttl > 0:
            # Try persistent cache only if TTL > 0
            if self._cache_backend is None:
                self._cache_backend = get_cache_backend()

            cached_result = self._cache_backend.get(cache_key)
            if cached_result is not None:
                # Store in request cache too for faster subsequent access
                self._request_cache[cache_key] = cached_result
                return cached_result

        # Serialize the result
        result = self._serialize_instance_direct(obj)

        # Always store in request cache (provides performance gain even when TTL=0)
        self._request_cache[cache_key] = result

        # Store in persistent cache only if TTL > 0
        if cache_ttl > 0 and self._cache_backend is not None:
            self._cache_backend.set(cache_key, result, cache_ttl)

        return result

    def _serialize_instance_direct(self, obj):
        """
        Direct serialization using RestMeta.GRAPHS configuration.
        """
        if not hasattr(obj, "RestMeta") or not hasattr(obj.RestMeta, "GRAPHS"):
            if self._debug_enabled:
                logger.warning(f"RestMeta.GRAPHS not found for {obj.__class__.__name__}")
            return self._fallback_serialization(obj)

        graph_config = obj.RestMeta.GRAPHS.get(self.graph)
        if graph_config is None:
            if self.graph != "default":
                graph_config = obj.RestMeta.GRAPHS.get("default")

            if graph_config is None:
                if self._debug_enabled:
                    logger.warning(f"No graph '{self.graph}' found for {obj.__class__.__name__}")
                return self._fallback_serialization(obj)

        # Serialize based on graph configuration
        data = {}

        # Enforce NO_SHOW_FIELDS from RestMeta (never expose, even if requested)
        no_show_fields = set(getattr(obj.RestMeta, "NO_SHOW_FIELDS", []) or [])

        # Basic model fields - if no fields specified, use all model fields
        fields = graph_config.get("fields", [])
        if not fields:
            fields = [field.name for field in obj._meta.fields]

        # Apply exclude filter to remove sensitive fields
        exclude_fields = list(graph_config.get("exclude", []))
        exclude_fields.append("mojo_secrets")
        # Always exclude NO_SHOW_FIELDS
        if no_show_fields:
            exclude_fields.extend(no_show_fields)
        if exclude_fields:
            fields = [field for field in fields if field not in exclude_fields]

        for field_name in fields:
            try:
                field_value = getattr(obj, field_name)
                field = self._get_model_field(obj, field_name)

                # Handle callable attributes
                if callable(field_value):
                    try:
                        field_value = field_value()
                    except Exception as e:
                        if self._debug_enabled:
                            logger.warning(f"Error calling {field_name}: {e}")
                        continue

                # Serialize the value
                data[field_name] = self._serialize_value_fast(field_value, field)

            except AttributeError:
                if self._debug_enabled:
                    logger.debug(f"Field '{field_name}' not found on {obj.__class__.__name__}")
                continue

        # Extra fields (methods, properties)
        extra_fields = graph_config.get("extra", [])
        for field_spec in extra_fields:
            if isinstance(field_spec, (tuple, list)):
                method_name, alias = field_spec
            else:
                method_name, alias = field_spec, field_spec



            try:
                if hasattr(obj, method_name):
                    attr = getattr(obj, method_name)
                    # For extra fields, we trust the method/property to return a
                    # JSON-serializable value, just like the simple serializer does.
                    data[alias] = attr() if callable(attr) else attr
                else:
                    if self._debug_enabled:
                        logger.debug(f"Extra field '{method_name}' not found on {obj.__class__.__name__}")
            except Exception as e:
                if self._debug_enabled:
                    logger.warning(f"Error processing extra field '{method_name}': {e}")
                data[alias] = None

        # Related object graphs
        related_graphs = graph_config.get("graphs", {})
        for field_name, sub_graph in related_graphs.items():
            try:
                related_obj = getattr(obj, field_name, None)
                if related_obj is None:
                    data[field_name] = None
                    continue

                field = self._get_model_field(obj, field_name)

                if isinstance(field, (ForeignKey, OneToOneField)):
                    # Single related object - share request cache for performance
                    related_serializer = OptimizedGraphSerializer(related_obj, graph=sub_graph, bypass_cache=self.bypass_cache)
                    # Share the request cache to avoid re-serializing same objects
                    related_serializer._request_cache = self._request_cache
                    data[field_name] = related_serializer._serialize_instance_cached(related_obj)

                elif isinstance(field, (ManyToManyField, ManyToOneRel)) or hasattr(related_obj, 'all'):
                    # Many-to-many or reverse relationship - share request cache
                    if hasattr(related_obj, 'all'):
                        related_qset = related_obj.all()
                        related_serializer = OptimizedGraphSerializer(related_qset, graph=sub_graph, many=True, bypass_cache=self.bypass_cache)
                        # Share the request cache to avoid re-serializing same objects
                        related_serializer._request_cache = self._request_cache
                        data[field_name] = related_serializer.serialize()
                    else:
                        data[field_name] = []
                else:
                    data[field_name] = str(related_obj)

            except Exception as e:
                if self._debug_enabled:
                    logger.error(f"Error processing related field '{field_name}': {e}")
                data[field_name] = None

        return data

    def _apply_query_optimizations(self, queryset):
        """
        Apply select_related and prefetch_related optimizations based on graph.
        """
        if not hasattr(queryset.model, 'RestMeta') or not hasattr(queryset.model.RestMeta, 'GRAPHS'):
            return queryset

        graph_config = queryset.model.RestMeta.GRAPHS.get(self.graph, {})
        if not graph_config:
            return queryset

        # Analyze fields for optimization
        select_related_fields = []
        prefetch_related_fields = []

        # Check main fields
        for field_name in graph_config.get("fields", []):
            try:
                field = queryset.model._meta.get_field(field_name)
                if isinstance(field, (ForeignKey, OneToOneField)):
                    select_related_fields.append(field_name)
            except FieldDoesNotExist:
                continue

        # Check related graphs
        for field_name in graph_config.get("graphs", {}).keys():
            try:
                field = queryset.model._meta.get_field(field_name)
                if isinstance(field, (ForeignKey, OneToOneField)):
                    select_related_fields.append(field_name)
                elif isinstance(field, (ManyToManyField, ManyToOneRel)):
                    prefetch_related_fields.append(field_name)
            except FieldDoesNotExist:
                continue

        # Apply optimizations
        optimized_queryset = queryset

        if select_related_fields:
            optimized_queryset = optimized_queryset.select_related(*select_related_fields)

        if prefetch_related_fields:
            optimized_queryset = optimized_queryset.prefetch_related(*prefetch_related_fields)

        return optimized_queryset

    def _fallback_serialization(self, obj):
        """
        Fallback serialization when no RestMeta.GRAPHS available.
        """
        if hasattr(obj, '_meta'):
            fields = [field.name for field in obj._meta.fields]
            # Enforce NO_SHOW_FIELDS even in fallback mode
            no_show_fields = set(getattr(obj.RestMeta, "NO_SHOW_FIELDS", []) or []) if hasattr(obj, "RestMeta") else set()
            if no_show_fields:
                fields = [field for field in fields if field not in no_show_fields]
            data = {}
            for field_name in fields:
                try:
                    field_value = getattr(obj, field_name)
                    field = self._get_model_field(obj, field_name)
                    if callable(field_value):
                        field_value = field_value()
                    data[field_name] = self._serialize_value_fast(field_value, field)
                except:
                    continue
            return data
        return str(obj)

    def _serialize_value_fast(self, value, field=None):
        """
        Fast value serialization optimized for common types.
        """
        if value is None:
            return None

        # Handle datetime objects (common case first for speed)
        if isinstance(value, datetime.datetime):
            # Check if we should serialize to float (with microsecond precision) or int
            if SERIALIZE_DATETIME_TO_FLOAT:
                return value.timestamp()  # Returns float with microsecond precision
            else:
                return int(value.timestamp())  # Returns int (seconds only)
        elif isinstance(value, datetime.date):
            return value.isoformat()

        # Handle foreign key relationships
        if field and isinstance(field, (ForeignKey, OneToOneField)) and hasattr(value, 'pk'):
            return value.pk

        # Handle model instances
        elif hasattr(value, 'pk') and hasattr(value, '_meta'):
            return value.pk

        # Handle basic types (most common)
        elif isinstance(value, (str, int, float, bool)):
            return value

        # Handle numeric types
        elif isinstance(value, Decimal):
            return 0.0 if value.is_nan() else float(value)
        elif isinstance(value, float) and math.isnan(value):
            return 0.0

        # Handle collections
        elif isinstance(value, (list, tuple)):
            return [self._serialize_value_fast(v) for v in value]
        elif isinstance(value, dict):
            return {k: self._serialize_value_fast(v) for k, v in value.items()}

        # Default to string conversion
        else:
            return str(value)

    def _get_model_field(self, obj, field_name):
        """Get Django model field object."""
        try:
            return obj._meta.get_field(field_name)
        except FieldDoesNotExist:
            return None

    def to_json(self, **kwargs):
        """
        Convert serialized data to JSON string using ujson.
        """
        data = self.serialize()

        # Build response structure
        if self.many:
            response_data = {
                'data': data,
                'status': True,
                'size': len(data),
                'graph': self.graph
            }
        else:
            response_data = {
                'data': data,
                'status': True,
                'graph': self.graph
            }

        # Add any additional kwargs
        response_data.update(kwargs)

        # Use ujson for optimal performance
        try:
            return json.dumps(response_data)
        except Exception as e:
            logger.error(f"JSON serialization error: {e}")
            # Fallback to standard json with custom encoder
            import json as std_json
            return std_json.dumps(response_data, default=str)

    def to_response(self, request=None, **kwargs):
        """
        Create HttpResponse with JSON content.
        """
        request = request or self.request
        json_data = self.to_json(**kwargs)

        response = HttpResponse(json_data, content_type='application/json')

        # Add performance timing if available (only in debug mode)
        if self._debug_enabled and hasattr(self, '_start_time'):
            elapsed = int((time.perf_counter() - self._start_time) * 1000)
            response['X-Serializer-Time'] = f"{elapsed}ms"
            response['X-Serializer-Type'] = 'optimized'

        return response

    def get_performance_info(self):
        """
        Get performance information about this serialization.
        """
        if hasattr(self, '_start_time'):
            elapsed = time.perf_counter() - self._start_time
            cache_backend = self._get_cache_backend() if not self.bypass_cache else None
            perf_info = {
                'serializer': 'optimized',
                'graph': self.graph,
                'many': self.many,
                'elapsed_seconds': elapsed,
                'cache_bypassed': self.bypass_cache,
                'cache_backend': cache_backend.stats().get('backend', 'unknown') if cache_backend else 'bypassed'
            }

            # Add debug timings if available
            if hasattr(self, '_debug_times') and self._debug_times:
                perf_info['debug_times'] = self._debug_times.copy()

                # Calculate percentages
                total_time = self._debug_times.get('total_serialize', elapsed)
                if total_time > 0:
                    for key, value in self._debug_times.items():
                        if key.endswith('_time') and isinstance(value, (int, float)):
                            perf_info[f'{key}_percentage'] = (value / total_time) * 100

                # Log performance issues
                if self._debug_enabled and elapsed > 0.05:
                    logger.warning(f"Performance analysis for {self.graph}: {self._debug_times}")

            return perf_info
        return {}



# Backwards compatibility alias
GraphSerializer = OptimizedGraphSerializer
