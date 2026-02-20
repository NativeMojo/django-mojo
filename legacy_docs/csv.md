# CSV Serializer Documentation

The CSV serializer provides powerful data export capabilities with support for streaming, localization, timezone conversion, foreign key traversal, and JSONField access.

## Table of Contents

- [Basic Usage](#basic-usage)
- [Field Configuration](#field-configuration)
- [Foreign Key Traversal](#foreign-key-traversal)
- [JSONField Access](#jsonfield-access)
- [Localization](#localization)
- [Timezone Support](#timezone-support)
- [Streaming for Large Datasets](#streaming-for-large-datasets)
- [RestMeta Integration](#restmeta-integration)
- [Custom Localizers](#custom-localizers)
- [Performance Optimization](#performance-optimization)

---

## Basic Usage

### Simple Export

```python
from mojo.serializers.formats.csv import CsvFormatter

formatter = CsvFormatter()

# Export queryset to CSV
response = formatter.serialize_queryset(
    queryset=Order.objects.all(),
    fields=['id', 'customer_name', 'total', 'created'],
    filename="orders.csv"
)
```

### Via REST API

```python
POST /api/orders/list
{
    "download_format": "csv",
    "filename": "orders_export.csv"
}
```

---

## Field Configuration

### Specifying Fields

You can specify fields in three ways:

#### 1. Simple Field List

```python
fields = ['id', 'name', 'email', 'created']
```

#### 2. Fields with Custom Headers

```python
fields = [
    ('id', 'Order ID'),
    ('customer_name', 'Customer Name'),
    ('total', 'Total Amount'),
    ('created', 'Order Date')
]
```

#### 3. Via RestMeta.FORMATS

```python
class Order(MojoModel):
    class RestMeta:
        FORMATS = {
            'csv': ['id', 'customer.name', 'total', 'created']
        }
```

---

## Foreign Key Traversal

The CSV serializer supports traversing foreign key relationships using dot notation.

### Basic Foreign Key Access

```python
class Order(MojoModel):
    customer = models.ForeignKey(Customer)
    
    class RestMeta:
        FORMATS = {
            'csv': [
                'id',
                'customer.name',        # Access customer's name
                'customer.email',       # Access customer's email
                'total'
            ]
        }
```

### Multiple Levels Deep

```python
class Order(MojoModel):
    location = models.ForeignKey(Location)
    
    class RestMeta:
        FORMATS = {
            'csv': [
                'id',
                'location.name',                    # Location name
                'location.city.name',               # City name (if Location has city FK)
                'location.city.state.code',         # State code (3 levels deep)
                'total'
            ]
        }
```

### Null Handling

If any part of the foreign key chain is null, the serializer returns "N/A":

```python
# If order.location is None
'location.name'  # Returns "N/A" instead of crashing
```

### Performance Tip

Use `select_related()` to avoid N+1 query problems:

```python
queryset = Order.objects.select_related(
    'customer',
    'location',
    'location__city',
    'location__city__state'
)
```

---

## JSONField Access

The serializer supports traversing into JSONField data using dot notation.

### Basic JSONField Access

```python
class Order(MojoModel):
    metadata = models.JSONField()  # {"shipping": {"address": "123 Main St"}}
    
    class RestMeta:
        FORMATS = {
            'csv': [
                'id',
                'metadata.shipping.address',    # Access nested JSON
                'metadata.tracking.number',
                'total'
            ]
        }
```

### Combining Foreign Key + JSONField

```python
class Order(MojoModel):
    customer = models.ForeignKey(Customer)
    metadata = models.JSONField()
    
    class RestMeta:
        FORMATS = {
            'csv': [
                'id',
                'customer.name',                              # FK traversal
                'customer.metadata.preferences.language',     # FK + JSON traversal
                'metadata.shipping.method',                   # Direct JSON traversal
                'total'
            ]
        }
```

---

## Localization

Localization allows you to format field values using built-in or custom formatters.

### Available Localizers

#### Currency Formatters

```python
'cents_to_currency'       # 12345 → "123.45"
'cents_to_dollars'        # 12345 → "$123.45"
'currency|€'              # 123.45 → "€123.45"
```

#### Date/Time Formatters

```python
'date|%Y-%m-%d'                    # 2024-01-15
'date|%m/%d/%Y'                    # 01/15/2024
'datetime|%Y-%m-%d %H:%M:%S'       # 2024-01-15 14:30:00
'datetime|%m/%d/%Y %I:%M %p'       # 01/15/2024 02:30 PM
'time|%H:%M:%S'                    # 14:30:00
'timezone|America/New_York'        # Convert and format with timezone
```

#### Number Formatters

```python
'number|2'               # Format with 2 decimal places: 123.45
'number|0'               # Integer format: 123
'percentage|1'           # 0.123 → "12.3%"
'thousands|,'            # 1234567 → "1,234,567"
```

#### Text Formatters

```python
'title'                  # "hello world" → "Hello World"
'upper'                  # "hello" → "HELLO"
'lower'                  # "HELLO" → "hello"
'truncate|50'            # Truncate to 50 characters
```

#### Boolean Formatters

```python
'yes_no'                 # True → "Yes", False → "No"
'true_false'             # True → "True", False → "False"
'on_off'                 # True → "On", False → "Off"
```

#### Collection Formatters

```python
'join|, '                # ['a', 'b', 'c'] → "a, b, c"
'join|; '                # ['a', 'b', 'c'] → "a; b; c"
'count'                  # ['a', 'b', 'c'] → "3"
```

#### File Formatters

```python
'filesize|auto'          # 1024 → "1.00 KB", 1048576 → "1.00 MB"
'filesize|MB'            # Force MB: 1048576 → "1.00 MB"
```

### Using Localization

#### Option 1: In RestMeta

```python
class Order(MojoModel):
    class RestMeta:
        FORMATS = {
            'csv': ['id', 'customer.name', 'amount_cents', 'created', 'is_active']
        }
        
        FORMATS_LOCALIZE = {
            'csv': {
                'amount_cents': 'cents_to_dollars',
                'created': 'datetime|%m/%d/%Y %I:%M %p',
                'is_active': 'yes_no'
            }
        }
```

#### Option 2: In Graph Definition

```python
class Order(MojoModel):
    class RestMeta:
        GRAPHS = {
            'export': {
                'fields': ['id', 'name', 'price_cents', 'discount', 'created'],
                'localize': {
                    'price_cents': 'cents_to_dollars',
                    'discount': 'percentage|1',
                    'created': 'datetime|%m/%d/%Y'
                }
            }
        }
```

#### Option 3: Programmatically

```python
formatter = CsvFormatter()

localize = {
    'price': 'cents_to_dollars',
    'created': 'datetime|%m/%d/%Y',
    'status': 'title',
    'tags': 'join|; '
}

response = formatter.serialize_queryset(
    queryset=Product.objects.all(),
    fields=['id', 'name', 'price', 'created', 'status', 'tags'],
    localize=localize,
    filename="products.csv"
)
```

### Localization with Foreign Keys

```python
class Order(MojoModel):
    class RestMeta:
        FORMATS = {
            'csv': [
                'id',
                'customer.name',
                'customer.created',
                'amount_cents'
            ]
        }
        
        FORMATS_LOCALIZE = {
            'csv': {
                'customer.created': 'date|%Y-%m-%d',      # Localize FK field
                'amount_cents': 'cents_to_dollars'
            }
        }
```

---

## Timezone Support

The CSV serializer supports automatic timezone conversion for all datetime fields.

### How It Works

1. **Timezone from Request**: Client sends timezone in request
2. **Auto-Conversion**: All datetime fields are converted to the specified timezone
3. **Localizer Integration**: Timezone is passed to datetime localizers
4. **Format Preservation**: Formatting happens after timezone conversion

### Via REST API Request

```python
POST /api/orders/list
{
    "download_format": "csv",
    "timezone": "America/New_York",
    "localize": {
        "created": "datetime|%m/%d/%Y %I:%M %p",
        "updated": "datetime|%Y-%m-%d %H:%M:%S"
    }
}
```

### Supported Timezone Strings

Any valid IANA timezone string:
- `America/New_York`
- `Europe/London`
- `Asia/Tokyo`
- `Australia/Sydney`
- `UTC`

### Automatic Conversion (No Localizer)

If you specify a timezone but don't define localizers, datetime fields are **automatically converted**:

```python
POST /api/orders/list
{
    "download_format": "csv",
    "timezone": "Europe/London"
}
```

All datetime fields will automatically be converted to London time with default formatting.

### Timezone + Localization

```python
POST /api/orders/list
{
    "download_format": "csv",
    "timezone": "Asia/Tokyo",
    "localize": {
        "created": "datetime|%Y年%m月%d日 %H:%M",
        "updated": "date|%Y-%m-%d"
    }
}
```

The timezone conversion happens **before** formatting is applied.

### Per-Field Timezone Override

```python
{
    "download_format": "csv",
    "timezone": "America/New_York",     # Default for all fields
    "localize": {
        "created": "datetime|%m/%d/%Y",        # Uses America/New_York
        "server_time": "timezone|UTC"          # Force UTC for this field
    }
}
```

### Example Flow

**Database Value** (UTC):
```
created: 2024-01-15 14:00:00 UTC
```

**Request**:
```json
{
    "timezone": "America/New_York",
    "localize": {
        "created": "datetime|%m/%d/%Y %I:%M %p"
    }
}
```

**CSV Output**:
```
01/15/2024 09:00 AM
```
(Converted to EST, which is UTC-5)

---

## Streaming for Large Datasets

The CSV serializer automatically streams large datasets to prevent memory issues.

### Automatic Streaming

```python
formatter = CsvFormatter(streaming_threshold=1000)

# If queryset has > 1000 records, automatically streams
response = formatter.serialize_queryset(
    queryset=Order.objects.all(),  # 50,000 records
    fields=['id', 'customer.name', 'total'],
    filename="large_export.csv"
)
```

### How Streaming Works

1. **Threshold Check**: Counts queryset records
2. **StreamingHttpResponse**: Uses Django's streaming response
3. **Iterator Mode**: Uses `queryset.iterator()` to fetch records in batches
4. **Memory Efficient**: Only holds one batch in memory at a time

### Configure Streaming Threshold

```python
# Stream for datasets > 500 records
formatter = CsvFormatter(streaming_threshold=500)

# Disable streaming (not recommended for large datasets)
response = formatter.serialize_queryset(
    queryset=large_queryset,
    stream=False  # Force standard response
)
```

### Streaming with Localization

Streaming works seamlessly with localization and timezone conversion:

```python
formatter = CsvFormatter(streaming_threshold=1000)

response = formatter.serialize_queryset(
    queryset=Order.objects.all(),  # 100,000 records
    fields=['id', 'customer.name', 'amount_cents', 'created'],
    localize={
        'amount_cents': 'cents_to_dollars',
        'created': 'datetime|%m/%d/%Y'
    },
    timezone='America/Los_Angeles',
    filename="orders.csv"
)
```

---

## RestMeta Integration

### Complete RestMeta Example

```python
class Order(MojoModel):
    customer = models.ForeignKey(Customer)
    amount_cents = models.IntegerField()
    discount = models.DecimalField()
    created = models.DateTimeField()
    metadata = models.JSONField()
    is_active = models.BooleanField()
    
    class RestMeta:
        # Define CSV format
        FORMATS = {
            'csv': [
                ('id', 'Order ID'),
                ('customer.name', 'Customer Name'),
                ('customer.email', 'Customer Email'),
                ('amount_cents', 'Total'),
                ('discount', 'Discount'),
                ('created', 'Order Date'),
                ('metadata.shipping.method', 'Shipping Method'),
                ('is_active', 'Active')
            ]
        }
        
        # Define localization for CSV
        FORMATS_LOCALIZE = {
            'csv': {
                'amount_cents': 'cents_to_dollars',
                'discount': 'percentage|1',
                'created': 'datetime|%m/%d/%Y %I:%M %p',
                'is_active': 'yes_no'
            }
        }
        
        # Alternative: Define in graph
        GRAPHS = {
            'export': {
                'fields': [
                    'id',
                    'customer.name',
                    'amount_cents',
                    'created'
                ],
                'localize': {
                    'amount_cents': 'cents_to_dollars',
                    'created': 'datetime|%m/%d/%Y'
                }
            }
        }
```

### API Request with RestMeta

```python
# Uses FORMATS and FORMATS_LOCALIZE from RestMeta
POST /api/orders/list
{
    "download_format": "csv",
    "filename": "orders.csv",
    "timezone": "America/New_York"
}

# Or use a specific graph
POST /api/orders/list
{
    "download_format": "csv",
    "graph": "export",
    "timezone": "America/Chicago"
}
```

---

## Custom Localizers

You can create custom localizers for specialized formatting needs.

### Creating a Custom Localizer

```python
# In mojo/serializers/formats/localizers.py or your app's localizers.py

from mojo.serializers.formats.localizers import localizer

@localizer('phone_format')
def phone_format(value, extra=None):
    """Format phone number as (XXX) XXX-XXXX"""
    if not value:
        return ""
    
    digits = ''.join(filter(str.isdigit, str(value)))
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return value

@localizer('status_badge')
def status_badge(value, extra=None):
    """Convert status codes to readable badges"""
    status_map = {
        'pending': '⏳ Pending',
        'approved': '✅ Approved',
        'rejected': '❌ Rejected',
        'processing': '⚙️ Processing'
    }
    return status_map.get(value, value)

@localizer('duration')
def duration_format(value, extra='short'):
    """Format duration in seconds to human readable"""
    if value is None:
        return ""
    
    seconds = int(value)
    
    if extra == 'short':
        if seconds < 60:
            return f"{seconds}s"
        elif seconds < 3600:
            return f"{seconds//60}m"
        else:
            return f"{seconds//3600}h"
    else:  # long format
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        return f"{hours}h {minutes}m {secs}s"
```

### Using Custom Localizers

```python
class Order(MojoModel):
    class RestMeta:
        FORMATS = {
            'csv': ['id', 'phone', 'status', 'processing_time']
        }
        
        FORMATS_LOCALIZE = {
            'csv': {
                'phone': 'phone_format',
                'status': 'status_badge',
                'processing_time': 'duration|long'
            }
        }
```

### Localizer with Timezone Support

```python
@localizer('business_hours')
def business_hours_format(value, extra=None, timezone=None):
    """Format datetime showing if it's during business hours"""
    if not value:
        return ""
    
    # Convert to timezone if provided
    if timezone:
        from mojo.helpers import dates
        value = dates.get_local_time(timezone, value)
    
    hour = value.hour
    is_business_hours = 9 <= hour < 17
    
    formatted = value.strftime('%Y-%m-%d %I:%M %p')
    badge = '🟢' if is_business_hours else '🔴'
    
    return f"{badge} {formatted}"
```

---

## Performance Optimization

### 1. Use select_related() for Foreign Keys

```python
# BAD: N+1 queries
queryset = Order.objects.all()

# GOOD: Single query with joins
queryset = Order.objects.select_related(
    'customer',
    'location',
    'location__city'
)

response = formatter.serialize_queryset(
    queryset=queryset,
    fields=['id', 'customer.name', 'location.city.name'],
    filename="orders.csv"
)
```

### 2. Use prefetch_related() for Reverse FKs

```python
queryset = Customer.objects.prefetch_related('orders')

# Can now access orders efficiently in custom methods
```

### 3. Limit Fields

Only export fields you need:

```python
# BAD: Exports all model fields
fields = None

# GOOD: Only export necessary fields
fields = ['id', 'name', 'email', 'created']
```

### 4. Use Streaming for Large Exports

```python
# For datasets > 1000 records
formatter = CsvFormatter(streaming_threshold=1000)

response = formatter.serialize_queryset(
    queryset=large_queryset,
    fields=fields,
    stream=True  # Enable streaming
)
```

### 5. Database Optimization

```python
# Use only() to limit database columns
queryset = Order.objects.select_related('customer').only(
    'id', 'created', 'total',
    'customer__name', 'customer__email'
)
```

### 6. Avoid Heavy Localization on Large Datasets

```python
# If performance is critical, minimize complex localizers
# Simple formatters are faster
localize = {
    'created': 'date|%Y-%m-%d',     # Fast
    'total': 'number|2'              # Fast
}
```

---

## Complete Example

### Model Definition

```python
class Order(MojoModel):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    location = models.ForeignKey(Location, on_delete=models.SET_NULL, null=True)
    amount_cents = models.IntegerField()
    discount = models.DecimalField(max_digits=5, decimal_places=2)
    status = models.CharField(max_length=20)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)
    metadata = models.JSONField(default=dict)
    is_active = models.BooleanField(default=True)
    
    class RestMeta:
        FORMATS = {
            'csv': [
                ('id', 'Order #'),
                ('customer.name', 'Customer'),
                ('customer.email', 'Email'),
                ('location.city.name', 'City'),
                ('amount_cents', 'Total'),
                ('discount', 'Discount'),
                ('status', 'Status'),
                ('created', 'Created'),
                ('updated', 'Last Updated'),
                ('metadata.shipping.method', 'Shipping'),
                ('is_active', 'Active')
            ]
        }
        
        FORMATS_LOCALIZE = {
            'csv': {
                'amount_cents': 'cents_to_dollars',
                'discount': 'percentage|1',
                'status': 'title',
                'created': 'datetime|%m/%d/%Y %I:%M %p',
                'updated': 'datetime|%m/%d/%Y %I:%M %p',
                'is_active': 'yes_no'
            }
        }
```

### API Request

```python
POST /api/orders/list
{
    "download_format": "csv",
    "filename": "orders_2024.csv",
    "timezone": "America/New_York"
}
```

### Resulting CSV

```csv
Order #,Customer,Email,City,Total,Discount,Status,Created,Last Updated,Shipping,Active
1001,John Doe,john@example.com,New York,$145.99,10.0%,Pending,01/15/2024 09:30 AM,01/16/2024 02:15 PM,Express,Yes
1002,Jane Smith,jane@example.com,Los Angeles,$89.50,5.0%,Approved,01/15/2024 10:45 AM,01/15/2024 11:00 AM,Standard,Yes
```

---

## Error Handling

The CSV serializer handles errors gracefully:

### Null Foreign Keys

```python
# If order.location is None
'location.name'  # Returns "N/A"
```

### Missing JSONField Keys

```python
# If metadata.shipping doesn't exist
'metadata.shipping.method'  # Returns "N/A"
```

### Invalid Localizers

```python
# If localizer doesn't exist
localize = {'field': 'invalid_localizer'}
# Logs warning and returns original value
```

### Timezone Conversion Errors

```python
# If timezone is invalid
timezone = 'Invalid/Timezone'
# Logs warning and uses original datetime
```

---

## Best Practices

1. **Always use select_related()** for foreign key fields to avoid N+1 queries
2. **Enable streaming** for large datasets (> 1000 records)
3. **Specify timezone** in requests for consistent datetime formatting
4. **Use localization** for currency, dates, and user-facing data
5. **Provide custom headers** for better readability
6. **Test with production data** to verify performance
7. **Monitor export times** and optimize queries as needed
8. **Use only()** to limit database columns when possible
9. **Cache frequently exported** data if appropriate
10. **Document custom localizers** for your team

---

## Troubleshooting

### Slow Exports

1. Check for N+1 queries using Django Debug Toolbar
2. Add select_related() for all foreign keys
3. Enable streaming for large datasets
4. Reduce number of fields
5. Use only() to limit database columns

### Memory Issues

1. Enable streaming: `stream=True`
2. Lower streaming threshold: `streaming_threshold=500`
3. Reduce batch size in querysets
4. Clear querysets after export

### Incorrect Timezone Conversion

1. Verify timezone string is valid IANA format
2. Check that datetime fields are timezone-aware in database
3. Ensure `USE_TZ = True` in Django settings
4. Test with known timestamps

### Missing Foreign Key Data

1. Verify select_related() includes all FK paths
2. Check for null foreign keys (returns "N/A")
3. Verify field names match model fields

### Localization Not Working

1. Check localizer name is registered
2. Verify syntax: `'localizer_name|extra'`
3. Check field name matches exactly
4. Review logs for localization warnings
