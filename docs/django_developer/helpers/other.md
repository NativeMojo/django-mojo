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

## ua (User Agent Parsing)

```python
from mojo.helpers import ua

parsed = ua.parse("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)...")
# {"browser": "Chrome", "os": "macOS", "device": "desktop"}
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
