# safe_fetch — Django Developer Reference

`mojo/helpers/safe_fetch.py` is the framework's one outbound fetcher for URLs somebody
else chose — a user-supplied page, a remote client's published metadata document, a
self-probe of a configured base URL. Anything that hands an attacker-influenced URL to
`requests` without this guard is an SSRF sink.

```python
from mojo.helpers.safe_fetch import safe_fetch

result, err = safe_fetch("https://example.com/thing.json", timeout=5)
if err:
    return {"error": err}
data = result.text
```

## What it refuses, and why

An SSRF attack points a server at its own private network: `http://169.254.169.254/`
for cloud instance credentials, `http://10.0.0.5/` for an internal admin panel,
`http://127.0.0.1:6379/` for Redis. A hostname check alone is not enough — a public
domain name can resolve to a private address, and a public URL can redirect to one.

`safe_fetch` therefore:

- **resolves the hostname and judges every address it gets back.** One private answer
  condemns the host. An IP literal is judged directly, without a lookup.
- **refuses an unresolvable host before contacting the transport.** Treating "I could
  not look it up" as "probably fine" is fail-open: an attacker's authoritative
  nameserver can SERVFAIL the guard's lookup and answer a private address to the
  transport's own lookup.
- **fails closed on anything it cannot parse.** An address string that is not an
  address counts as private.
- **re-checks every redirect hop** — absolute, relative (`/next`) and scheme-relative
  (`//host/x`) — with the same scheme and address rules as the first URL. Redirects are
  followed by the helper, never by `requests` (`allow_redirects=False` on every call).
- **caps redirects, body bytes and socket time.**

Blocked ranges (`BLOCKED_NETWORKS`), plus anything `ipaddress` calls private, loopback,
link-local, reserved or multicast, with IPv4-mapped IPv6 (`::ffff:10.0.0.1`) unwrapped
first:

`0.0.0.0/8` · `10.0.0.0/8` · `100.64.0.0/10` · `127.0.0.0/8` · `169.254.0.0/16` ·
`172.16.0.0/12` · `192.168.0.0/16` · `240.0.0.0/4` · `::1/128` · `fc00::/7` ·
`fe80::/10` · `2002::/16`

## Signature

```python
safe_fetch(url, timeout=DEFAULT_TIMEOUT, max_bytes=MAX_RAW_BYTES,
           max_redirects=MAX_REDIRECTS, headers=None, allow_hosts=None,
           schemes=DEFAULT_SCHEMES, resolver=None, transport=None)
```

| Parameter | Default | Meaning |
|---|---|---|
| `url` | — | The URL to fetch. Never trusted. |
| `timeout` | `DEFAULT_TIMEOUT` (`10`) | Passed straight to the transport — requests' per-socket-operation semantics. |
| `max_bytes` | `MAX_RAW_BYTES` (`1_048_576`) | Body cap. Bytes past the cap are dropped and `truncated` is set. |
| `max_redirects` | `MAX_REDIRECTS` (`3`) | Hops followed. `0` makes exactly one request. |
| `headers` | `None` | Merged over `{"User-Agent": DEFAULT_USER_AGENT}`; caller keys win. |
| `allow_hosts` | `None` | Hostnames exempt from the private/unresolvable refusal. See below. |
| `schemes` | `DEFAULT_SCHEMES` (`("http", "https")`) | Accepted schemes, at the initial URL **and** every hop. Pass `("https",)` so a redirect cannot downgrade the fetch. |
| `resolver` | `None` | `resolver(hostname)` → iterable of address **strings**. Defaults to `socket.getaddrinfo`. |
| `transport` | `None` | Anything exposing `requests.Session.get`. Defaults to a `requests.Session` the helper creates and closes. |

Returns `(result, error)`. Exactly one is `None`, and **nothing raises** for a bad URL
or a network failure — consumers never need to import `requests` to stay correct.

`result` is an `objict`:

