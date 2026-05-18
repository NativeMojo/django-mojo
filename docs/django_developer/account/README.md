# Account & Authentication — Django Developer Reference

- [User Model](user.md) — User model, permissions, JWT auth
- [Group Model](group.md) — Group/organization model, membership, hierarchy
- [Authentication Flow](auth.md) — JWT tokens, login, password reset
- [Auth Pages](auth_pages.md) — Hosted `/auth` login and register pages; branding, bouncer integration, multi-tenant group forwarding
- [OAuth / Social Login](oauth.md) — Provider setup, auto-link logic, email verification, MFA behaviour, adding new providers
- [Email Change](email_change.md) — Self-service email address change flow
- [API Keys](api_keys.md) — Group-scoped programmatic access, permissions, token lifecycle
- [Webhook Signing](webhook_signing.md) — Per-Group HMAC primitive for outbound webhooks, auto-signing via `jobs.publish_webhook(group=...)`, receiver verification helper
- [Notifications](notifications.md) — User inbox, WebSocket + push delivery, expiry
- [GeoIP](geoip.md) — IP geolocation, threat intelligence, time lookup
- [Geofencing](geofence.md) — Policy-based geographic access control: rule DSL, system+group rules, decorator, settings, bypass permission
- [Login Events](login_events.md) — UserLoginEvent model, geo tracking, anomaly flags, metrics
- [Inactive User/Group Sweep](inactive_sweep.md) — Auto-warn and disable inactive users and groups; feature flags, exemptions, email templates, incident events
- [Disable Lifecycle](disable_lifecycle.md) — Unified disable/reactivate state for User and Group: `metadata.protected.disable.*` schema, REST POST_SAVE_ACTIONS, throttle-read endpoint, service API
- [Bouncer](bouncer.md) — Bot detection gate for login, registration, and public message pages; adaptive learning, device reputation, signature management
- [Public Messages](bouncer.md#public-messages-contact--support) — Bouncer-gated contact/support intake; `PublicMessage` model, `KIND_SCHEMAS`, notify_admins, admin RestMeta
