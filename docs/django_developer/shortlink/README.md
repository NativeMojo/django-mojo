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

# Rendition shortlink (same, for a FileRendition)
url = shorten(rendition=rendition_obj, source="fileman")

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
shorten(url="", file=None, rendition=None, source="", expire_days=3, expire_hours=0,
        metadata=None, track_clicks=False, resolve_file=True,
        bot_passthrough=False, user=None, group=None, base_url=None)
```

| Param | Default | Description |
|---|---|---|
| `url` | `""` | Destination URL. Required unless `file` or `rendition` is provided. |
| `file` | `None` | `fileman.File` instance for file-sharing shortlinks. |
| `rendition` | `None` | `fileman.FileRendition` instance for rendition-sharing shortlinks. Pass instead of (or alongside) `file`. |
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

**Raises:** `ValueError` if none of `url`, `file`, or `rendition` is provided.

---

## Models

### ShortLink

The core model. Located at `mojo/apps/shortlink/models/shortlink.py`.

`RestMeta` permissions:

- `VIEW_PERMS = ["manage_shortlinks", "owner"]`
- `SAVE_PERMS = ["manage_shortlinks", "owner"]`

This means users with owner access can operate on their own `ShortLink` records (where `shortlink.user == request.user`) without global `manage_shortlinks`.

| Field | Type | Description |
|---|---|---|
| `code` | CharField(10) | Unique 7-char alphanumeric code. Auto-generated. |
| `url` | TextField | Destination URL. Empty when using file-only links. |
| `source` | CharField(50) | Traceability tag (indexed). |
| `user` | FK → User | Creator (nullable). |
| `group` | FK → Group | Group scope (nullable). |
| `file` | FK → File | Linked fileman.File (nullable). |
| `rendition` | FK → FileRendition | Linked fileman.FileRendition (nullable). Used for rendition share links. |
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
link = ShortLink.create(file=file_obj, source="fileman", resolve_file=True)
link = ShortLink.create(rendition=rendition_obj, source="fileman", resolve_file=True)

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

Read-only via REST (`CAN_UPDATE = CAN_CREATE = CAN_DELETE = False`).

Note: click-history REST access remains admin-scoped (`manage_shortlinks`) rather than owner-scoped.

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
- Apple Messages: `iMessage`, `iMessageFetchAgent`, `MessagesURLPreview`
- Signal
- Google Chat: `Google-HTTP-Java-Client`, `GoogleChat`
- Teams/Outlook preview: `SkypeUriPreview`, `Microsoft Teams`, `ms-office`
- Gmail preview: `GoogleImageProxy`, `Gmail`
- Yahoo Mail: `YahooMailProxy`
- Thunderbird, Spark, `notion.so`, `linear.app`, `ZoomWebhook`

When a bot is detected and `bot_passthrough=False`, the endpoint returns an HTML page with OG meta tags and a `<meta http-equiv="refresh">` fallback redirect.

Use `is_bot_user_agent(ua_string)` to check manually:

```python
from mojo.apps.shortlink.models import is_bot_user_agent

is_bot_user_agent("Slackbot-LinkExpanding 1.0")  # True
is_bot_user_agent("Mozilla/5.0 (iPhone; ...)")   # False
```

---

## File and Rendition Shortlinks

Link a `fileman.File` or `fileman.FileRendition` instead of (or alongside) a URL:

```python
from mojo.apps.fileman.models import File, FileRendition

file = File.objects.get(pk=123)
rendition = FileRendition.objects.get(pk=456)

# Dynamic: generates a fresh download URL on each click (default)
url = shorten(file=file, source="fileman")
url = shorten(rendition=rendition, source="fileman")

# Snapshot: captures the download URL at creation time
url = shorten(file=file, source="fileman", resolve_file=False)
url = shorten(rendition=rendition, source="fileman", resolve_file=False)
```

Dynamic resolution (`resolve_file=True`, the default) calls `file.get_direct_download_url()` or `rendition.get_direct_download_url()` on every click. Using `get_direct_download_url()` (rather than `generate_download_url()`) avoids infinite recursion when the resolver is called from within the shortlink system itself.

Snapshot mode (`resolve_file=False`) captures the URL once at creation — useful when the URL is stable and presigning is not required.

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
- If `track_clicks=True` and `link.user` exists:
  - `sl:click:<code>` in account `user-<user.pk>` (per-link user analytics)
  - Retention: expires 7 days after link expiry
  - If link never expires, this per-link metric is stored without TTL

Every `shorten()` call records:

- `shortlink:created` — global creation counter

Shortlink metrics use `category="shortlinks"`. Global counters use `account="global"`.

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
| `SHORTLINK_SITE_NAME` | `None` | Product name shown on the "link unavailable" page. Unset = no brand line. |
| `SHORTLINK_HOME_URL` | `None` | Target of the "back to site" button on that page. Unset = no button. |
| `BASE_URL` | `"/"` | Fallback if no shortlink-specific base setting is configured. |

`SHORTLINK_FALLBACK_URL` was removed in 1.2.51 — see below.

---

## The "link unavailable" page

`GET /s/<code>` returns **HTTP 404** and renders `shortlink/link_unavailable.html`
whenever a link cannot be used — unknown code, expired, `is_active=False`, or a
row that resolves to no destination. All four render the **same body**, so the
response never reveals whether a code was ever real. The response also carries
`Cache-Control: no-store` and the page sets `<meta name="robots" content="noindex">`.

Until 1.2.51 these cases issued a `302` to `SHORTLINK_FALLBACK_URL` (falling back
to `BASE_URL`, then `/`), which dumped the visitor on the site root with no
explanation. That setting is gone; the endpoint no longer redirects on failure.

### Customizing it

For small changes, set `SHORTLINK_SITE_NAME` and `SHORTLINK_HOME_URL`. The
shipped page works with neither set — it just omits the brand line and the button.

`SHORTLINK_HOME_URL` intentionally has **no fallback to `BASE_URL`**. A shortlink
host is often a bare redirect domain with nothing served at `/`, and pointing a
"back to site" button there would recreate the behavior this replaced. Set it
only when there is a real destination.

To replace the page entirely, override the template. The most reliable way is a
directory in `TEMPLATES[0]["DIRS"]`, which always wins over app templates:

```python
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],   # put your copy at templates/shortlink/link_unavailable.html
    "APP_DIRS": True,
    ...
}]
```

Alternatively, ship `templates/shortlink/link_unavailable.html` inside one of your
own apps — but that only wins if the app is listed **before** `mojo.apps.shortlink`
in `INSTALLED_APPS`, since `APP_DIRS` resolves in installed order.

Your template receives `site_name` and `home_url` in its context. Keep it
self-contained (inline CSS, no external asset requests) — it is served to
visitors who may have no relationship with your app, and to link-preview bots.

---

## Permissions

- `manage_shortlinks` — full access to shortlinks and click-history REST endpoints
- `owner` — access to own shortlinks on `/api/shortlink/link*` (when `shortlink.user == request.user`)
- `shortlink` click-history endpoints remain `manage_shortlinks` scoped
- The redirect endpoint (`/s/<code>`) is public — no authentication required

---

## Setup

1. Add `"mojo.apps.shortlink"` to `INSTALLED_APPS`
2. Run `python manage.py makemigrations shortlink`
3. Run `python manage.py migrate`
