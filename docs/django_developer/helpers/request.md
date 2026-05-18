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

## Webhook Signature Verification

### `verify_signed_request(request, secret)`

Returns `True` if the raw request body matches the `X-Mojo-Signature` header using HMAC-SHA256 keyed on `secret`. Returns `False` (never raises) when the secret is `None`, the header is absent, or the signature does not match. Uses `hmac.compare_digest` for constant-time comparison.

```python
from mojo.helpers.request import verify_signed_request
from mojo.apps.account.models import Group

group = Group.objects.filter(uuid=group_uuid).first()
if not verify_signed_request(request, group.get_webhook_secret() if group else None):
    raise merrors.PermissionDeniedException("invalid signature", 401, 401)
```

The secret is typically fetched with `group.get_webhook_secret()` (defaults to `auto_create=False`, so a missing secret correctly returns `False` rather than minting one). See [Webhook Signing](../account/webhook_signing.md) for the full pattern.

## request.DATA

`request.DATA` is an [`objict`](objict.md) — a dict subclass with attribute access, dot-notation nested keys, and type-safe getters. Set by `MojoMiddleware` from all request sources (POST body, GET params, JSON body).

```python
# All equivalent
request.DATA.get("field")
request.DATA["field"]
request.DATA.field

# Type-safe access (converts string query params to the right type)
page = request.DATA.get_typed("page", 1, int)
size = request.DATA.get_typed("page_size", 20, int)

# Nested dot-notation keys (client sent "address.city=Austin")
city = request.DATA.get("address.city")
last = request.DATA.get("metadata.user.last_name")
```

See [objict.md](objict.md) for the full reference.
