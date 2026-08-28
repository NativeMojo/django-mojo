# Other Helpers — Django Developer Reference

## stats

```python
from mojo.helpers import stats

result = stats.aggregate(queryset, field="amount")
# Returns: {"sum": 1000, "avg": 50.0, "min": 10, "max": 200, "count": 20}
```

## qrcode

```python
from mojo.helpers import qrcode

# Generate QR code as base64 PNG
b64 = qrcode.generate("https://example.com")

# Generate and save to file
qrcode.generate_to_file("https://example.com", "/tmp/qr.png")
```

### build_vcard

Build a vCard 3.0 (default) or MeCard payload string from a structured dict. Pass the result as `data` to `generate_qrcode()` to render a contact QR code.

```python
from mojo.helpers.qrcode import build_vcard, generate_qrcode

payload_str = build_vcard({
    "name": "Jane Doe",
    "org": "Acme Inc",
    "phone": ["+15551234567", "+15557654321"],
    "email": "jane@acme.com",
})
qr = generate_qrcode(data=payload_str, fmt="png", error_correction="h")
```

- `name` is required; all other fields optional.
- `phone`, `email`, and `url` accept a string or a list of strings.
- `fmt="mecard"` produces a compact MeCard payload instead of vCard 3.0.
- Raises `QRCodeError` on missing `name` or unknown `fmt`.
- Values are escaped per RFC 6350 (vCard) or MeCard rules before concatenation.

## filetypes

```python
from mojo.helpers import filetypes

mime = filetypes.get_mime_type("document.pdf")   # "application/pdf"
ext = filetypes.get_extension("image/jpeg")       # ".jpg"
is_img = filetypes.is_image("image/png")          # True
```

## domain

Large utility module for domain parsing, email extraction, and domain validation.

```python
from mojo.helpers import domain

# Extract domain from email
d = domain.from_email("alice@example.com")  # "example.com"

# Validate domain format
is_valid = domain.is_valid("example.com")

# Parse domain parts
parts = domain.parse("subdomain.example.co.uk")
# {"subdomain": "subdomain", "domain": "example", "tld": "co.uk"}
```

## geoip

```python
from mojo.helpers.geoip import lookup

info = lookup("1.2.3.4")
# {"country": "US", "city": "New York", "lat": 40.71, "lon": -74.00, ...}
```

Requires a GeoIP database file configured in settings:

```python
GEOIP_PATH = "/path/to/GeoLite2-City.mmdb"
```

## sysinfo

```python
from mojo.helpers import sysinfo

info = sysinfo.get()
# {"hostname": "server1", "cpu_count": 4, "memory_gb": 16, ...}
```

## paths

`configure_paths(base_dir)` (called once from Django settings) sets module-level
globals for common project paths — `PROJECT_ROOT`, `VAR_ROOT`, `CONFIG_ROOT`,
`BIN_ROOT`, `MEDIA_ROOT`, `STATIC_ROOT`, and others.

```python
from mojo.helpers import paths

paths.PROJECT_ROOT    # project root
paths.MEDIA_ROOT      # VAR_ROOT / "media"
paths.CONFIG_ROOT     # committed config/ dir
paths.VAR_ROOT        # gitignored var/ dir (per-machine state)
```

`resolve_conf(name, var_root=None, config_root=None)` resolves a `.conf` filename
to its effective path, preferring a local override: `VAR_ROOT/name` if it exists,
else `CONFIG_ROOT/name`. Whole-file resolution — no per-key merge. Used by testit
to let `var/dev_server.conf` override the committed `config/dev_server.conf`; see
[testit Overview § Dev-server host/port](../testit/Overview.md#dev-server-hostport-dev_serverconf).

```python
conf_path = paths.resolve_conf("dev_server.conf")
```

## urls

`safe_nav_url(value, default="")` — the scheme guard for a caller-supplied
navigation target that the server is about to render into an `href` (or a
`<meta http-equiv=refresh>`). Django autoescaping stops attribute breakout but
does **not** neutralize a scheme-based payload, so `javascript:alert(1)` would
otherwise survive into the attribute and execute on click.

```python
from mojo.helpers import urls

redirect_url = urls.safe_nav_url(request.DATA.get("redirect"))
# "" when refused — wrap the link in {% if redirect_url %} so it is OMITTED
# rather than rendered dead.
```

| Input | Result |
|---|---|
| `https://example.com/x`, `http://localhost:3000/cb` | returned **unchanged** |
| `/dashboard`, `/a?b=c#d`, `dashboard/settings` | returned **unchanged** (no scheme) |
| `//example.com/x` | returned **unchanged** — see the off-origin note below |
| `javascript:`, `data:`, `vbscript:`, `mailto:`, `tel:`, `myapp://home` | `default` |
| `JaVaScRiPt:`, `java<TAB>script:`, leading-space/C0 `javascript:` | `default` |
| `None`, `""`, a `list`, a `dict`, an `int` | `default` |
| `http://[::1/x` (unparsable authority) | `default` |

**A value that passes is returned byte-identically** — it is never normalized to
an absolute URL, so relative destinations keep working for callers that depend
on them. Normalization happens only *inside* the judgment.

**The host is deliberately not restricted.** A legitimate cross-origin `https://`
destination must keep working. Host restriction is the separate, opt-in concern
of `ALLOWED_REDIRECT_URLS` / `AUTH_HANDOFF_ALLOWED_URLS` (see
`mojo.apps.account.services.redirect_allowlist`). Do not fold a host check in
here.

**Scheme-relative and path-relative values pass through unchanged and may still
resolve off-origin** — `//evil.test/x`, `/\evil.test/x` and `\\evil.test/x` all
parse scheme-less. That is coherent with the point above: a cross-origin
`https://evil.test` is explicitly permitted, so refusing its equivalent spelling
would not add a boundary. This is the one place the guard is looser than the
in-tree sibling `_safe_home_url` in `mojo/apps/shortlink/rest/redirect.py`, which
refuses protocol-relative and is deliberately **not** migrated to this helper —
its input is an admin-writable setting naming that deployment's own home page,
where an off-site value is genuinely unwanted.

**Browser-side twin.** `safeNavUrl()` in
`mojo/apps/account/templates/account/auth_base.html` is the same contract for the
hosted auth pages. The two must keep agreeing on what is refused; they differ
only in that the browser twin returns the resolved absolute href.

Current callers: `landing_context()` and `landing_redirect()` in
`mojo/apps/account/services/token_landing.py` — the shared machinery behind the
three emailed-token confirmation landings (email verify, email change, account
deactivation) and behind the `/auth` compatibility redirect that forwards a
legacy link's `?redirect=` passenger. The per-view `_render_verify` /
`_render_confirm` helpers those pages used to have were removed in #3257.
