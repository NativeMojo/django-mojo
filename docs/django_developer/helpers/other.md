# Other Helpers — Django Developer Reference

## redis

```python
from mojo.helpers.redis import get_client

r = get_client()
r.set("key", "value", ex=3600)
value = r.get("key")
```

Wraps the `redis-py` client with connection management from settings. Configure via:

```python
# settings.py
REDIS_HOST = "localhost"
REDIS_PORT = 6379
REDIS_DB = 0
```

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

```python
from mojo.helpers import paths

base = paths.get_base_dir()          # project root
media = paths.get_media_root()       # MEDIA_ROOT from settings
```
