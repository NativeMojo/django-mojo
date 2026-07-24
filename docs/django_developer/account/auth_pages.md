# Auth Pages — Django Developer Reference

Django-served login and registration pages with bouncer bot detection,
OAuth (Google, Apple, GitHub), passkeys, SMS login, password reset, and magic
link support. All branding and feature configuration is controlled through the
[auth config](auth_config.md).

---

## Overview

The framework serves fully-featured auth pages directly from Django — no
separate frontend app required. Pages are bouncer-gated so bots never see the
login form, and all branding is configured via the structured auth config
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

# ---- Deployment-wide auth config default ----
AUTH_CONFIG = {
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

`AUTH_CONFIG` replaces all retired flat `AUTH_*` / `AUTH_REGISTER_*` settings.
See [Auth Config](auth_config.md) for the full schema and migration table.

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

Set `theme.favicon_url` in `AUTH_CONFIG` to your favicon path — the template
includes `<link rel="icon" href="...">` when this key is non-empty.

### 3. OAuth Setup

Enable Google/Apple/GitHub by configuring their provider credentials. The
login methods must also be included in `AUTH_CONFIG.login.methods` (or the
group's `metadata.auth_config.login.methods`) — all three are included by
default when no explicit list is set:

```python
# Google OAuth
GOOGLE_CLIENT_ID     = 'your-client-id'
GOOGLE_CLIENT_SECRET = 'your-client-secret'

# Apple OAuth
APPLE_CLIENT_ID = 'your-service-id'
APPLE_TEAM_ID   = 'your-team-id'
APPLE_KEY_ID    = 'your-key-id'
# Place your .p8 key in var/keys/

# GitHub OAuth
GITHUB_CLIENT_ID     = 'your-client-id'
GITHUB_CLIENT_SECRET = 'your-client-secret'
GITHUB_SCOPES        = 'read:user user:email'  # default
```

```python
# AUTH_CONFIG must include the provider in login.methods:
AUTH_CONFIG = {
    "login": {"methods": ["password", "google", "apple", "github"]},
    ...
}
```

A provider button renders whenever its method token is enabled — configure the
credentials before enabling a provider, or its button dead-ends on the
provider's error page.

The OAuth callback URL is the auth page itself (`/auth?code=xxx&state=yyy`).
The `mbp` pass cookie uses `SameSite=Lax` so it is included on the OAuth
redirect back from the provider.

---

## Templates

All templates live in `account/templates/account/` and can be overridden by
placing a file with the same path in your project's `TEMPLATES` directories.

| Template | Purpose |
|----------|---------|
| `auth_base.html` | Base layout: overlay, card, hero include, script setup |
| `auth_hero.html` | Left panel partial — override to swap imagery/messaging |
| `login.html` | Login page — extends base; all views + methods from auth config |
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

Or set the appropriate keys in `AUTH_CONFIG.theme` and the default template
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

Set via `AUTH_CONFIG.theme.custom_css` (inline `<style>` block) or
`AUTH_CONFIG.theme.custom_css_url` (external `<link>`):

```python
AUTH_CONFIG = {
    "theme": {
        "custom_css": ":root { --mat-accent: #e74c3c; --mat-page-bg: #1a1a2e; }",
        # OR:
        "custom_css_url": "https://cdn.yourapp.com/auth-overrides.css",
    }
}
```

`custom_css` must not contain `<`, `@import`, or external URL references
(`://`). `custom_css_url` must be an `https://` URL. These are enforced at save
time on Group REST writes and recommended to enforce on `AUTH_CONFIG` too
(the validator is public: `auth_config.validate_auth_config`).

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
| `/api/auth/config` | GET | `on_auth_config` | Public auth config for custom front-ends |
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

Methods rendered are those listed in the resolved auth config's
`login.methods`. Available tokens: `password`, `sms`, `passkey`, `magic`,
`google`, `apple`, `github`.

- `password` — email/password sign in
- `sms` — phone number + 6-digit SMS code sign in
- `google` — Google OAuth redirect flow
- `apple` — Apple OAuth redirect flow
- `github` — GitHub OAuth redirect flow
- `passkey` — WebAuthn discoverable credential flow
- `magic` — send a one-click sign-in email
- Forgot password (code or link method; auto-routes via SMS when identity is phone)
- Reset code entry / set new password (from `?token=pr:...`)

**Anti-enumeration UX (SMS view).** `on_sms_login` is deliberately generic — it
returns the same success response whether or not the phone number has an account,
and only actually sends a code to a real account. `login.html` reflects this
honestly: the SMS view states up front that a code arrives only if the number is
already linked to an account, shows a non-committal post-submit message (no false
"we sent a code" certainty), and surfaces a "Create an account" link so a person
with no account has a clear path out. A snooping third party still learns nothing.
Do not change `on_sms_login` to branch on account existence — the privacy
guarantee depends on the uniform response.

The page leads with the **primary credential**: when `password` is in
`login.methods` the sign-in form is the landing view and every other method
(SMS, passkey, Google, Apple, GitHub) is a button below an "or continue with"
divider.
When `password` is **not** a method but `sms` is (a passwordless config), the
page opens directly on the SMS phone-entry form, with passkey/OAuth as
secondary buttons — so SMS login is never buried.

**Passkey failure UX.** WebAuthn collapses "no passkey on this device", "user
cancelled", and "timed out" into a single `NotAllowedError` (a privacy
guarantee), and the browser's raw `DOMException` message carries a W3C spec
URL. `mojo-auth.js` maps every passkey rejection to one plain-language message
before it reaches the page (the real error is still `console.error`-logged for
QA), so a spec link or stack-trace string never renders. As defense in depth,
`auth_base.html`'s `showMessage` helper runs `MojoAuth.sanitizeMessage()` on
every **error**-type message at the render layer — so any page extending the
base (login, register, contact, passkey enrollment) strips a stray URL even if
an upstream error path was missed. On the hosted login page a passkey failure
also offers an inline **"Sign in with a text code instead"** recovery action
that switches to the SMS view — shown only when the group offers SMS login and
the SMS view isn't already active. See
[web_developer/account/auth_pages.md](../../web_developer/account/auth_pages.md)
for the exact rejection contract consumed by custom front-ends.

- Session check on load (auto-redirect if already authenticated)
- OAuth callback handling (`?code=...&state=...`)
- Magic link token handling (`?token=ml:...`)

### Registration page (`/register`)

- Fields and identity field driven by `registration.fields` / `registration.identity_field`
- Methods rendered from `registration.methods`: `password`, `google`, `apple`, `github`
- Terms & conditions checkbox (when `theme.terms_url` is set)
- When `registration.passkey_prompt` is `"optional"` or `"required"`, redirects
  to `/passkey` after signup
- Phone-first flow when `phone` field has `verify: "sms"` in the schema
  (3-step state machine: phone → SMS code → profile)
- **Passwordless registration** — when `password` is absent from
  `registration.fields`, the form renders no password input and the account is
  created with `set_unusable_password()`. The user logs in afterward via the
  SMS-code flow. Requires a `phone` field with `verify: "sms"` in the same
  schema. See [Auth Config — Passwordless Registration](auth_config.md#passwordless-registration).
- **Existing-phone login via register** — when the schema marks `phone` with
  `verify: "sms"` and the submitted phone already belongs to an account, the
  SMS-verified token proves phone ownership and the requester is signed into
  the existing account instead of receiving a duplicate error. The submitted
  profile fields are ignored; the existing account is unmodified. Without
  `verify: "sms"` on the phone field, an existing phone is still a hard
  duplicate error.
  - If `group_uuid` is supplied and the existing account is not yet a member
    of that group, a `GroupMember` is created and `USER_REGISTERED_HANDLER`
    fires for that group (per-group setup runs). If the account is already a
    member, it is a pure login and `USER_REGISTERED_HANDLER` does not fire.
  - The hosted form reads the `account_exists` flag from
    `/auth/phone/register/verify` and skips the profile step (step 3)
    entirely, submitting the register call immediately with only
    `phone` + `verified_phone_token`.

### SMS code autofill

Every OTP text — the SMS-code login (`_send_otp`) and the registration
phone-verify (`/auth/phone/register/start`) — is sent with an **origin-bound
one-time-code line** appended:

```
Your verification code is: 123456

@auth.example.com #123456
```

The `@host` is taken from the request's `Origin` header (falling back to
`Host`), so it matches the page the user is on. This line is required by
Android Chrome's WebOTP API and is used by iOS Security Code AutoFill too.

The hosted login (SMS view) and registration (stepped verify) pages call the
WebOTP API via a shared `_mat.watchOtp` helper — filling the code field, and
submitting, the moment the SMS arrives. iOS has no WebOTP; it autofills from
the keyboard suggestion bar via the code input's `autocomplete="one-time-code"`
attribute. Both mechanisms require the page to be served over HTTPS.

### Passkey enrollment page (`/passkey`)

Not bouncer-gated. Themed by the resolved auth config. Reads the JWT from
localStorage and runs the WebAuthn registration round-trip client-side. Used
post-registration (when `passkey_prompt != off`) and standalone from account
settings.

Enrollment rejections are mapped just like sign-in, but with
enrollment-flavored copy (e.g. `InvalidStateError` → "This device may already
have a passkey for this account.", cancel/timeout → "Passkey setup was
cancelled or timed out. You can try again.") — the raw `DOMException` from
`navigator.credentials.create()` never renders here either.

### When methods are disabled

Methods absent from `login.methods` / `registration.methods` are excluded from
the rendered HTML server-side. When all OAuth providers and passkeys are
absent, the "or continue with" divider is excluded entirely.

---

## Configurable Registration Form

`registration.fields` drives both the bouncer-rendered register form and the
server-side validator. See [Auth Config — registration](auth_config.md)
for the schema and `register_schema.py` for the field resolution logic.

```python
# Phone-as-identity example via AUTH_CONFIG
AUTH_CONFIG = {
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

Omitting `password` from the field list makes registration **passwordless** — the account is created with an unusable password and the user signs in via SMS code. The schema must include a `phone` field with `verify: "sms"` for a passwordless config to be valid (enforced at config-write time and defensively on every registration request):

```python
# Passwordless registration — no password field
AUTH_CONFIG = {
    "registration": {
        "fields": [
            {"name": "first_name", "required": True},
            {"name": "last_name",  "required": True},
            {"name": "phone",      "required": True, "verify": "sms"},
        ],
        "identity_field": "phone",
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
| POST | `/api/auth/phone/register/start` | Body `{phone}` → `{session_token, expires_in}`. Accepts phones that already have accounts (the register flow handles the login). |
| POST | `/api/auth/phone/register/verify` | Body `{session_token, code}` → `{verified_phone_token, expires_in, account_exists}`. `account_exists` is `true` when the verified phone already belongs to an account — the hosted form uses this to skip the profile step. A wrong code returns 400 but does **not** consume the session — the caller may retry the correct code on the same `session_token` within the TTL. Only a successful match consumes the session. The returned `verified_phone_token` is single-use. |

### Extra (non-canonical) fields

`registration.fields` is a **closed** set of canonical `User` columns
(`first_name`, `last_name`, `email`, `phone`, `dob`, `password`). For
consumer-specific data — promo codes, referral/tracking tokens — declare
`registration.extra_fields` instead. Default is empty (no extra fields, no
behavior change for other tenants):

```python
AUTH_CONFIG = {
    "registration": {
        "extra_fields": [
            {"name": "promo", "label": "Promo code"},        # required defaults False
            {"name": "ref",   "label": "Referral",  "required": True},
        ],
    }
}
```

Each entry is `{"name", "label"?, "required"?}`. Names that collide with a
canonical field are rejected at config-write time. Like `registration.fields`,
this resolves per-group down the parent chain.

**Render behavior** (hosted register page): for each declared extra field, if a
matching URL query param is present (e.g. `/register?promo=WELCOME100`) the value
is captured **silently** (no visible input); otherwise the page renders a plain
text input asking for it. `required` is a client-side UX hint only.

**Capture + storage** (`on_register`): the capture allowlist is the union of the
group's declared `extra_fields` names and the legacy global
`REGISTRATION_EXTRA_FIELDS` setting (so existing deployments are unaffected).
Captured values are persisted to `user.metadata["registration"]` (a
`name → value` dict) **and** passed in the `extra=` kwarg to the
`USER_REGISTERED_HANDLER`, so a consumer handler can act on them
(e.g. validate/grant a promo).

---

## Multi-Tenant: Group Resolution

The hosted pages resolve the operator group from:
1. The request hostname matching a `Group.auth_domain`, or
2. The `?group_uuid=<uuid>` query parameter.

The resolved group's `uuid` is emitted as `window._matConfig.groupUuid`. Submit
handlers in `register.html` and `login.html` include it in the POST payload
automatically. See [auth_config.md](auth_config.md) for how the config
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
