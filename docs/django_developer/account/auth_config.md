# Auth Config — Django Developer Reference

Per-group structured configuration for the hosted auth pages. Replaces the
retired flat `AUTH_*` / `AUTH_REGISTER_*` settings.

---

## Overview

An auth config is a three-section object:

```
theme        — branding, layout, CSS overrides
registration — form fields, signup methods, passkey-on-signup policy
login        — which login methods are offered
```

Resolution order (deep-merged, last wins):

```
Code defaults (DEFAULT_AUTH_CONFIG)
  <- AUTH_CONFIG setting (deployment-wide JSON)
  <- group.metadata["auth_config"], walked root → group down the parent chain
```

Deep-merge semantics: dicts merge key-by-key, lists and scalars replace
wholesale. So setting `login.methods` on a group replaces the inherited list
rather than appending to it.

---

## Config Schema

### `theme`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `app_title` | string | `"DJANGO MOJO"` | Brand name in card header |
| `logo_url` | string | `""` | Logo image URL (header + hero panel) |
| `favicon_url` | string | `""` | Favicon URL |
| `hero_image_url` | string | `""` | Left panel background image |
| `hero_headline` | string | `"Welcome back"` | Text over the hero image |
| `hero_subheadline` | string | `"Admin Portal"` | Supporting text below headline |
| `back_to_website_url` | string | `""` | "Back to website" link in hero (overridable via `?back=` URL param) |
| `terms_url` | string | `""` | Terms & Conditions link on register page |
| `layout` | string | `"card"` | `"card"` or `"fullscreen"` |
| `api_base` | string | `""` | API host (empty = same origin) |
| `success_redirect` | string | `"/"` | Redirect target after login |
| `custom_css` | string | `""` | Inline CSS block injected after the theme stylesheet |
| `custom_css_url` | string | `""` | `https://` URL to an external CSS file |

### `registration`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `true` | Whether the registration page is shown |
| `fields` | list\|null | `null` | Field schema (null → default email form) — see register_schema |
| `identity_field` | string | `""` | `"email"` or `"phone"` (empty → auto-pick) |
| `min_age` | int\|null | `null` | Minimum age gate (years) applied when `dob` is a field |
| `methods` | list | `["password","google","apple"]` | Offered signup methods |
| `passkey_prompt` | string | `"off"` | `"off"`, `"optional"`, or `"required"` — passkey enrollment after signup |

### `login`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `methods` | list | `["password","sms","passkey","magic","google","apple"]` | Offered login methods |

Valid login method tokens: `password`, `sms`, `passkey`, `magic`, `google`, `apple`.
Valid registration method tokens: `password`, `google`, `apple`.

---

## Deployment-Wide Default — `AUTH_CONFIG`

Set a JSON object in `settings.py` to apply to all groups before per-group
overrides:

```python
# settings.py
AUTH_CONFIG = {
    "theme": {
        "app_title": "Acme Platform",
        "logo_url": "https://cdn.acme.com/logo.svg",
        "layout": "fullscreen",
        "success_redirect": "/dashboard",
    },
    "login": {
        "methods": ["password", "google"],
    },
    "registration": {
        "methods": ["password", "google"],
        "passkey_prompt": "optional",
    },
}
```

`AUTH_CONFIG` can also be a JSON string (e.g. when set via an environment
variable or the `Setting` model at runtime).

---

## Per-Group Config — `group.metadata["auth_config"]`

Store a partial auth config in `group.metadata["auth_config"]`. Only the keys
present are merged — absent keys inherit from the deployment default or code
defaults.

```python
group.metadata = group.metadata or {}
group.metadata["auth_config"] = {
    "theme": {
        "app_title": "Client Brand",
        "logo_url": "https://cdn.clientbrand.com/logo.svg",
        "hero_headline": "Welcome to Client Brand",
        "success_redirect": "/client-dashboard",
    },
    "login": {
        "methods": ["password", "sso"],      # valid tokens only
    },
    "registration": {
        "passkey_prompt": "required",
    },
}
group.save()
```

Validation runs in `Group.on_rest_pre_save` — a bad `metadata.auth_config` on a
REST PATCH returns 400 immediately rather than breaking the auth page at render
time. Validated constraints:

- `theme.layout` must be `"card"` or `"fullscreen"` (if present)
- `theme.custom_css` must not contain `<` (XSS break-out) or `@import` or
  external URLs (`://`, `url(//)`)
- `theme.custom_css_url` must start with `https://`
- `login.methods` must be a non-empty list of valid tokens
- `registration.methods` must be a list of valid tokens
- `registration.passkey_prompt` must be `"off"`, `"optional"`, or `"required"`
- `registration.fields` is validated via `register_schema.validate_fields_config`

