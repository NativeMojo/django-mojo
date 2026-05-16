# Auth Pages — Web Developer Reference

Django-served login and registration pages. These are fully functional out of
the box — no frontend app required. The pages handle all auth flows including
OAuth, passkeys, password reset, and magic login links.

---

## Page URLs

| URL | Purpose |
|-----|---------|
| `/auth` | Login page (default, configurable via `BOUNCER_LOGIN_PATH`) |
| `/register` | Registration page (configurable via `BOUNCER_REGISTER_PATH`) |

Both pages are protected by the bouncer bot detection gate. On first visit,
users see a brief verification challenge. After passing, they receive an
HttpOnly pass cookie that skips the challenge on subsequent visits.

---

## Login Page (`/auth`)

Supports all auth flows:

- **Email/password** — standard sign-in form
- **Google OAuth** — redirects to Google, returns to `/auth?code=...&state=...`
- **Apple OAuth** — same flow
- **Passkeys** — WebAuthn discoverable credential flow
- **Forgot password** — choose code (6-digit OTP) or link method
- **Magic login link** — sends a one-click sign-in email
- **Session check** — auto-redirects if user is already authenticated

### URL Parameters

| Param | Purpose |
|-------|---------|
| `?token=ml:...` | Magic login token — auto-consumed on page load |
| `?token=pr:...` | Password reset token — opens "Set New Password" view |
| `?code=...&state=...` | OAuth callback — auto-completes the OAuth flow |
| `?redirect=<url>` | Custom redirect after login — relative or absolute URL (also `?next=` or `?returnTo=`). Preserved through bouncer challenge. |
| `?back=<url>` | Override the "Back to website" hero link. Falls back to `AUTH_BACK_TO_WEBSITE_URL` setting if not provided. |
| `?title=My+App` | Override brand title (URL param overrides settings) |
| `?subtitle=Welcome` | Override subtitle text |
| `?logo=/logo.png` | Override logo URL |
| `?group_uuid=<uuid>` | Load per-group branding (logo, brand name, OAuth settings, success redirect). Used when multiple groups share one auth domain. **Note:** must be `group_uuid`, not `group` — the framework dispatcher reserves `?group=` for integer IDs and rejects UUID values with 400 before the page renders. |

### After Login

On successful authentication:
1. Access and refresh tokens are stored in `localStorage`
2. An overlay shows "Signed in! Taking you there now..."
3. User is redirected to `AUTH_SUCCESS_REDIRECT` (default `/`)

---

## Registration Page (`/register`)

Fields:
- First name / Last name (optional, side-by-side)
- Email (required)
- Password (required, with visibility toggle)
- Terms & Conditions checkbox

Also supports Google/Apple OAuth sign-up (same buttons, same flow — the
backend auto-creates the account on first OAuth login).

After registration, the user is automatically logged in and redirected.

---

## OAuth Flow

1. User clicks "Google" or "Apple" button
2. `MojoAuth.startGoogleLogin()` / `MojoAuth.startAppleLogin()` redirects to provider
3. Provider redirects back to `/auth?code=xxx&state=yyy`
4. The page JS detects the `code` + `state` params and calls `MojoAuth.completeOAuthLogin()`
5. Backend exchanges code for tokens, creates/links user
6. User is redirected to success page

The `mbp` pass cookie uses `SameSite=Lax` so it's included when the browser
navigates back from the OAuth provider. This means the user lands on the full
login page (not the bouncer challenge) after the OAuth redirect.

---

## Linking to Auth Pages

From your application, link to the login and registration pages:

```html
<a href="/auth">Sign In</a>
<a href="/register">Create Account</a>

<!-- With redirect back to current page -->
<a href="/auth?redirect=/dashboard">Sign In</a>

<!-- With absolute redirect (cross-origin app) -->
<a href="/auth?redirect=http://myapp.example.com/portal/">Sign In</a>

<!-- With back-to-website override -->
<a href="/auth?redirect=/portal&back=https://www.example.com">Sign In</a>

<!-- With custom branding via URL params -->
<a href="/auth?title=Acme+Corp&logo=/acme-logo.png">Sign In to Acme</a>
```

---

## Cross-Origin Redirect Handoff

When `?redirect=` points to the **same origin** as the auth page, the JWT in
`localStorage` is shared with the destination — no extra step is needed. When
the redirect target is on a **different origin** (e.g. auth at
`auth.example.com`, app at `app.example.com`), `localStorage` is partitioned by
origin so the destination cannot read the tokens minted by the auth page. To
avoid the infinite "no JWT → bounce to auth" loop, the auth page issues a
short-lived, single-use **handoff code** and appends it to the redirect URL:

```
https://app.example.com/portal?auth_code=<32-hex>
```

The flow:

1. Auth page completes login (password / OAuth / passkey / magic link / MFA).
2. `_mat.redirect()` parses `redirect` and detects a different origin.
3. The page POSTs `/api/auth/handoff` (authenticated via `Authorization: Bearer`
   header) and gets back a `code`.
4. The browser navigates to `<redirect>?auth_code=<code>`.
5. The app calls `MojoAuth.handleAuthCodeFromURL()` on bootstrap — it strips
   the param, POSTs `/api/auth/exchange`, and stores the resulting access +
   refresh tokens in its own `localStorage`.

Codes are 32-hex random strings, single-use, and expire after
`AUTH_HANDOFF_CODE_TTL` seconds (default 60). The exchange endpoint is
public (so the app can reach it before it has a JWT) and rate-limited to
20 attempts/min/IP.

### App bootstrap snippet

