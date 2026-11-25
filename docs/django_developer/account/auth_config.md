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
| `app_title` | string | `"DJANGO MOJO"` | Brand name in card header and on the bouncer challenge |
| `auth_provider_name` | string | `"DJANGO MOJO"` | Account provider named on the bouncer challenge (for example, `"Maestro"`) |
| `logo_url` | string | `""` | Logo image URL (header + hero panel + bouncer challenge) |
| `favicon_url` | string | `""` | Favicon URL or path. When set it is the **only** icon declared on the hosted auth pages — the deployment's own `/favicon/*` links and web manifest are suppressed. Not constrained to `https://` (a relative path is valid). |
| `hero_image_url` | string | `""` | Left panel background image |
| `hero_image_url_light` | string | `""` | Optional light-appearance hero image; falls back to `hero_image_url` |
| `hero_image_url_dark` | string | `""` | Optional dark-appearance hero image; falls back to `hero_image_url` |
| `hero_headline` | string | `"Welcome back"` | Text over the hero image |
| `hero_subheadline` | string | `"Admin Portal"` | Supporting text below headline |
| `hero_image_position` | string | `"center"` | Image crop: `center`, `top`, `bottom`, `left`, or `right` |
| `back_to_website_url` | string | `""` | Back link in hero or compact header (overridable via `?back=`); must be relative or absolute HTTP(S) |
| `back_to_website_label` | string | `"Back to website"` | Destination-aware back-link label |
| `terms_url` | string | `""` | Terms & Conditions link on register page |
| `layout` | string | `"minimal"` | `minimal`, `compact`, `branded-panel`, or `editorial`; legacy `card` and `fullscreen` map to `compact` and `branded-panel` |
| `appearance` | string | `"system"` | `light`, `dark`, or `system` (`prefers-color-scheme`); the bouncer challenge follows it too |
| `accent_color` | string | `"#6384ff"` | Six-digit hex accent shared by auth controls and the bouncer challenge |
| `api_base` | string | `""` | API host (empty = same origin) |
| `success_redirect` | string | `"/"` | Redirect target after login. Must resolve to `http`/`https` (a relative path is fine) — the auth page scheme-guards this value on its way to the browser, so a custom app scheme such as `myapp://home` is refused and sign-in dead-ends on **every** attempt. Point it at an `https` universal/app link instead. Not validated on write (`validate_auth_config` does not check it), so the failure surfaces at login, not at save |
| `custom_css` | string | `""` | Inline CSS block injected after the theme stylesheet (also on the bouncer challenge) |
| `custom_css_url` | string | `""` | `https://` URL to an external CSS file (also on the bouncer challenge) |

### `registration`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `true` | Whether the registration page is shown |
| `fields` | list\|null | `null` | Field schema (null → default email form) — see register_schema |
| `extra_fields` | list | `[]` | Per-group non-canonical fields (promo/ref/tracking). An entry may be a name string or `{"name", "label"?, "required"?, "capture_only"?, "help_text"?}`. Hosted-page normalization defaults `required` and `capture_only` to `false`, `help_text` to `""`, and a missing label to the title-cased name. Canonical and auth/navigation-reserved names are rejected. Safe declared values survive Bouncer and login/register switcher hops. Default empty — no extra inputs, no behavior change for other tenants. |
| `identity_field` | string | `""` | `"email"` or `"phone"` (empty → auto-pick) |
| `min_age` | int\|null | `null` | Minimum age gate (years) applied when `dob` is a field |
| `methods` | list | `["password","google","apple","github"]` | Password and OAuth signup methods. Verified SMS signup uses the phone identity and field schema. |
| `passkey_prompt` | string | `"off"` | `"off"`, `"optional"`, or `"required"` — passkey enrollment after signup |

### `login`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `methods` | list | `["password","sms","passkey","magic","google","apple","github"]` | Offered login methods |
| `heading` | string | `"Sign In"` | Destination-aware page heading |
| `supporting_copy` | string | `""` | Explanation below the heading, e.g. which account to use |

Valid login method tokens are `password`, `sms`, `passkey`, `magic`, `google`, `apple`, `github` and `microsoft`.
Valid registration method tokens are `password`, `google`, `apple`, `github` and `microsoft`.

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
        "layout": "editorial",
        "appearance": "system",
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

- `theme.layout` must be a known preset or the `card`/`fullscreen` legacy alias
- `theme.appearance` must be `"light"`, `"dark"`, or `"system"`
- `theme.accent_color` must be a six-digit hex color
- `theme.hero_image_position` must be a known crop token
- `theme.back_to_website_url` must be a relative or absolute HTTP(S) URL
- `theme.back_to_website_label`, `theme.auth_provider_name`, and
  `login.heading` must be non-empty strings; `login.supporting_copy` must be a
  string and may be empty
- `theme.custom_css` must not contain `<` (XSS break-out) or `@import` or
  external URLs (`://`, `url(//)`)
