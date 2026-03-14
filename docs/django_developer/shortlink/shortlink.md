# ShortLink App — Django Developer Reference

## Overview

The shortlink app provides URL shortening with OG metadata previews for bot/crawler user-agents, optional click tracking, file linking via fileman, expiry control, and metrics integration.

Short URLs follow the pattern: `https://yourdomain.com/s/Xk9mR2p`

---

## Quick Start

```python
from mojo.apps.shortlink import shorten

# Basic shortlink (expires in 3 days)
url = shorten("https://example.com/verify?token=abc", source="sms")

# With custom OG metadata for rich previews
url = shorten(
    "https://example.com/invoice/123",
    source="email",
    metadata={"og:title": "Invoice #123", "og:description": "View your invoice"},
)

# File shortlink (resolves download URL dynamically on each click)
url = shorten(file=file_obj, source="fileman")

# Transactional link — skip bot detection, just redirect
url = shorten("https://example.com/reset?t=xyz", source="sms", bot_passthrough=True)

# With click tracking
url = shorten("https://example.com/promo", source="email", track_clicks=True)

# Never expires
url = shorten("https://example.com/docs", source="web", expire_days=0, expire_hours=0)

# Custom base URL
url = shorten("https://example.com/page", source="partner", base_url="https://short.co")
```

---

## `shorten()` API

```python
shorten(url="", file=None, source="", expire_days=3, expire_hours=0,
        metadata=None, track_clicks=False, resolve_file=True,
        bot_passthrough=False, user=None, group=None, base_url=None)
```

| Param | Default | Description |
|---|---|---|
| `url` | `""` | Destination URL. Required unless `file` is provided. |
| `file` | `None` | `fileman.File` instance for file-sharing shortlinks. |
| `source` | `""` | Traceability tag: `"sms"`, `"email"`, `"fileman"`, etc. Used in metrics. |
| `expire_days` | `3` | Days until expiry. Set both `expire_days=0` and `expire_hours=0` for no expiry. |
| `expire_hours` | `0` | Additional hours until expiry. Combined with `expire_days`. |
| `metadata` | `None` | Dict of OG/Twitter Card tags, e.g. `{"og:title": "My Page", "og:image": "https://..."}`. |
| `track_clicks` | `False` | Log each visit with IP, user-agent, referer, and bot detection. |
| `resolve_file` | `True` | When `file` is set: `True` = generate fresh download URL per click, `False` = snapshot URL at creation. |
| `bot_passthrough` | `False` | Skip bot detection and OG preview — always redirect. Use for transactional links. |
| `is_protected` | `False` | Protected links are not deleted by the automatic cleanup job. |
| `user` | `None` | User who created the link. |
| `group` | `None` | Group scope for permissions. |
| `base_url` | `None` | Override base URL. Default: `SHORTLINK_BASE_URL` or `BASE_URL` from settings. |

**Returns:** Full short URL string, e.g. `"https://itf.io/s/Xk9mR2p"`

**Raises:** `ValueError` if neither `url` nor `file` is provided.

---

## Models

### ShortLink

The core model. Located at `mojo/apps/shortlink/models/shortlink.py`.

| Field | Type | Description |
|---|---|---|
| `code` | CharField(10) | Unique 7-char alphanumeric code. Auto-generated. |
| `url` | TextField | Destination URL. Empty when using file-only links. |
| `source` | CharField(50) | Traceability tag (indexed). |
| `user` | FK → User | Creator (nullable). |
| `group` | FK → Group | Group scope (nullable). |
| `file` | FK → File | Linked fileman.File (nullable). |
| `hit_count` | IntegerField | Total resolve count. Incremented atomically via `F()`. |
| `expires_at` | DateTimeField | When the link expires. `null` = never. |
| `is_active` | BooleanField | Soft-delete flag. |
| `track_clicks` | BooleanField | Whether to log individual clicks. |
| `resolve_file` | BooleanField | Dynamic vs snapshot file URL resolution. |
| `bot_passthrough` | BooleanField | Skip bot detection entirely. |
| `is_protected` | BooleanField | Protected from automatic cleanup job deletion. |
| `metadata` | JSONField | OG/Twitter Card tags and scraped cache. |

**Key methods:**

```python
link = ShortLink.create(url="...", source="sms", expire_days=3)

# Resolve: returns URL, increments hit_count, records metric. None if expired/inactive.
destination = link.resolve()

# Log a click (only if track_clicks=True)
click = link.log_click(request)

# Get merged OG metadata (custom keys override scraped keys)
og = link.get_og_metadata()

# Check expiry
link.is_expired  # property
```

### ShortLinkClick

Per-click record. Only created when `track_clicks=True`. Located at `mojo/apps/shortlink/models/click.py`.

| Field | Type | Description |
|---|---|---|
| `shortlink` | FK → ShortLink | Parent link. |
| `ip` | GenericIPAddressField | Visitor IP. |
| `user_agent` | TextField | Truncated to 1000 chars. |
| `referer` | TextField | HTTP referer, truncated to 2000 chars. |
| `is_bot` | BooleanField | Auto-detected from user-agent. |
| `created` | DateTimeField | Click timestamp. |

Read-only via REST (`CAN_SAVE = CAN_CREATE = False`).

---

## OG Metadata

Metadata is stored in a flat JSONField. Any key starting with `og:` or `twitter:` is rendered as a `<meta>` tag for bot user-agents.

### Custom metadata (set at creation)