```html
<script src="https://auth.example.com/api/account/static/mojo-auth.js"></script>
<script>
  // baseURL must point at the auth origin so /api/auth/exchange resolves
  MojoAuth.init({ baseURL: 'https://auth.example.com' });

  MojoAuth.handleAuthCodeFromURL()
    .then(function (data) {
      if (data) {
        // Tokens are stored — boot the app
        bootApp();
      } else if (MojoAuth.isAuthenticated()) {
        bootApp();
      } else {
        window.location.href = 'https://auth.example.com/auth?redirect=' +
          encodeURIComponent(window.location.href);
      }
    });
</script>
```

### Security note

There is no allowlist on the redirect destination. A malicious
`?redirect=evil.example.com` link, opened by an already-signed-in user, will
hand a JWT to `evil` after auto-session-resume. Deployments that need stricter
control should layer their own allowlist (e.g. `ALLOWED_REDIRECT_URLS`) over
the `?redirect=` param.

---

## Customization

### Branding — via admin portal

All branding is configurable from the admin portal using the `Setting` model:

| Setting | Description |
|---------|-------------|
| `AUTH_APP_TITLE` | Brand name in card header |
| `AUTH_LOGO_URL` | Logo image URL |
| `AUTH_FAVICON_URL` | Favicon URL (served by nginx) |
| `AUTH_HERO_IMAGE_URL` | Left panel background image |
| `AUTH_HERO_HEADLINE` | Text over the hero image |
| `AUTH_HERO_SUBHEADLINE` | Supporting text below headline |
| `AUTH_BACK_TO_WEBSITE_URL` | "Back to website" link in hero panel |
| `AUTH_TERMS_URL` | Terms & Conditions link on register page |

### Per-Group Branding

When the platform hosts multiple groups with different branding, the auth pages
can resolve a group automatically and apply its settings.

**Custom auth domain** — point `auth.clientbrand.com` at the same Django
backend. The server detects the hostname, resolves the group, and serves that
group's logo, brand name, features, and success redirect. No URL params needed.

**`?group_uuid=<uuid>` param** — for shared-domain deployments, append
`?group_uuid=<uuid>` to the auth page URL. The group's branding is applied
and the param is preserved through navigation (login ↔ register) and the
OAuth round-trip (Google/Apple callback includes `?group_uuid=` in the
return URL).

```html
<!-- Link to the auth page with group-scoped branding -->
<a href="/auth?group_uuid=abc123uuid">Sign In to Client Brand</a>
```

The param name is `group_uuid` rather than `group` because the framework's
URL dispatcher reserves `?group=` for integer-ID lookup and rejects any
non-integer value with `400 Invalid group ID` before the bouncer page
runs.

Per-group settings that take effect: all `AUTH_*` settings (logo, title, hero,
OAuth buttons, success redirect, layout, CSS). Each setting resolves with a
parent-chain fallback: group → parent group → global.

**Group forwarded on submit** — when the auth page resolves a group (via
hostname or `?group=`), the rendered register and login forms automatically
include `group_uuid` in the POST body sent to `/api/auth/register` and
`/api/auth/login`. This satisfies servers configured with
`REQUIRE_GROUP_ON_REGISTRATION = True` from the hosted page, and gives the
backend's `request.group` middleware and `USER_LOGIN_HANDLER` the operator
context on password logins. Single-tenant deployments are unaffected —
when no group is resolved, `group_uuid` is omitted from the body.

### Features — enable/disable per provider

| Setting | Default | Description |
|---------|---------|-------------|
| `AUTH_ENABLE_GOOGLE` | `False` | Show Google OAuth button |
| `AUTH_ENABLE_APPLE` | `False` | Show Apple OAuth button |
| `AUTH_ENABLE_PASSKEYS` | `False` | Show Passkey button |

When all three are disabled, the "or continue with" divider and OAuth button
row are excluded from the page entirely.

### Layout

| Setting | Value | Description |
|---------|-------|-------------|
| `AUTH_LAYOUT` | `card` (default) | Centered card on page background |
| `AUTH_LAYOUT` | `fullscreen` | Edge-to-edge, hero 50% / form 50% |

### Theme colors

The CSS uses custom properties. Override in your project's CSS:

```css
:root {
    --mat-page-bg: #686278;
    --mat-card-bg: #2c2638;
    --mat-accent: #7c5cbf;
    --mat-accent-hover: #6b4eae;
    --mat-input-bg: #353042;
    --mat-border: #433d52;
    --mat-text: #f2eff7;
    --mat-muted: #9e96ad;
}
```

---

## Static Assets

The auth pages load CSS and JS from these API endpoints:

```
GET /api/account/static/mojo-auth-theme.css   → dark premium theme
GET /api/account/static/mojo-auth.js          → MojoAuth library
```

These are served by Django with browser caching headers (`Cache-Control:
public, max-age=86400` in production, `no-store` in debug mode).

---

## Honeypot Decoy Pages

Common bot-scanned paths serve a visually identical login page that submits
to a dead endpoint:

| Path | GET | POST |
|------|-----|------|
| `/login` | Decoy login page | Logs credentials, returns "Invalid credentials" |
| `/signin` | Decoy login page | Same |
| `/signup` | Decoy login page | Same |

The decoy form submits to `window.location.pathname` (same path the bot
found). Bots scanning `/login` see a working login page, submit credentials,
and get a realistic error. The submission is logged as a security incident.

---

## Bouncer Challenge

First-time visitors (no pass cookie) see a brief verification challenge before
the login page. The challenge tier is based on the pre-screen risk score:

| Pre-screen score | Challenge | Friction |
|-----------------|-----------|----------|
| < 20 | Static button, centered | Near-zero |
| 20–39 | Button shifts between spots | Low |
| >= 40 | Moving target button | Moderate |

After passing the challenge, an HttpOnly `mbp` pass cookie is set (24h TTL).
Subsequent visits within 24h skip the challenge entirely.
