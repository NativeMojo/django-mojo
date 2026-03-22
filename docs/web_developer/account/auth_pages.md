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
| `?redirect=/path` | Custom redirect after login (also `?next=` or `?returnTo=`) |
| `?title=My+App` | Override brand title (URL param overrides settings) |
| `?subtitle=Welcome` | Override subtitle text |
| `?logo=/logo.png` | Override logo URL |

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

<!-- With custom branding via URL params -->
<a href="/auth?title=Acme+Corp&logo=/acme-logo.png">Sign In to Acme</a>
```

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
