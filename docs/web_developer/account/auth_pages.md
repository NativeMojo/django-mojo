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
Default set: `password`, `sms`, `passkey`, `magic`, `google`, `apple`, `github`.

- **password** — email/password sign in
- **sms** — phone number + 6-digit SMS code sign in
- **google** — redirects to Google, returns to `/auth?code=...&state=...`
- **apple** — same flow
- **github** — same flow
- **passkey** — WebAuthn discoverable credential flow
- **magic** — sends a one-click sign-in email
- Forgot password (code or link; auto-routes via SMS when identity is phone)
- Session check — auto-redirects if user is already authenticated

When `password` is not among `login.methods` but `sms` is (a passwordless
config), the page opens directly on the SMS phone-entry form rather than a
sign-in form — so SMS sign-in is the first thing the user sees. With
`password` present, the sign-in form leads and "Sign in with a code" is a
button alongside passkey/Google/Apple/GitHub.

**Anti-enumeration UX.** SMS sign-in never reveals whether a phone number has an
account — `POST /api/auth/sms/login` returns the same generic success for known
and unknown numbers, and a code is only actually sent to a real account. So the
form sets honest expectations instead of branching on existence: it states up
front that a code arrives *only if the number is already linked to an account*,
and surfaces a "New here? Create an account" link in the SMS view. A person with
no account is no longer dead-ended on a code screen waiting for a text that never
comes — they are pointed to sign-up — while a snooping third party still learns
nothing about whether the number has an account. When the resolved auth config
sets `registration.enabled` to `false`, both this SMS sign-up link and the main
"Create one" switcher are omitted so invite-only groups do not advertise a
disabled registration path.

### URL Parameters

| Param | Purpose |
|-------|---------|
| `?token=ml:...` | Magic login token — auto-consumed on page load |
| `?token=pr:...` | Password reset token — opens "Set New Password" view |
| `?code=...&state=...` | OAuth callback — auto-completes the OAuth flow |
| `?redirect=<url>` | Custom redirect after login (also `?next=` or `?returnTo=`). Preserved through the bouncer challenge **and** the OAuth provider round-trip. Only `http`/`https` URLs and same-origin relative paths are accepted — see below. |
| `?back=<url>` | Override the "Back to website" hero link |
| `?group_uuid=<uuid>` | Load per-group branding and restrict to the group's enabled methods. Must be `group_uuid` — the framework reserves `?group=` for integer IDs. |
| `?auth_theme=<preset>` | Preview/use `minimal`, `compact`, `branded-panel`, or `editorial`. Unknown values are ignored. |
| `?auth_appearance=<mode>` | Preview/use `light`, `dark`, or `system`. Unknown values are ignored. |

Theme and appearance overrides are safe enum selections only; the URL cannot
inject custom copy, colors, images, or CSS. Valid values ride the same
login/register/passkey and bouncer links as `group_uuid` and `redirect`.
The split `branded-panel` and `editorial` layouts show hero artwork; `minimal`
and `compact` intentionally do not.

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

Also supports Google/Apple/GitHub OAuth sign-up (same buttons, same flow).

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

## Passkey Error Handling

Passkey sign-in (`MojoAuth.loginWithPasskeyDiscoverable()` /
`loginWithPasskey()`) and enrollment (`MojoAuth.registerPasskey()`) reject with
a **plain `Error`** carrying user-safe, plain-language `message` text — never
the browser's raw `DOMException`. WebAuthn collapses "no passkey found", "user
cancelled", and "timed out" into a single `NotAllowedError` whose native
message includes a W3C spec URL; the library maps all of that to friendly copy
before rejecting, so you can surface `err.message` directly.

The rejection still preserves diagnostics:

- `err.message` — friendly, display-ready copy (sign-in vs enrollment flavored).
- `err.name` — the original `DOMException` name (`NotAllowedError`,
  `SecurityError`, `NotSupportedError`, `InvalidStateError`, …), so you can
  still branch on it.
- `err.cause` — the original `DOMException`, untouched.
- The original error is also `console.error`-logged (`WebAuthn error:`) for
  field debugging.

Server-side failures from the begin/complete round-trip (method disabled,
rate-limit, 4xx/5xx) are **not** remapped — only the browser prompt is wrapped —
so a backend error still reaches you in the backend's own error shape.

Two helpers back this and are safe to reuse in a custom UI:

