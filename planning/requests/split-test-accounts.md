# Split test_accounts into Granular Test Modules

**Type**: request
**Status**: planned
**Date**: 2026-04-02
**Priority**: high

## Description

`test_accounts` is a 469-test monolith marked `serial: True` because 2 of its 28 files use `server_settings()`. This forces all 469 tests to run sequentially even though 26 files are safe to parallelize. Split into 8 domain-focused modules so only the 2 files that need serial actually run serial, unlocking ~383 tests for parallel execution.

## Proposed Split

| New Module | Files | Tests | Serial? |
|---|---|---|---|
| `test_auth` | accounts.py, magic_login.py, secrets.py, test_permissions.py | ~49 | No |
| `test_oauth` | oauth.py, oauth_apple.py | ~40 | Yes (server_settings) |
| `test_mfa` | totp.py, totp_recovery.py, passkeys.py, verification.py | ~126 | No |
| `test_phone` | phone_change.py, sms.py | ~28 | No |
| `test_email` | email_change.py | ~58 | No |
| `test_security` | bouncer.py, security_events.py, session_revoke.py, device_tracking.py, pii.py | ~46 | Yes (bouncer uses server_settings) |
| `test_notifications` | notifications.py, notification_prefs.py, push_notifications.py | ~55 | No |
| `test_user_mgmt` | deactivation.py, invite_flow.py, member_invite.py, username_change.py, api_keys.py, user_api_keys.py | ~67 | No |

## Investigation

- `server_settings()` used only in `bouncer.py` (line 291) and `oauth.py` (line 74)
- All files share `requires_apps: ["mojo.apps.account"]`
- No cross-file dependencies — each file has its own setup function
- Current `__init__.py` has `TESTIT = {"serial": True, "requires_apps": ["mojo.apps.account"]}`

## Plan

**Status**: planned
**Planned**: 2026-04-02

### Objective

Split the 469-test `test_accounts` monolith into 8 domain-focused modules, reducing serial tests from 469 to ~86 and enabling parallel execution for ~383 tests.

### Steps

1. Create 8 new module directories under `tests/`:
   - `tests/test_auth/__init__.py` — `TESTIT = {"requires_apps": ["mojo.apps.account"]}`
   - `tests/test_oauth/__init__.py` — `TESTIT = {"requires_apps": ["mojo.apps.account"], "serial": True}`
   - `tests/test_mfa/__init__.py` — `TESTIT = {"requires_apps": ["mojo.apps.account"]}`
   - `tests/test_phone/__init__.py` — `TESTIT = {"requires_apps": ["mojo.apps.account"]}`
   - `tests/test_email/__init__.py` — `TESTIT = {"requires_apps": ["mojo.apps.account"]}`
   - `tests/test_security/__init__.py` — `TESTIT = {"requires_apps": ["mojo.apps.account"], "serial": True}`
   - `tests/test_notifications/__init__.py` — `TESTIT = {"requires_apps": ["mojo.apps.account"]}`
   - `tests/test_user_mgmt/__init__.py` — `TESTIT = {"requires_apps": ["mojo.apps.account"]}`

2. Move files with `git mv` (preserves history):
   - `test_accounts/{accounts,magic_login,secrets,test_permissions}.py` -> `test_auth/`
   - `test_accounts/{oauth,oauth_apple}.py` -> `test_oauth/`
   - `test_accounts/{totp,totp_recovery,passkeys,verification}.py` -> `test_mfa/`
   - `test_accounts/{phone_change,sms}.py` -> `test_phone/`
   - `test_accounts/email_change.py` -> `test_email/`
   - `test_accounts/{bouncer,security_events,session_revoke,device_tracking,pii}.py` -> `test_security/`
   - `test_accounts/{notifications,notification_prefs,push_notifications}.py` -> `test_notifications/`
   - `test_accounts/{deactivation,invite_flow,member_invite,username_change,api_keys,user_api_keys}.py` -> `test_user_mgmt/`

3. Delete `tests/test_accounts/` (will be empty after moves)

4. Update `tests/test_helpers/test_runner_config.py` test that references `test_accounts` module path — point to one of the new modules instead

5. Run full test suite to verify all tests pass with same counts

### Design Decisions

- **git mv**: preserves file history in git blame/log
- **No code changes**: all 28 files are fully independent (verified — zero cross-file imports or shared state)
- **All modules require mojo.apps.account**: same app dependency, just different serial flags
- **Only test_oauth and test_security are serial**: the only modules containing files that use `server_settings()`
- **Name `test_auth` not `test_accounts`**: avoids confusion with existing `test_account` (singular) module

### Edge Cases

- **`test_account` (singular) already exists**: separate module testing account models directly — no conflict
- **No numbered file prefixes**: none of the files use numeric ordering, so no reordering concerns
- **Test data isolation**: verified each file creates its own test users/data independently

### Testing

- Full suite: `bin/run_tests` — all tests should pass with same total counts
- Individual modules: `bin/run_tests -t test_auth`, `bin/run_tests -t test_mfa`, etc.

### Docs

- No doc changes needed — module structure is self-documenting via TESTIT config
