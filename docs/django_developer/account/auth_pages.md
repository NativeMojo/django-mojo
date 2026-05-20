# Auth Pages — Django Developer Reference

Django-served login and registration pages with bouncer bot detection,
OAuth (Google, Apple), passkeys, SMS login, password reset, and magic link
support. All branding and feature configuration is controlled through the
[portal config](portal_config.md).

---

## Overview

The framework serves fully-featured auth pages directly from Django — no
separate frontend app required. Pages are bouncer-gated so bots never see the
login form, and all branding is configured via the structured portal config
object (group-owned, deep-merged from the parent chain).

```
/auth       → bouncer gate → login page
/register   → bouncer gate → registration page
/passkey    → passkey enrollment page (authenticated, not bouncer-gated)
/login      → honeypot decoy (traps bots)
/signin     → honeypot decoy
/signup     → honeypot decoy
```

---

## Setup

### 1. Settings

```python
# settings.py

# ---- Bouncer gate paths (file-backed, no runtime override) ----
BOUNCER_LOGIN_PATH    = 'auth'      # real login page path (default: 'auth')
BOUNCER_REGISTER_PATH = 'register'  # real registration page path
BOUNCER_PASSKEY_PATH  = 'passkey'   # passkey enrollment page path

# ---- Deployment-wide portal config default ----
AUTH_PORTAL = {
    "theme": {
        "app_title": "My App",
        "logo_url": "https://cdn.example.com/logo.svg",
        "layout": "card",
        "success_redirect": "/",
    },
    "login": {
        "methods": ["password", "google", "passkey"],
    },
    "registration": {
        "methods": ["password", "google"],
        "passkey_prompt": "optional",
    },
}
```

`AUTH_PORTAL` replaces all retired flat `AUTH_*` / `AUTH_REGISTER_*` settings.
See [Portal Config](portal_config.md) for the full schema and migration table.

### 2. Nginx — Static Assets & Favicon

The auth pages load CSS and JS from API endpoints (no Django static files):

```
GET /api/account/static/mojo-auth-theme.css   → dark premium theme
GET /api/account/static/mojo-auth.js          → MojoAuth library
GET /api/account/static/mojo-auth.css         → legacy light theme (if needed)
```

These are served by Django with `Cache-Control` headers. In production, nginx
can intercept and serve directly for better performance:

```nginx
server {
    listen 443 ssl;
    server_name app.yourproject.com;

    location = /favicon.ico {
        alias /var/www/yourproject/static/favicon.ico;
    }
    location = /apple-touch-icon.png {
        alias /var/www/yourproject/static/apple-touch-icon.png;
    }

    location /api/account/static/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_cache_valid 200 1d;
        add_header X-Cache $upstream_cache_status;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Set `theme.favicon_url` in `AUTH_PORTAL` to your favicon path — the template
includes `<link rel="icon" href="...">` when this key is non-empty.

### 3. OAuth Setup

Enable Google/Apple by configuring their provider credentials. The login
methods must also be included in `AUTH_PORTAL.login.methods` (or the group's
`metadata.portal.login.methods`):

```python
# Google OAuth
GOOGLE_OAUTH_CLIENT_ID     = 'your-client-id'
GOOGLE_OAUTH_CLIENT_SECRET = 'your-client-secret'

