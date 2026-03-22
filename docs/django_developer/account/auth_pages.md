# Auth Pages — Django Developer Reference

Django-served login and registration pages with bouncer bot detection,
OAuth (Google, Apple), passkeys, password reset, and magic link support.

---

## Overview

The framework serves fully-featured auth pages directly from Django — no
separate frontend app required. Pages are bouncer-gated so bots never see the
login form, and all branding is configurable via DB-backed settings (runtime
changeable from the admin portal).

```
/auth       → bouncer gate → login page
/register   → bouncer gate → registration page
/login      → honeypot decoy (traps bots)
/signin     → honeypot decoy
/signup     → honeypot decoy
```

---

## Setup

### 1. Settings

All auth page settings use `settings.get()` (DB-backed with file fallback).
Configure in `settings.py` for defaults, override at runtime via the `Setting`
model in the admin portal.

```python
# settings.py

# ---- Bouncer gate ----
BOUNCER_LOGIN_PATH = 'auth'          # real login page path (default: 'auth')
BOUNCER_REGISTER_PATH = 'register'   # real registration page path

# ---- Auth page branding ----
AUTH_APP_TITLE = 'My App'            # brand name shown in header
AUTH_LOGO_URL = ''                   # logo image URL (header + hero panel)
AUTH_FAVICON_URL = ''                # favicon URL (served by nginx, see below)
AUTH_HERO_IMAGE_URL = ''             # left panel background image
AUTH_HERO_HEADLINE = 'Welcome back'  # text over the hero image
AUTH_HERO_SUBHEADLINE = ''           # optional supporting text
AUTH_BACK_TO_WEBSITE_URL = ''        # "Back to website" pill link in hero panel
AUTH_TERMS_URL = ''                  # Terms & Conditions link on register page

# ---- Auth features ----
AUTH_ENABLE_GOOGLE = False           # show Google OAuth button
AUTH_ENABLE_APPLE = False            # show Apple OAuth button
AUTH_ENABLE_PASSKEYS = False         # show Passkey button (also requires browser support)

# ---- Routing ----
AUTH_API_BASE = ''                   # API host (empty = same origin, which is correct for most projects)
AUTH_SUCCESS_REDIRECT = '/'          # where to redirect after login

# ---- Layout ----
AUTH_LAYOUT = 'card'                 # 'card' (centered card) or 'fullscreen' (edge-to-edge)
```

All settings can be overridden per-group via `Setting.set(key, value, group=group)`.

### 2. Nginx — Static Assets & Favicon

The auth pages load CSS and JS from API endpoints (no Django static files):

```
GET /api/account/static/mojo-auth-theme.css   → dark premium theme
GET /api/account/static/mojo-auth.js          → MojoAuth library
GET /api/account/static/mojo-auth.css          → legacy light theme (if needed)
```

These are served by Django with `Cache-Control` headers. In production, nginx
can intercept and serve directly for better performance:

```nginx
server {
    listen 443 ssl;
    server_name app.yourproject.com;

    # Favicon — served by nginx directly
    location = /favicon.ico {
        alias /var/www/yourproject/static/favicon.ico;
    }
    location = /apple-touch-icon.png {
        alias /var/www/yourproject/static/apple-touch-icon.png;
    }

    # Auth page static assets — optional nginx cache
    location /api/account/static/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_cache_valid 200 1d;
        add_header X-Cache $upstream_cache_status;
    }

    # Everything else to Django
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Set `AUTH_FAVICON_URL` to your favicon path (e.g., `/favicon.ico`) — the template
includes `<link rel="icon" href="...">` when this setting is set.

### 3. OAuth Setup

Enable Google/Apple by setting the OAuth provider credentials and the auth page flags:

```python
# Google OAuth
GOOGLE_OAUTH_CLIENT_ID = 'your-client-id'
GOOGLE_OAUTH_CLIENT_SECRET = 'your-client-secret'
AUTH_ENABLE_GOOGLE = True

# Apple OAuth
APPLE_OAUTH_CLIENT_ID = 'your-service-id'
APPLE_OAUTH_TEAM_ID = 'your-team-id'
APPLE_OAUTH_KEY_ID = 'your-key-id'
# Place your .p8 key in var/keys/
AUTH_ENABLE_APPLE = True

