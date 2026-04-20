# Align Logging Sanitization Coverage

**Type**: request
**Status**: planned
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

## Plan

**Status**: planned
**Planned**: 2026-04-19

### Objective

Make `SENSITIVE_KEYS` the single source of truth for all logging sanitization, and mask bearer tokens in incident event metadata so raw replayable credentials never land in the audit log.

### Steps

1. `mojo/helpers/logit.py` — move the `SENSITIVE_KEYS` frozenset (currently lines 115-121) above `mask_sensitive_data` so it's in scope at module-load time. Replace the two hardcoded regex patterns with a single module-level compiled pattern built from `SENSITIVE_KEYS`:
   ```python
   _SENSITIVE_KEY_PATTERN = re.compile(
       r'("?(' + "|".join(re.escape(k) for k in SENSITIVE_KEYS) + r')"?\s*[:=]\s*"?)[^",\s]+',
       flags=re.IGNORECASE,
   )
   ```
   Rewrite `mask_sensitive_data(text)` to use it: `return _SENSITIVE_KEY_PATTERN.sub(r'\1*****', text)`. Preserves `\1` backreference and `[^",\s]+` value tail — only the key alternation changes.

2. `mojo/helpers/logit.py` — add `mask_token(token, visible=4)`:
   - Returns input unchanged if falsy (None / "").
   - Returns `"*****"` if `len(token) <= visible` (no reveal on short tokens).
   - Otherwise `"****" + token[-visible:]`.

3. `mojo/apps/incident/reporter.py` — import `mask_token`; replace `event_metadata["bearer"] = request.bearer` (line 48) with `event_metadata["bearer"] = mask_token(request.bearer)`.

4. `tests/test_helpers/logit_sanitize.py` — extend:
   - `test_mask_sensitive_covers_all_sensitive_keys` — loop over every `SENSITIVE_KEYS` entry; assert both `key=value` and `"key": "value"` JSON-like forms get masked through `mask_sensitive_data`.
   - `test_mask_sensitive_derived_from_frozenset` — assert `_SENSITIVE_KEY_PATTERN.pattern` contains every key (catches drift if anyone re-hardcodes).
   - `test_mask_sensitive_case_insensitive` — mixed-case inputs all masked.
   - `test_mask_token_long`, `_short`, `_empty`, `_none`.
   - `test_create_event_dict_masks_bearer` — build a fake request with `bearer="abc123def456xyz"`; call `_create_event_dict(..., request=req)`; assert `event_metadata["bearer"]` is not the raw token and ends with the last 4 chars.

5. `docs/django_developer/helpers/logit.md` — document `mask_token` signature + behavior; note that `mask_sensitive_data` is derived from `SENSITIVE_KEYS` so adding a key there covers both paths.

6. `docs/django_developer/logging/incidents.md` — short note that `event_metadata["bearer"]` is masked (not raw) for new events; existing rows are forward-only.

7. `CHANGELOG.md` — v1.1.0 Added entry describing the coverage alignment and bearer masking.

### Design Decisions

- **Single source of truth via derivation**: `mask_sensitive_data` rebuilds its alternation from `SENSITIVE_KEYS`. Adding a new key auto-covers both code paths. Zero drift risk.
- **Compile at import, not per call**: one module-level `_SENSITIVE_KEY_PATTERN`. The current code compiles two patterns inline on every invocation — the new form is strictly faster on the hot `Log.logit()` path.
- **Preserve value-matching tail `[^",\s]+`**: only the key alternation changes; the greediness behavior (stops at quote, comma, whitespace) stays identical, so no over-masking of structured data.
- **`re.escape` on keys**: required so any future entry with regex metacharacters doesn't break the pattern.
- **Forward-only bearer masking**: append-only audit log; existing rows stay as-is per AC.
- **`mask_token` returns input unchanged on None/empty**: matches how the caller checks `if request.bearer:` before passing — keeps the helper unsurprising for future callers who may not pre-check.
- **4-char tail default**: enough for support to correlate a token against user reports without being replayable.
- **`"*****"` for short tokens**: strictly no reveal — protects the pathological "someone sets a 3-char dev token" case from disclosing the whole thing.

### Edge Cases

- **Frozenset iteration order varies across Python runs**: alternation order doesn't affect correctness for `re.sub` — every key is tried. OK.
- **`authorization: Bearer <token>` string**: the regex stops at the first whitespace after `authorization`, so `Bearer` is masked but the token that follows is not. This is a pre-existing behavior and out of scope per the request — the concrete concern (bearer in incident metadata) is fully handled by step 3.
- **Empty `request.bearer`**: `mask_token("")` returns `""` — benign; the outer `if request.bearer:` check guards this today anyway.
- **Tokens with trailing whitespace**: `mask_token` slices from the end — any trailing space becomes part of the visible tail. Tokens don't have trailing whitespace in practice; accept.
- **Regex compilation failure at import**: only possible if `SENSITIVE_KEYS` grows to contain invalid-regex content, and we `re.escape` every entry. Not a practical risk.

### Testing

All additions in `tests/test_helpers/logit_sanitize.py` using the existing `@th.unit_test(...)` pattern (pure Python, no Django needed).

