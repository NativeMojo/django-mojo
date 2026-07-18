---
id:
type: bug
title: "test_account.test_user_actions — 4 tests omit is_email_verified before login and fail on long-lived servers"
priority: P3
effort: XS
owner:
opened: 2026-07-17
depends_on: []
related: []
links: []
---

# test_account.test_user_actions — 4 tests omit `is_email_verified` before login

## What & Why

Referred from mojo-verify (`mverify_api` MVERIFY-API-025, test-suite failure
catalogue, serial run 2026-06-07). Four tests in
`tests/test_account/test_user_actions.py` failed with
"bare admin login failed" / "plain user login failed":

- `users_perm_can_force_verify_phone_on_other_user`
- `owner_only_cannot_force_verify_email`
- `users_perm_can_set_password_on_other_user`
- `users_perm_can_disable_other_user`

**Root cause (confirmed by inspection at the time):** these tests create their
login user with `is_active=True` but omit `is_email_verified=True` before
`opts.client.login(...)`, and login requires a verified email. Adjacent passing
tests in the same file (e.g. `users_perm_can_change_email_on_other_user`,
~line 287) do set it.

**Fix:** set `is_email_verified = True` on the created user before `save()` in
each affected test setup, matching the passing siblings.

**Caveat:** the diagnosis is from a 2026-06-07 run; the file may have moved on
since. Verify the omission is still present before fixing — if the tests now
pass, close as already-fixed.

## Repro — bugs only

1. Dev server on :9009 against a long-lived DB.
2. Run the `test_account.test_user_actions` module serially.
- Expected: all pass.
- Actual (2026-06-07): the four tests above fail at login.

## Notes

Downstream tracker: mverify_api
`planning/confirmed/MVERIFY-API-025-test-suite-catalogue-91-failing-tests-framework-wi.md`
(cluster B).

## Resolution
- closed: YYYY-MM-DD
- branch:
- files changed:
- tests added:
