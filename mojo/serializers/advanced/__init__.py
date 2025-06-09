"""
Advanced Django Model and QuerySet Serializer

This module provides comprehensive serialization capabilities for Django models and QuerySets
with support for:

- RestMeta.GRAPHS configuration
- Multiple output formats (JSON, CSV, Excel, HTML)
- Nested relationships and custom fields
- Performance optimizations with caching and select_related
- Pagination and sorting for collections
- Streaming responses for large datasets
- Localization and field formatting

Main Classes:
    AdvancedGraphSerializer: Serialize single model instances or lists
    CollectionSerializer: Serialize QuerySets with pagination and sorting
    ResponseFormatter: Handle HTTP responses in multiple formats

Usage Examples:
    # Serialize a single model instance
    serializer = AdvancedGraphSerializer(user, graph="detail")
    data = serializer.serialize()

    # Serialize a QuerySet
    serializer = CollectionSerializer(User.objects.all(), graph="list")
    response = serializer.to_response(request)

    # Export to CSV
    csv_response = rest_csv(request, User.objects.all(), fields=['name', 'email'])
"""

from .serializer import (
    AdvancedGraphSerializer,
    CollectionSerializer,
    serialize_model,
    serialize_collection,
    serialize_to_response,
    timeit
)

from .formats import (
    JsonFormatter,
    CsvFormatter,
    ExcelFormatter,
    ResponseFormatter,
    get_formatter
)

from .formats.json import (
    to_json,
    to_pretty_json,
    to_compact_json,
    ExtendedJSONEncoder
)

from .formats.csv import (
    generate_csv,
    generate_csv_stream,
    serialize_to_csv
)

from .formats.excel import (
    generate_excel,
    serialize_to_excel,
    create_multi_sheet_excel,
    qsetToExcel  # Legacy compatibility
)

from .formats.response import (
    rest_status,
    rest_success,
    rest_error,
    rest_permission_denied,
    rest_not_found,
    rest_json,
    rest_csv,
    rest_excel,
    rest_html,
    get_cached_count,
    get_request_elapsed,
)

from .formats.localizers import (
    register_localizer,
    get_localizer,
    list_localizers,
    apply_localizer,
    localizer  # decorator
)

# Version info
__version__ = "2.0.0"
__author__ = "Django Mojo Team"

# Public API
__all__ = [
    # Main serializer classes
    'AdvancedGraphSerializer',
    'CollectionSerializer',

    # Format handlers
    'JsonFormatter',
    'CsvFormatter',
    'ExcelFormatter',
    'ResponseFormatter',

    # Convenience functions
    'serialize_model',
    'serialize_collection',
    'serialize_to_response',
    'serialize_to_csv',
    'serialize_to_excel',

    # JSON utilities
    'to_json',
    'to_pretty_json',
    'to_compact_json',
    'ExtendedJSONEncoder',

    # Export functions
    'generate_csv',
    'generate_csv_stream',
    'generate_excel',
    'create_multi_sheet_excel',

    # Response helpers
    'rest_status',
    'rest_success',
    'rest_error',
    'rest_permission_denied',
    'rest_not_found',
    'rest_json',
    'rest_csv',
    'rest_excel',
    'rest_html',

    # Utilities
    'get_cached_count',
    'get_request_elapsed',
    'get_formatter',
    'timeit',

    # Localizers
    'register_localizer',
    'get_localizer',
    'list_localizers',
    'apply_localizer',
    'localizer',
]


# Convenience shortcuts for common use cases
def serialize(instance, graph="default", many=None, request=None, format="json", **kwargs):
    """
    Universal serialization function that automatically chooses the right serializer.

    :param instance: Model instance, QuerySet, or list of objects
    :param graph: RestMeta graph name to use
    :param many: Force many=True for lists (auto-detected for QuerySets)
    :param request: Django request object
    :param format: Output format ('json', 'csv', 'excel', 'response')
    :param kwargs: Additional options
    :return: Serialized data or HttpResponse (if format='response')
    """
    from django.db.models import QuerySet

    # Auto-detect if we're dealing with a collection
    if isinstance(instance, QuerySet):
        if format == "response":
            return CollectionSerializer(instance, graph=graph, request=request, **kwargs).to_response()
        elif format in ["csv", "excel"]:
            formatter = ResponseFormatter(request)
            return formatter.format_response(instance, format, **kwargs)
        else:
            return CollectionSerializer(instance, graph=graph, request=request, **kwargs).serialize()

    elif isinstance(instance, (list, tuple)) or many:
        if format == "response":
            return AdvancedGraphSerializer(instance, graph=graph, many=True, request=request, **kwargs).to_response()
        elif format in ["csv", "excel"]:
            formatter = ResponseFormatter(request)
            return formatter.format_response(instance, format, **kwargs)
        else:
            return AdvancedGraphSerializer(instance, graph=graph, many=True, request=request, **kwargs).serialize()

    else:
        # Single instance
        if format == "response":
            return AdvancedGraphSerializer(instance, graph=graph, request=request, **kwargs).to_response()
        elif format in ["csv", "excel"]:
            formatter = ResponseFormatter(request)
            return formatter.format_response([instance], format, **kwargs)
        else:
            return AdvancedGraphSerializer(instance, graph=graph, request=request, **kwargs).serialize()


def to_response(instance, graph="default", request=None, **kwargs):
    """
    Shortcut to serialize and return HTTP response.

    :param instance: Model instance, QuerySet, or list
    :param graph: RestMeta graph name
    :param request: Django request object
    :param kwargs: Additional options
    :return: HttpResponse
    """
    return serialize(instance, graph=graph, request=request, format="response", **kwargs)


def to_csv_response(instance, fields=None, filename="export.csv", request=None, **kwargs):
    """
    Shortcut to export data as CSV response.

    :param instance: QuerySet or list of objects
    :param fields: Fields to include in CSV
    :param filename: Download filename
    :param request: Django request object
    :param kwargs: Additional options
    :return: HttpResponse with CSV file
    """
    formatter = ResponseFormatter(request)
    return formatter.format_response(instance, "csv", fields=fields, filename=filename, **kwargs)


def to_excel_response(instance, fields=None, filename="export.xlsx", request=None, **kwargs):
    """
    Shortcut to export data as Excel response.

    :param instance: QuerySet or list of objects
    :param fields: Fields to include in Excel
    :param filename: Download filename
    :param request: Django request object
    :param kwargs: Additional options
    :return: HttpResponse with Excel file
    """
    formatter = ResponseFormatter(request)
    return formatter.format_response(instance, "excel", fields=fields, filename=filename, **kwargs)


# Add shortcuts to __all__
__all__.extend([
    'serialize',
    'to_response',
    'to_csv_response',
    'to_excel_response'
])
