# Auth Pages — Web Developer Reference

Django-served login and registration pages. These are fully functional out of
the box — no frontend app required. The pages handle all auth flows including
OAuth, passkeys, SMS login, password reset, and magic login links.

All branding and feature configuration is controlled per group via the auth
config. See [Auth Config](auth_config.md) for details and the
`GET /api/auth/config` endpoint.

---

## Page URLs

| URL | Purpose |
|-----|---------|
| `/auth` | Login page (default, configurable via `BOUNCER_LOGIN_PATH`) |
| `/register` | Registration page (configurable via `BOUNCER_REGISTER_PATH`) |
| `/passkey` | Passkey enrollment page (authenticated, not bouncer-gated) |

Both `/auth` and `/register` are protected by the bouncer bot detection gate.
On first visit, users see a brief verification challenge. After passing, they
receive an HttpOnly pass cookie that skips the challenge on subsequent visits.

---

## Login Page (`/auth`)

Which methods are shown depends on the resolved auth config's `login.methods`.
Default set: `password`, `sms`, `passkey`, `magic`, `google`, `apple`.

- **password** — email/password sign in
- **sms** — phone number + 6-digit SMS code sign in
- **google** — redirects to Google, returns to `/auth?code=...&state=...`
- **apple** — same flow
- **passkey** — WebAuthn discoverable credential flow
- **magic** — sends a one-click sign-in email
- Forgot password (code or link; auto-routes via SMS when identity is phone)
- Session check — auto-redirects if user is already authenticated

When `password` is not among `login.methods` but `sms` is (a passwordless
config), the page opens directly on the SMS phone-entry form rather than a
sign-in form — so SMS sign-in is the first thing the user sees. With
`password` present, the sign-in form leads and "Sign in with a code" is a
button alongside passkey/Google/Apple.

### URL Parameters

| Param | Purpose |
|-------|---------|
| `?token=ml:...` | Magic login token — auto-consumed on page load |
| `?token=pr:...` | Password reset token — opens "Set New Password" view |
| `?code=...&state=...` | OAuth callback — auto-completes the OAuth flow |
| `?redirect=<url>` | Custom redirect after login (also `?next=` or `?returnTo=`). Preserved through bouncer challenge. |
| `?back=<url>` | Override the "Back to website" hero link |
| `?group_uuid=<uuid>` | Load per-group branding and restrict to the group's enabled methods. Must be `group_uuid` — the framework reserves `?group=` for integer IDs. |

### After Login

1. Access and refresh tokens are stored in `localStorage`
2. An overlay shows "Signed in! Taking you there now..."
3. User is redirected to `theme.success_redirect` (default `/`)

---

## Registration Page (`/register`)

Default fields (configurable via `registration.fields` in the auth config):
- First name / Last name (optional, side-by-side)
- Email (required)
- Password (required, with visibility toggle)
- Terms & Conditions checkbox (when `theme.terms_url` is set)

**Phone-first flow** — when the schema marks `phone` with `verify: "sms"`, the
form is a three-step state machine:

1. **Step 1 — Identity**: phone number entry only
2. **Step 2 — Verify**: 6-digit SMS code with "Resend code" and "Back" links
3. **Step 3 — Profile**: remaining fields (name, DOB, password) and final submit

When `/auth/phone/register/verify` returns `account_exists: true`, the hosted
form **skips step 3** and immediately submits the register call with only
`phone` + `verified_phone_token`. The server signs the user into the existing
account and returns JWT tokens — the same response shape as a new registration.
Profile fields submitted alongside the token are ignored for an existing account.

**Existing-phone behavior summary:**

| Schema `phone.verify` | Phone already registered | Result |
|---|---|---|
| `"sms"` | Yes | Signed into existing account (JWT tokens returned) |
| `"sms"` | No | New account created (JWT tokens or `requires_verification`) |
| absent | Yes | 400 — duplicate account error |

**Passwordless registration** — when `password` is absent from
`registration.fields`, the form has no password input. The account is created
without a usable password. The user signs in afterward using the SMS-code flow:

1. `POST /api/auth/sms/login` with `{"phone_number": "<phone>"}` — sends a 6-digit code and returns `{"status": true}`.
2. `POST /api/auth/sms/verify` with `{"phone_number": "<phone>", "code": "<code>"}` — returns JWT tokens on success.

A passwordless schema always includes a `phone` field with `verify: "sms"` — you
can confirm this by inspecting `registration.fields` from `GET /api/auth/config`.

### SMS code autofill

OTP texts (login and registration) include an origin-bound last line —
`@<your-domain> #<code>` — so browsers can autofill the code. The hosted
`/auth` and `/register` pages already handle this. If you build a custom
code-entry UI:

- Put `autocomplete="one-time-code"` on the code `<input>` — iOS Safari
  autofills from the keyboard suggestion bar.
- For Android Chrome, call the WebOTP API while the code step is visible:
  `navigator.credentials.get({ otp: { transport: ['sms'] } })`, then fill the
  field with the resolved `.code`.
- Both require an HTTPS page whose domain matches the `@host` in the SMS — the
  server derives that host from your request's `Origin` header.
Passwordless accounts may also enroll a passkey (if `registration.passkey_prompt`
is enabled) as an additional login path.

