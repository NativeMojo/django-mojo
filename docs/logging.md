# Logging System Documentation

Django-MOJO provides a dual logging system:

1. **File/Console Logging** (`mojo.helpers.logit`) - Traditional logging with enhanced formatting
2. **Database Logging** (`mojo.apps.logit`) - Structured logging to PostgreSQL for auditing and analysis

## Table of Contents

- [File/Console Logging](#fileconsole-logging)
- [Database Logging](#database-logging)
- [Integration Examples](#integration-examples)
- [Best Practices](#best-practices)

---

## File/Console Logging

The `mojo.helpers.logit` module provides an enhanced logging wrapper around Python's standard `logging` library with:

- Pretty-formatted output for dictionaries and complex structures
- Color-coded console output
- Automatic log rotation
- Thread-safe operations
- Sensitive data masking

### Quick Start

```python
from mojo.helpers import logit

# Get a logger
logger = logit.get_logger("myapp", "myapp.log")

# Log messages
logger.info("Application started")
logger.debug("Debug information")
logger.warning("Warning message")
logger.error("Error occurred")
logger.exception("Exception with stack trace")
```

### Convenience Functions

For common logging patterns, use the built-in convenience functions:

```python
from mojo.helpers import logit

# Logs to mojo.log
logit.info("Info message")
logit.warning("Warning message")

# Logs to debug.log
logit.debug("Debug information")

# Logs to error.log
logit.error("Error occurred")
logit.exception("Exception with traceback")
```

### Creating Custom Loggers

```python
from mojo.helpers import logit

# Create a logger for specific functionality
logger = logit.get_logger(
    name="payments",           # Logger name
    filename="payments.log",   # Log file name
    debug=False                # Log level (True = DEBUG, False = INFO)
)

logger.info("Payment processed", {
    "amount": 99.99,
    "user_id": 123,
    "status": "completed"
})
```

### Pretty Printing

The logging system includes pretty printing for complex data structures:

```python
from mojo.helpers import logit

# Log complex structures with automatic formatting
data = {
    "user": "john.doe@example.com",
    "permissions": ["read", "write"],
    "settings": {
        "theme": "dark",
        "notifications": True
    }
}

# Pretty print to console
logit.pretty_print(data)
# Alias: logit.pp(data)

# Format as string
formatted = logit.pretty_format(data)

# Log with pretty formatting (happens automatically)
logger.info("User data:", data)
```

**Output:**
```
{
  "user": "john.doe@example.com",
  "permissions": [
    "read",
    "write"
  ],
  "settings": {
    "theme": "dark",
    "notifications": true
  }
}
```

### Color-Coded Console Output

```python
from mojo.helpers import logit

# Print colored messages to console
logit.color_print("Success!", logit.ConsoleLogger.GREEN)
logit.color_print("Warning!", logit.ConsoleLogger.YELLOW)
logit.color_print("Error!", logit.ConsoleLogger.RED)

# Available colors:
# - ConsoleLogger.GREEN
# - ConsoleLogger.YELLOW
# - ConsoleLogger.RED
# - ConsoleLogger.BLUE
# - ConsoleLogger.PINK
# - ConsoleLogger.WHITE
```

### Sensitive Data Masking

The logging system automatically masks sensitive information:

```python
from mojo.helpers import logit

logger = logit.get_logger("security", "security.log")

# These fields are automatically masked
data = {
    "username": "john",
    "password": "secret123",
    "api_key": "abc-def-ghi",
    "token": "bearer xyz",
    "credit_card": "4111111111111111"
}

logger.info("Login attempt", data)

# Logged as:
# {
#   "username": "john",
#   "password": "*****",
#   "api_key": "*****",
#   "token": "*****",
#   "credit_card": "*****"
# }
```

**Automatically masked fields:**
- password, pwd, secret, token, access_token, api_key, authorization
- ssn, credit_card, card_number, pin, cvv

### Log File Management

Logs are stored in the `LOG_ROOT` directory (configured in `mojo.helpers.paths`):

- Default: `<project_root>/logs/`
- **Automatic rotation**: When log files reach 10MB, they are rotated
- **Backup count**: 3 backup files are kept (e.g., `app.log`, `app.log.1`, `app.log.2`, `app.log.3`)

### Logger API Reference

```python
logger = logit.get_logger(name, filename, debug)
```

**Methods:**
- `logger.info(*args)` - Log info messages
- `logger.debug(*args)` - Log debug messages
- `logger.warning(*args)` / `logger.warn(*args)` - Log warnings
- `logger.error(*args)` - Log errors
- `logger.critical(*args)` - Log critical errors
- `logger.exception(*args)` - Log exceptions with stack trace

**Parameters:**
- Multiple arguments are concatenated
- Dictionaries are pretty-formatted automatically
- Other types are converted to strings

### Example: Comprehensive Logging

```python
from mojo.helpers import logit
import datetime

# Create application logger
app_logger = logit.get_logger("myapp", "myapp.log")

# Log simple messages
app_logger.info("Application started successfully")

# Log with context
app_logger.info("Processing order", {
    "order_id": 12345,
    "user": "john@example.com",
    "items": ["item1", "item2"],
    "total": 99.99,
    "timestamp": datetime.datetime.now()
})

# Error logging with exception
try:
    result = 10 / 0
except Exception as e:
    app_logger.exception("Division error occurred")
    # Automatically captures and logs stack trace

# Debug logging (only if debug=True)
debug_logger = logit.get_logger("debug", "debug.log", debug=True)
debug_logger.debug("Detailed debug information", {
    "variable1": "value1",
    "variable2": "value2"
})
```

---

## Database Logging

The `mojo.apps.logit` app provides structured logging to PostgreSQL for:

- Audit trails
- User activity tracking
- Request/response logging
- Model change tracking
- Security event logging

### Log Model

Logs are stored in the `Log` model with these fields:

- **id** - BigInt primary key
- **created** - Timestamp (auto-added)
- **level** - Log level (info, warning, error, debug, critical)
- **kind** - Log category/type (e.g., "api_call", "model:changed", "auth:login")
- **method** - HTTP method (GET, POST, etc.)
- **path** - Request path
- **payload** - Additional data (JSON)
- **ip** - IP address
- **duid** - Device unique ID
- **uid** - User ID
- **username** - Username
- **user_agent** - User agent string
- **log** - Log message/data
- **model_name** - Related model (e.g., "account.User")
- **model_id** - Related model ID

### Logging to Database

#### Method 1: Via MojoModel Methods

**All models inheriting from MojoModel** have built-in logging methods. This is the recommended approach as it automatically associates logs with the model instance.

##### Instance Methods (from model instances)

```python
from mojo.apps.account.models import User

user = User.objects.get(id=123)

# Log from model instance - automatically includes model_name and model_id
user.log(
    log="User updated profile",
    kind="profile:update",
    level="info"
)

# Log with structured data
user.log(
    log={"action": "profile_update", "changes": {"email": "new@example.com"}},
    kind="profile:update",
    level="info"
)

# Alternative method name (same as log)
user.model_logit(
    request=request,
    log="User action performed",
    kind="user:action",
    level="info"
)
```

**Available on every MojoModel instance:**
- `instance.log(log, kind, level, **kwargs)` - Log from instance, automatically uses active request
- `instance.model_logit(request, log, kind, level, **kwargs)` - Log with explicit request

##### Class Methods (without instance)

```python
from mojo.apps.account.models import User

# Log at class level with specific model_id
User.class_logit(
    request=request,
    log="User action",
    kind="user:action",
    model_id=123,  # Specify which user
    level="info"
)

# Debug logging from any model
User.debug("Debug information", some_var, {"data": "value"})
```

**Available as class methods:**
- `Model.class_logit(request, log, kind, model_id, level, **kwargs)` - Log at class level
- `Model.debug(log, *args)` - Debug logging (uses file logger)

#### Method 2: Direct Log.logit()

```python
from mojo.apps.logit.models import Log

# Log with request context
Log.logit(
    request=request,
    log="User performed action",
    kind="user:action",
    model_name="account.User",
    model_id=123,
    level="info"
)

# Log without request (system logging)
Log.logit(
    request=None,
    log="Background job completed",
    kind="cronjob:success",
    level="info",
    ip_address="127.0.0.1",
    user_agent="CronJob/1.0"
)

# Log structured data
Log.logit(
    request=request,
    log={
        "event": "payment_processed",
        "amount": 99.99,
        "currency": "USD",
        "order_id": 12345
    },
    kind="payment:processed",
    model_name="payments.Order",
    model_id=12345,
    level="info"
)
```

### Automatic Request Context

When `request` is provided, the following fields are automatically captured:

- **uid** - User ID (from `request.user`)
- **username** - Username
- **ip** - IP address (from `request.ip`)
- **duid** - Device UID (from `request.duid`)
- **path** - Request path
- **method** - HTTP method
- **user_agent** - User agent string

### Log Kinds (Categories)

Use consistent `kind` values to categorize logs:

```python
# Authentication
kind="auth:login"
kind="auth:logout"
kind="auth:failed"

# Model operations
kind="model:created"
kind="model:changed"
kind="model:deleted"

# API operations
kind="api:success"
kind="api:error"

# Payments
kind="payment:processed"
kind="payment:failed"

# Custom
kind="feature:usage"
kind="report:generated"
```

### Log Levels

- **debug** - Detailed debugging information
- **info** - General informational messages (default)
- **warning** - Warning messages
- **error** - Error messages
- **critical** - Critical errors

### Querying Logs

```python
from mojo.apps.logit.models import Log
from datetime import datetime, timedelta

# Get recent logs
recent_logs = Log.objects.filter(
    created__gte=datetime.now() - timedelta(days=7)
).order_by('-created')

# Get logs for specific user
user_logs = Log.objects.filter(uid=123)

# Get logs by kind
payment_logs = Log.objects.filter(kind__startswith="payment:")

# Get error logs
errors = Log.objects.filter(level="error")

# Get logs for specific model
model_logs = Log.objects.filter(
    model_name="account.User",
    model_id=123
)

# Complex query
activity_logs = Log.objects.filter(
    created__gte=datetime.now() - timedelta(hours=24),
    level__in=["info", "warning"],
    kind__startswith="user:"
).order_by('-created')[:100]
```

### REST API

Logs can be accessed via REST API:

```
GET /api/logs              - List logs
GET /api/logs/<id>         - Get specific log
POST /api/logs             - Create log (admin only)
DELETE /api/logs/<id>      - Delete log (admin only)
```

**Permissions:**
- **View**: `manage_logs`, `view_logs`, or `admin`
- **Create/Edit**: `admin` only
- **Delete**: `admin` only

**Query parameters:**
- `kind` - Filter by log kind
- `level` - Filter by level
- `uid` - Filter by user ID
- `model_name` - Filter by model
- `created__gte` - Filter by date (after)
- `created__lte` - Filter by date (before)

Example:
```javascript
// Fetch error logs from last 24 hours
fetch('/api/logs?level=error&created__gte=2025-01-31T00:00:00')
  .then(r => r.json())
  .then(data => {
    console.log(data.items);
  });
```

### Sensitive Data Protection

Database logs automatically mask sensitive fields using the same masking logic as file logging:

```python
Log.logit(
    request=request,
    log={
        "user": "john@example.com",
        "password": "secret123",  # Automatically masked
        "api_key": "abc123"       # Automatically masked
    },
    kind="auth:attempt"
)

# Stored in database as:
# {
#   "user": "john@example.com",
#   "password": "*****",
#   "api_key": "*****"
# }
```

### MojoModel Built-in Logging

**Every model inheriting from MojoModel automatically has logging capabilities.** This is one of the core features of Django-MOJO that makes logging seamless across your application.

#### Automatic Model Context

When you use `instance.log()`, it automatically captures:
- **model_name** - Full model path (e.g., "account.User")
- **model_id** - Instance primary key
- **request context** - From `ACTIVE_REQUEST` context variable

```python
from mojo.apps.account.models import User

user = User.objects.get(id=123)

# Simple log - automatically includes model_name="account.User" and model_id=123
user.log("User logged in", kind="auth:login")

# With request (automatically captured if in request context)
user.log("Profile updated", kind="profile:update", level="info")
```

#### Instance vs Class Logging

**Instance logging** (when you have an object):
```python
order = Order.objects.get(id=456)

# Best for: Logging actions on specific instances
order.log("Order shipped", kind="order:shipped")
order.log({"status": "shipped", "tracking": "123ABC"}, kind="order:status")
```

**Class logging** (when you don't have an instance):
```python
# Best for: Logging class-level operations or before instance exists
Order.class_logit(
    request=request,
    log="Order query executed",
    kind="order:query",
    model_id=0,  # No specific instance
    level="info"
)
```

#### Integration with REST Operations

MojoModel automatically integrates with REST operations:

```python
class Order(models.Model, MojoModel):
    # ... fields ...
    
    class RestMeta:
        LOG_CHANGES = True  # Enable automatic change logging
    
    def on_rest_saved(self, changed_fields, created):
        """Automatically called after REST save"""
        if created:
            # Log creation
            self.log("Order created", kind="order:created")
        elif changed_fields:
            # Log changes
            self.log({
                "action": "order_updated",
                "changes": list(changed_fields.keys())
            }, kind="order:updated")
        
        super().on_rest_saved(changed_fields, created)
    
    def on_rest_pre_delete(self):
        """Called before REST delete"""
        self.log("Order deleted", kind="order:deleted", level="warning")
```

#### Debug Logging from Models

Every model also has a `debug()` class method for file-based debugging:

```python
class PaymentProcessor:
    @classmethod
    def process_payment(cls, amount):
        # Debug log to file (not database)
        Order.debug("Processing payment", {"amount": amount})
        
        # ... process payment ...
        
        Order.debug("Payment complete", {"result": "success"})
```

#### Complete MojoModel Logging API

**Instance Methods:**
```python
instance.log(log, kind="model_log", level="info", **kwargs)
# - Uses active request from ACTIVE_REQUEST context variable
# - Automatically includes model_name and model_id
# - Returns Log instance

instance.model_logit(request, log, kind="model_log", level="info", **kwargs)
# - Explicit request parameter
# - Same as log() but with explicit request
# - Returns Log instance
```

**Class Methods:**
```python
Model.class_logit(request, log, kind="cls_log", model_id=0, level="info", **kwargs)
# - Log at class level
# - Specify model_id explicitly or 0 for class-level
# - Returns Log instance

Model.debug(log, *args)
# - Debug logging to file (not database)
# - Supports multiple arguments and dicts
# - Returns None
```

**Properties:**
```python
Model.get_model_string()
# - Returns: "app_label.ModelName" (e.g., "account.User")
# - Used internally for model_name field
```

#### Example: Complete Order Tracking

```python
from django.db import models
from mojo.models import MojoModel

class Order(models.Model, MojoModel):
    user = models.ForeignKey('account.User', on_delete=models.CASCADE)
    total = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, default='pending')
    
    class RestMeta:
        LOG_CHANGES = True
    
    def on_rest_created(self):
        """Log when order is created via REST"""
        self.log({
            "event": "order_created",
            "user_id": self.user_id,
            "total": float(self.total)
        }, kind="order:created")
    
    def ship(self, tracking_number):
        """Ship the order"""
        self.status = 'shipped'
        self.save()
        
        # Log shipping
        self.log({
            "event": "order_shipped",
            "tracking": tracking_number
        }, kind="order:shipped")
    
    def cancel(self, reason):
        """Cancel the order"""
        old_status = self.status
        self.status = 'cancelled'
        self.save()
        
        # Log cancellation
        self.log({
            "event": "order_cancelled",
            "reason": reason,
            "previous_status": old_status
        }, kind="order:cancelled", level="warning")
    
    @classmethod
    def generate_report(cls):
        """Generate order report (class method)"""
        orders = cls.objects.filter(status='completed')
        
        cls.class_logit(
            request=None,
            log=f"Order report generated: {orders.count()} orders",
            kind="order:report",
            model_id=0
        )
        
        return orders

# Usage
order = Order.objects.get(id=123)
order.ship("TRACK123")  # Automatically logs shipping
order.cancel("Customer request")  # Automatically logs cancellation
```

---

## Integration Examples

### Example 1: API Request Logging

```python
from mojo import decorators as md
from mojo.helpers import logit
from mojo.helpers.response import JsonResponse

logger = logit.get_logger("api", "api.log")

@md.POST("orders")
def create_order(request):
    # Log to file
    logger.info("Order creation request", {
        "user": request.user.username,
        "ip": request.ip
    })
    
    try:
        # Process order
        order = create_order_from_request(request)
        
        # Log to database
        order.log(
            log={"action": "order_created", "total": order.total},
            kind="order:created",
            level="info"
        )
        
        return JsonResponse({"order": order.to_dict()})
    
    except Exception as e:
        # Log error to both systems
        logger.exception("Order creation failed")
        
        Log.logit(
            request=request,
            log=str(e),
            kind="order:error",
            level="error"
        )
        
        return JsonResponse({"error": str(e)}, status=500)
```

### Example 2: User Activity Tracking

```python
from mojo.apps.account.models import User
from mojo.apps.logit.models import Log

class UserActivityTracker:
    """Track user activity across the application"""
    
    @staticmethod
    def log_login(request, user):
        """Log successful login"""
        Log.logit(
            request=request,
            log=f"User {user.username} logged in",
            kind="auth:login",
            model_name="account.User",
            model_id=user.id,
            level="info"
        )
    
    @staticmethod
    def log_logout(request, user):
        """Log logout"""
        Log.logit(
            request=request,
            log=f"User {user.username} logged out",
            kind="auth:logout",
            model_name="account.User",
            model_id=user.id,
            level="info"
        )
    
    @staticmethod
    def log_failed_login(request, username):
        """Log failed login attempt"""
        Log.logit(
            request=request,
            log=f"Failed login attempt for {username}",
            kind="auth:failed",
            level="warning"
        )
    
    @staticmethod
    def log_profile_update(user, changes):
        """Log profile updates"""
        user.log(
            log={"action": "profile_updated", "changes": changes},
            kind="profile:update",
            level="info"
        )

# Usage in views
def login_view(request):
    user = authenticate(request.DATA.username, request.DATA.password)
    if user:
        UserActivityTracker.log_login(request, user)
        return JsonResponse({"success": True})
    else:
        UserActivityTracker.log_failed_login(request, request.DATA.username)
        return JsonResponse({"error": "Invalid credentials"}, status=401)
```

### Example 3: Background Job Logging

```python
from mojo.helpers import logit
from mojo.apps.logit.models import Log

logger = logit.get_logger("cronjobs", "cronjobs.log")

def daily_report_job():
    """Generate daily reports (cron job)"""
    
    # Log to file
    logger.info("Starting daily report generation")
    
    try:
        # Generate reports
        report_count = generate_reports()
        
        # Log to database (without request)
        Log.logit(
            request=None,
            log=f"Daily reports generated: {report_count}",
            kind="cronjob:reports",
            level="info",
            user_agent="CronJob/1.0"
        )
        
        logger.info(f"Completed daily reports: {report_count}")
        
    except Exception as e:
        logger.exception("Daily report generation failed")
        
        Log.logit(
            request=None,
            log=f"Report generation failed: {str(e)}",
            kind="cronjob:error",
            level="error",
            user_agent="CronJob/1.0"
        )
```

### Example 4: Model Change Tracking

```python
from mojo.models import MojoModel
from django.db import models

class Order(models.Model, MojoModel):
    # ... fields ...
    
    class RestMeta:
        LOG_CHANGES = True  # Enable automatic change logging
    
    def on_rest_saved(self, changed_fields, created):
        """Called automatically after REST save"""
        
        if created:
            self.log(
                log="Order created",
                kind="order:created",
                level="info"
            )
        elif changed_fields:
            self.log(
                log={
                    "action": "order_updated",
                    "changed_fields": list(changed_fields.keys())
                },
                kind="order:updated",
                level="info"
            )
        
        super().on_rest_saved(changed_fields, created)
```

---

## Best Practices

### 1. Choose the Right Logging Method

**Use File Logging for:**
- Application debugging
- Development/troubleshooting
- High-volume logs (performance)
- Temporary diagnostic information

**Use Database Logging for:**
- Audit trails
- User activity tracking
- Security events
- Long-term record keeping
- Logs that need to be queried/analyzed

### 2. Use Appropriate Log Levels

```python
# DEBUG - Detailed diagnostic information
logger.debug("Processing item", {"item_id": 123, "step": 5})

# INFO - General informational messages
logger.info("Order processed successfully")

# WARNING - Warning messages
logger.warning("High memory usage detected")

# ERROR - Error messages
logger.error("Payment gateway timeout")

# CRITICAL - Critical system failures
logger.critical("Database connection lost")
```

### 3. Structure Your Log Data

Use consistent structure for better analysis:

```python
# Good - Structured
Log.logit(
    request=request,
    log={
        "event": "payment_processed",
        "order_id": 12345,
        "amount": 99.99,
        "currency": "USD",
        "gateway": "stripe"
    },
    kind="payment:processed"
)

# Avoid - Unstructured string
Log.logit(
    request=request,
    log="Payment processed for order 12345 amount 99.99 USD via stripe",
    kind="payment:processed"
)
```

### 4. Use Consistent Log Kinds

Establish a naming convention:

```python
# Category:Action format
"auth:login"
"auth:logout"
"payment:processed"
"order:created"
"report:generated"
```

### 5. Don't Over-Log

```python
# Good - Log significant events
logger.info("User registered", {"user_id": 123})

# Avoid - Logging every trivial operation
logger.info("Function started")
logger.info("Variable x = 5")
logger.info("Function ended")
```

### 6. Include Context

Always include relevant context:

```python
# Good
logger.error("Payment failed", {
    "order_id": 123,
    "user_id": 456,
    "amount": 99.99,
    "error": str(e)
})

# Less useful
logger.error("Payment failed")
```

### 7. Clean Up Old Logs

Database logs can accumulate quickly. Implement cleanup:

```python
from mojo.apps.logit.models import Log
from datetime import datetime, timedelta

# Delete logs older than 90 days
cutoff = datetime.now() - timedelta(days=90)
Log.objects.filter(created__lt=cutoff).delete()

# Or keep only specific kinds longer
Log.objects.filter(
    created__lt=cutoff,
    kind__startswith="debug:"
).delete()
```

### 8. Monitor Log Volume

```python
from mojo.apps.logit.models import Log

# Check log volume
log_count = Log.objects.filter(
    created__gte=datetime.now() - timedelta(days=1)
).count()

if log_count > 100000:
    logger.warning(f"High log volume detected: {log_count} logs in 24h")
```

### 9. Use Exceptions Properly

```python
try:
    risky_operation()
except Exception as e:
    # Use exception() to capture stack trace
    logger.exception("Operation failed")
    
    # Or manually include context
    logger.error("Operation failed", {
        "error": str(e),
        "error_type": type(e).__name__
    })
```

### 10. Create Specialized Loggers

```python
# Create domain-specific loggers
auth_logger = logit.get_logger("auth", "auth.log")
payment_logger = logit.get_logger("payments", "payments.log")
api_logger = logit.get_logger("api", "api.log")

# Use them appropriately
auth_logger.info("User logged in")
payment_logger.info("Payment processed")
api_logger.info("API request received")
```

---

## Configuration

### File Logging Configuration

Logs are stored in `LOG_ROOT` directory (default: `<project>/logs/`):

```python
# In mojo/helpers/paths.py or settings
LOG_ROOT = "/var/log/myapp/"  # Custom log directory
```

**Log rotation settings** (in `mojo/helpers/logit.py`):
```python
MAX_LOG_SIZE = 10 * 1024 * 1024  # 10MB
LOG_BACKUP_COUNT = 3             # Keep 3 backup files
```

### Database Logging Configuration

No configuration needed - uses your Django database.

**Indexes** for performance:
- `created` - For time-based queries
- `level` - For filtering by severity
- `kind` - For filtering by category
- `uid` - For user-specific logs
- `(model_name, model_id)` - For model-specific logs
- `(created, kind)` - For common query patterns

---

## Troubleshooting

### File Logs Not Created

Check permissions on LOG_ROOT directory:
```bash
chmod 755 /path/to/logs
```

### Database Logs Not Appearing

Check migrations are applied:
```bash
python manage.py migrate logit
```

### High Disk Usage

Rotate or clean up old logs:
```bash
# Manually rotate logs
cd /path/to/logs
gzip *.log
```

### Slow Log Queries

Add indexes for your query patterns:
```python
# In migration
models.AddIndex(
    model_name='log',
    index=models.Index(fields=['created', 'kind', 'level'])
)
```