# Apple OAuth
APPLE_OAUTH_CLIENT_ID = 'your-service-id'
APPLE_OAUTH_TEAM_ID   = 'your-team-id'
APPLE_OAUTH_KEY_ID    = 'your-key-id'
# Place your .p8 key in var/keys/
```

```python
# AUTH_PORTAL must include the provider in login.methods:
AUTH_PORTAL = {
    "login": {"methods": ["password", "google", "apple"]},
    ...
}
```

The OAuth callback URL is the auth page itself (`/auth?code=xxx&state=yyy`).
The `mbp` pass cookie uses `SameSite=Lax` so it is included on the OAuth
redirect back from Google/Apple.

---

## Templates

All templates live in `account/templates/account/` and can be overridden by
placing a file with the same path in your project's `TEMPLATES` directories.

| Template | Purpose |
|----------|---------|
| `auth_base.html` | Base layout: overlay, card, hero include, script setup |
| `auth_hero.html` | Left panel partial — override to swap imagery/messaging |
| `login.html` | Login page — extends base; all views + methods from portal config |
| `register.html` | Registration page — extends base; redirects to `/passkey` when `passkey_prompt != off` |
| `passkey_enroll.html` | Standalone passkey enrollment page |
| `bouncer_challenge.html` | Bouncer challenge (default branded, opt-in override per group) |
| `bouncer_decoy.html` | Honeypot decoy login page |

### Template blocks (in `auth_base.html`)

| Block | Purpose |
|-------|---------|
| `page_title` | HTML `<title>` |
| `extra_css` | Additional `<link>` or `<style>` tags |
| `title` | Card heading (h1) |
| `subtitle_block` | Text below heading (login/register switcher link) |
| `content` | Main form content (views, inputs, buttons) |
| `switcher` | Bottom link (e.g., "Don't have an account? Create one") |
| `page_script` | Page-specific JavaScript |

### Overriding the hero panel

Create `templates/account/auth_hero.html` in your project:

```html
<div class="mat-hero" style="background-image:url('/static/hero.jpg')">
    <div class="mat-hero-top">
        <img class="mat-hero-logo" src="/static/logo-white.svg" alt="My App" />
        <a class="mat-hero-back" href="https://myapp.com">Back to website &rarr;</a>
    </div>
    <div class="mat-hero-bottom">
        <div class="mat-hero-headline">Your headline here</div>
        <div class="mat-hero-sub">Supporting text</div>
    </div>
</div>
```

Or set the appropriate keys in `AUTH_PORTAL.theme` and the default template
handles it.

---

## CSS Theme

The dark premium theme is in `mojo-auth-theme.css` using CSS custom properties:

```css
:root {
    --mat-page-bg: #686278;       /* page background */
    --mat-card-bg: #2c2638;       /* card background */
    --mat-deep: #1e1a28;          /* overlay / deep contrast */
    --mat-accent: #7c5cbf;        /* primary accent (buttons, focus) */
    --mat-accent-hover: #6b4eae;  /* accent hover state */
    --mat-input-bg: #353042;      /* input field background */
    --mat-border: #433d52;        /* subtle borders */
    --mat-text: #f2eff7;          /* primary text */
    --mat-muted: #9e96ad;         /* secondary/muted text */
    --mat-radius: 20px;           /* card border radius */
    --mat-field-radius: 10px;     /* input/button radius */
}
```

### Custom CSS — runtime overrides

Set via `AUTH_PORTAL.theme.custom_css` (inline `<style>` block) or
`AUTH_PORTAL.theme.custom_css_url` (external `<link>`):

```python
AUTH_PORTAL = {
    "theme": {
        "custom_css": ":root { --mat-accent: #e74c3c; --mat-page-bg: #1a1a2e; }",
        # OR:
        "custom_css_url": "https://cdn.yourapp.com/auth-overrides.css",
    }
}
```

`custom_css` must not contain `<`, `@import`, or external URL references
(`://`). `custom_css_url` must be an `https://` URL. These are enforced at save
time on Group REST writes and recommended to enforce on `AUTH_PORTAL` too
(the validator is public: `portal_config.validate_portal_config`).

### Layouts

| `theme.layout` | Behavior |
|----------------|----------|
| `card` (default) | Centered card with rounded corners on the page background |
| `fullscreen` | Edge-to-edge, no border radius, hero 50% / form 50% |

---

## URL Routes

| Path | Method | View | Description |
|------|--------|------|-------------|
| `/{BOUNCER_LOGIN_PATH}` | GET | `on_login_page` | Bouncer-gated login page |
| `/{BOUNCER_REGISTER_PATH}` | GET | `on_register_page` | Bouncer-gated registration page |
| `/{BOUNCER_PASSKEY_PATH}` | GET | `on_passkey_enroll_page` | Passkey enrollment (authenticated) |
| `/{BOUNCER_CONTACT_PATH}` | GET | `on_contact_page` | Bouncer-gated contact/support page |
| `/login` | GET | `on_decoy_page` | Honeypot decoy |
| `/login` | POST | `on_decoy_post` | Dead endpoint — logs, returns fake error |
| `/signin` | GET/POST | (same) | Honeypot decoy |
| `/signup` | GET/POST | (same) | Honeypot decoy |
| `/api/auth/portal` | GET | `on_auth_portal` | Public portal config for custom front-ends |
| `/api/account/bouncer/assess` | POST | `on_bouncer_assess` | Bouncer signal assessment |
| `/api/account/bouncer/event` | POST | `on_bouncer_event` | Client event reporting |
| `/api/account/static/mojo-auth-theme.css` | GET | Static | Dark theme CSS |
| `/api/account/static/mojo-auth.js` | GET | Static | MojoAuth JS library |
| `/api/account/static/mojo-auth.css` | GET | Static | Legacy light theme CSS |

