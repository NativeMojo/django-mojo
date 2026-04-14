# logit — Django Developer Reference

## Overview

`logit` is the framework's structured logging system. All log calls are automatically routed to the appropriate log file based on level.

## Import

```python
from mojo.helpers import logit
```

## Log Levels & Routing

| Function | Log File | Use Case |
|---|---|---|
| `logit.info(msg, ...)` | `mojo.log` | General informational messages |
| `logit.warn(msg, ...)` | `mojo.log` | Warnings (also `logit.warning()`) |
| `logit.error(msg, ...)` | `error.log` | Errors requiring attention |
| `logit.exception(msg, ...)` | `error.log` | Errors with full stack trace |
| `logit.debug(msg, ...)` | `debug.log` | Development/debugging output |

## Basic Usage

```python
from mojo.helpers import logit

logit.info("Processing order", order_id=42)
logit.warn("Rate limit approaching", user_id=5)
logit.error("Payment failed", order_id=42, reason="card_declined")
logit.debug("Query result", count=100)
```

## Multiple Arguments

Pass additional values as positional args — they are appended to the message:

```python
logit.info("User action", user, action, extra_data)
```

## Sensitive Data Masking

### String-based masking — `mask_sensitive_data(text)`

Regex-based masker for log strings. Keys matching patterns like `password`, `token`, `api_key`, `secret` are automatically masked:

```python
logit.info("Auth attempt", {"username": "alice", "password": "secret"})
# Logs: {"username": "alice", "password": "*****"}
```

### Dict-based sanitization — `sanitize_dict(data)`

Strips sensitive keys from a dict or nested dict, returning a sanitized copy. Use this before storing or logging any structured dict that may contain user-supplied fields:

```python
from mojo.helpers.logit import sanitize_dict

safe = sanitize_dict({"username": "alice", "password": "secret", "token": "abc123"})
# Returns: {"username": "alice", "password": "*****", "token": "*****"}
```

Sanitized keys include: `password`, `pwd`, `new_password`, `current_password`, `secret`, `token`, `access_token`, `api_key`, `authorization`, `ssn`, `credit_card`, `card_number`, `pin`, `cvv`. Matching is case-insensitive and the function recurses into nested dicts.

Both the incident system (`report_event` kwargs) and the `Log` model (`payload` field) automatically apply sanitization — you do not need to call `sanitize_dict` manually when using those APIs.

## Named Loggers

Create a named logger writing to a specific file:

```python
logger = logit.get_logger("myapp", "myapp.log")
logger.info("App-specific event")
```

## Pretty Printing

```python
logit.pretty_print({"key": "value", "nested": {"a": 1}})
formatted = logit.pretty_format(my_dict)
```

## In Models

`MojoModel` provides `self.log()` which wraps logit and writes to the `Log` database model:

```python
# In a model method
self.log(log="Order processed", kind="order:processed")
```

Use `logit` directly for framework-level or service-layer logging that doesn't need a DB record.