| Key | Value |
|---|---|
| `url` | Final URL after redirects (not necessarily the one you passed) |
| `status_code` | Response status. A non-200 is a *result*, not an error — the caller decides. |
| `headers` | The response's header mapping (case-insensitive in production) |
| `content` | Body bytes, at most `max_bytes` |
| `text` | The response's own decoding of those capped bytes |
| `truncated` | `True` when more than `max_bytes` was available |

## Error strings

| String | Cause |
|---|---|
| `Unsupported scheme '<scheme>'. Only <schemes> are allowed.` | Initial URL's scheme is not in `schemes` |
| `Invalid URL — no hostname found` | Initial URL is unparsable or has no host |
| `Cannot fetch private or internal addresses` | Initial host is, or resolves to, a blocked address |
| `Could not connect to <hostname>` | Initial host does not resolve, a hop's host does not resolve, or the transport raised `ConnectionError` (including `ConnectTimeout`) |
| `Redirect target is not a valid URL` | A `Location` that will not parse, or resolves to something with no host |
| `Redirect to unsupported scheme '<scheme>'` | A hop leaves `schemes` |
| `Redirect target is a private or internal address` | A hop's host is, or resolves to, a blocked address |
| `Too many redirects (max <n>)` | `max_redirects` exhausted |
| `Request timed out after <timeout>s` | Transport raised `Timeout` |
| `Request failed` | Any other `requests` exception |

## The seams: `resolver`, `transport`, `allow_hosts`, `schemes`

`resolver` and `transport` exist so tests can cover every branch with no network and no
patching of process-wide state (which the default test tier forbids). A resolver is any
callable taking a hostname and returning address strings; a transport is any object with
`get(url, timeout=, headers=, allow_redirects=False, stream=True)`.

A bare `requests.Response` is enough of a fake. **Its `headers` must be a
`CaseInsensitiveDict`** — `Response.is_redirect` tests `"location" in self.headers`, so a
plain dict with `Location` is silently *not* a redirect and your redirect test passes for
the wrong reason:

```python
import requests
from requests.structures import CaseInsensitiveDict

def _response(status, headers=None, body=b""):
    resp = requests.Response()
    resp.status_code = status
    resp.headers = CaseInsensitiveDict(headers or {})
    resp._content = body
    resp._content_consumed = True
    resp.encoding = "utf-8"
    return resp
```

`allow_hosts` exempts named hosts from the private/unresolvable refusal, at the initial
URL and at every hop. It exists for the case where the configured address legitimately
*is* private — a service probing its own `BASE_URL`. Three things to know:

- entries are unbracketed `urlparse(...).hostname` values — an IPv6 base URL is listed as
  `::1`, not `[::1]`
- the exemption covers every port on that host
- an allowed host is not resolved or checked **at all**

Whether the exemption is warranted is the caller's decision; the helper just honours it.

## Known limits

1. **Check-then-connect.** The guard's lookup and the transport's own lookup are two
   separate DNS queries. A record that changes between them (DNS rebinding) is not
   caught. Pinning the checked address through to the socket is a different contract
   (HTTPS only, no redirects) and is not what this helper does.
2. **The byte cap counts decoded bytes.** `iter_content` decodes content-encoding as it
   streams, so a compressed body cannot inflate past the cap — exact under the
   `urllib3 >= 2.7.0` pin in `pyproject.toml`. Memory is bounded by `max_bytes` plus one
   chunk.
3. **`timeout` bounds each socket operation, not the transfer.** A server dripping one
   byte per interval keeps a fetch alive far longer than `timeout` seconds. A wall-clock
   transfer budget is not implemented.

## Lower-level entry points

```python
from mojo.helpers.safe_fetch import is_blocked_ip, is_private_hostname

is_blocked_ip("169.254.169.254")          # True — address object or string
is_private_hostname("internal.example")   # resolves, then judges
```

`is_private_hostname` answers *private* only: a hostname that does not resolve is
`False` here. `safe_fetch` treats unresolvable as a refusal in its own right.

## Callers

- `mojo/apps/assistant/services/tools/web.py` — the `browse_url` assistant tool
- `mojo/apps/assistant/services/tools/docs.py` — `_validate_base_url`
