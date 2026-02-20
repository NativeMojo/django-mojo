# request — Django Developer Reference

## Import

```python
from mojo.helpers import request as req_helpers
# or specific imports
from mojo.helpers.request import get_remote_ip, get_user_agent, get_device_id
```

## Client Information

### `get_remote_ip(request)`
Returns the real client IP, respecting proxy headers (`X-Forwarded-For`, `X-Real-IP`).

```python
ip = get_remote_ip(request)
```

### `get_ip_sources(request)`
Returns all IP-related metadata as a dict (useful for logging/audit).

```python
sources = get_ip_sources(request)
# {"remote_addr": "1.2.3.4", "x_forwarded_for": "1.2.3.4, 10.0.0.1", ...}
```

### `get_user_agent(request)`
Returns the raw User-Agent string.

```python
ua = get_user_agent(request)
```

### `parse_user_agent(request)`
Returns a structured dict with `browser`, `os`, and `device` info.

```python
ua_info = parse_user_agent(request)
# {"browser": "Chrome 120", "os": "macOS 14", "device": "desktop"}
```

### `get_device_id(request)`
Returns the device ID from `X-Device-Id` header or `device_id` param.

```python
device_id = get_device_id(request)
```

### `get_referer(request)`
Returns the HTTP Referer header value.

## Request Data Parsing

`request.DATA` is already set by `MojoMiddleware`. In rare cases where you need to parse outside of middleware:

```python
from mojo.helpers.request_parser import parse_request_data

data = parse_request_data(request)  # returns objict
```

## objict

`request.DATA` is an `objict` instance — a dict subclass with attribute access and dot-notation support.

```python
# These are equivalent:
request.DATA.get("field")
request.DATA["field"]
request.DATA.field

# Typed access with default
request.DATA.get_typed("page_size", 10, int)

# Nested (key "user.name" is auto-expanded)
request.DATA.user.name
```
