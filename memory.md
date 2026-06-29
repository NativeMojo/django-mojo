# Django-MOJO — Working Memory

_Hygiene: max 5 bullets per section. Outcomes over narrative. Archive when resolved._

## Current Focus
-

## Key Decisions
_Non-obvious choices made — why, not just what._
- Extra (non-canonical) register fields live in `auth_config.registration.extra_fields` (per-group, default `[]`), NOT in `registration.fields` (closed canonical set). `on_register` capture allowlist = group-declared names ∪ global `REGISTRATION_EXTRA_FIELDS`; captured values persist to `user.metadata["registration"]` AND pass to `USER_REGISTERED_HANDLER`. Hosted page: URL query param → silent capture, else plain text input. (ITEM-001 / REQ-029)
- OTP/verification flows are **retry-safe**: read → compare → consume the secret/session ONLY on success (never `getdel`-before-compare). Brute force is bounded by the per-IP rate limit + TTL, NOT per-session attempt counters — consistent across `_verify_otp`, `verify_phone_verify_code`, and `phone_register.verify_code`. Do not "harden" by deleting on a wrong attempt; that burns the session and dead-ends the happy path. (ITEM-005)

## Watch List
_Fragile areas, known debt, things to tread carefully._
-

## In Progress
-

## Archive
