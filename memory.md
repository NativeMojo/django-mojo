# Django-MOJO — Working Memory

_Hygiene: max 5 bullets per section. Outcomes over narrative. Archive when resolved._

## Current Focus
-

## Key Decisions
_Non-obvious choices made — why, not just what._
- Extra (non-canonical) register fields live in `auth_config.registration.extra_fields` (per-group, default `[]`), NOT in `registration.fields` (closed canonical set). `on_register` capture allowlist = group-declared names ∪ global `REGISTRATION_EXTRA_FIELDS`; captured values persist to `user.metadata["registration"]` AND pass to `USER_REGISTERED_HANDLER`. Hosted page: URL query param → silent capture, else plain text input. (ITEM-001 / REQ-029)

## Watch List
_Fragile areas, known debt, things to tread carefully._
-

## In Progress
-

## Archive
