# Advanced Django Model Serializer

A comprehensive serialization library for Django models and QuerySets with support for multiple output formats, performance optimizations, and advanced configuration through `RestMeta.GRAPHS`.

## Features

- **RestMeta.GRAPHS Configuration**: Use declarative graph configurations for consistent serialization
- **Multiple Output Formats**: JSON, CSV, Excel, HTML debug views
- **Performance Optimizations**: Caching, select_related, streaming responses
- **Nested Relationships**: Deep serialization of related models
- **Custom Fields**: Support for methods, properties, and computed values
- **Pagination & Sorting**: Built-in collection handling with pagination
- **Localization**: Field-level formatting and localization

## Quick Start

### Basic Usage

```python
from mojo.serializers.advanced import AdvancedGraphSerializer, CollectionSerializer

# Serialize a single model instance
user = User.objects.get(pk=1)
serializer = AdvancedGraphSerializer(user, graph="detail")
data = serializer.serialize()

# Serialize a QuerySet
users = User.objects.all()
serializer = CollectionSerializer(users, graph="list", size=25)
response_data = serializer.serialize()

# Create HTTP response
response = serializer.to_response(request)
```

### RestMeta.GRAPHS Configuration

Define serialization graphs in your Django models:

```python
class User(models.Model):
    name = models.CharField(max_length=100)
    email = models.EmailField()
    profile = models.OneToOneField('Profile', on_delete=models.CASCADE)
    posts = models.ForeignKey('Post', related_name='author')
    created = models.DateTimeField(auto_now_add=True)

    class RestMeta:
        GRAPHS = {
            "default": {
                "fields": ["id", "name", "email", "created"],
            },
            "list": {
                "fields": ["id", "name", "email"],
                "extra": [
                    ("get_full_name", "full_name"),  # Method with alias
                    "post_count",  # Property or method
                ]
            },
            "detail": {
                "fields": ["id", "name", "email", "created"],
                "extra": ["get_full_name", "is_active"],
                "graphs": {
                    "profile": "summary",  # Related object with sub-graph
                    "posts": "list",       # Related QuerySet
                }
            },
            "export": {
                "fields": ["id", "name", "email", "created"],
                "extra": [("profile.bio", "biography")],  # Nested field access
            }
        }

    def get_full_name(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def post_count(self):
        return self.posts.count()

class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    bio = models.TextField()
    avatar = models.ImageField()

    class RestMeta:
        GRAPHS = {
            "summary": {
                "fields": ["bio"],
                "extra": ["avatar_url"]
            }
        }

    def avatar_url(self):
        return self.avatar.url if self.avatar else None
```

## Advanced Usage

### Multiple Output Formats

```python
from mojo.serializers.advanced import (
    serialize, to_response, to_csv_response, to_excel_response
)

# Auto-detect format from request
def user_list_view(request):
    users = User.objects.all()
    return to_response(users, graph="list", request=request)

# Force specific formats
def user_csv_export(request):
    users = User.objects.all()
    return to_csv_response(
        users,
        fields=["name", "email", "created"],
        filename="users.csv",
        request=request
    )

def user_excel_export(request):
    users = User.objects.all()
    return to_excel_response(
        users,
        graph="export",
        filename="users.xlsx",
        request=request
    )
```

### Performance Optimizations

```python
# Automatic select_related for foreign keys
users = User.objects.all()  # Will auto-add select_related('profile')
serializer = AdvancedGraphSerializer(users, graph="detail", many=True)

# Caching for repeated serialization
cache = {}
for user in users:
    data = AdvancedGraphSerializer(user, graph="detail", cache=cache).serialize()

# Streaming responses for large datasets
def large_export(request):
    large_queryset = User.objects.all()  # 100k+ records
    return to_csv_response(
        large_queryset,
        fields=["name", "email"],
        filename="all_users.csv",
        stream=True  # Enables streaming
    )
```

### Collection Serialization with Pagination

```python
from mojo.serializers.advanced import CollectionSerializer

def paginated_users(request):
    users = User.objects.all()

    serializer = CollectionSerializer(
        users,
        graph="list",
        size=request.GET.get('size', 25),
        start=request.GET.get('start', 0),
        sort=request.GET.get('sort', 'name'),
        request=request
    )

    return serializer.to_response()

# Response format:
{
    "data": [...],
    "status": true,
    "count": 1500,
    "size": 25,
    "start": 0,
    "sort": ["name"],
    "graph": "list",
    "datetime": 1640995200
}
```

### Custom Localization