# Passkeys
AUTH_ENABLE_PASSKEYS = True   # button shown only when browser supports WebAuthn
```

The OAuth callback URL is the auth page itself (`/auth?code=xxx&state=yyy`).
The `mbp` pass cookie uses `SameSite=Lax` so it's included on the OAuth
redirect back from Google/Apple.

---

## Templates

All templates live in `account/templates/account/` and can be overridden by
placing a file with the same path in your project's `TEMPLATES` directories.

| Template | Purpose |
|----------|---------|
| `auth_base.html` | Base layout: overlay, card, hero include, script setup |
| `auth_hero.html` | Left panel partial — override to swap imagery/messaging |
| `login.html` | Login page — extends base, all 5 views + OAuth |
| `register.html` | Registration page — extends base |
| `bouncer_challenge.html` | Bouncer challenge (MojoVerify branded, not overridable) |
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

Or just set the `AUTH_HERO_*` settings and the default template handles it.

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

Two settings for injecting custom styles without touching template files:

**`AUTH_CUSTOM_CSS`** — inline CSS block, injected as a `<style>` tag after the
theme stylesheet. Set via admin portal for instant runtime changes:

```python
# Via admin portal / Setting model:
Setting.set('AUTH_CUSTOM_CSS', ':root { --mat-accent: #e74c3c; --mat-page-bg: #1a1a2e; }')
```

**`AUTH_CUSTOM_CSS_URL`** — URL to an external CSS file, loaded as a `<link>` tag:

```python
Setting.set('AUTH_CUSTOM_CSS_URL', 'https://cdn.yourapp.com/auth-overrides.css')
```

Both are loaded after `mojo-auth-theme.css`, so they override any theme values.
Use `AUTH_CUSTOM_CSS` for quick variable overrides. Use `AUTH_CUSTOM_CSS_URL`
for a full custom stylesheet hosted on your CDN.

### Layouts

| `AUTH_LAYOUT` | Behavior |
|---------------|----------|
| `card` (default) | Centered card with rounded corners on the page background |
| `fullscreen` | Edge-to-edge, no border radius, hero 50% / form 50% |

---

## URL Routes

| Path | Method | View | Description |
|------|--------|------|-------------|
| `/{BOUNCER_LOGIN_PATH}` | GET | `on_login_page` | Bouncer-gated login page |
| `/{BOUNCER_REGISTER_PATH}` | GET | `on_register_page` | Bouncer-gated registration page |
| `/login` | GET | `on_decoy_page` | Honeypot decoy |
| `/login` | POST | `on_decoy_post` | Dead endpoint — logs, returns fake error |
| `/signin` | GET/POST | (same) | Honeypot decoy |
| `/signup` | GET/POST | (same) | Honeypot decoy |
| `/api/account/bouncer/assess` | POST | `on_bouncer_assess` | Bouncer signal assessment |
| `/api/account/bouncer/event` | POST | `on_bouncer_event` | Client event reporting |
| `/api/account/static/mojo-auth-theme.css` | GET | Static | Dark theme CSS |
| `/api/account/static/mojo-auth.js` | GET | Static | MojoAuth JS library |
| `/api/account/static/mojo-auth.css` | GET | Static | Legacy light theme CSS |

All root-level paths (`/auth`, `/login`, etc.) are registered as absolute URLs
and bypass the `/api/` prefix.

---

## Auth Page Features

### Login page (`/auth`)

- Email/password sign in
- Google OAuth (when `AUTH_ENABLE_GOOGLE=True`)
- Apple OAuth (when `AUTH_ENABLE_APPLE=True`)
- Passkey authentication (when `AUTH_ENABLE_PASSKEYS=True` + browser support)
- Forgot password (code or link method)
- Reset code entry
- Set new password (from emailed `pr:` token link)
- Magic login link request
- Session check on load (auto-redirect if already authenticated)
- OAuth callback handling (Google/Apple redirect back)
- Magic link token handling (`?token=ml:...`)
- Password reset link handling (`?token=pr:...`)

### Registration page (`/register`)

- First name / last name (optional)
- Email + password
- Terms & conditions checkbox
- Google/Apple OAuth sign-up
- Link to login page

### When providers are disabled

When all OAuth providers and passkeys are disabled, the "or continue with"
divider and button row are excluded from the HTML entirely (server-side,
not hidden by JS).

---

## Settings Reference

### Branding (DB-backed via `settings.get()`)

| Setting | Default | Description |
|---------|---------|-------------|
| `AUTH_APP_TITLE` | `'DJANGO MOJO'` | Brand name in card header |
| `AUTH_LOGO_URL` | `''` | Logo image URL (header + hero) |
| `AUTH_FAVICON_URL` | `''` | Favicon URL |
| `AUTH_HERO_IMAGE_URL` | `''` | Left panel background image |
| `AUTH_HERO_HEADLINE` | `'Welcome back'` | Text over hero image |
| `AUTH_HERO_SUBHEADLINE` | `''` | Supporting text below headline |
| `AUTH_BACK_TO_WEBSITE_URL` | `''` | "Back to website" link in hero |
| `AUTH_TERMS_URL` | `''` | Terms link on register page |
| `AUTH_CUSTOM_CSS` | `''` | Inline CSS block injected after the theme stylesheet |
| `AUTH_CUSTOM_CSS_URL` | `''` | URL to an external CSS file loaded after the theme |

### Features (DB-backed)

| Setting | Default | Description |
|---------|---------|-------------|
| `AUTH_ENABLE_GOOGLE` | `False` | Show Google OAuth button |
| `AUTH_ENABLE_APPLE` | `False` | Show Apple OAuth button |
| `AUTH_ENABLE_PASSKEYS` | `False` | Show Passkey button |

### Routing (DB-backed)

| Setting | Default | Description |
|---------|---------|-------------|
| `AUTH_API_BASE` | `''` | API host (empty = same origin) |
| `AUTH_SUCCESS_REDIRECT` | `'/'` | Redirect target after login |
| `AUTH_LAYOUT` | `'card'` | `'card'` or `'fullscreen'` |

### Bouncer gate (file-based via `settings.get_static()`)

| Setting | Default | Description |
|---------|---------|-------------|
| `BOUNCER_LOGIN_PATH` | `'auth'` | Real login page URL path |
| `BOUNCER_REGISTER_PATH` | `'register'` | Real registration page URL path |

See [bouncer.md](bouncer.md) for the full bouncer settings reference.