```python
url = shorten(
    "https://example.com/page",
    source="email",
    metadata={
        "og:title": "My Page",
        "og:description": "A description for link previews",
        "og:image": "https://example.com/image.jpg",
        "twitter:card": "summary_large_image",
    },
)
```

### Async scraping

When no custom `og:*` keys are provided and `bot_passthrough=False`, an async job is fired to scrape OG tags from the destination URL. Scraped tags are stored under `metadata["_scraped"]` and merged at render time. Custom keys always override scraped keys.

The scraper:
- Uses stdlib `urllib.request` (no external dependencies)
- Has a 5-second timeout and 256 KB read limit
- Only parses `text/html` responses
- Rejects private/internal IPs (SSRF protection)
- Runs via the jobs system with 2 retries and 15-second max execution

### Metadata merge order

When a bot requests a shortlink, `get_og_metadata()` returns:

1. Start with scraped tags (`metadata["_scraped"]`)
2. Override with custom tags (any non-`_` prefixed key in `metadata`)

This means you can provide a custom `og:title` while letting the scraper fill in `og:image` and `og:description` automatically.

---

## Bot Detection

The redirect endpoint checks the `User-Agent` header against known bot signatures:

- Slackbot, Twitterbot, facebookexternalhit, LinkedInBot, Discordbot
- TelegramBot, WhatsApp, Applebot, Googlebot, Instagram
- `com.google.android.apps.messaging` (Android Messages)

When a bot is detected and `bot_passthrough=False`, the endpoint returns an HTML page with OG meta tags and a `<meta http-equiv="refresh">` fallback redirect.

Use `is_bot_user_agent(ua_string)` to check manually:

```python
from mojo.apps.shortlink.models import is_bot_user_agent

is_bot_user_agent("Slackbot-LinkExpanding 1.0")  # True
is_bot_user_agent("Mozilla/5.0 (iPhone; ...)")   # False
```

---

## File Shortlinks

Link a `fileman.File` instead of (or alongside) a URL:

```python
from mojo.apps.fileman.models import File

file = File.objects.get(pk=123)

# Dynamic: generates a fresh download URL on each click
url = shorten(file=file, source="fileman")

# Snapshot: captures the download URL at creation time
url = shorten(file=file, source="fileman", resolve_file=False)
```

Dynamic resolution (`resolve_file=True`, the default) calls `file.generate_download_url()` on every click. This is ideal for S3 presigned URLs that expire.

Snapshot mode (`resolve_file=False`) captures the URL once at creation — useful when the URL is stable.

---

## Click Tracking

Off by default. Enable per-link:

```python
url = shorten("https://example.com/promo", source="email", track_clicks=True)
```

When enabled, each visit creates a `ShortLinkClick` record with IP, user-agent, referer, and bot detection. Access via REST or directly:

```python
from mojo.apps.shortlink.models import ShortLink

link = ShortLink.objects.get(code="Xk9mR2p")
clicks = link.clicks.all()  # reverse FK: ShortLinkClick queryset
bot_clicks = link.clicks.filter(is_bot=True).count()
```

---

## Metrics Integration

Every `resolve()` call records Redis time-series metrics via `mojo.apps.metrics`:

- `shortlink:click` — global click counter
- `shortlink:click:<source>` — per-source counter (e.g. `shortlink:click:sms`)

Every `shorten()` call records:

- `shortlink:created` — global creation counter

All metrics use `category="shortlinks"` and `account="global"`.

---

## Expiry

Expiry is computed from `expire_days` and `expire_hours` combined:

```python
# Expires in 3 days (default)
shorten("https://...", source="sms")

# Expires in 2 hours
shorten("https://...", source="sms", expire_days=0, expire_hours=2)

# Expires in 1 day and 6 hours (30 hours total)
shorten("https://...", source="sms", expire_days=1, expire_hours=6)

# Never expires
shorten("https://...", source="web", expire_days=0, expire_hours=0)
```

Expired links return `None` from `resolve()` and redirect to the fallback URL.

---

## Automatic Cleanup

A cron job runs daily at 3:30 AM to delete expired shortlinks that have been expired for more than 7 days. This keeps the database clean without losing links that just expired.

**Protection:** Set `is_protected=True` to prevent a link from being auto-deleted, even after it expires. Useful for audit trails or links you may want to reactivate later.

```python
# This link will never be auto-deleted
url = shorten("https://example.com/audit", source="legal", is_protected=True)

# Protect an existing link
link.is_protected = True
link.save(update_fields=["is_protected", "modified"])
```

Protected links still expire normally (they stop redirecting), they just aren't removed from the database.

The cleanup job is defined in `mojo/apps/shortlink/cronjobs.py` and the worker logic is in `mojo/apps/shortlink/asyncjobs.py`.

---

## Settings

| Setting | Default | Description |
|---|---|---|
| `SHORTLINK_BASE_URL` | `None` | Base URL for generated short links (e.g. `https://itf.io`). |
| `SHORTLINK_FALLBACK_URL` | `None` | Where to redirect invalid/expired codes. |
| `BASE_URL` | `"/"` | Fallback if neither shortlink-specific setting is configured. |

---

## Permissions

- `manage_shortlinks` — required to view/manage shortlinks via REST API
- The redirect endpoint (`/s/<code>`) is public — no authentication required

---

## Setup

1. Add `"mojo.apps.shortlink"` to `INSTALLED_APPS`
2. Run `python manage.py makemigrations shortlink`
3. Run `python manage.py migrate`
