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

Keys matching patterns like `password`, `token`, `secret`, `key` are automatically masked in log output:

```python
logit.info("Auth attempt", {"username": "alice", "password": "secret"})
# Logs: {"username": "alice", "password": "***"}
```

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