- `MojoAuth.getError(err)` — extract a display string from any caught error (an
  `Error`, a string, a backend `{error}` / `{errors:[…]}` shape, or a raw
  `DOMException` as a backstop). Strips spec URLs from the result.
- `MojoAuth.sanitizeMessage(text)` — strip URLs and `See: https://…` fragments
  out of an arbitrary message string (non-strings pass through unchanged). The
  hosted pages also run this at the render layer on every error-type message,
  and offer an inline "Sign in with a text code instead" fallback when a passkey
  sign-in fails and the group also offers SMS login.

---

## OAuth Flow

1. User clicks "Google", "Apple", or "GitHub" button
2. `MojoAuth.startGoogleLogin()` / `MojoAuth.startAppleLogin()` / `MojoAuth.startGitHubLogin()` redirects to provider
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

`redirect`, `next`, `returnTo` and `back` ride the same links, so a destination
you hand a group-scoped portal survives the whole flow. **This is fixed in this
release:** the register → passkey-enrollment hop previously dropped every param
after the first whenever `group_uuid` was combined with one of them, and the
visitor landed on the group's `success_redirect` (or `/`) instead of where they
came from. Nothing changes on your side — links that already worked keep
working, and a `?group_uuid=` with no second param was never affected.

**Group forwarded on submit** — when the auth page resolves a group, the
rendered forms automatically include `group_uuid` in the POST body. This
satisfies servers configured with `REQUIRE_GROUP_ON_REGISTRATION = True`.

Fetch `GET /api/auth/config?group_uuid=<uuid>` to get the resolved config for
a group — useful for custom front-ends. See [Auth Config](auth_config.md).

The bouncer challenge uses the resolved group's `theme.app_title`, `logo_url`,
and `accent_color`, then names `theme.auth_provider_name` in its "Secure
sign-in via …" explanation. This tells visitors both which destination they
are entering and whose account credentials they should use before the login
form appears.

---

## Cross-Origin Redirect Handoff

When `?redirect=` points to a different origin from the auth page, the auth
page issues a short-lived single-use handoff code:

```
https://app.example.com/portal?auth_code=<32-hex>
```

The flow:
1. Auth page completes login.
2. Detects cross-origin redirect, POSTs `/api/auth/handoff` with
   `{"redirect_uri": "<resolved destination>"}` → gets `code`.
3. Browser navigates to `<redirect>?auth_code=<code>`.
4. The app calls `MojoAuth.handleAuthCodeFromURL()` on bootstrap — strips the
   param, POSTs `/api/auth/exchange`, stores resulting tokens.

Codes are single-use and expire after `AUTH_HANDOFF_CODE_TTL` seconds (default 60).

