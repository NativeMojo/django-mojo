# Django-MOJO — Working Memory

_Hygiene: max 5 bullets per section. Outcomes over narrative. Archive when resolved._

## Current Focus
-

## Key Decisions
_Non-obvious choices made — why, not just what._
- Extra (non-canonical) register fields live in `auth_config.registration.extra_fields` (per-group, default `[]`), NOT in `registration.fields` (closed canonical set). `on_register` capture allowlist = group-declared names ∪ global `REGISTRATION_EXTRA_FIELDS`; captured values persist to `user.metadata["registration"]` AND pass to `USER_REGISTERED_HANDLER`. Hosted page: URL query param → silent capture, else plain text input. (ITEM-001 / REQ-029)
- OTP/verification flows are **retry-safe**: read → compare → consume the secret/session ONLY on success (never `getdel`-before-compare). Brute force is bounded by the per-IP rate limit + TTL, NOT per-session attempt counters — consistent across `_verify_otp`, `verify_phone_verify_code`, and `phone_register.verify_code`. Do not "harden" by deleting on a wrong attempt; that burns the session and dead-ends the happy path. (ITEM-005) — Where consume-FIRST is required (to prevent duplicate users / double-firing `USER_REGISTERED_HANDLER`), instead **restore** the consumed token if the post-consume work fails: `phone_register.restore()`, called in `on_register`'s `except` path around the handler-firing atomic block (scoped so post-handler failures keep it consumed). Don't "simplify" that try/except away. (ITEM-008)
- **Account enumeration is forbidden** across auth flows: sign-in/start responses are identical for known vs unknown identifiers; existence is only revealed AFTER the user proves ownership (enters the texted/emailed code) — defeats the spouse-snooping threat. Fix sign-in dead-ends with generic honest copy + a visible sign-up link (`login.html` SMS view), NEVER a per-number branch or `account_exists` signal on sign-in. `on_sms_login` stays uniform. (ITEM-006)
- **Display-name moderation is advisory, not a hard block**: `User.validate_name_fields` logs+allows a content_guard `block` decision instead of raising, because content_guard's naive-substring matching over-blocks legitimate names (Matsushita, Harshita, Scunthorpe — "shit"/"cunt" substrings). content_guard core is unchanged; comment/chat/contact_form surfaces still hard-block. Don't reinstate the `raise`. (ITEM-007)
- **Client IP (`request.ip`) comes from `X-Real-IP`, never `X-Forwarded-For`.** `get_remote_ip` reads the proxy-authoritative `X-Real-IP` (the universal `asgi.inc` sets it to `$remote_addr`, overwriting any client value), falls back to `REMOTE_ADDR`, and normalizes via `_normalize_ip` (IP:port / bracketed / IPv4-mapped IPv6). The leftmost `X-Forwarded-For` is client-spoofable — do NOT reinstate reading it. Holds for both direct and load-balanced deploys (both preserve the client into `$remote_addr`); every deployment MUST set `X-Real-IP`. (ITEM-009)

## Watch List
_Fragile areas, known debt, things to tread carefully._
- `request.ip` may now be `None` on garbage/missing IP (since ITEM-009). `UserLoginEvent.ip_address` is non-nullable and `account/rest/user.py:637` swallows the error → a login event is **silently dropped** in a misconfigured deploy (production-unreachable behind the X-Real-IP proxy). Fix needs a model migration — file a separate item before relying on complete login-IP audit. (ITEM-009)

## In Progress
-

## Archive