**DOB field** — three segmented numeric inputs (`MM` / `DD` / `YYYY`), mobile
numeric keyboard, paste-aware (`MM/DD/YYYY`, `MM-DD-YYYY`, `YYYY-MM-DD`),
submits as ISO `yyyy-mm-dd`.

**After registration** — when `registration.passkey_prompt` is `"optional"` or
`"required"`, the page redirects to `/passkey` instead of straight to
`success_redirect`.

Also supports Google/Apple OAuth sign-up (same buttons, same flow).

---

## Passkey Enrollment Page (`/passkey`)

A standalone, themeable passkey enrollment page. Not bouncer-gated — the
visitor must already be authenticated. The page reads the JWT from `localStorage`
and runs the WebAuthn registration round-trip.

**Typical use:**
1. User registers on `/register`
2. When `registration.passkey_prompt` is `"optional"` or `"required"`, the
   register page redirects here
3. Can also be linked standalone from account settings

**URL parameters:** same as `/auth` (`group_uuid`, `redirect`, `back`).

---

## OAuth Flow

1. User clicks "Google" or "Apple" button
2. `MojoAuth.startGoogleLogin()` / `MojoAuth.startAppleLogin()` redirects to provider
3. Provider redirects back to `/auth?code=xxx&state=yyy`
4. The page JS detects the `code` + `state` params and calls `MojoAuth.completeOAuthLogin()`
5. Backend exchanges code for tokens, creates/links user
6. User is redirected to success page

The `mbp` pass cookie uses `SameSite=Lax` so it is included on the OAuth
redirect back from the provider.

---

## Linking to Auth Pages

```html
<a href="/auth">Sign In</a>
<a href="/register">Create Account</a>

<!-- With redirect back to current page -->
<a href="/auth?redirect=/dashboard">Sign In</a>

<!-- With absolute redirect (cross-origin app) -->
<a href="/auth?redirect=http://myapp.example.com/portal/">Sign In</a>

<!-- With group-specific branding and methods -->
<a href="/auth?group_uuid=abc123uuid">Sign In to Client Brand</a>

<!-- Link to passkey enrollment from account settings -->
<a href="/passkey?group_uuid=abc123uuid&redirect=/settings">Add Passkey</a>
```

---

## Per-Group Branding

When the platform hosts multiple groups with different branding, the auth pages
resolve a group automatically and apply its auth config (theme, methods,
passkey policy).

**Custom auth domain** — point `auth.clientbrand.com` at the same Django
backend. The server detects the hostname, resolves the group, and serves that
group's auth config. No URL params needed.

**`?group_uuid=<uuid>` param** — for shared-domain deployments, append
`?group_uuid=<uuid>` to the auth page URL. The group's auth config is applied
and the param is preserved through navigation (login ↔ register switcher), the
OAuth round-trip, and the login → passkey enrollment redirect.

**Group forwarded on submit** — when the auth page resolves a group, the
rendered forms automatically include `group_uuid` in the POST body. This
satisfies servers configured with `REQUIRE_GROUP_ON_REGISTRATION = True`.

Fetch `GET /api/auth/config?group_uuid=<uuid>` to get the resolved config for
a group — useful for custom front-ends. See [Auth Config](auth_config.md).

---

## Cross-Origin Redirect Handoff

When `?redirect=` points to a different origin from the auth page, the auth
page issues a short-lived single-use handoff code:

```
https://app.example.com/portal?auth_code=<32-hex>
```

The flow:
1. Auth page completes login.
2. Detects cross-origin redirect, POSTs `/api/auth/handoff` → gets `code`.
3. Browser navigates to `<redirect>?auth_code=<code>`.
4. The app calls `MojoAuth.handleAuthCodeFromURL()` on bootstrap — strips the
   param, POSTs `/api/auth/exchange`, stores resulting tokens.

Codes are single-use and expire after `AUTH_HANDOFF_CODE_TTL` seconds (default 60).

```html
<script src="https://auth.example.com/api/account/static/mojo-auth.js"></script>
<script>
  MojoAuth.init({ baseURL: 'https://auth.example.com' });
  MojoAuth.handleAuthCodeFromURL().then(function (data) {
    if (data || MojoAuth.isAuthenticated()) {
      bootApp();
    } else {
      window.location.href = 'https://auth.example.com/auth?redirect=' +
        encodeURIComponent(window.location.href);
    }
  });
</script>
```

---

## Static Assets

```
GET /api/account/static/mojo-auth-theme.css   → dark premium theme
GET /api/account/static/mojo-auth.js          → MojoAuth library
```

Served with `Cache-Control: public, max-age=86400` in production.

---

## Honeypot Decoy Pages

| Path | GET | POST |
|------|-----|------|
| `/login` | Decoy login page | Logs credentials, returns "Invalid credentials" |
| `/signin` | Decoy login page | Same |
| `/signup` | Decoy login page | Same |

---

## Bouncer Challenge

| Pre-screen score | Challenge | Friction |
|-----------------|-----------|----------|
| < 20 | Static button, centered | Near-zero |
| 20–39 | Button shifts between spots | Low |
| >= 40 | Moving target button | Moderate |

After passing, an HttpOnly `mbp` pass cookie is set (24h TTL). Subsequent
visits skip the challenge.
