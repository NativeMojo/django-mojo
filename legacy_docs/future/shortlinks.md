# Design: URL Shortener for SMS Links

> **Status:** Ready for implementation
> **Target repo:** `django-mojo` (new app: `mojo/apps/shortlink/`)

---

## Role

You are a **senior Django developer** on the Mojo platform team. You build reusable, production-grade Django apps that ship inside `django-mojo` — the shared framework used by all Mojo projects (MojoVerify, MojoPay, etc.).

**You know:**
- The django-mojo app conventions: `MojoModel` base class, `@md` decorator-based REST endpoints, `mojo.helpers` utilities, `apps.json` registration
- How to write models that are simple, well-indexed, and don't over-abstract
- That business logic belongs on the model or in thin service functions — not in views, not in bloated service classes
- The testit framework (not pytest): `@th.django_unit_test()`, `@th.django_unit_setup()`, `opts.client` for HTTP calls
- SMS constraints: 160-char single segment, carrier STOP requirements, link truncation risks

**Your standards:**
- No over-engineering. One model, one public function, one redirect view. That's the whole app.
- No unused features. No admin UI, no REST CRUD, no analytics dashboard — just the shortener.
- Idempotent and safe. Code collisions handled via retry. Expired links degrade gracefully (redirect to fallback, not 404).
- Tests cover the happy path, edge cases (expiry, missing codes, collisions), and integration with the calling code (SMS sends use short URLs).

---

## Problem

SMS URLs are too long. A single SMS segment is 160 chars. Our other projects verification links look like:

```
https://myproject.com/register/verify?token=a1b2c3d4-e5f6-7890-abcd-ef1234567890
```

That's 80+ chars for the URL alone — before the message text and STOP language. Multi-segment SMS costs more and some carriers truncate.

## Solution

A DB-backed URL shortener as a reusable django-mojo app. Produces links like:

```
https://itf.io/s/Xk9mR2p
```

28 chars. Saves ~50 chars per SMS.

---

## Implementation Prompt

You are building a new django-mojo app: `mojo/apps/shortlink/`. This is a reusable URL shortener. Any django-mojo project can use it.

### Part 1: django-mojo app (`mojo/apps/shortlink/`)

#### File structure

```
mojo/apps/shortlink/
├── __init__.py          # Public API: shorten(), SHORTLINK_BASE_URL
├── models/
│   ├── __init__.py      # Re-export ShortLink
│   └── shortlink.py     # ShortLink model
├── rest/
│   └── redirect.py      # GET /api/shortlink/s/<code> → 302 redirect
└── migrations/
    └── (auto-generated)
```

#### Model: `ShortLink` (`shortlink/models/shortlink.py`)

```python
from django.db import models
from mojo.abstracts.models import MojoModel  # standard django-mojo base model


class ShortLink(MojoModel):
    code = models.CharField(max_length=10, unique=True, db_index=True)
    url = models.TextField()
    source = models.CharField(max_length=50, default="unknown", db_index=True)
    hit_count = models.IntegerField(default=0)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        db_table = "shortlink"

    def __str__(self):
        return f"{self.code} → {self.url[:60]}"

    @classmethod
    def create(cls, url, source="unknown", expires_hours=72):
        """Create a short link. Returns the ShortLink instance."""
        from mojo.helpers import crypto, dates
        from datetime import timedelta

        # Generate unique code — 7 chars, alphanumeric, no ambiguous chars
        for _ in range(5):  # retry on collision (astronomically unlikely)
            code = crypto.random_string(7, allow_digits=True, allow_chars=True, allow_special=False)
            if not cls.objects.filter(code=code).exists():
                break
        else:
            raise RuntimeError("Failed to generate unique short code after 5 attempts")

        expires_at = dates.utcnow() + timedelta(hours=expires_hours) if expires_hours else None

        return cls.objects.create(
            code=code,
            url=url,
            source=source,
            expires_at=expires_at,
        )

    @classmethod
    def resolve(cls, code):
        """Look up a code, increment hit_count, return the URL or None if expired/missing."""
        from django.db.models import F
        from mojo.helpers import dates

        link = cls.objects.filter(code=code).first()
        if not link:
            return None
        if link.expires_at and link.expires_at < dates.utcnow():
            return None

        cls.objects.filter(pk=link.pk).update(hit_count=F("hit_count") + 1)
        return link.url
```

#### Public API: `shortlink/__init__.py`

```python
"""
ShortLink — URL shortener for SMS and other space-constrained channels.

Usage:
    from mojo.apps.shortlink import shorten

    short_url = shorten("https://example.com/long/path?token=abc123", source="register")
    # Returns: "https://itf.io/s/Xk9mR2p"
"""


def shorten(url, source="unknown", expires_hours=72, base_url=None):
    """
    Create a short link and return the full short URL string.

    Args:
        url: The destination URL to shorten.
        source: Tag for traceability (e.g. "register", "connect", "kyc").
        expires_hours: Hours until the link expires. None = never. Default 72.
        base_url: Override the base URL. Default: settings.SHORTLINK_BASE_URL or BASE_URL.

    Returns:
        Full short URL string, e.g. "https://myproject.com/s/Xk9mR2p"
    """
    from .models import ShortLink
    from mojo.helpers.settings import settings

    link = ShortLink.create(url=url, source=source, expires_hours=expires_hours)

    if not base_url:
        base_url = getattr(settings, "SHORTLINK_BASE_URL", None) or \
                   getattr(settings, "BASE_URL", "https://myproject.com")

    return f"{base_url.rstrip('/')}/s/{link.code}"
```