**The destination may be allowlisted server-side — that is an operator
choice.** `?redirect=` is attacker-supplied and the code buys an access +
refresh token pair, so a deployment can restrict which destinations get one.
**The check is opt-in and off by default:** with neither
`AUTH_HANDOFF_ALLOWED_URLS` nor `AUTH_HANDOFF_RESOLVER` configured the server
mints for any destination (and records an internal incident naming it); with
either configured it returns `400` and mints nothing for anything unlisted. See
[Cross-Origin Auth Handoff](authentication.md#cross-origin-auth-handoff).

Either way, **when the mint is refused the auth page shows an error and stays
put** — it does not fall back to navigating to the destination without a code.
If your app's origin is a legitimate destination, ask the operator to add it
before pointing `?redirect=` at it on a deployment that enforces; on one that
does not, it works today but shows up in their incident feed as a destination to
allowlist.

`MojoAuth.requestHandoffCode(destination)` takes the destination as a required
argument and rejects without one, regardless of what the server enforces. Treat
a rejection as "do not navigate".

**Both navigation params are scheme-guarded.** `?back=` (the "Back to website"
link) and `?redirect=`/`?next=`/`?returnTo=` (the post-login destination) accept
only `http`/`https` URLs and same-origin relative paths. One guard, two
different outcomes:

- a refused **`?back=`** value is dropped and the hero link stays hidden;
- a refused **`?redirect=`** value means the page navigates nowhere and shows
  "That destination isn't allowed. Please return to the app and try again."

So `javascript:` and `data:` are refused, and so is any non-web scheme —
`mailto:`, `tel:`, and custom app schemes like `myapp://home`. If you were
bouncing users into a native app that way, point `?redirect=` at an `https`
universal/app link instead. The same guard applies to the group's configured
`success_redirect`, which is where the destination comes from when no param is
present.

> **A custom scheme *is* accepted as an OAuth `redirect_uri` — different
> parameter, different endpoint.** `redirect_uri` on
> `GET /api/auth/oauth/<provider>/begin` is matched server-side against the
> operator's allowlist and supports mobile deep links (see
> [OAuth § Native apps](oauth.md#native-apps--custom-url-schemes)). It does not
> pass through this browser guard. `?redirect=` on the hosted auth pages does,
> and refuses one no matter what the operator has allowlisted.

This is a **scheme** check only, and it runs in the browser before any request
is made — the destination **host** is not restricted by it. Host restriction is
the separate, opt-in, server-side allowlist described above.

**Your origin may be *gated*, which changes what the exchange returns.** A
deployment can declare certain destination hosts gated: the exchange then
answers with a [group-scoped token](authentication.md#group-scoped-tokens-grouptoken)
package — `access_token` starting `gt1.`, `token_type`, `expires_in`, `group`,
and **no `refresh_token` key at all** — instead of the usual JWT pair. Off by
default; nothing changes for a deployment that has not opted in. Three
consequences for a destination app:

- **Never assume `refresh_token` is present.** Branch on the token string
  (`gt1.` ⇒ group token), not on a stored flag.
- **There is no refresh.** On expiry, bounce back through the auth origin with
  `?redirect=<current url>` — the snippet below already does exactly that.
- **If you run your own OAuth callback page, it stops working** once gating
  enforces (`400 "redirect_uri is not on the allowlist"`). Use the hosted auth
  pages' OAuth buttons instead; they route back through this same handoff.

A visitor who is not a member of the group that owns a gated destination gets a
`403` at the handoff, and the auth page shows the server's message rather than
the generic one. Superusers get the same `403` — they can never hold a group
token.

```html
<script src="https://auth.example.com/api/account/static/mojo-auth.js"></script>
<script>
  MojoAuth.init({ baseURL: 'https://auth.example.com' });

  function toAuth() {
    // No refresh path for a group-scoped session — re-bounce instead.
    window.location.href = 'https://auth.example.com/auth?redirect=' +
      encodeURIComponent(window.location.href);
  }

  MojoAuth.handleAuthCodeFromURL().then(function (data) {
    if (data) return bootApp();                       // just exchanged a code
    if (!MojoAuth.isAuthenticated()) return toAuth();
    // Kind-aware: a JWT is judged on its exp claim, a gt1. token on the
    // token_expires_at saved from the exchange response.
    if (MojoAuth.isTokenExpired()) {
      if (MojoAuth.getTokenType() === 'grouptoken') return toAuth();
      return MojoAuth.refreshToken().then(bootApp).catch(toAuth);
    }
    bootApp();
  }).catch(toAuth);
</script>
```

---

## Static Assets

```
GET /api/account/static/mojo-auth-theme.css   → responsive layout + appearance presets
GET /api/account/static/mojo-auth.js          → MojoAuth library
```

Served with `Cache-Control: public, max-age=86400` in production.

---

## Embedding — `/auth`, `/register` and `/passkey` may refuse to be framed

**Do not frame them.** On a deployment that has enabled the hosted-page
Content-Security-Policy, these three pages are served with
`Content-Security-Policy: … frame-ancestors 'none' …`, so a browser refuses to
render them inside an `<iframe>`, `<frame>`, `<embed>` or `<object>` from any
origin, including your own. They hold access and refresh tokens in
`localStorage`; framing them is a clickjacking surface with no legitimate use.

**The header is opt-in and off by default** (`AUTH_CSP_ENABLED` ships `False`),
so on a stock deployment no CSP header is sent at all and these pages can still
be framed. Treat that as an accident of configuration, not a supported
integration — an operator can turn the header on at any time and a framed
embed breaks that day.

Link or redirect to them instead — see
[Linking to Auth Pages](#linking-to-auth-pages) and
[Cross-Origin Redirect Handoff](#cross-origin-redirect-handoff).

`/contact` is **never** affected: even with the policy enabled it deliberately
omits `frame-ancestors` so it can stay embeddable from an external marketing
site. See [Public Messages](public_messages.md).

No REST endpoint, request payload, response body or status code is affected
either way. The policy, when enabled, also locks `script-src` to a per-request
nonce, which matters only if your deployment overrides the page templates — see
the backend note in `docs/django_developer/security/csp.md`.

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