- `theme.custom_css_url` must start with `https://`
- `login.methods` must be a non-empty list of valid tokens
- `registration.methods` must be a list of valid tokens
- `registration.passkey_prompt` must be `"off"`, `"optional"`, or `"required"`
- `registration.fields` is validated via `register_schema.validate_fields_config` — a schema that omits `password` is accepted only when it also includes a `phone` field with `verify: "sms"` (see Passwordless Registration below)
- `registration.extra_fields` is validated via `register_schema.validate_extra_fields_config` — each entry must have a non-empty `name` string; `label` and `help_text` (if present) must be strings; `required` and `capture_only` (if present) must be booleans; a capture-only field cannot also be required; canonical/identity, tenancy/navigation/display, credential/device, and OAuth/passkey request-control names are reserved (complete list below)

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

`public_auth_config()` preserves `registration.extra_fields` in its configured
wire shape, including legacy string shorthand. Custom clients should normalize
string entries to a field name and apply the defaults above themselves. New
config writes reject `capture_only: true` combined with `required: true`; as a
runtime safeguard for unvalidated deployment or legacy persisted config, the
hosted schema treats `capture_only` as authoritative and normalizes `required`
to `false`. That normalization does not rewrite the public raw response.
`capture_only` is presentation policy, not an integrity or security boundary:
the register endpoint still accepts an allowlisted value supplied by any
client, so referral and promo validation belongs in the registration handler.

The hosted Bouncer and login/register switcher forward only names declared by
this resolved schema (not names present only in `REGISTRATION_EXTRA_FIELDS`).
The shared forwarding/capture sanitizer accepts one non-empty scalar string no
longer than 512 characters with no ASCII controls. Empty, repeated/list-shaped,
control-bearing, and oversize values are dropped without truncation. Extras do
not propagate to passkey, contact, or OAuth-consent destinations; encoded values
remain data beneath the server-selected auth/register path. Reserved names are:
canonical/identity (`first_name`, `last_name`, `email`, `phone`, `dob`,
`password`, `username`, `phone_number`); tenancy/navigation/display (`group`,
`group_uuid`, `redirect`, `next`, `returnTo`, `back`, `webapp_base_url`,
`redirect_uri`, `force_reauth`, `auth_theme`, `auth_appearance`);
credential/device (`token`, `code`, `state`, `auth_code`, `bouncer_token`,
`verified_phone_token`, `session_token`, `mfa_token`, `access_token`,
`refresh_token`, `recovery_code`, `current_password`, `new_password`, `duid`,
`muid`, `fp`); and OAuth/passkey (`client_id`, `response_type`, `scope`,
`code_challenge`, `code_challenge_method`, `code_verifier`, `grant_type`,
`resource`, `challenge_id`, `credential`).

The `request` parameter on `resolve_auth_config` enables the
`X-Mojo-Test-Auth-Config` header override in test mode (loopback + test flag
only — not honoured in production).

---

## Passwordless Registration

For SMS-only sign-up, use a phone identity, mark the phone field with
`verify: "sms"` and set `registration.methods` to `[]`. The hosted form
collects the phone number, SMS code and configured profile fields.
`POST /api/auth/register` requires the single-use `verified_phone_token`
for that number and calls Django's `set_unusable_password()` on the new
account. Users can sign in later with SMS or an enrolled passkey.

**Mandatory guard:** a schema that omits `password` MUST include a `phone` field with `verify: "sms"`. `validate_fields_config` rejects a no-password schema without an SMS-verified phone at config-write time. `on_register` also re-checks this defensively (the deployment-wide `AUTH_CONFIG` setting and the `X-Mojo-Test-Register-Fields` test header bypass `validate_auth_config`).

```python
# Passwordless registration via AUTH_CONFIG
AUTH_CONFIG = {
    "registration": {
        "methods": [],
        "fields": [
            {"name": "first_name", "required": True},
            {"name": "last_name",  "required": True},
            {"name": "phone",      "required": True, "verify": "sms"},
        ],
        "identity_field": "phone",
    }
}
```

When the resolved methods omit `password`, the hosted SMS form omits even a
legacy password field and the server ignores any submitted password. With the
`password` method enabled, the schema determines whether the form requires
a password. OAuth providers can be enabled independently alongside SMS.

---

## Login Method Soft-Gating

Group-scoped login requests use `login.methods`. A disabled method returns
403 when the request identifies a group. Native sign-up through
`POST /api/auth/register` always checks the resolved registration config,
including the deployment default when `group_uuid` is absent. Closing
registration or submitting a new email/password sign-up with the `password`
method disabled returns 403 with "Registration is not enabled". While
registration is open, a returning SMS-verified phone can still sign in.
Invites, admin-created accounts and OAuth sign-up use separate flows.
Hosted OAuth checks each provider against the group's registration methods.

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
| `auth_layout` | normalized `cfg.theme.layout`, optionally overridden by the allowlisted `auth_theme` query value |
| `auth_appearance` | normalized `cfg.theme.appearance`, optionally overridden by the allowlisted `auth_appearance` query value |
| `accent_color` | validated `cfg.theme.accent_color` |
| `brand_name` | `cfg.theme.app_title` |
| `login_heading`, `login_supporting_copy` | `cfg.login.heading`, `cfg.login.supporting_copy` |
| `logo_url`, `favicon_url`, `hero_*`, `back_to_website_*`, etc. | `cfg.theme.*` (appearance-specific hero URLs fall back to `hero_image_url`) |

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
