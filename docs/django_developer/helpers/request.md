# request — Django Developer Reference

## Import

```python
from mojo.helpers import request as req_helpers
# or specific imports
from mojo.helpers.request import get_remote_ip, get_user_agent, get_device_id
```

## Client Information

### `get_remote_ip(request)`
Returns the real client IP from the proxy-authoritative `X-Real-IP` header, falling back
to `REMOTE_ADDR`. The result is normalized: surrounding whitespace is stripped, an `IP:port`
suffix is removed, bracketed IPv6 is unwrapped, and IPv4-mapped IPv6 (`::ffff:1.2.3.4`) is
collapsed to plain IPv4.

`X-Forwarded-For` is **not consulted** — its leftmost entry is client-controlled and
spoofable. `X-Real-IP` must be set by the reverse proxy to the true client address, which
the shipped `asgi.inc` does (`proxy_set_header X-Real-IP $remote_addr;`).

**Deployment requirement:** your nginx (or equivalent) config must set `X-Real-IP` to
`$remote_addr` and must overwrite any client-supplied value. Without this, `request.ip`
falls back to `REMOTE_ADDR` (which is correct in direct-connect setups but may be the
proxy IP behind a load balancer).

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