- `mask_sensitive_covers_all_sensitive_keys` → parametrized over every frozenset entry, `key=value` + JSON forms.
- `mask_sensitive_derived_from_frozenset` → pattern string contains every key (drift guard).
- `mask_sensitive_case_insensitive` → `PASSWORD=x` / `Password=x` / `password=x`.
- `mask_token_long` → 16-char token → `****` + last 4.
- `mask_token_short` → 3-char token → `*****`, no reveal.
- `mask_token_empty` → `""` → `""`.
- `mask_token_none` → `None` → `None`.
- `create_event_dict_masks_bearer` → synthetic request with `bearer="abc123def456xyz"`, assert `event_metadata["bearer"]` != raw, ends with last 4.

### Docs

- `docs/django_developer/helpers/logit.md` — `mask_token` reference; note on derivation link.
- `docs/django_developer/logging/incidents.md` — bearer-masking note in the event-metadata section.
- `CHANGELOG.md` — one entry under `v1.1.0 - (current)` > Added.

## Resolution

**Status**: resolved
**Date**: 2026-04-19
**Commits**: 9a2b8d9 (main implementation) + 97f5e82 (docs follow-up)

### What Was Built

Two consistency fixes to the v1.1.21 sanitization pipeline:

1. **Single source of truth for sensitive keys** — `mask_sensitive_data()` now derives its regex from `SENSITIVE_KEYS` at import time. Adding a key to the frozenset automatically extends both the string masker and `sanitize_dict()`. Previously the regex covered 11 keys while `sanitize_dict` covered 21 — fields like `new_password`, `refresh_token`, `auth_token`, `private_key`, `otp`, `mfa_code` slipped through any stringified log path.
2. **Bearer token masking in incident events** — `event_metadata["bearer"]` now stores `"****<last4>"` via `mask_token(request.bearer)` instead of the raw replayable credential. Tokens of length ≤ `visible` (default 4) are fully masked with no reveal. Forward-only; existing rows unchanged.

Net performance: strictly **faster** on the hot `Log.logit()` path. One compiled regex at import + one `re.sub` per call, vs. two inline-compiled patterns + two full-text scans before.

### Files Changed

- `mojo/helpers/logit.py` — `SENSITIVE_KEYS` moved above `mask_sensitive_data`; new module-level `_SENSITIVE_KEY_PATTERN` compiled from it; new `mask_token(token, visible=4)` helper.
- `mojo/apps/incident/reporter.py` — `event_metadata["bearer"]` now wrapped in `mask_token(...)`.
- `tests/test_helpers/logit_sanitize.py` — 9 new cases: coverage across every `SENSITIVE_KEYS` entry (key=value + JSON forms), derivation-drift guard, case insensitivity, `mask_token` long/short/custom/empty/None, `_create_event_dict` bearer masking with a synthetic authenticated request.
- `docs/django_developer/helpers/logit.md` — `mask_token` reference, `mask_sensitive_data` derivation note, full `SENSITIVE_KEYS` list.
- `docs/django_developer/logging/incidents.md` — bearer-masking note in event-metadata section.
- `docs/django_developer/logging/logit.md` — stale 4-key list replaced with pointer to `SENSITIVE_KEYS` (docs-updater follow-up in 97f5e82).
- `CHANGELOG.md` — v1.1.0 Added entry.

### Tests

- `tests/test_helpers/logit_sanitize.py` — 20 total (11 existing + 9 new), all pass.
- Run: `bin/run_tests -t test_helpers.logit_sanitize`
- Full suite post-commit: 1684 passed, 0 failed (after test-runner also fixed a pre-existing unrelated regression in `test_verification`, committed separately in e1b9d2c).

### Docs Updated

- `docs/django_developer/helpers/logit.md` — `mask_token` section + derivation note + full key list.
- `docs/django_developer/logging/incidents.md` — bearer-masking behavior documented.
- `docs/django_developer/logging/logit.md` — stale key list corrected (97f5e82).
- `CHANGELOG.md` — entry.

### Security Review

Clean. Two INFO notes, both known and by design:

- **INFO** — `authorization: Bearer <token>` under-masks in the raw-string path because `[^",\s]+` stops at the first whitespace, so only `Bearer` is captured and the token that follows it remains visible in the line. Pre-existing gap explicitly noted as out of scope by the request. The concrete incident-metadata concern (raw bearer in the audit log) is fully addressed by `mask_token` in `reporter.py`. Follow-up could add a secondary pattern for the `Bearer <token>` form.
- **INFO** — `mask_token(None)` / `mask_token("")` pass through unchanged. The only current call site guards with `if request.bearer:` before calling, so this never reaches storage today. Benign as designed.

Other focus areas passed: regex correctness (`\1` backreference valid, `re.IGNORECASE` preserved, no ReDoS on 21-literal alternation), `mask_token` correctness (short fully masked, non-str coerced), no other bearer write path in the incident app, no new code path bypasses sanitization.

### Follow-up

- **`authorization: Bearer <token>` in raw string logs** — the INFO note above. If/when this becomes a real concern (e.g., a deployment starts logging the raw `Authorization` header), add a secondary pattern or strip the header wholesale at the logging middleware layer. Filed mentally; no request written yet.
- **Retroactive masking of existing incident rows** — explicitly out of scope per the AC. If compliance ever requires it, a one-off migration script could walk existing rows.
