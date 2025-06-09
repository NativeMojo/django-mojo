# Try to import ujson for better performance, fallback to standard json
try:
    import ujson
    HAS_UJSON = True
except ImportError:
    HAS_UJSON = False

import json
import time
import datetime
import math
from decimal import Decimal
from functools import wraps
from itertools import chain

from django.db.models import ForeignKey, OneToOneField, ManyToOneRel, ManyToManyField, F
from django.db.models import QuerySet
from django.core.exceptions import FieldDoesNotExist
from django.http import HttpResponse

from mojo.helpers import logit

logger = logit.get_logger("advanced_serializer", "advanced_serializer.log")

# Cache for expensive operations
SERIALIZER_CACHE = {}

def timeit(func):
    """Decorator to time function execution."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        start_time = time.perf_counter()
        result = func(*args, **kwargs)
        end_time = time.perf_counter()
        total_time = end_time - start_time
        logger.info(f'Function {func.__name__} took {total_time:.4f} seconds')
        return result
    return wrapper


class AdvancedGraphSerializer:
    """
    Advanced serializer for Django models and QuerySets with comprehensive features:
    - RestMeta.GRAPHS configuration support
    - Multiple output formats (JSON, CSV, Excel, HTML)
    - Caching and performance optimizations
    - Nested relationships and custom fields
    - Pagination and sorting for collections
    """

    def __init__(self, instance, graph="default", many=False, request=None, **kwargs):
        """
        :param instance: Model instance or QuerySet
        :param graph: The graph type to use (e.g., "default", "list", "detail")
        :param many: Boolean, if True, serializes a QuerySet
        :param request: Django request object for context
        :param kwargs: Additional options (cache, format, etc.)
        """
        self.graph = graph
        self.request = request
        self.cache = kwargs.get('cache', {})
        self.format = kwargs.get('format', 'json')
        self.qset = None
        
        # Handle QuerySet vs single instance
        if isinstance(instance, QuerySet):
            self.many = True
            self.qset = instance
            self.instance = instance  # Keep as QuerySet for lazy evaluation
        else:
            self.many = many
            self.instance = instance if many else instance
            if many and not isinstance(instance, (list, tuple)):
                self.instance = [instance]

    @timeit
    def serialize(self):
        """
        Main serialization method that routes to appropriate handler.
        """
        if self.many:
            if isinstance(self.instance, QuerySet):
                return self._serialize_queryset(self.instance)
            else:
                return [self._serialize_instance(obj) for obj in self.instance]
        return self._serialize_instance(self.instance)

    def _serialize_queryset(self, qset):
        """
        Serialize a QuerySet with optimizations.
        """
        # Apply select_related optimizations if available
        if hasattr(qset, 'model') and hasattr(qset.model, 'RestMeta'):
            select_related_fields = self._get_select_related_fields(qset.model)
            if select_related_fields:
                qset = qset.select_related(*select_related_fields)
                logger.info(f"Applied select_related: {select_related_fields}")

        return [self._serialize_instance(obj) for obj in qset]

    def _get_select_related_fields(self, model):
        """
        Get fields that should be select_related for performance.
        """
        if not hasattr(model, 'RestMeta') or not hasattr(model.RestMeta, 'GRAPHS'):
            return []

        graph_config = model.RestMeta.GRAPHS.get(self.graph, {})
        fields = graph_config.get('fields', [])
        graphs = graph_config.get('graphs', {})
        
        select_fields = []
        for field_name in chain(fields, graphs.keys()):
            try:
                field = model._meta.get_field(field_name)
                if isinstance(field, (ForeignKey, OneToOneField)):
                    select_fields.append(field_name)
            except FieldDoesNotExist:
                continue
                
        return select_fields

    def _serialize_instance(self, obj):
        """
        Serialize a single model instance using RestMeta.GRAPHS configuration.
        """
        # Check cache first
        cache_key = self._get_cache_key(obj)
        if cache_key in self.cache:
            return self.cache[cache_key]

        if not hasattr(obj, "RestMeta") or not hasattr(obj.RestMeta, "GRAPHS"):
            logger.warning(f"RestMeta.GRAPHS not found for {obj.__class__.__name__}")
            return self._fallback_serialization(obj)

        graph_config = obj.RestMeta.GRAPHS.get(self.graph)
        if graph_config is None:
            if self.graph != "default":
                logger.info(f"Graph '{self.graph}' not found, falling back to 'default'")
                self.graph = "default"
                graph_config = obj.RestMeta.GRAPHS.get("default")
            
            if graph_config is None:
                logger.warning(f"No graph configuration found for {obj.__class__.__name__}")
                return self._fallback_serialization(obj)

        logger.debug(f"Serializing {obj.__class__.__name__} with graph '{self.graph}': {graph_config}")

        # Start with basic field serialization
        data = self._serialize_fields(obj, graph_config.get("fields", []))

        # Add extra fields (methods, properties, etc.)
        self._add_extra_fields(obj, data, graph_config.get("extra", []))

        # Process related object graphs
        self._add_related_graphs(obj, data, graph_config.get("graphs", {}))

        # Cache the result
        if cache_key:
            self.cache[cache_key] = data

        return data

    def _get_cache_key(self, obj):
        """Generate cache key for an object."""
        if hasattr(obj, 'pk') and obj.pk:
            return f"{obj.__class__.__name__}_{obj.pk}_{self.graph}"
        return None

    def _fallback_serialization(self, obj):
        """Fallback when no RestMeta.GRAPHS is available."""
        if hasattr(obj, '_meta'):
            fields = [field.name for field in obj._meta.fields]
            return self._serialize_fields(obj, fields)
        return str(obj)

    def _serialize_fields(self, obj, fields):
        """
        Serialize basic model fields.
        """
        data = {}
        
        # If no fields specified, get all model fields
        if not fields:
            if hasattr(obj, '_meta'):
                fields = [field.name for field in obj._meta.fields]
            else:
                return {}

        for field_name in fields:
            try:
                field_value = getattr(obj, field_name)
                field_obj = self._get_model_field(obj, field_name)
                
                # Handle callable attributes
                if callable(field_value):
                    try:
                        field_value = field_value()
                    except Exception as e:
                        logger.warning(f"Error calling {field_name}: {e}")
                        continue

                # Serialize the value based on type
                data[field_name] = self._serialize_value(field_value, field_obj)
                
            except AttributeError:
                logger.warning(f"Field '{field_name}' not found on {obj.__class__.__name__}")
                continue
            except Exception as e:
                logger.error(f"Error serializing field '{field_name}': {e}")
                data[field_name] = None

        return data

    def _add_extra_fields(self, obj, data, extra_fields):
        """
        Add extra fields (methods, properties, computed values).
        """
        for field in extra_fields:
            if isinstance(field, (tuple, list)):
                method_name, alias = field
            else:
                method_name, alias = field, field

            try:
                if hasattr(obj, method_name):
                    attr = getattr(obj, method_name)
                    value = attr() if callable(attr) else attr
                    data[alias] = self._serialize_value(value)
                    logger.debug(f"Added extra field '{method_name}' as '{alias}'")
                else:
                    logger.warning(f"Extra field '{method_name}' not found on {obj.__class__.__name__}")
            except Exception as e:
                logger.error(f"Error processing extra field '{method_name}': {e}")
                data[alias] = None

    def _add_related_graphs(self, obj, data, related_graphs):
        """
        Process related object serialization using sub-graphs.
        """
        for field_name, sub_graph in related_graphs.items():
            try:
                related_obj = getattr(obj, field_name, None)
                if related_obj is None:
                    data[field_name] = None
                    continue

                field_obj = self._get_model_field(obj, field_name)
                
                if isinstance(field_obj, (ForeignKey, OneToOneField)):
                    # Single related object
                    logger.debug(f"Serializing related field '{field_name}' with graph '{sub_graph}'")
                    data[field_name] = AdvancedGraphSerializer(
                        related_obj, 
                        graph=sub_graph, 
                        cache=self.cache
                    ).serialize()
                    
                elif isinstance(field_obj, (ManyToManyField, ManyToOneRel)) or hasattr(related_obj, 'all'):
                    # Many-to-many or reverse foreign key relationship
                    if hasattr(related_obj, 'all'):
                        related_qset = related_obj.all()
                        logger.debug(f"Serializing many-to-many field '{field_name}' with graph '{sub_graph}'")
                        data[field_name] = AdvancedGraphSerializer(
                            related_qset, 
                            graph=sub_graph, 
                            many=True,
                            cache=self.cache
                        ).serialize()
                    else:
                        data[field_name] = []
                else:
                    logger.warning(f"Unsupported field type for '{field_name}': {type(field_obj)}")
                    data[field_name] = str(related_obj)
                    
            except Exception as e:
                logger.error(f"Error processing related field '{field_name}': {e}")
                data[field_name] = None

    def _get_model_field(self, obj, field_name):
        """Get Django model field object."""
        try:
            if hasattr(obj, '_meta'):
                return obj._meta.get_field(field_name)
        except FieldDoesNotExist:
            pass
        return None

    def _serialize_value(self, value, field_obj=None):
        """
        Serialize individual values with type-specific handling.
        """
        if value is None:
            return None
            
        # Handle datetime objects
        if isinstance(value, datetime.datetime):
            return int(value.timestamp())
        elif isinstance(value, datetime.date):
            return value.isoformat()
            
        # Handle numeric types
        elif isinstance(value, Decimal):
            return 0.0 if value.is_nan() else float(value)
        elif isinstance(value, float):
            return 0.0 if math.isnan(value) else value
            
        # Handle related objects
        elif hasattr(value, 'pk'):
            if isinstance(field_obj, (ForeignKey, OneToOneField)):
                return value.pk
            return str(value)
            
        # Handle collections
        elif isinstance(value, (list, tuple)):
            return [self._serialize_value(v) for v in value]
        elif isinstance(value, dict):
            return {k: self._serialize_value(v) for k, v in value.items()}
            
        # Handle other types
        elif isinstance(value, (str, int, bool)):
            return value
        else:
            return str(value)

    def to_json(self, **kwargs):
        """
        Convert serialized data to JSON string.
        """
        data = self.serialize()
        
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
        
        try:
            if HAS_UJSON:
                return ujson.dumps(response_data)
            else:
                return json.dumps(response_data, cls=ExtendedJSONEncoder)
        except Exception as e:
            logger.error(f"JSON serialization error: {e}")
            # Fallback to standard json
            return json.dumps(response_data, cls=ExtendedJSONEncoder)

    def to_response(self, request=None, **kwargs):
        """
        Return appropriate HTTP response based on request headers.
        """
        request = request or self.request
        
        if not request:
            return HttpResponse(self.to_json(**kwargs), content_type='application/json')
            
        # Determine response format from request
        accept_header = request.headers.get('Accept', '')
        
        if 'application/json' in accept_header:
            return HttpResponse(self.to_json(**kwargs), content_type='application/json')
        elif 'text/html' in accept_header:
            return self._render_html_response(request, **kwargs)
        else:
            return HttpResponse(self.to_json(**kwargs), content_type='application/json')

    def _render_html_response(self, request, **kwargs):
        """
        Render HTML response for debugging/viewing.
        """
        data = self.serialize()
        if self.many:
            response_data = {'data': data, 'status': True, 'size': len(data), 'graph': self.graph}
        else:
            response_data = {'data': data, 'status': True, 'graph': self.graph}
        
        response_data.update(kwargs)
        
        # Use pretty JSON for HTML display
        json_output = json.dumps(response_data, cls=ExtendedJSONEncoder, indent=4, sort_keys=True)
        
        html_content = f"""
        <html>
        <head>
            <title>API Response</title>
            <style>
                body {{ font-family: monospace; margin: 20px; }}
                pre {{ background: #f5f5f5; padding: 20px; border-radius: 5px; }}
            </style>
        </head>
        <body>
            <h1>API Response</h1>
            <pre>{json_output}</pre>
        </body>
        </html>
        """
        
        return HttpResponse(html_content, content_type='text/html')


class ExtendedJSONEncoder(json.JSONEncoder):
    """JSON encoder that handles Django model types."""
    
    def default(self, obj):
        if isinstance(obj, datetime.datetime):
            return int(obj.timestamp())
        elif isinstance(obj, datetime.date):
            return obj.isoformat()
        elif isinstance(obj, Decimal):
            return 0.0 if obj.is_nan() else float(obj)
        elif isinstance(obj, float) and math.isnan(obj):
            return 0.0
        elif hasattr(obj, 'pk'):
            return obj.pk
        return super().default(obj)


class CollectionSerializer:
    """
    Advanced collection serializer with pagination, sorting, and filtering.
    """
    
    def __init__(self, queryset, graph="list", request=None, **kwargs):
        self.queryset = queryset
        self.graph = graph
        self.request = request
        self.size = kwargs.get('size', 25)
        self.start = kwargs.get('start', 0)
        self.sort = kwargs.get('sort', None)
        self.format = kwargs.get('format', 'json')
        self.cache = kwargs.get('cache', {})

    @timeit
    def serialize(self):
        """
        Serialize collection with pagination and sorting.
        """
        # Get total count for pagination
        total_count = self.queryset.count()
        
        # Apply sorting
        sorted_qset, sort_args = self._apply_sorting(self.queryset)
        
        # Apply pagination
        paginated_qset = sorted_qset[self.start:self.start + self.size]
        
        # Serialize items
        serializer = AdvancedGraphSerializer(
            paginated_qset, 
            graph=self.graph, 
            many=True,
            cache=self.cache
        )
        data = serializer.serialize()
        
        return {
            'data': data,
            'status': True,
            'count': total_count,
            'size': self.size,
            'start': self.start,
            'sort': sort_args if sort_args else None,
            'graph': self.graph,
            'datetime': int(time.time())
        }

    def _apply_sorting(self, qset):
        """
        Apply sorting to queryset.
        """
        if not self.sort:
            return qset, None
            
        sort_args = []
        for sort_field in self.sort.split(','):
            sort_field = sort_field.strip()
            if sort_field:
                # Handle reverse sorting
                if sort_field.startswith('-'):
                    sort_args.append(F(sort_field[1:]).desc(nulls_last=True))
                else:
                    sort_args.append(F(sort_field).asc(nulls_last=True))
        
        if sort_args:
            try:
                qset = qset.order_by(*sort_args)
                return qset, [str(arg) for arg in sort_args]
            except Exception as e:
                logger.error(f"Sorting error: {e}")
                return qset, None
        
        return qset, None

    def to_json(self, **kwargs):
        """Convert to JSON string."""
        data = self.serialize()
        data.update(kwargs)
        
        try:
            if HAS_UJSON:
                return ujson.dumps(data)
            else:
                return json.dumps(data, cls=ExtendedJSONEncoder)
        except Exception as e:
            logger.error(f"JSON serialization error: {e}")
            return json.dumps(data, cls=ExtendedJSONEncoder)

    def to_response(self, request=None, **kwargs):
        """Return HTTP response."""
        request = request or self.request
        return HttpResponse(self.to_json(**kwargs), content_type='application/json')

    def to_csv(self, fields=None, filename="export.csv"):
        """Export to CSV format."""
        if not fields:
            # Try to get fields from model RestMeta
            if hasattr(self.queryset.model, 'RestMeta') and hasattr(self.queryset.model.RestMeta, 'GRAPHS'):
                graph_config = self.queryset.model.RestMeta.GRAPHS.get(self.graph, {})
                fields = graph_config.get('fields', [])
            
            if not fields and hasattr(self.queryset.model, '_meta'):
                fields = [f.name for f in self.queryset.model._meta.fields]
        
        return csv.generate_csv(self.queryset, fields, filename)

    def to_excel(self, fields=None, filename="export.xlsx"):
        """Export to Excel format."""
        if not fields:
            # Try to get fields from model RestMeta
            if hasattr(self.queryset.model, 'RestMeta') and hasattr(self.queryset.model.RestMeta, 'GRAPHS'):
                graph_config = self.queryset.model.RestMeta.GRAPHS.get(self.graph, {})
                fields = graph_config.get('fields', [])
            
            if not fields and hasattr(self.queryset.model, '_meta'):
                fields = [f.name for f in self.queryset.model._meta.fields]
        
        return excel.generate_excel(self.queryset, fields, filename)


# Convenience functions for backwards compatibility
def serialize_model(instance, graph="default", **kwargs):
    """Serialize a single model instance."""
    return AdvancedGraphSerializer(instance, graph=graph, **kwargs).serialize()

def serialize_collection(queryset, graph="list", **kwargs):
    """Serialize a collection/queryset."""
    return CollectionSerializer(queryset, graph=graph, **kwargs).serialize()

def serialize_to_response(instance, graph="default", request=None, **kwargs):
    """Serialize and return HTTP response."""
    if isinstance(instance, QuerySet):
        serializer = CollectionSerializer(instance, graph=graph, request=request, **kwargs)
    else:
        many = kwargs.pop('many', False)
        serializer = AdvancedGraphSerializer(instance, graph=graph, many=many, request=request, **kwargs)
    
    return serializer.to_response(**kwargs)