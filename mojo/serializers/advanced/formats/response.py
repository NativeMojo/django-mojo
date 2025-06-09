import time
import hashlib
from django.http import HttpResponse, StreamingHttpResponse
from django.shortcuts import render
from django.core.cache import cache
from django.conf import settings

from mojo.helpers import logit
from .json import JsonFormatter
from .csv import CsvFormatter
from .excel import ExcelFormatter

logger = logit.get_logger("response_formatter", "response_formatter.log")

# Configuration constants
STATUS_ON_PERM_DENIED = getattr(settings, 'STATUS_ON_PERM_DENIED', 403)
REST_LIST_CACHE_COUNT = getattr(settings, 'REST_LIST_CACHE_COUNT', False)
DEBUG_REST_NO_LISTS = getattr(settings, 'DEBUG_REST_NO_LISTS', False)
REST_DISCLAIMER = getattr(settings, 'REST_DISCLAIMER', '')


class ResponseFormatter:
    """
    Advanced response formatter that handles multiple output formats and HTTP responses.
    """

    def __init__(self, request=None):
        """
        Initialize response formatter.

        :param request: Django request object
        """
        self.request = request
        self.json_formatter = JsonFormatter()
        self.csv_formatter = CsvFormatter()
        self.excel_formatter = ExcelFormatter()

    def format_response(self, data, format_type="json", status=200, **kwargs):
        """
        Format data as HTTP response in specified format.

        :param data: Data to format
        :param format_type: Output format ("json", "csv", "excel", "html")
        :param status: HTTP status code
        :param kwargs: Additional options
        :return: HttpResponse
        """
        if format_type == "json":
            return self._json_response(data, status, **kwargs)
        elif format_type == "csv":
            return self._csv_response(data, **kwargs)
        elif format_type in ["excel", "xlsx"]:
            return self._excel_response(data, **kwargs)
        elif format_type == "html":
            return self._html_response(data, status, **kwargs)
        else:
            # Default to JSON
            return self._json_response(data, status, **kwargs)

    def auto_format_response(self, data, status=200, **kwargs):
        """
        Automatically determine response format based on request Accept header.

        :param data: Data to format
        :param status: HTTP status code
        :param kwargs: Additional options
        :return: HttpResponse
        """
        if not self.request:
            return self._json_response(data, status, **kwargs)

        accept_types = self._parse_accept_header()

        if 'text/html' in accept_types:
            return self._html_response(data, status, **kwargs)
        elif 'text/csv' in accept_types:
            return self._csv_response(data, **kwargs)
        elif 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' in accept_types:
            return self._excel_response(data, **kwargs)
        else:
            return self._json_response(data, status, **kwargs)

    def _json_response(self, data, status=200, **kwargs):
        """Create JSON HTTP response."""
        try:
            json_data = self.json_formatter.serialize(data, **kwargs)
            response = HttpResponse(json_data, content_type='application/json', status=status)

            # Add elapsed time if request is available
            if self.request and hasattr(self.request, '_started'):
                elapsed = int((time.perf_counter() - self.request._started) * 1000)
                response['X-Response-Time'] = f"{elapsed}ms"

            return response
        except Exception as e:
            logger.error(f"JSON response formatting failed: {e}")
            error_data = {
                'status': False,
                'error': 'Response formatting failed',
                'message': str(e)
            }
            return HttpResponse(
                self.json_formatter.serialize(error_data),
                content_type='application/json',
                status=500
            )

    def _csv_response(self, data, **kwargs):
        """Create CSV HTTP response."""
        try:
            filename = kwargs.get('filename', 'export.csv')

            if hasattr(data, 'model'):  # QuerySet
                return self.csv_formatter.serialize_queryset(
                    data,
                    filename=filename,
                    **kwargs
                )
            elif isinstance(data, list):
                return self.csv_formatter.serialize_data(
                    data,
                    filename=filename,
                    **kwargs
                )
            else:
                # Convert single item to list
                return self.csv_formatter.serialize_data(
                    [data],
                    filename=filename,
                    **kwargs
                )
        except Exception as e:
            logger.error(f"CSV response formatting failed: {e}")
            return self._error_response(f"CSV export failed: {str(e)}")

    def _excel_response(self, data, **kwargs):
        """Create Excel HTTP response."""
        try:
            filename = kwargs.get('filename', 'export.xlsx')

            if hasattr(data, 'model'):  # QuerySet
                return self.excel_formatter.serialize_queryset(
                    data,
                    filename=filename,
                    **kwargs
                )
            elif isinstance(data, list):
                return self.excel_formatter.serialize_data(
                    data,
                    filename=filename,
                    **kwargs
                )
            else:
                # Convert single item to list
                return self.excel_formatter.serialize_data(
                    [data],
                    filename=filename,
                    **kwargs
                )
        except Exception as e:
            logger.error(f"Excel response formatting failed: {e}")
            return self._error_response(f"Excel export failed: {str(e)}")

    def _html_response(self, data, status=200, template=None, context=None, **kwargs):
        """Create HTML HTTP response."""
        if template and context is not None:
            # Use custom template
            return render(self.request, template, context, status=status)

        # Generate debug/API view HTML
        return self._render_debug_html(data, status, **kwargs)

    def _render_debug_html(self, data, status=200, **kwargs):
        """Render debug HTML view for API responses."""
        try:
            # Format data as pretty JSON for display
            json_output = self.json_formatter.pretty_serialize(data)

            # Get request information
            request_data = {}
            if self.request:
                if hasattr(self.request, 'DATA'):
                    request_data = self.request.DATA.asDict()
                else:
                    request_data = {
                        'method': self.request.method,
                        'path': self.request.path,
                        'params': dict(self.request.GET.items())
                    }

            # Generate HTML content
            html_content = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>API Response</title>
                <meta charset="utf-8">
                <style>
                    body {{
                        font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
                        margin: 0;
                        padding: 20px;
                        background-color: #f8f9fa;
                    }}
                    .container {{
                        max-width: 1200px;
                        margin: 0 auto;
                        background: white;
                        border-radius: 8px;
                        box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                        overflow: hidden;
                    }}
                    .header {{
                        background: #343a40;
                        color: white;
                        padding: 15px 20px;
                        border-bottom: 1px solid #dee2e6;
                    }}
                    .header h1 {{
                        margin: 0;
                        font-size: 18px;
                    }}
                    .section {{
                        border-bottom: 1px solid #dee2e6;
                    }}
                    .section-header {{
                        background: #e9ecef;
                        padding: 10px 20px;
                        font-weight: bold;
                        color: #495057;
                        border-bottom: 1px solid #dee2e6;
                    }}
                    .content {{
                        padding: 0;
                    }}
                    pre {{
                        margin: 0;
                        padding: 20px;
                        background: #f8f9fa;
                        overflow-x: auto;
                        font-size: 12px;
                        line-height: 1.4;
                        color: #212529;
                    }}
                    .json {{
                        background: #fff;
                    }}
                    .status-success {{ color: #28a745; }}
                    .status-error {{ color: #dc3545; }}
                    .meta {{
                        padding: 15px 20px;
                        background: #f8f9fa;
                        color: #6c757d;
                        font-size: 12px;
                    }}
                </style>
            </head>
            <body>
                <div class="container">
                    <div class="header">
                        <h1>API Response Debug View</h1>
                    </div>

                    {self._render_request_section(request_data)}

                    <div class="section">
                        <div class="section-header">Response Data</div>
                        <div class="content">
                            <pre class="json">{self._escape_html(json_output)}</pre>
                        </div>
                    </div>

                    <div class="meta">
                        Status: <span class="{'status-success' if status < 400 else 'status-error'}">{status}</span>
                        {f" | Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}" if self.request else ""}
                        {f" | {REST_DISCLAIMER}" if REST_DISCLAIMER else ""}
                    </div>
                </div>
            </body>
            </html>
            """

            return HttpResponse(html_content, content_type='text/html', status=status)

        except Exception as e:
            logger.error(f"HTML response rendering failed: {e}")
            return HttpResponse(
                f"<html><body><h1>Error rendering response</h1><p>{str(e)}</p></body></html>",
                content_type='text/html',
                status=500
            )

    def _render_request_section(self, request_data):
        """Render request information section."""
        if not request_data:
            return ""

        request_json = self.json_formatter.pretty_serialize(request_data)
        return f"""
        <div class="section">
            <div class="section-header">Request Information</div>
            <div class="content">
                <pre>{self._escape_html(request_json)}</pre>
            </div>
        </div>
        """

    def _escape_html(self, text):
        """Escape HTML characters in text."""
        return (str(text)
                .replace('&', '&amp;')
                .replace('<', '&lt;')
                .replace('>', '&gt;')
                .replace('"', '&quot;')
                .replace("'", '&#39;'))

    def _error_response(self, message, status=500):
        """Create error response."""
        error_data = {
            'status': False,
            'error': message,
            'datetime': int(time.time())
        }
        return self._json_response(error_data, status)

    def _parse_accept_header(self):
        """Parse request Accept header."""
        if not self.request:
            return ['application/json']

        # Check for explicit format parameter
        if hasattr(self.request, 'DATA') and hasattr(self.request.DATA, 'get'):
            format_param = self.request.DATA.get('_format') or self.request.DATA.get('format')
            if format_param:
                format_map = {
                    'json': 'application/json',
                    'csv': 'text/csv',
                    'excel': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    'html': 'text/html'
                }
                if format_param in format_map:
                    return [format_map[format_param]]

        # Parse Accept header
        accept_header = self.request.META.get('HTTP_ACCEPT', 'application/json')
        accept_types = [media_type.strip().split(';')[0] for media_type in accept_header.split(',')]

        return accept_types


# Convenience functions for backwards compatibility
def rest_status(request, status, data=None, **kwargs):
    """
    Create status response.

    :param request: Django request
    :param status: Boolean status
    :param data: Response data
    :param kwargs: Additional data
    :return: HttpResponse
    """
    if isinstance(data, str):
        if status:
            response_data = {'message': data}
        else:
            response_data = {'error': data}
    else:
        response_data = data or {}

    response_data.update(kwargs)
    response_data['status'] = status
    response_data['datetime'] = int(time.time())

    formatter = ResponseFormatter(request)
    return formatter.auto_format_response(response_data)


def rest_success(request, data=None, **kwargs):
    """Create success response."""
    return rest_status(request, True, data, **kwargs)


def rest_error(request, error, error_code=None, status=400):
    """Create error response."""
    response_data = {
        'error': error,
        'status': False,
        'datetime': int(time.time())
    }

    if error_code:
        response_data['error_code'] = error_code

    formatter = ResponseFormatter(request)
    return formatter.auto_format_response(response_data, status=status)


def rest_permission_denied(request, error="Permission denied", error_code=403):
    """Create permission denied response."""
    return rest_error(request, error, error_code, status=STATUS_ON_PERM_DENIED)


def rest_not_found(request, error="Not found"):
    """Create not found response."""
    return rest_error(request, error, error_code=404, status=404)


def rest_json(request, data, filename="export.json", **kwargs):
    """Create JSON file download response."""
    formatter = ResponseFormatter(request)
    json_data = formatter.json_formatter.serialize(data, **kwargs)

    response = HttpResponse(json_data, content_type='application/json')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def rest_csv(request, queryset, fields, filename="export.csv", **kwargs):
    """Create CSV download response."""
    formatter = ResponseFormatter(request)
    return formatter.csv_formatter.serialize_queryset(
        queryset=queryset,
        fields=fields,
        filename=filename,
        **kwargs
    )


def rest_excel(request, queryset, fields, filename="export.xlsx", **kwargs):
    """Create Excel download response."""
    formatter = ResponseFormatter(request)
    return formatter.excel_formatter.serialize_queryset(
        queryset=queryset,
        fields=fields,
        filename=filename,
        **kwargs
    )


def rest_html(request, html_content=None, template=None, context=None, status=200):
    """Create HTML response."""
    formatter = ResponseFormatter(request)

    if html_content:
        return HttpResponse(html_content, content_type='text/html', status=status)
    elif template:
        return render(request, template, context or {}, status=status)
    else:
        return HttpResponse(
            "<html><body><h1>Hello, World!</h1><p>Welcome to the API.</p></body></html>",
            content_type='text/html',
            status=status
        )


def get_cached_count(queryset, timeout=1800):
    """
    Get cached count for queryset.

    :param queryset: Django QuerySet
    :param timeout: Cache timeout in seconds
    :return: Count integer
    """
    if not REST_LIST_CACHE_COUNT:
        return queryset.count()

    # Generate cache key from query
    query_hash = hashlib.sha256(str(queryset.query).encode()).hexdigest()
    cache_key = f"rest_count_{query_hash}"

    count = cache.get(cache_key)
    if count is None:
        count = queryset.count()
        cache.set(cache_key, count, timeout=timeout)
        logger.debug(f"Cached count for query: {count}")

    return count


def get_request_elapsed(request):
    """Get elapsed time for request in milliseconds."""
    if request and hasattr(request, '_started'):
        return int((time.perf_counter() - request._started) * 1000)
    return 0
