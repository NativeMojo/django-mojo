import csv
import io
from decimal import Decimal
from datetime import datetime, date
from django.http import StreamingHttpResponse, HttpResponse
from django.db.models import QuerySet
from mojo.helpers import logit

logger = logit.get_logger("csv_formatter", "csv_formatter.log")


class CsvFormatter:
    """
    Advanced CSV formatter with streaming support and RestMeta.GRAPHS integration.
    """

    def __init__(self, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL,
                 encoding='utf-8', streaming_threshold=1000):
        """
        Initialize CSV formatter.

        :param delimiter: Field delimiter (default comma)
        :param quotechar: Quote character for fields containing special chars
        :param quoting: Quoting behavior (csv.QUOTE_MINIMAL, etc.)
        :param encoding: Character encoding for output
        :param streaming_threshold: Minimum rows to trigger streaming response
        """
        self.delimiter = delimiter
        self.quotechar = quotechar
        self.quoting = quoting
        self.encoding = encoding
        self.streaming_threshold = streaming_threshold

    def serialize_queryset(self, queryset, fields=None, graph=None, filename="export.csv",
                          headers=None, localize=None, stream=True, timezone=None):
        """
        Serialize a Django QuerySet to CSV format.

        :param queryset: Django QuerySet to serialize
        :param fields: List of field names or tuples (field_name, display_name)
        :param graph: RestMeta graph name to use for field configuration
        :param filename: Output filename
        :param headers: Custom header names (overrides field names)
        :param localize: Localization configuration
        :param stream: Enable streaming for large datasets
        :param timezone: Timezone string for datetime localization (e.g., 'America/New_York')
        :return: HttpResponse or StreamingHttpResponse
        """
        # Determine if we should stream based on queryset size
        should_stream = stream and queryset.count() > self.streaming_threshold

        # Get fields configuration
        field_config = self._get_field_config(queryset, fields, graph)

        if should_stream:
            return self._create_streaming_response(queryset, field_config, filename,
                                                 headers, localize, timezone)
        else:
            return self._create_standard_response(queryset, field_config, filename,
                                                headers, localize, timezone)

    def serialize_data(self, data, fields=None, filename="export.csv", headers=None):
        """
        Serialize list of dictionaries or objects to CSV.

        :param data: List of dictionaries or objects
        :param fields: Field names to include
        :param filename: Output filename
        :param headers: Custom header names
        :return: HttpResponse
        """
        if not data:
            return self._create_empty_response(filename)

        # Auto-detect fields if not provided
        if not fields:
            fields = self._auto_detect_fields(data[0])

        # Prepare field configuration
        field_config = self._prepare_field_config(fields, headers)

        # Generate CSV content
        output = io.StringIO()
        writer = csv.writer(output, delimiter=self.delimiter,
                          quotechar=self.quotechar, quoting=self.quoting)

        # Write header
        writer.writerow(field_config['headers'])

        # Write data rows
        for item in data:
            row = self._extract_row_data(item, field_config['field_names'])
            writer.writerow(row)

        # Create response
        response = HttpResponse(output.getvalue(), content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename={filename}'
        return response

    def _get_field_config(self, queryset, fields, graph):
        """
        Get field configuration from various sources.
        """
        if fields:
            return self._prepare_field_config(fields)

        # Try to get from RestMeta.GRAPHS
        if graph and hasattr(queryset.model, 'RestMeta'):
            rest_meta = queryset.model.RestMeta
            if hasattr(rest_meta, 'GRAPHS') and graph in rest_meta.GRAPHS:
                graph_config = rest_meta.GRAPHS[graph]
                graph_fields = graph_config.get('fields', [])
                if graph_fields:
                    return self._prepare_field_config(graph_fields)

        # Fallback to model fields
        model_fields = [f.name for f in queryset.model._meta.fields]
        return self._prepare_field_config(model_fields)

    def _prepare_field_config(self, fields, headers=None):
        """
        Prepare field configuration for CSV generation.
        """
        field_names = []
        field_headers = []

        for i, field in enumerate(fields):
            if isinstance(field, (tuple, list)):
                field_name, display_name = field
                field_names.append(field_name)
                field_headers.append(display_name)
            else:
                field_names.append(field)
                field_headers.append(field.replace('_', ' ').replace('.', ' ').title())

        # Override with custom headers if provided
        if headers:
            field_headers = headers[:len(field_names)]

        return {
            'field_names': field_names,
            'headers': field_headers
        }

    def _create_streaming_response(self, queryset, field_config, filename,
                                 headers, localize, timezone=None):
        """
        Create streaming HTTP response for large datasets.
        """
        logger.info(f"Creating streaming CSV response for {queryset.count()} records")

        def csv_generator():
            # Create CSV writer with pseudo-buffer
            pseudo_buffer = PseudoBuffer()
            writer = csv.writer(pseudo_buffer, delimiter=self.delimiter,
                              quotechar=self.quotechar, quoting=self.quoting)

            # Yield header row
            yield writer.writerow(field_config['headers'])

            # Yield data rows
            for obj in queryset.iterator():  # Use iterator for memory efficiency
                try:
                    row = self._extract_row_data(obj, field_config['field_names'], localize, timezone)
                    yield writer.writerow(row)
                except Exception as e:
                    logger.error(f"Error processing row for object {obj.pk}: {e}")
                    # Continue with next row instead of failing completely
                    continue

        response = StreamingHttpResponse(csv_generator(), content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename={filename}'
        response['Cache-Control'] = 'no-cache'
        return response

    def _create_standard_response(self, queryset, field_config, filename,
                                headers, localize, timezone=None):
        """
        Create standard HTTP response for smaller datasets.
        """
        output = io.StringIO()
        writer = csv.writer(output, delimiter=self.delimiter,
                          quotechar=self.quotechar, quoting=self.quoting)

        # Write header
        writer.writerow(field_config['headers'])

        # Write data rows
        for obj in queryset:
            try:
                row = self._extract_row_data(obj, field_config['field_names'], localize, timezone)
                writer.writerow(row)
            except Exception as e:
                logger.error(f"Error processing row for object {obj.pk}: {e}")
                continue

        response = HttpResponse(output.getvalue(), content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename={filename}'
        return response

    def _create_empty_response(self, filename):
        """
        Create response for empty dataset.
        """
        response = HttpResponse('', content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename={filename}'
        return response

    def _extract_row_data(self, obj, field_names, localize=None, timezone=None):
        """
        Extract row data from an object based on field names.
        """
        row = []

        for field_name in field_names:
            try:
                value = self._get_field_value(obj, field_name)
                value = self._process_field_value(value, field_name, localize, timezone)
                row.append(self._format_csv_value(value))
            except Exception as e:
                logger.warning(f"Error extracting field '{field_name}': {e}")
                row.append("N/A")

        return row

    def _get_field_value(self, obj, field_name):
        """
        Get field value from object, supporting nested field access, foreign keys, and JSONField traversal.
        """
        # Handle nested field access (e.g., "parent.id", "parent.name", "metadata.defaults.role")
        if '.' in field_name:
            return self._get_nested_field_value(obj, field_name)

        # Standard field access
        if hasattr(obj, field_name):
            value = getattr(obj, field_name)
            return value() if callable(value) else value

        # Dictionary-style access
        if isinstance(obj, dict):
            return obj.get(field_name, None)

        return None

    def _get_nested_field_value(self, obj, field_path):
        """
        Get value from nested field path like "parent.id", "parent.name", "metadata.defaults.role".
        Uses robust field type detection for ForeignKey and JSONField traversal.
        """
        try:
            parts = field_path.split('.')
            current = obj

            for i, field_part in enumerate(parts):
                if current is None:
                    return None

                # Check if this is a model field using get_model_field
                field = None
                if hasattr(current, 'get_model_field'):
                    field = current.get_model_field(field_part)
                elif hasattr(current.__class__, 'get_model_field'):
                    field = current.__class__.get_model_field(field_part)

                if field:
                    if field.get_internal_type() == "ForeignKey":
                        # Handle foreign key field
                        current = getattr(current, field_part, None)
                        if current is None:
                            return None
                    elif field.get_internal_type() == "JSONField":
                        # Handle JSONField using jsonfield_as_objict
                        if hasattr(current, 'jsonfield_as_objict'):
                            json_obj = current.jsonfield_as_objict(field_part)
                            # Get remaining path for JSONField traversal
                            remaining_path = '.'.join(parts[i+1:])
                            if remaining_path:
                                return json_obj.get(remaining_path, "N/A")
                            else:
                                return json_obj
                        else:
                            # Fallback to direct access
                            current = getattr(current, field_part, {})
                            if not isinstance(current, dict):
                                return current
                    else:
                        # Regular field
                        current = getattr(current, field_part, None)
                else:
                    # No model field found, try direct access
                    if hasattr(current, field_part):
                        current = getattr(current, field_part)
                    elif isinstance(current, dict):
                        current = current.get(field_part)
                    else:
                        return None

                # Handle callable attributes
                if callable(current):
                    current = current()

            return current
        except Exception as e:
            logger.warning(f"Error accessing nested field '{field_path}': {e}")
            return None

    def _process_field_value(self, value, field_name, localize=None, timezone=None):
        """
        Process field value with localization and special handling.
        """
        if value is None:
            return "N/A"

        # Apply localization if configured
        if localize and field_name in localize:
            try:
                localizer_config = localize[field_name]
                if '|' in localizer_config:
                    localizer_name, extra = localizer_config.split('|', 1)
                else:
                    localizer_name, extra = localizer_config, None

                # Import and apply localizer
                from mojo.serializers.formats.localizers import get_localizer
                localizer = get_localizer(localizer_name)
                if localizer:
                    # Pass timezone as context for datetime localizers
                    if timezone and localizer_name in ('datetime', 'date', 'time'):
                        # Use timezone parameter instead of extra if not already specified
                        if not extra or '|' not in localizer_config:
                            return localizer(value, extra, timezone=timezone)
                    return localizer(value, extra)
            except Exception as e:
                logger.warning(f"Localization failed for field '{field_name}': {e}")

        # Auto-convert datetime to timezone if no localizer specified
        if timezone and isinstance(value, datetime):
            try:
                from mojo.helpers import dates
                value = dates.get_local_time(timezone, value)
            except Exception as e:
                logger.warning(f"Timezone conversion failed for field '{field_name}': {e}")

        return value

    def _format_csv_value(self, value):
        """
        Format value for CSV output.
        """
        if value is None:
            return ""

        # Handle model instances
        if hasattr(value, 'pk'):
            return str(value.pk)

        # Handle datetime objects
        elif isinstance(value, datetime):
            return value.strftime('%Y-%m-%d %H:%M:%S')
        elif isinstance(value, date):
            return value.strftime('%Y-%m-%d')

        # Handle numeric types
        elif isinstance(value, Decimal):
            return str(float(value)) if not value.is_nan() else "0"
        elif isinstance(value, (int, float)):
            return str(value)

        # Handle collections
        elif isinstance(value, (list, tuple)):
            return '; '.join(str(item) for item in value)
        elif isinstance(value, dict):
            return str(value)  # Could be enhanced with better dict formatting

        # Default string conversion
        else:
            return str(value)

    def _auto_detect_fields(self, sample_item):
        """
        Auto-detect fields from a sample data item.
        """
        if isinstance(sample_item, dict):
            return list(sample_item.keys())
        elif hasattr(sample_item, '_meta'):
            return [f.name for f in sample_item._meta.fields]
        elif hasattr(sample_item, '__dict__'):
            return list(sample_item.__dict__.keys())
        else:
            return ['value']  # Fallback for primitive types


class PseudoBuffer:
    """
    A buffer for streaming CSV generation.
    """

    def writerow(self, row):
        """Write the row by returning it as a CSV line."""
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(row)
        return output.getvalue()

    def write(self, value):
        """Write the value by returning it directly."""
        return value
