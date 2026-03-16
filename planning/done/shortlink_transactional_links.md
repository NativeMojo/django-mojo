# Request: Shortlink Wrapping for Transactional Token Links

## Status
Resolved — 2026-03-16

## Resolution

- `mojo/apps/account/utils/webapp_url.py` — new: `get_webapp_base_url()`, `get_webapp_auth_path()`, `build_token_url()`
- `mojo/apps/shortlink/__init__.py` — added `maybe_shorten_url()`: wraps URL if shortlink app installed, always `bot_passthrough=False`
- `mojo/apps/account/models/user.py` — `send_invite(request=None)`: builds `token_url`, shortlinks invite, passes both `token` + `token_url` to template
- `mojo/apps/account/rest/user.py` — all three send paths (`on_user_forgot`, `on_magic_login_send`, `on_email_verify_send`) now build and shortlink `token_url`; SMS magic login sends full URL instead of raw token
- No new tests (existing send tests cover the paths; shortlink is no-op when app not installed)

## Priority
Medium

## Summary

Wrap transactional token links (invite, magic login, password reset, email verify) in
shortlinks when the `shortlink` app is installed and `SHORTLINK_TRANSACTIONAL = True`.

Goals:
1. **UX** — clean, short URLs in SMS messages instead of `ml:a3f8b2...` token blobs
2. **Bot protection** — messaging app link-preview bots (iMessage, WhatsApp, Slack) expand
   URLs before the user sees them. Without shortlink wrapping, a bot can "pre-click" a
   single-use token link and consume it before the user arrives. Shortlinks intercept bots
   with an OG preview page and only redirect real users.

---

## Current State

All token send paths pass a raw token string into a template context dict. The email
template is responsible for building the full URL. There is no URL-building step in the
Python layer to intercept.

| Flow | Where token is sent | Delivery |
|------|---------------------|----------|
| Invite | `user.send_invite()` → `send_template_email("invite", {token})` | Email |
| Magic login | `on_magic_login_send` → `send_template_email("magic_login_link", {token})` | Email |
| Magic login | `on_magic_login_send` → `phonehub.send_sms(..., f"...token: {magic_token}")` | SMS (raw token, no URL at all) |
| Password reset | `on_user_forgot` → `send_template_email("password_reset_link", {token})` | Email |
| Email verify | `on_email_verify_send` → `send_template_email("email_verify_link", {token})` | Email |

---

## Design

### Prerequisite: `get_webapp_base_url(request, user, group)`

Before building any token URL we need to know the correct frontend base URL. This is
non-trivial in multi-tenant deployments where one admin portal manages many tenants —
`HTTP_ORIGIN` would be the *admin* origin, not the *target tenant's* webapp.

A helper (lives in `mojo/helpers/` or `mojo/apps/account/utils/`) with the following
lookup chain (first non-empty value wins):

1. `request.DATA.get("webapp_base_url")` — explicit per-request override; lets a single
   admin portal send invites that deep-link into any tenant's webapp
2. `group.get_metadata_value("webapp_base_url")` — tenant group config, traverses parent
   chain (handles org hierarchy automatically)
3. `user.org.get_metadata_value("webapp_base_url")` — user's primary org
4. `settings.get("WEBAPP_BASE_URL")` — project-wide default
5. `request.META.get("HTTP_ORIGIN")` — last-resort request context
6. `settings.get("BASE_URL", "/")` — final fallback

`request` and `group` are both optional (SMS send paths may not have a live request).

```python
def get_webapp_base_url(request=None, user=None, group=None):
    if request is not None:
        val = request.DATA.get("webapp_base_url")
        if val:
            return val.rstrip("/")
    if group is not None:
        val = group.get_metadata_value("webapp_base_url")
        if val:
            return val.rstrip("/")
    if user is not None:
        org = getattr(user, "org", None)
        if org is not None:
            val = org.get_metadata_value("webapp_base_url")
            if val:
                return val.rstrip("/")
    val = settings.get("WEBAPP_BASE_URL") or ""
    if val:
        return val.rstrip("/")
    if request is not None:
        val = request.META.get("HTTP_ORIGIN") or ""
        if val:
            return val.rstrip("/")
    return settings.get("BASE_URL", "/").rstrip("/")
```

All token URL building replaces `settings.get("BASE_URL")` with a call to
`get_webapp_base_url(request=request, user=user, group=group)`.

---

### New helper: `maybe_shorten_token_url(url, source, user, expire_hours)`

A single helper that:
1. Checks `SHORTLINK_TRANSACTIONAL` setting (default `False`)
2. Checks `shortlink` is in `INSTALLED_APPS` (graceful degradation)
3. If both true: calls `shorten(url, source=source, user=user, expire_hours=expire_hours, bot_passthrough=False)`
4. Returns the short URL or the original URL unchanged

Lives in `mojo/apps/shortlink/` (since it depends on that app) as a thin utility,
importable only when the app is installed.

### URL building

Currently no URL is built in Python — the template handles it. We need to build the full
destination URL in Python before shortening.

Different frontend apps route auth differently — we cannot hardcode paths per flow.

