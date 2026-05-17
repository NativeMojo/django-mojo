# Register flow leaves display_name NULL

**Type**: issue
**Status**: planned
**Date**: 2026-05-17
**Severity**: medium

## Description
Users created through the password registration flow (`POST /api/auth/register`, the endpoint the Bouncer registration page submits to) end up with `display_name = NULL`. The User model has an `on_rest_pre_save()` hook that auto-generates a display_name from the username when one isn't provided, but the register handler bypasses that hook by calling Django's `user.save()` directly. OAuth registration is unaffected because it either sets `display_name` from the provider profile or relies on its own code path.

## Context
- `display_name` is the field surfaced in the UI, member lists (`account/models/member.py:77-78`), search (`SEARCH_FIELDS = ["username", "email", "display_name", "phone_number"]`), and push-notification device naming (`account/rest/push.py:68`).
- For users registered via Bouncer, member-listing and search-by-display_name silently return blank entries until somebody updates the profile through REST.
- Email-based registrations behave the same way as phone-based ones — both go through `on_register` and both miss the hook.
- The `infer_names_from_email()` helper called from `on_rest_created()` is also skipped, so business emails like `john.smith@acme.com` no longer get `first_name`/`last_name` inferred at registration time.

## Acceptance Criteria
- After `POST /api/auth/register` succeeds, the created user has a non-empty `display_name` (matches `generate_display_name()` output when the client did not supply one).
- `infer_names_from_email()` still runs for new password-registered users with business emails (regression: first/last name inference should not silently break).
- The new regression test `tests/test_register/register.py::test_register_sets_display_name` passes.
- Existing register tests in `tests/test_register/` continue to pass.

