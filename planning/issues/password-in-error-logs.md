# Plaintext Passwords Logged in REST Error Handler

**Type**: bug
**Status**: open
**Date**: 2026-04-14
**Severity**: critical

## Description

The REST dispatcher in `mojo/decorators/http.py` passes `request_data=request.DATA` to `class_report_incident_for_user` in all four exception handlers (lines 99, 114, 128, 144). When a login attempt fails — wrong username (raises `PermissionDeniedException` at `account/rest/user.py:78`) or wrong password (line 81) — the full `request.DATA` including the plaintext `password` field is persisted to the incident/event system.

This means every failed login writes the user's cleartext password into audit logs, SIEM, and any downstream log aggregation.

## Context

- Violates OWASP logging guidelines and PCI-DSS requirements
- Real user passwords have been exposed in production logs since at least 2026-04-03
- Affects all endpoints that receive sensitive fields (login, registration, password reset), not just login — though login is the most frequently triggered path
- The raw `request.DATA` is an `objict` and may contain `password`, `new_password`, `current_password`, `token`, etc.

## Acceptance Criteria

- The `password` field (and other sensitive fields like `new_password`, `current_password`, `token`, `secret`) must never appear in incident event payloads
- Fix must apply globally in the dispatcher so no individual endpoint needs to remember to sanitize
- Existing incident logging must continue to work for non-sensitive fields
- A regression test confirms sensitive fields are stripped from logged request data

## Investigation

**Likely root cause**: `mojo/decorators/http.py` dispatcher passes raw `request.DATA` to incident reporting without any field sanitization.

**Confidence**: confirmed

**Code path**:
1. `mojo/decorators/http.py:95-108` — `MojoException` handler passes `request_data=request.DATA`
2. `mojo/decorators/http.py:110-121` — `PermissionError` handler, same
3. `mojo/decorators/http.py:123-136` — `ValueError` handler, same
4. `mojo/decorators/http.py:138-153` — catch-all `Exception` handler, same
5. `mojo/apps/account/rest/user.py:78` — login raises `PermissionDeniedException` (subclass of `MojoException`), triggering handler #1
6. `mojo/models/rest.py:1265` — `class_report_incident` passes `**context` (including `request_data`) to `incident.report_event()`
7. `mojo/apps/incident/reporter.py:52-62` — `**kwargs` flow unsanitized into `event_metadata` and are persisted to the Event model

**Additional exposure**: `logit.Log.logit()` (`mojo/apps/logit/models/log.py:123`) — the `payload` kwarg is stored raw. Only the `log` field (line 92) passes through `logit.mask_sensitive_data()`.

**Existing partial mitigation**: `logit.mask_sensitive_data()` (`mojo/helpers/logit.py:105-112`) — regex-based masker that catches `password`, `token`, `api_key`, `secret`, etc. in stringified text. Already used by `logit.Log` for the `log` field, but not used by incident reporting at all.

**Regression test**: not feasible — requires a running server with incident event storage to verify logged payloads

**Related files**:
- `mojo/apps/incident/reporter.py` — primary fix location (sanitize kwargs in `_create_event_dict` before storing to metadata)
- `mojo/apps/logit/models/log.py` — secondary fix (sanitize `payload` kwarg like `log` field)
- `mojo/helpers/logit.py` — existing `mask_sensitive_data()` to reuse or extend
- `mojo/decorators/http.py` — callers that pass `request_data=request.DATA`

## Plan

### Approach: Sanitize at the storage chokepoints, not callers

Rather than patching the 4 exception handlers in `http.py` (caller-side), sanitize at the two storage chokepoints so **all** callers are protected automatically:

#### Change 1: `mojo/apps/incident/reporter.py` — `_create_event_dict()`
Before `event_metadata.update(processed_kwargs)` (line 62), run `logit.mask_sensitive_data()` on any string/dict values in `processed_kwargs`. Specifically:
- If a kwarg value is a dict (like `request_data`), stringify it then mask, or strip known sensitive keys directly
- Stripping keys is safer than regex masking on structured data — use a small `SENSITIVE_KEYS` set (`password`, `new_password`, `current_password`, `token`, `secret`, `api_key`, `access_token`, `pin`, `cvv`, `ssn`) and replace values with `"*****"`
- Apply recursively to nested dicts

#### Change 2: `mojo/apps/logit/models/log.py` — `logit()` method
Line 123 passes `kwargs.get("payload", None)` raw. If payload is a string, run `logit.mask_sensitive_data()` on it before storing. If it's a dict, apply the same key-stripping as Change 1.

#### Shared helper
Add a `sanitize_dict(data, sensitive_keys=SENSITIVE_KEYS)` function in `mojo/helpers/logit.py` next to the existing `mask_sensitive_data()`. This gives both incident and logit a single function to call on structured data (dicts/objicts). Keep `mask_sensitive_data()` for string-based masking.

### What NOT to change
- `mojo/decorators/http.py` — no changes needed; the chokepoint fix protects all callers
- No per-endpoint fixes in `account/rest/user.py` — the framework handles it

### Risks
- `mask_sensitive_data` regex is greedy — could mask legitimate values that happen to contain `token=` in a string. The key-stripping approach for dicts avoids this.
- Must handle `objict` (which behaves like dict) — verify `.items()` works on request.DATA
