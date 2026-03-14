# ShortLink — REST API Reference

## Overview

The shortlink app provides URL shortening with automatic rich previews for messaging platforms (Slack, iMessage, WhatsApp, etc.), optional click analytics, and configurable expiry.

## Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/s/<code>` | public | Resolve short code and redirect (or OG preview for bots) |
| GET | `/api/shortlink/link` | `manage_shortlinks` | List short links |
| POST | `/api/shortlink/link` | `manage_shortlinks` | Create short link via REST model endpoint |
| GET | `/api/shortlink/link/<id>` | `manage_shortlinks` | Get short link details |
| POST/PUT | `/api/shortlink/link/<id>` | `manage_shortlinks` | Update short link |
| DELETE | `/api/shortlink/link/<id>` | `manage_shortlinks` | Delete short link |
| POST | `/api/shortlink/link/create` | authenticated | Create a short URL string via helper endpoint |
| GET | `/api/shortlink/history` | `manage_shortlinks` | List click history |
| GET | `/api/shortlink/history/<id>` | `manage_shortlinks` | Get click history record |

---

## Redirect Endpoint

**GET** `/s/<code>`

Public endpoint — no authentication required.

This is the short URL that users click. Behavior depends on the visitor:

| Visitor | Behavior |
|---|---|
| Normal browser | 302 redirect to destination URL |
| Bot/crawler (Slack, Facebook, Twitter, etc.) | Returns HTML page with OG meta tags, then auto-redirects via `<meta http-equiv="refresh">` |
| Bot with `bot_passthrough=True` | 302 redirect (same as normal browser) |
| Invalid/expired code | 302 redirect to fallback URL |

### Bot Preview HTML

When a bot user-agent is detected, the response is a minimal HTML page containing:

```html
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Page Title</title>
<meta property="og:title" content="Page Title">
<meta property="og:description" content="A description">
<meta property="og:image" content="https://example.com/image.jpg">
<meta name="twitter:card" content="summary_large_image">
<meta http-equiv="refresh" content="0;url=https://destination.com/page">
</head>
<body>
<p>Redirecting to <a href="https://destination.com/page">https://destination.com/page</a></p>
</body>
</html>
```

This allows messaging platforms to show rich link previews with custom titles, descriptions, and images, while the user still ends up at the destination URL.

### Detected Bot User-Agents

Slackbot, Twitterbot, facebookexternalhit, LinkedInBot, Discordbot, TelegramBot, WhatsApp, Applebot, Googlebot, Instagram, Android Messages (`com.google.android.apps.messaging`).

---

## Managing ShortLinks (REST CRUD)

Requires the `manage_shortlinks` permission.

### List ShortLinks

**GET** `/api/shortlink/link`

```json
{
  "status": true,
  "data": [
    {
      "id": 1,
      "code": "Xk9mR2p",
      "url": "https://example.com/page",
      "source": "email",
      "hit_count": 42,
      "expires_at": "2026-03-16T00:00:00Z",
      "is_active": true,
      "created": "2026-03-13T12:00:00Z"
    }
  ]
}
```

Supports filtering: `?source=sms`, `?is_active=true`, `?search=example.com`

Use `?graph=default` for full details including metadata, user, and group.

### Get ShortLink Detail

**GET** `/api/shortlink/link/<id>`

### Create ShortLink

**POST** `/api/shortlink/link`

```json
{
  "url": "https://example.com/page",
  "source": "email",
  "metadata": {
    "og:title": "My Page",
    "og:description": "Custom preview text",
    "og:image": "https://example.com/preview.jpg"
  }
}
```

### Update ShortLink

**POST** `/api/shortlink/link/<id>`

```json
{
  "is_active": false
}
```

### Delete ShortLink

**DELETE** `/api/shortlink/link/<id>`

---

## Quick Create Endpoint

**POST** `/api/shortlink/link/create`

Requires authentication (`@requires_auth`) and reads input from `request.DATA`.

Use this endpoint when you want a ready-to-use short URL string in one call.

```json
{
  "url": "https://example.com/page",
  "source": "email",
  "expire_days": 3,
  "expire_hours": 0,
  "metadata": {
    "og:title": "My Page"
  },
  "track_clicks": true,
  "resolve_file": true,
  "bot_passthrough": false,
  "is_protected": false,
  "base_url": "https://itf.io"
}
```

