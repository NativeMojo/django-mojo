# GeoIP whitelist/unblock/block actions silently ignored

**Type**: bug
**Status**: resolved
**Date**: 2026-04-20
**Severity**: high

## Description
`PUT /api/system/geoip/<pk>` with a body like `{"whitelist": "Known office ip"}` (or `unblock`, `block`, `unwhitelist`, `refresh`, `threat_analysis`) has no effect. The IP is not whitelisted or unblocked. No error is returned; the response looks successful.

## Context
Operators rely on this endpoint to whitelist known office IPs and clear false-positive blocks. Because the request succeeds quietly, an operator believes the IP is whitelisted when it is not — leaving them locked out or still blocked in production. Affects all `GeoLocatedIP` POST_SAVE_ACTIONS.

## Acceptance Criteria
- `PUT /api/system/geoip/<pk>` with `{"whitelist": "<reason>"}` calls `GeoLocatedIP.on_action_whitelist` and sets `is_whitelisted=True`, `whitelisted_reason=<reason>`, clears block fields.
- `{"unblock": "<reason>"}`, `{"block": {...}}`, `{"unwhitelist": true}`, `{"refresh": true}`, `{"threat_analysis": true}` all route to their `on_action_*` handlers.
- Regression test asserts that after PUT with `{"whitelist": "..."}`, the DB row shows `is_whitelisted=True`.

## Investigation
**Likely root cause**: Trailing comma on `RestMeta.POST_SAVE_ACTIONS` converts the list into a 1-tuple containing a list.

At [mojo/apps/account/models/geolocated_ip.py:101](mojo/apps/account/models/geolocated_ip.py:101):
```python
POST_SAVE_ACTIONS = ["refresh", "threat_analysis", "block", "unblock", "whitelist", "unwhitelist"],
```
The trailing comma makes the RHS `(["refresh", ..., "unwhitelist"],)` — a tuple whose only element is a list.

The dispatcher at [mojo/models/rest.py:1006](mojo/models/rest.py:1006) does `if key in post_save_actions:`. Since the tuple contains a list (not strings), `"whitelist" in post_save_actions` is always `False`. The key then falls through to `on_rest_save_field("whitelist", ...)` at line 1010, which has no matching model field, so the value is discarded and no `on_action_whitelist` is invoked.

**Confidence**: confirmed (via code reading — trailing comma is unambiguous Python)
**Code path**:
- [mojo/apps/account/models/geolocated_ip.py:101](mojo/apps/account/models/geolocated_ip.py:101) — source of the trailing comma
- [mojo/models/rest.py:997-1010](mojo/models/rest.py:997) — where POST_SAVE_ACTIONS is checked
- [mojo/apps/account/models/geolocated_ip.py:479-489](mojo/apps/account/models/geolocated_ip.py:479) — the `on_action_*` handlers that never fire

**Fix**: Remove the trailing comma on line 101.

**Regression test**: not written yet — should go in `tests/test_account/` and assert that a `PUT` to the geoip endpoint with `{"whitelist": "..."}` sets `is_whitelisted=True` on the DB row.

**Related files**:
- `mojo/apps/account/models/geolocated_ip.py`
- `tests/test_account/` (new regression test)

## Resolution

**Status**: resolved
**Date**: 2026-04-20
**Commit**: ba9fc0a

### What Was Built
Removed the trailing comma on `GeoLocatedIP.RestMeta.POST_SAVE_ACTIONS` so the REST dispatcher (`on_rest_save`) correctly routes `whitelist`, `unblock`, `block`, `unwhitelist`, `refresh`, and `threat_analysis` to their `on_action_*` handlers.

### Files Changed
- `mojo/apps/account/models/geolocated_ip.py` — removed trailing comma on line 101
- `tests/test_account/test_geoip_actions.py` — new regression tests
- `CHANGELOG.md` — Fixed entry under v1.1.26

### Tests
- `tests/test_account/test_geoip_actions.py` — 3 tests: config shape, whitelist dispatch, unblock dispatch
- Run: `bin/run_tests -t test_account.test_geoip_actions`
- Full suite: 1710 passed, 0 failed

### Docs Updated
- Existing geoip docs already documented all six actions — no content changes needed
- `CHANGELOG.md` — added a Fixed entry

### Security Review
Review flagged two concerns that are **pre-existing** and out of scope for this one-line fix, now surfaced because the actions actually dispatch:
1. `GeoLocatedIP.RestMeta` defines no `SAVE_PERMS`, so write operations fall through to `VIEW_PERMS`. Users with read-only `view_security` can now invoke privileged whitelist/block actions. Recommend adding explicit `SAVE_PERMS = ['manage_users', 'manage_security', 'security']`.
2. Regression tests bypass the REST auth layer. Adding an `opts.client`-based integration test asserting 403 for unauthenticated / under-privileged callers would harden coverage.

### Follow-up
- Add explicit `SAVE_PERMS` on `GeoLocatedIP.RestMeta` (security-review finding #1)
- Add client-level permission test for geoip actions (security-review finding #2)
- Consider truncating reason strings to 255 chars in `on_action_block/unblock/whitelist` handlers
