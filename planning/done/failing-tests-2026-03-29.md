# Failing Tests After Security System + Permission Changes

**Type**: issue
**Status**: resolved
**Date**: 2026-03-29
**Priority**: high

## Description

After the recent batch of commits (security system, permission cleanup, login event tracking), several tests are failing that were passing yesterday. Need to investigate and fix each one.

## Failing Tests

From full suite run on 2026-03-29:

### Incident Logging Tests
- `test_accounts.deactivation` — "deactivate request: incident logged" / "deactivate confirm: incident logged before anonymisation"
- `test_accounts.phone_change` — "phone/change/request: incident logged"
- `test_accounts.sessions` — "session revoke: incident logged on success" / "session revoke: incident logged on failed attempt"

### OAuth Tests
- `test_accounts.oauth` — "oauth: auto-link creates new user for unknown email" / "oauth: disabled user is rejected" / "oauth: new user created via OAuth has unusable password"

### Other
- `test_accounts.push_notifications` — "comprehensive_test_mode_verification" / "cleanup_test_data"
- `test_accounts.user_api_key` — "user_api_key: cleanup"
- `test_accounts.write_protect` — "write-protect: superuser can set is_email_verified=True on a new user"

## Likely Causes

Recent commits in the last 24 hours:
- `96bc94f` — new full blown security system with llm integration
- `e50c7dd` / `08719d2` — permission cleanup
- `b2ecb9e` — login geolocation tracking (UserLoginEvent added to jwt_login)
- `80054c2` — hardening of login event endpoints

The incident logging failures likely stem from the security system or permission changes, not login event tracking (which is wrapped in try/except).

## Approach

- Use `bin/run_tests -s` to stop on first failure
- Use `bin/run_tests -s --continue` to resume from last failure after fixing
- Fix the code (not the tests) unless the test expectation is wrong
- Work with the user if a fix requires design decisions