You can create file-based shortlinks by passing a `file` id:

```json
{
  "file": 124,
  "source": "fileman",
  "resolve_file": true
}
```

**Response**

```json
{
  "status": true,
  "data": {
    "short_link": "https://itf.io/s/Xk9mR2p",
    "original_url": "https://example.com/page"
  }
}
```

---

## View Click History

Requires the `manage_shortlinks` permission. Only available for links created with `track_clicks=True`.

**GET** `/api/shortlink/history?shortlink=<id>`

```json
{
  "status": true,
  "data": [
    {
      "id": 1,
      "ip": "203.0.113.45",
      "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
      "referer": "https://mail.google.com/",
      "is_bot": false,
      "created": "2026-03-13T14:30:00Z",
      "shortlink": {
        "id": 5,
        "code": "Xk9mR2p",
        "url": "https://example.com/page",
        "source": "email",
        "hit_count": 42,
        "is_active": true
      }
    }
  ]
}
```

Click records are read-only — they cannot be created, updated, or deleted via the API.

---

## Response Graphs

### ShortLink Graphs

| Graph | Fields |
|---|---|
| `default` | All fields including metadata, user (basic), group (basic) |
| `basic` | id, code, url, source, hit_count, is_active |
| `list` | id, code, url, source, hit_count, expires_at, is_active, created, user (basic), group (basic) |

Usage: `?graph=default`, `?graph=list`, `?graph=basic`

### ShortLinkClick Graphs

| Graph | Fields |
|---|---|
| `default` | id, ip, user_agent, referer, is_bot, created, shortlink (basic) |

---

## OG Metadata

The `metadata` field is a flat JSON object. Any key starting with `og:` or `twitter:` is rendered as a meta tag for bot previews.

### Supported Tags

Standard OpenGraph and Twitter Card tags:

```json
{
  "og:title": "Page Title",
  "og:description": "A description of the page",
  "og:image": "https://example.com/image.jpg",
  "og:url": "https://example.com/page",
  "og:type": "website",
  "og:site_name": "Example",
  "twitter:card": "summary_large_image",
  "twitter:title": "Page Title",
  "twitter:description": "A description",
  "twitter:image": "https://example.com/image.jpg"
}
```

### Auto-Scraping

If no custom `og:*` tags are provided when the link is created, the system automatically scrapes OG tags from the destination URL in the background. Scraped tags are stored internally and served to bots.

If you provide custom tags, they take priority — scraped tags only fill in gaps. For example, you can set a custom `og:title` and let the scraper fill in `og:image` and `og:description` from the destination page.

### Disabling Bot Previews

Set `bot_passthrough=True` on the link to skip bot detection entirely. Bots will receive a plain 302 redirect like any other visitor. Use this for transactional links (password resets, verification emails) where preview content is not needed.

---

## Expiry

Links expire based on `expire_days` and `expire_hours` (combined):

| expire_days | expire_hours | Result |
|---|---|---|
| 3 (default) | 0 (default) | Expires in 3 days |
| 0 | 2 | Expires in 2 hours |
| 1 | 6 | Expires in 30 hours |
| 0 | 0 | Never expires |

Expired links redirect to the fallback URL (configured via `SHORTLINK_FALLBACK_URL` or `BASE_URL`).

---

## Settings

Configure in your Django settings or via the MOJO settings system:

| Setting | Default | Description |
|---|---|---|
| `SHORTLINK_BASE_URL` | `None` | Base URL for short links (e.g. `https://itf.io`). Falls back to `BASE_URL`. |
| `SHORTLINK_FALLBACK_URL` | `None` | Redirect target for invalid/expired codes. Falls back to `BASE_URL`, then `"/"`. |
| `BASE_URL` | `"/"` | Default base URL if shortlink-specific settings are not configured. |

---

## Permissions

| Permission | Required For |
|---|---|
| `manage_shortlinks` | Viewing, creating, updating, deleting shortlinks and viewing click history |
| authenticated user | Creating helper links via `/api/shortlink/link/create` |
| *(none)* | Accessing `/s/<code>` redirect endpoint (public) |
