# Align Logging Sanitization Coverage

**Type**: request
**Status**: open
**Date**: 2026-04-14
**Priority**: medium

## Description

Two consistency improvements to the logging sanitization pipeline added in v1.1.21 (see `planning/done/password-in-error-logs.md`):

1. Align the regex-based `mask_sensitive_data()` with the key-based `SENSITIVE_KEYS` set so both code paths cover the same fields
2. Mask bearer tokens stored in incident event metadata so audit logs do not contain raw, replayable credentials

Both are pre-existing gaps surfaced by the security review of the password-in-error-logs fix. They live in the same sanitization pipeline and should be fixed together.

## Context

The v1.1.21 security fix added `sanitize_dict()` and `SENSITIVE_KEYS` to protect dict-type data flowing into incident events and `Log.logit()`. The existing `mask_sensitive_data()` regex masker was left untouched, which created two inconsistencies:

- **Coverage drift**: `sanitize_dict` now knows about `new_password`, `current_password`, `refresh_token`, `id_token`, `auth_token`, `bearer_token`, `private_key`, `otp`, `mfa_code` — but `mask_sensitive_data` does not. Any code path that stringifies data before passing it to logit (e.g., `middleware/logging.py:170` which serializes `request.DATA` to JSON and passes through `Log.logit()`) relies on the narrower regex and will miss those fields.
- **Raw bearer tokens in audit logs**: `mojo/apps/incident/reporter.py:48` unconditionally stores `event_metadata["bearer"] = request.bearer` for authenticated requests. Bearer tokens are effectively credentials — if an incident log is leaked (SIEM compromise, backup exfil, support export), every captured bearer can be replayed until it expires.

The incident was raised by the security-review agent during build of `password-in-error-logs`. Fixing both together keeps the sanitization story coherent and avoids a future incident where one path leaks a field the other already strips.

## Acceptance Criteria

- `mask_sensitive_data()` and `sanitize_dict()` cover the exact same set of sensitive field names. Adding a new field to `SENSITIVE_KEYS` must automatically extend the string masker — no second place to update.
- A string log containing any key in `SENSITIVE_KEYS` is masked when it flows through `Log.logit()` or `mask_sensitive_data()` directly.
- `event_metadata["bearer"]` in incident events never contains the full raw token. It is either omitted entirely or masked to the last N characters (e.g., `"****abc12345"`) so support can correlate without replaying.
- Existing tests still pass; new tests cover the new behaviors.
- No caller-side changes needed in endpoints or middleware.

## Investigation

**What exists**:
- `mojo/helpers/logit.py:105-112` — `mask_sensitive_data(text)` regex masker, used by `Log.logit()` for the `log` and `payload` string fields
- `mojo/helpers/logit.py:115-142` — `SENSITIVE_KEYS` frozenset and `sanitize_dict(data)` (added in v1.1.21)
- `mojo/apps/incident/reporter.py:47-48` — raw bearer storage in `event_metadata`
- `mojo/apps/logit/models/log.py` — uses `mask_sensitive_data` and `sanitize_dict` on payload
- `mojo/middleware/logging.py:170` — `Log.logit(request, request.DATA.to_json(as_string=True), "request")` — the primary stringified-payload path that relies on `mask_sensitive_data`
- `tests/test_helpers/logit_sanitize.py` — 11 existing tests to extend

**What changes**:

### Change 1: Derive the regex from `SENSITIVE_KEYS`

Rewrite `mask_sensitive_data()` in `mojo/helpers/logit.py` to build its regex patterns from `SENSITIVE_KEYS` at import time rather than hard-coding them:

```python
_SENSITIVE_KEY_PATTERN = re.compile(
    r'("?(' + "|".join(re.escape(k) for k in SENSITIVE_KEYS) + r')"?\s*[:=]\s*"?)[^",\s]+',
    flags=re.IGNORECASE,
)

def mask_sensitive_data(text):
    return _SENSITIVE_KEY_PATTERN.sub(r'\1*****', text)
```

Requires moving `SENSITIVE_KEYS` above `mask_sensitive_data()` in the module (currently defined after). Compile the pattern once at module load, not on every call.

### Change 2: Mask bearer tokens in incident metadata

In `mojo/apps/incident/reporter.py`, replace line 48:

```python
# Before
if request.bearer:
    event_metadata["bearer"] = request.bearer

# After
if request.bearer:
    event_metadata["bearer"] = mask_token(request.bearer)
```

Add a `mask_token(token, visible=4)` helper in `mojo/helpers/logit.py` that returns `"****" + token[-visible:]` when the token is longer than `visible`, else `"*****"`. Four characters is enough for support correlation while keeping the token unusable.

### Constraints

- **Backwards compat**: Audit logs are append-only; existing rows with raw bearers stay as-is. Only new events are masked.
- **Regex greediness**: The existing regex stops at `[^",\s]+`, so it will not over-mask structured data. Deriving from `SENSITIVE_KEYS` preserves this behavior — we only change the key alternation, not the value-matching tail.
- **`re.escape`**: Required when joining keys into the pattern so any future key with regex metacharacters does not break the pattern.
- **Case insensitivity**: Existing regex uses `re.IGNORECASE`; preserve this so `Password=foo` and `PASSWORD=foo` are both masked.
- **No per-endpoint changes**: Both fixes live in `mojo/helpers/logit.py` and `mojo/apps/incident/reporter.py` — no REST handler or middleware touches.

**Related files**:
- `mojo/helpers/logit.py` — both changes (derive regex + add `mask_token`)
- `mojo/apps/incident/reporter.py` — apply `mask_token` to bearer
- `tests/test_helpers/logit_sanitize.py` — extend with new test cases
- `docs/django_developer/helpers/logit.md` — document `mask_token` and the derivation link
- `docs/django_developer/logging/incidents.md` — note bearer masking

## Tests Required

- `mask_sensitive_data` masks all fields in `SENSITIVE_KEYS` — parametrized over the full set, using both `key=value` and `"key": "value"` JSON-like forms
- Adding a new key to `SENSITIVE_KEYS` is automatically picked up by `mask_sensitive_data` (test asserts both functions agree after a test-local monkey patch or by iterating over `SENSITIVE_KEYS`)
- Case-insensitive masking — `PASSWORD=secret`, `Password=secret`, `password=secret` all redacted
- `mask_token` with normal-length token returns `"****<last4>"`
- `mask_token` with short token (≤ visible length) returns `"*****"` without exposing any characters
- `mask_token` on empty/None returns empty/None safely
- `_create_event_dict` with a request that has `request.bearer` set produces `event_metadata["bearer"]` that (a) is masked, (b) never equals the raw token, (c) ends with the last 4 characters of the raw token

## Out of Scope

- Retroactive masking of existing incident event rows — this is a forward-only fix
- Changing the `SENSITIVE_KEYS` set itself (already comprehensive after v1.1.21)
- Rewriting `Log.logit()` to use `sanitize_dict` for all dict-valued logs (already handled in v1.1.21 for the `payload` field)
- Masking other request/response fields outside the sensitive-key set (phone numbers, emails, etc.) — privacy, not credential exposure, and belongs in a separate request
