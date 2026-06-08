# Account & Authentication — REST API Reference

## Common Flows

### User Registration & Onboarding

1. [Auth Pages](auth_pages.md) — built-in bouncer-gated `/auth`, `/register`, and `/passkey` pages (or build your own)
   - [Auth Config](auth_config.md) — `GET /api/auth/config`; per-group branding, enabled methods, passkey enrollment policy
2. [Authentication § Registration](authentication.md#registration) — `POST /api/auth/register` endpoint
3. [Email Verification](email_verification.md) — verification gate, send/confirm flow, invite links
4. [Authentication § Login](authentication.md#login) — first login after verification

### Securing the Login Flow

1. [Bouncer](bouncer.md) — bot detection gate, challenge page, assess endpoint, token lifecycle; embedding on static pages / SPAs, MojoSentinel continuous monitoring, nginx drop-in gating
2. [Authentication](authentication.md) — login, MFA challenge, token refresh
3. [Passkeys](passkeys.md) / [Magic Login](magic_login.md) — passwordless alternatives

---

## API Reference

- [Authentication](authentication.md) — Login, registration, token refresh, password reset
- [Step-Up Auth (HTTP 440)](step_up_auth.md) — Handling `reauth_required`: when a sensitive op needs a recent login, re-auth and retry; the `440` vs `401`/`403` distinction
- [Auth Config](auth_config.md) — `GET /api/auth/config`; per-group branding and enabled auth methods for custom front-ends
- [Passkeys](passkeys.md) — Passwordless login with WebAuthn/FIDO2
- [TOTP / Authenticator App](mfa_totp.md) — 2FA and standalone login with Google Authenticator etc.
- [SMS OTP](mfa_sms.md) — 2FA and standalone login via SMS code
- [Magic Login Links](magic_login.md) — Passwordless login via emailed link
- [OAuth / Social Login](oauth.md) — Login with Google (and more)
- [Email & Phone Verification](email_verification.md) — Verification gates, send/verify flow, invite links
- [Email Change](email_change.md) — Self-service email address change with password confirmation
- [Phone Number Change](phone_change.md) — Self-service phone number change with OTP verification
- [User Self-Management](user_self_management.md) — Everything a logged-in user can do for their own account (profile, avatar, password, email, phone, passkeys, TOTP, files, notifications, activity log)
- [User API](user.md) — User profile, registration, password reset
- [Group API](group.md) — Groups, membership, permissions
- [Admin Portal API Guide](admin_portal.md) — Building admin consoles (users, groups, secure settings)
- [API Keys](api_keys.md) — Long-lived tokens for programmatic access
- [Webhook Signing](webhook_signing.md) — Per-Group HMAC secret for outbound webhooks, `X-Mojo-Signature` header, rotation endpoint
- [Webhook Subscriptions](webhook_subscriptions.md) — CRUD endpoints for managing webhook receivers per Group
- [Custom Auth Models](custom_auth_models.md) — JWT, OAuth, and passkeys for non-User models (e.g. game.Player)
- [Notifications](notifications.md) — Inbox, mark read, WebSocket delivery
- [GeoIP & Geofencing](geoip.md) — IP geolocation, time lookup, and geofence pre-flight check (`GET /api/geo/check`)
- [Login Events](login_events.md) — Login history with geolocation, aggregation for maps, anomaly detection
- [Public Messages](public_messages.md) — Bouncer-gated contact / support form and admin endpoint