```python
from mojo.serializers.advanced.formats.localizers import register_localizer

# Register custom localizer
@register_localizer('currency_eur')
def format_euro(value, extra=None):
    return f"€{value:.2f}"

# Use in CSV/Excel export
csv_response = to_csv_response(
    Product.objects.all(),
    fields=['name', 'price'],
    localize={'price': 'currency_eur'},
    filename='products.csv'
)
```

### Response Helpers

```python
from mojo.serializers.advanced import (
    rest_success, rest_error, rest_not_found, rest_permission_denied
)

def api_view(request):
    try:
        user = User.objects.get(pk=request.GET['id'])
        data = serialize(user, graph="detail")
        return rest_success(request, data)
    except User.DoesNotExist:
        return rest_not_found(request, "User not found")
    except PermissionError:
        return rest_permission_denied(request, "Access denied")
    except Exception as e:
        return rest_error(request, str(e))
```

## Configuration Options

### Serializer Options

```python
serializer = AdvancedGraphSerializer(
    instance=user,
    graph="detail",           # Graph configuration to use
    many=False,               # Whether serializing multiple objects
    request=request,          # Django request for context
    cache={},                 # Shared cache for performance
    format="json"             # Output format hint
)
```

### Collection Options

```python
serializer = CollectionSerializer(
    queryset=users,
    graph="list",             # Graph configuration
    size=25,                  # Page size
    start=0,                  # Start offset
    sort="name,-created",     # Sort fields (- for descending)
    format="json",            # Output format
    request=request           # Django request
)
```

### Export Options

```python
# CSV Export
csv_response = to_csv_response(
    queryset,
    fields=['name', 'email'],     # Fields to include
    filename='export.csv',        # Download filename
    headers=['Name', 'Email'],    # Custom column headers
    localize={'date': 'date|%Y-%m-%d'},  # Field localization
    stream=True                   # Enable streaming for large datasets
)

# Excel Export
excel_response = to_excel_response(
    queryset,
    fields=['name', 'email', 'created'],
    filename='export.xlsx',
    sheet_name='Users',
    freeze_panes=True,           # Freeze header row
    auto_width=True              # Auto-adjust column widths
)
```

## Graph Configuration Reference

### Field Types

```python
class RestMeta:
    GRAPHS = {
        "example": {
            # Basic model fields
            "fields": ["id", "name", "email"],

            # Extra fields (methods, properties, computed values)
            "extra": [
                "method_name",                    # Simple method/property
                ("method_name", "alias"),         # Method with alias
                ("nested.field", "flat_name"),   # Nested field access
            ],

            # Related object graphs
            "graphs": {
                "foreign_key_field": "sub_graph_name",
                "many_to_many_field": "list_graph",
                "reverse_fk": "related_graph"
            }
        }
    }
```

### Nested Field Access

```python
# Access nested fields with dot notation
"extra": [
    ("user.profile.bio", "biography"),
    ("metadata.category.name", "category"),
    ("settings.preferences.theme", "theme")
]
```

## Performance Tips

1. **Use select_related**: The serializer automatically applies `select_related()` for foreign keys in your graph
2. **Enable caching**: Pass a shared cache dictionary when serializing multiple related objects
3. **Stream large exports**: Set `stream=True` for CSV exports with >1000 records
4. **Optimize graphs**: Keep graph configurations focused - avoid over-fetching data
5. **Use pagination**: Always paginate large QuerySets with CollectionSerializer

## API Reference

### Core Classes

- `AdvancedGraphSerializer`: Main serializer for single instances or lists
- `CollectionSerializer`: Specialized serializer for QuerySets with pagination
- `ResponseFormatter`: HTTP response handler for multiple formats

### Format Handlers

- `JsonFormatter`: Enhanced JSON serialization with ujson support
- `CsvFormatter`: CSV export with streaming support
- `ExcelFormatter`: Excel export with openpyxl
- `ResponseFormatter`: Multi-format HTTP response handler

### Convenience Functions

- `serialize()`: Universal serialization function
- `to_response()`: Quick HTTP response generation
- `to_csv_response()`: CSV export response
- `to_excel_response()`: Excel export response

### Response Helpers

- `rest_success()`, `rest_error()`: Status responses
- `rest_not_found()`, `rest_permission_denied()`: Error responses
- `get_cached_count()`: Cached QuerySet counting

## Requirements

- Django 3.2+
- Python 3.8+
- ujson (optional, for better JSON performance)
- openpyxl (optional, for Excel export)

## License

This project is part of the Django Mojo framework.