---

## Service API

```python
from mojo.apps.account.services import auth_config

# Resolve the full config for a group (returns objict)
cfg = auth_config.resolve_auth_config(group=group, request=request)
cfg.theme.app_title          # → "Acme Platform"
cfg.login.methods            # → ["password", "google"]
cfg.registration.passkey_prompt  # → "optional"

# Public-safe subset (what GET /api/auth/config returns)
pub = auth_config.public_auth_config(cfg)

# Resolve group from request.DATA["group_uuid"]
group = auth_config.resolve_group_from_request(request)

# Soft-gate: raise PermissionDeniedException if method is disabled for group
auth_config.assert_login_method("sms", group)  # no-op if group is None

# Validate a raw dict before saving
auth_config.validate_auth_config(raw_dict)   # raises ValueException on bad config
```

The `request` parameter on `resolve_auth_config` enables the
`X-Mojo-Test-Auth-Config` header override in test mode (loopback + test flag
only — not honoured in production).

---

## Login Method Soft-Gating

When a `group_uuid` resolves a group on a login or registration request, the
auth config's `login.methods` / `registration.methods` lists are consulted.
Disabled methods return a 403 with a human-readable message. This is a **UX
guardrail only** — it is not enforced when `group_uuid` is absent. Callers
that omit `group_uuid` are not restricted.

Endpoints that enforce this:
- `POST /api/auth/login` (password, SMS, passkey, magic, Google, Apple)
- `POST /api/auth/register` (password, Google, Apple)

---

## Passkey Enrollment Page

A reusable passkey enrollment page is served at `/{BOUNCER_PASSKEY_PATH}`
(default `/passkey`). It is themed by the resolved auth config.

```
BOUNCER_PASSKEY_PATH = 'passkey'   # file-backed setting (default)
```

The page is not bouncer-gated — the visitor must already be authenticated
(the page reads the JWT from localStorage and runs the WebAuthn registration
round-trip client-side). Use cases:

1. **Post-registration** — when `registration.passkey_prompt` is `"optional"`
   or `"required"`, the hosted `/register` page redirects here after signup.
2. **Account settings** — link to `/passkey?group_uuid=<uuid>` from your own
   account settings UI.

---

## Template Context Keys (for template overriders)

`_auth_context()` now emits these instead of reading flat settings:

| Key | Source |
|-----|--------|
| `login_methods` | `cfg.login.methods` |
| `registration_methods` | `cfg.registration.methods` |
| `registration_enabled` | `cfg.registration.enabled` |
| `passkey_prompt` | `cfg.registration.passkey_prompt` |
| `passkey_url` | `/{BOUNCER_PASSKEY_PATH}{group_qs}` |
| `auth_layout` | `cfg.theme.layout` |
| `brand_name` | `cfg.theme.app_title` |
| `logo_url`, `favicon_url`, `hero_*`, etc. | `cfg.theme.*` |

---

## Migrating from Flat Settings

These settings are **retired** — remove them from `settings.py`:

| Retired setting | Replacement in `AUTH_CONFIG` |
|----------------|------------------------------|
| `AUTH_APP_TITLE` | `theme.app_title` |
| `AUTH_LOGO_URL` | `theme.logo_url` |
| `AUTH_FAVICON_URL` | `theme.favicon_url` |
| `AUTH_HERO_IMAGE_URL` | `theme.hero_image_url` |
| `AUTH_HERO_HEADLINE` | `theme.hero_headline` |
| `AUTH_HERO_SUBHEADLINE` | `theme.hero_subheadline` |
| `AUTH_BACK_TO_WEBSITE_URL` | `theme.back_to_website_url` |
| `AUTH_TERMS_URL` | `theme.terms_url` |
| `AUTH_LAYOUT` | `theme.layout` |
| `AUTH_API_BASE` | `theme.api_base` |
| `AUTH_SUCCESS_REDIRECT` | `theme.success_redirect` |
| `AUTH_CUSTOM_CSS` | `theme.custom_css` |
| `AUTH_CUSTOM_CSS_URL` | `theme.custom_css_url` |
| `AUTH_ENABLE_GOOGLE` | `login.methods` (include `"google"`) |
| `AUTH_ENABLE_APPLE` | `login.methods` (include `"apple"`) |
| `AUTH_ENABLE_PASSKEYS` | `login.methods` (include `"passkey"`) |
| `AUTH_REGISTER_FIELDS` | `registration.fields` |
| `AUTH_REGISTER_IDENTITY_FIELD` | `registration.identity_field` |
| `AUTH_MIN_AGE_YEARS` | `registration.min_age` |