**Simplification: single auth endpoint with `flow` param.**

The frontend exposes one auth entry point that dispatches on a `flow` query param. This
means we only need *one* path per tenant, not one per flow:

```
{webapp_base_url}{webapp_auth_path}?flow={flow}&token={token}
```

**Configuration lives in group metadata — no Django settings required.**

`group.metadata` is already in the database and configurable via API without a deploy.
Two keys per group (both optional, both inherit from parent group via `get_metadata_value`):

| Key | Default | Example |
|-----|---------|---------|
| `webapp_base_url` | — | `"https://app.acme.com"` |
| `webapp_auth_path` | `"/auth"` | `"/login"` |

**Path resolution** — `get_webapp_auth_path(group=None)`:

```python
def get_webapp_auth_path(group=None):
    if group is not None:
        val = group.get_metadata_value("webapp_auth_path")
        if val:
            return val.rstrip("/")
    return settings.get("WEBAPP_AUTH_PATH", "/auth")
```

**Full URL per flow:**

```python
base_url = get_webapp_base_url(request=request, user=user, group=group)
auth_path = get_webapp_auth_path(group=group)
token_url = f"{base_url}{auth_path}?flow={flow}&token={token}"
```

Each call site resolves both, builds the URL, calls `maybe_shorten_token_url`, and passes
the result to the template as `token_url` (alongside the raw `token` for backward-compat
with templates that build their own URL).

**Flow values** passed as the `flow` query param:

| Flow | `flow=` value |
|------|--------------|
| Invite | `invite` |
| Magic login | `magic_login` |
| Password reset | `password_reset` |
| Email verify | `email_verify` |

### SMS magic login — special case

Currently sends the raw token string in the SMS body. Should instead build a full URL
and send that:

```python
# Before
phonehub.send_sms(user.phone_number, f"Your login link token: {magic_token}")

# After
base_url = get_webapp_base_url(request=request, user=user)
auth_path = get_webapp_auth_path(group=getattr(request, "group", None))
login_url = maybe_shorten_token_url(
    f"{base_url}{auth_path}?flow=magic_login&token={magic_token}",
    source="magic_login_sms",
    user=user,
    expire_hours=1,
)
phonehub.send_sms(user.phone_number, f"Your login link: {login_url}")
```

---

## Shortlink Parameters per Flow

| Flow | source | expire_hours | expire_days | bot_passthrough | OG metadata |
|------|--------|-------------|-------------|-----------------|-------------|
| Invite | `"invite"` | — | 7 | `False` | Optional: set from group name |
| Magic login email | `"magic_login"` | 1 | 0 | `False` | None (skip scrape) |
| Magic login SMS | `"magic_login_sms"` | 1 | 0 | `False` | None |
| Password reset | `"password_reset"` | 1 | 0 | `False` | None |
| Email verify | `"email_verify"` | 24 | 0 | `False` | None |

`bot_passthrough=False` on all flows — the OG preview page is the bot firewall. Real
users get a normal redirect.

For flows with no OG metadata, the scraper fires on the destination URL but will get a
`text/html` app shell (no OG tags) — no harm done, just a silent scrape miss.

---

## Settings

| Setting | Default | Purpose |
|---------|---------|---------|
| `WEBAPP_BASE_URL` | `""` | Project-wide frontend base URL (fallback; prefer `group.metadata["webapp_base_url"]`) |
| `WEBAPP_AUTH_PATH` | `"/auth"` | Project-wide auth path fallback (prefer `group.metadata["webapp_auth_path"]`) |
| `BASE_URL` | `"/"` | Legacy final fallback (already exists) |

---

## Files to Change

| File | Change |
|------|--------|
| `mojo/apps/account/utils/webapp_url.py` | New: `get_webapp_base_url(request, user, group)` and `get_webapp_auth_path(group)` |
| `mojo/apps/shortlink/__init__.py` | Add `maybe_shorten_token_url(url, source, user, expire_hours, expire_days)` helper |
| `mojo/apps/account/models/user.py` — `send_invite()` | Resolve `webapp_base_url`, build URL, call helper, pass `token_url` to template context |
| `mojo/apps/account/rest/user.py` — `on_magic_login_send` | Resolve `webapp_base_url`, build URL (both email and SMS paths), call helper |
| `mojo/apps/account/rest/user.py` — `on_user_forgot` | Resolve `webapp_base_url`, build URL, call helper |
| `mojo/apps/account/rest/user.py` — `on_email_verify_send` | Resolve `webapp_base_url`, build URL, call helper |

---

## Backward Compatibility

- Default is `False` — zero change for projects not opting in
- Raw `token` is still passed to templates alongside `token_url` — templates that
  build their own URLs continue to work unchanged
- If `shortlink` is not in `INSTALLED_APPS`, `maybe_shorten_token_url` returns the
  original URL unchanged (checked via `apps.is_installed`)

---

## Out of Scope

- OG metadata customisation per project (future: pass `og_metadata` kwarg through)
- Phone-number invite flows (no SMS invite path exists yet)
- Shortlink analytics / click reporting (already built into the shortlink app)