## Investigation
**Likely root cause**: `on_register` at [mojo/apps/account/rest/user.py:362-383](mojo/apps/account/rest/user.py#L362-L383) builds the user with `User(...)` and calls `user.save()` directly. The display_name auto-population lives in the REST framework hook `on_rest_pre_save` at [mojo/apps/account/models/user.py:822-823](mojo/apps/account/models/user.py#L822-L823) (`if not self.display_name: self.display_name = self.generate_display_name()`), and the post-save inference lives in `on_rest_created` at [mojo/apps/account/models/user.py:770-774](mojo/apps/account/models/user.py#L770-L774). Neither hook fires when the model is saved outside `on_rest_request`/`on_rest_create`, so registered users get `display_name = None` and miss email-based name inference.

**Confidence**: confirmed (regression test fails: `display_name must be populated after register, got None for username='reg_displayname_...'`).

**Code path**:
- [mojo/apps/account/rest/user.py:244](mojo/apps/account/rest/user.py#L244) — `on_register` entry, decorated with `@md.requires_bouncer_token('registration')`.
- [mojo/apps/account/rest/user.py:363-383](mojo/apps/account/rest/user.py#L363-L383) — direct `User(...)` + `user.save()` (no hook invocation).
- [mojo/apps/account/models/user.py:780-826](mojo/apps/account/models/user.py#L780-L826) — `on_rest_pre_save` (display_name backfill at 822-823).
- [mojo/apps/account/models/user.py:770-774](mojo/apps/account/models/user.py#L770-L774) — `on_rest_created` (`infer_names_from_email` + metrics).
- [mojo/apps/account/rest/oauth.py:139-145](mojo/apps/account/rest/oauth.py#L139-L145) — OAuth path that *does* set `display_name` explicitly (reference for fix shape).

**Regression test**: `tests/test_register/register.py::test_register_sets_display_name` — currently fails with `display_name=None`, will pass once the fix lands.

**Related files**:
- `mojo/apps/account/rest/user.py` (fix target — set `user.display_name = user.generate_display_name()` before save, and call `user.infer_names_from_email()` so business-email name inference is preserved).
- `mojo/apps/account/models/user.py` (reference for `generate_display_name`, `infer_names_from_email`, and the existing hook logic — consider whether the backfill should move into Django's `save()` so all save paths are covered).
- `tests/test_register/register.py` (regression test already added).

## Plan

**Status**: planned
**Planned**: 2026-05-17

### Objective
Make `generate_display_name()` walk a sensible priority chain (first/last → email → phone), and have the password-register flow invoke it (plus `infer_names_from_email()`) before save so Bouncer-registered users get a non-NULL `display_name` and inferred first/last names.

### Steps
1. `mojo/apps/account/models/user.py:731-738` — rewrite `generate_display_name()` to walk a priority chain:
   - If `first_name` AND `last_name` are set → return `f"{first_name} {last_name}".strip()`
   - Else if `email` is set → return email local-part with `_`/`.` → space, title-cased (current behavior, scoped to email)
   - Else if `phone_number` is set → return the phone number as-is
   - Else fall back to the existing username-based transformation (safety net for legacy callers / users created without any identity)
2. `mojo/apps/account/rest/user.py:382-383` — inside the atomic block, immediately before `user.save()`:
   - Call `user.infer_names_from_email()` FIRST (so the rewritten `generate_display_name()` can pick the names branch when inference succeeds)
   - Then `if not user.display_name: user.display_name = user.generate_display_name()`
3. `tests/test_register/register.py` — keep the existing `test_register_sets_display_name`; add three more tests:
   - `test_register_display_name_priority_names` — submit first/last + email → `display_name == "First Last"`
   - `test_register_display_name_priority_phone_only` — phone identity, no names → `display_name == phone_number`
   - `test_register_infers_names_from_business_email` — submit `john.smith@acme.com` with no first/last → assert `first_name == "John"`, `last_name == "Smith"`, `display_name == "John Smith"`
4. `CHANGELOG.md` — bugfix entry: "Fix: password-register flow (`POST /api/auth/register`) now populates `display_name` and infers first/last from business emails; `generate_display_name()` now walks first-name+last-name → email → phone fallback chain."

### Design Decisions
- **Rewrite `generate_display_name()` instead of computing display_name inline in the register handler**: every caller (`on_rest_pre_save` backfill at line 822-823, the `full_name` property at line 277, OAuth fallbacks) gets the same priority chain — single source of truth, no drift between code paths.
- **Surgical call-site fix in `on_register` (not a `User.save()` override)**: only two non-test paths construct `User(...)` directly — register (this bug) and OAuth (which already handles `display_name` explicitly per-provider). A `save()` override would change behavior for tests that intentionally pass `display_name=None`. KISS.
- **Run `infer_names_from_email()` pre-save, not post-save (unlike `on_rest_created`'s post-save `.update()` dance)**: simpler — one save, no follow-up `.update()`. Safe because `infer_names_from_email` doesn't depend on the row being persisted, and the inferred values must be in place before `generate_display_name()` can pick the names branch.
- **Keep username-derived fallback as the final tier in `generate_display_name`**: safety net for code paths where neither names, email, nor phone are present (e.g. test users built with only a username, system-created service users).

### Edge Cases
- User submits first/last → `infer_names_from_email` no-ops (it returns early when either name is set); display_name comes from priority #1.
- Phone identity, no names, no email → display_name is the phone number itself (priority #3). Acceptable per user direction; better than NULL or a title-cased phone.
- Phone identity, no names, business email also provided → inference fills names → display_name = "First Last" (priority #1).
- Consumer email domain (gmail.com, etc.) → inference skipped per existing rules; display_name = email local-part titled (priority #2).
- Existing user re-registers → blocked earlier at user.py:316-321; fix only touches new users.
- Other callers of `generate_display_name()` (`full_name` property, REST backfill) — behavior changes for users who have names/email/phone set, going from "title-cased username" to "First Last" / email-derived / phone. This is strictly an improvement; no caller depends on the username-derived form.

### Testing
- `register: display_name must be auto-populated from username (regression)` -> `tests/test_register/register.py::test_register_sets_display_name` (already in place)
- `register: display_name prefers first+last when both provided` -> `tests/test_register/register.py::test_register_display_name_priority_names`
- `register: display_name falls back to phone number when no names or email` -> `tests/test_register/register.py::test_register_display_name_priority_phone_only`
- `register: business email infers first/last and builds display_name from them` -> `tests/test_register/register.py::test_register_infers_names_from_business_email`
- Full `tests/test_register/` and `tests/test_oauth/` suites must continue to pass (no regression in OAuth display_name handling or REST `on_rest_pre_save` backfill).

### Docs
- `CHANGELOG.md` — one-line bugfix entry covering both the register fix and the `generate_display_name()` priority-chain change.
- No `docs/django_developer/` or `docs/web_developer/` changes — the `display_name` field, its existence on every user, and the public API contract are unchanged. This restores intended behavior, doesn't introduce new surface.
