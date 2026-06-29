# Django-MOJO — Working Memory

_Hygiene: max 5 bullets per section. Outcomes over narrative. Archive when resolved._

## Current Focus
-

## Key Decisions
_Non-obvious choices made — why, not just what._
- Extra (non-canonical) register fields live in `auth_config.registration.extra_fields` (per-group, default `[]`), NOT in `registration.fields` (closed canonical set). `on_register` capture allowlist = group-declared names ∪ global `REGISTRATION_EXTRA_FIELDS`; captured values persist to `user.metadata["registration"]` AND pass to `USER_REGISTERED_HANDLER`. Hosted page: URL query param → silent capture, else plain text input. (ITEM-001 / REQ-029)
- OTP/verification flows are **retry-safe**: read → compare → consume the secret/session ONLY on success (never `getdel`-before-compare). Brute force is bounded by the per-IP rate limit + TTL, NOT per-session attempt counters — consistent across `_verify_otp`, `verify_phone_verify_code`, and `phone_register.verify_code`. Do not "harden" by deleting on a wrong attempt; that burns the session and dead-ends the happy path. (ITEM-005)
- **Account enumeration is forbidden** across auth flows: sign-in/start responses are identical for known vs unknown identifiers; existence is only revealed AFTER the user proves ownership (enters the texted/emailed code) — defeats the spouse-snooping threat. Fix sign-in dead-ends with generic honest copy + a visible sign-up link (`login.html` SMS view), NEVER a per-number branch or `account_exists` signal on sign-in. `on_sms_login` stays uniform. (ITEM-006)
- **Display-name moderation is advisory, not a hard block**: `User.validate_name_fields` logs+allows a content_guard `block` decision instead of raising, because content_guard's naive-substring matching over-blocks legitimate names (Matsushita, Harshita, Scunthorpe — "shit"/"cunt" substrings). content_guard core is unchanged; comment/chat/contact_form surfaces still hard-block. Don't reinstate the `raise`. (ITEM-007)

## Watch List
_Fragile areas, known debt, things to tread carefully._
-

## In Progress
-

## Archive