#### Redirect endpoint: `shortlink/rest/redirect.py`

```python
import mojo.decorators as md
from django.http import HttpResponseRedirect
from mojo.helpers.settings import settings


# USE absolute paths so it supports https://itf.io/s/ instead of https://itf.io/api/shorlinks/s/
@md.GET("/s/<code>")
@md.public_endpoint()
def on_shortlink_redirect(request, code):
    """Redirect a short link to its destination URL."""
    from shortlink.models import ShortLink

    url = ShortLink.resolve(code)
    if not url:
        fallback = getattr(settings, "SHORTLINK_FALLBACK_URL", None) or \
                   getattr(settings, "BASE_URL", "https://myproject.com")
        return HttpResponseRedirect(fallback)

    return HttpResponseRedirect(url)
```

**Important:** USE absolute paths '/s/<code>' so it supports https://itf.io/s/ instead of https://itf.io/api/shorlinks/s/


#### Migration

Run `makemigrations shortlink` to auto-generate. New model, no rename tricks needed.

#### Settings (optional, add to project settings if needed)

```python
# URL shortener settings
SHORTLINK_BASE_URL = "https://itf.io"     # Base URL for short links
SHORTLINK_FALLBACK_URL = "https://myproject.com"  # Where expired/invalid codes redirect
```

If not set, falls back to `BASE_URL` for backward compatibility.

---


#### Tests to write:

```
1. test_shorten_creates_shortlink
   - Call ShortLink.create(url="https://example.com/long", source="test")
   - Assert: ShortLink record exists, code is 7 chars, url matches, source matches

2. test_shorten_returns_full_url
   - Call shorten("https://example.com/long", source="test")
   - Assert: returned string contains "/s/" and a 7-char code
   - Assert: returned string starts with BASE_URL (or SHORTLINK_BASE_URL)

3. test_different_urls_get_different_codes
   - Call shorten() twice with different URLs
   - Assert: codes are different

4. test_resolve_valid_code
   - Create a ShortLink, call ShortLink.resolve(code)
   - Assert: returns the original URL
   - Assert: hit_count incremented to 1

5. test_resolve_increments_hit_count
   - Create a ShortLink, call resolve() 3 times
   - Assert: hit_count is 3

6. test_resolve_expired_code
   - Create a ShortLink, force expires_at to the past
   - Call ShortLink.resolve(code)
   - Assert: returns None

7. test_resolve_missing_code
   - Call ShortLink.resolve("ZZZZZZZ")
   - Assert: returns None

8. test_redirect_endpoint_valid
   - Create a ShortLink
   - GET /s/{code}
   - Assert: response is 302, Location header matches original URL

9. test_redirect_endpoint_expired
   - Create a ShortLink, force expired
   - GET /s/{code}
   - Assert: response is 302, Location header is fallback URL (not 404)

10. test_redirect_endpoint_missing
    - GET /s/ZZZZZZZ
    - Assert: response is 302 to fallback URL

12. test_connection_invite_uses_short_url
    - Login, create a direct link with phone recipient
    - Mock phonehub.send_sms, capture the message
    - Assert: SMS body contains "/s/"
```

---

### What NOT to build

- No admin UI or REST CRUD for ShortLink — internal infrastructure only
- No custom domain — uses the project's base URL
- No analytics dashboard — `hit_count` + `source` field is enough
- No Redis cache layer — DB index is sub-ms for our volume, add cache later if needed
- No bulk operations or batch shortening
- Don't touch `tools/rest/phone.py` SMS sends — those are a different product

### Edge cases to handle

- **Code collision:** The retry loop (5 attempts) handles this. At 7 chars alphanumeric (62^7 = 3.5 trillion combinations), collisions are astronomically unlikely.
- **Expired links:** Return None from resolve(), redirect endpoint sends user to fallback URL — no error page, no info leak.
- **No base_url configured:** Falls back to `BASE_URL` then hardcoded `https://myproject.com`.
- **Frontend routing:** `/s/<code>` needs to reach the API. Document the nginx/proxy requirement.

### Files to read before starting

**django-mojo (for patterns):**
- `mojo/apps/phonehub/__init__.py` — public API pattern (convenience imports)
- `mojo/apps/phonehub/models/` — model structure
- `mojo/apps/account/rest/user.py` — REST endpoint patterns with `@md` decorators
- `mojo/helpers/crypto/utils.py:16` — `random_string()` signature
- `mojo/abstracts/models.py` — `MojoModel` base class (provides `created`, `modified`)