All root-level paths (`/auth`, `/register`, `/passkey`, etc.) are registered
as absolute URLs and bypass the `/api/` prefix.

---

## Auth Page Features

### Login page (`/auth`)

Methods rendered are those listed in the resolved portal config's
`login.methods`. Available tokens: `password`, `sms`, `passkey`, `magic`,
`google`, `apple`.

- `password` — email/password sign in
- `sms` — phone number + 6-digit SMS code sign in
- `google` — Google OAuth redirect flow
- `apple` — Apple OAuth redirect flow
- `passkey` — WebAuthn discoverable credential flow
- `magic` — send a one-click sign-in email
- Forgot password (code or link method; auto-routes via SMS when identity is phone)
- Reset code entry / set new password (from `?token=pr:...`)
- Session check on load (auto-redirect if already authenticated)
- OAuth callback handling (`?code=...&state=...`)
- Magic link token handling (`?token=ml:...`)

### Registration page (`/register`)

- Fields and identity field driven by `registration.fields` / `registration.identity_field`
- Methods rendered from `registration.methods`: `password`, `google`, `apple`
- Terms & conditions checkbox (when `theme.terms_url` is set)
- When `registration.passkey_prompt` is `"optional"` or `"required"`, redirects
  to `/passkey` after signup
- Phone-first flow when `phone` field has `verify: "sms"` in the schema
  (3-step state machine: phone → SMS code → profile)

### Passkey enrollment page (`/passkey`)

Not bouncer-gated. Themed by the resolved portal config. Reads the JWT from
localStorage and runs the WebAuthn registration round-trip client-side. Used
post-registration (when `passkey_prompt != off`) and standalone from account
settings.

### When methods are disabled

Methods absent from `login.methods` / `registration.methods` are excluded from
the rendered HTML server-side. When all OAuth providers and passkeys are
absent, the "or continue with" divider is excluded entirely.

---

## Configurable Registration Form

`registration.fields` drives both the bouncer-rendered register form and the
server-side validator. See [Portal Config — registration](portal_config.md)
for the schema and `register_schema.py` for the field resolution logic.

```python
# Phone-as-identity example via AUTH_PORTAL
AUTH_PORTAL = {
    "registration": {
        "fields": [
            {"name": "first_name", "required": True},
            {"name": "last_name",  "required": True},
            {"name": "phone",      "required": True, "verify": "sms"},
            {"name": "dob",        "required": True},
            {"name": "password",   "required": True},
        ],
        "identity_field": "phone",
        "min_age": 13,
    }
}
```

**Dev-mode SMS bypass:**

```python
AUTH_PHONE_VERIFY_DEV_BYPASS_CODE = "000000"   # DO NOT SET IN PROD
```

Two endpoints back the phone-verify flow:

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/auth/phone/register/start` | Body `{phone}` → `{session_token, expires_in}` |
| POST | `/api/auth/phone/register/verify` | Body `{session_token, code}` → `{verified_phone_token, expires_in}` |

---

## Multi-Tenant: Group Resolution

The hosted pages resolve the operator group from:
1. The request hostname matching a `Group.auth_domain`, or
2. The `?group_uuid=<uuid>` query parameter.

The resolved group's `uuid` is emitted as `window._matConfig.groupUuid`. Submit
handlers in `register.html` and `login.html` include it in the POST payload
automatically. See [portal_config.md](portal_config.md) for how the config
resolves down the parent chain.

---

## Bouncer Gate

See [bouncer.md](bouncer.md) for the full bouncer settings reference.

| Setting | Default | Description |
|---------|---------|-------------|
| `BOUNCER_LOGIN_PATH` | `'auth'` | Real login page URL path |
| `BOUNCER_REGISTER_PATH` | `'register'` | Real registration page URL path |
| `BOUNCER_PASSKEY_PATH` | `'passkey'` | Passkey enrollment page URL path |
| `BOUNCER_CONTACT_PATH` | `'contact'` | Bouncer-gated contact/support page URL path |
