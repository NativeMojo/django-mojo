# Auth Config — REST API Reference

Per-group structured configuration that controls what the hosted auth pages
look like and which login/registration methods they offer.

---

## Overview

The auth config has three sections:

- `theme` — branding, layout, and CSS
- `login` — which sign-in methods are shown
- `registration` — which sign-up methods are shown, passkey policy

Configuration is resolved per group: code defaults are overridden by a
deployment-wide `AUTH_CONFIG` setting, then further overridden by
`group.metadata["auth_config"]` walked down the group parent chain.

---

## `GET /api/auth/config`

Returns the resolved, public-safe auth config for a group. Use this to
drive your own custom auth UI so it respects the group's theming and offered
methods.

**Auth:** none required (public endpoint)

**Query parameters:**

| Param | Description |
|-------|-------------|
| `group_uuid` | Optional. Resolve config for this group. Omit for the deployment default. |

**Response:**

```json
{
  "status": true,
  "data": {
    "theme": {
      "app_title": "Acme Platform",
      "logo_url": "https://cdn.acme.com/logo.svg",
      "favicon_url": "",
      "hero_image_url": "",
      "hero_headline": "Welcome back",
      "hero_subheadline": "Admin Portal",
      "back_to_website_url": "",
      "terms_url": "",
      "layout": "card",
      "api_base": "",
      "success_redirect": "/dashboard",
      "custom_css": "",
      "custom_css_url": ""
    },
    "registration": {
      "enabled": true,
      "fields": null,
      "extra_fields": [],
      "identity_field": "",
      "min_age": null,
      "methods": ["password", "google"],
      "passkey_prompt": "optional"
    },
    "login": {
      "methods": ["password", "google", "passkey"]
    }
  }
}
```

Use `login.methods` and `registration.methods` to decide which buttons to
render. Use `theme` to apply branding.

`registration.extra_fields` is the list of non-canonical fields the group has
configured (promo codes, referral tokens, etc.). An empty list means no extra
fields. SPAs building a custom registration form should include any declared
extra-field names in their register payload — the server captures values for
allowlisted names and silently drops the rest.

`registration.fields: null` means the deployment default (email + password) is
in effect. A non-null `fields` list may omit `password` — when it does,
registration is **passwordless**: the account is created without a usable
password and the user signs in afterward via SMS code. In that case the
`fields` list always contains a `phone` entry with `verify: "sms"` (the server
rejects a passwordless config without it). Custom front-ends building a
registration form should check whether `password` appears in `fields` and
render (or omit) the password input accordingly.

---

## Login Method Soft-Gating

When you call a login or registration endpoint with a `group_uuid` and the
resolved auth config does not include the method you are using, the server
returns 403:

```json
{"status": false, "message": "This sign-in method is not available for this group"}
```

This is a **UX guardrail** — it is only enforced when `group_uuid` is present.
Omitting `group_uuid` bypasses the restriction. Fetch `GET /api/auth/config`
first and only offer buttons for the methods listed.

Affected endpoints:
- `POST /api/auth/login`
- `POST /api/auth/register`
- `POST /api/auth/phone/register/start`
- `POST /api/oauth/login/google`, `POST /api/oauth/login/apple`
- `POST /api/account/passkeys/authenticate/begin`

---

## Passkey Enrollment Page (`/passkey`)

A standalone, themeable passkey enrollment page served at `/passkey`
(configurable via `BOUNCER_PASSKEY_PATH`). Unlike `/auth` and `/register` it
is not bouncer-gated — the visitor must already be authenticated.

**Typical use:**
1. User registers on `/register`.
2. When `registration.passkey_prompt` is `"optional"` or `"required"`, the
   register page redirects to `/passkey?group_uuid=<uuid>` after signup.
3. User can also reach `/passkey` from your account settings page.

**URL parameters:** same as `/auth` (`group_uuid`, `redirect`, `back`).

---

## Per-Group Branding via `group_uuid`

All hosted auth pages (`/auth`, `/register`, `/passkey`) resolve a group from
`?group_uuid=<uuid>` and apply the group's auth config (theme, methods,
passkey policy). Use this for multi-tenant deployments where multiple groups
share one auth domain.

```html
<a href="/auth?group_uuid=abc123uuid">Sign In to Client Brand</a>
```

The `group_uuid` param is preserved through navigation (login ↔ register
switcher), the OAuth round-trip (Google/Apple callback), and the
login → passkey enrollment redirect.

---

## `mojo-auth.js` Helpers

```javascript
// Fetch the resolved auth config
const cfg = await MojoAuth.getAuthConfig({ groupUuid: 'abc123' });
// cfg.theme.appTitle, cfg.login.methods, cfg.registration.passsKeyPrompt, …

// Register a passkey for the currently authenticated user
await MojoAuth.registerPasskey();

// SMS login
const { sessionToken } = await MojoAuth.startSmsLogin(phoneNumber);
const result = await MojoAuth.verifySmsLogin(sessionToken, code);
```
