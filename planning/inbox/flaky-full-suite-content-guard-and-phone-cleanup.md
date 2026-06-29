---
# id is assigned by /scope on pickup — leave it blank
id:
type: bug
title: Full test suite is flaky — content_guard false-positive on random test emails + a phone user left uncleaned
priority: P2
effort:
owner:
opened: 2026-06-29
depends_on: []
related: [ITEM-005, ITEM-006]   # surfaced while building these
links: []
---

# Full test suite is flaky — content_guard false-positive on random test emails + a phone user left uncleaned

## What & Why
`bin/run_tests --agent` (the default suite) is **flaky**: a single, *different* test
fails on most full runs (~1 failure / 2255), while every module passes in isolation.
This breaks the "green baseline" invariant the build process depends on (a green run
is luck, not a guarantee). Two independent root causes were identified during the
ITEM-005/006 builds (2026-06-29); both are pre-existing and unrelated to those items.

## Acceptance Criteria
- [ ] The default suite passes **reliably** across repeated full runs (e.g. 5×) — 0 failures.
- [ ] Cause 1 (content_guard false-positive on random test emails) fixed.
- [ ] Cause 2 (a test leaving a phone user uncleaned) fixed — the polluting test cleans up.
- [ ] No test masks real behavior or asserts a bug; isolation is restored.

## Repro — bugs only
Run `bin/run_tests --agent` several times. Observe ~1 failure per full run, a
*different* test each time, from the two families below. `bin/run_tests --agent -t
test_register` alone passes 94/0 (both flaky tests pass in isolation).

Observed instances:
- `test_register::test_register_handler_raise_rolls_back` → 400 "Invalid display name:
  contains inappropriate content" instead of the expected 5xx.
- `test_register/configurable_form::test_phone_register_verify_account_exists` →
  "account_exists must be False for a phone with no account" (it came back True).

## Investigation
**Cause 1 — content_guard false-positive on random emails (flaky).**
`_fresh_email()` (`tests/test_register/register.py:51`, `tests/test_register/extra_fields.py:26`)
returns `reg_<suffix>_<uuid4().hex[:8]>@register.test`. The register flow derives a
display name from this; `User.validate_name_fields` (`mojo/apps/account/models/user.py:570-587`)
runs `content_guard.check_text(value, surface="name", policy={"text_block_threshold": 50})`,
which **occasionally returns `decision == "block"` on a random hex string**, 400-ing the
registration before the test's intended path runs. Fix options: make test emails/display
names content-guard-safe/deterministic, OR (if content_guard is over-blocking benign
hex) treat that as a real content_guard tuning bug and fix the heuristic/threshold.

**Cause 2 — phone-number test-data pollution (isolation).**
`configurable_form::test_phone_register_verify_account_exists` expects `account_exists`
False for a phone with no account, but a prior test in the full run created a User with
that phone and did not delete it (violates the rule "setup must clean up test data before
creating it"). Find the polluting phone-register/login test using the same number and add
cleanup (delete by phone_number in setup/teardown), or make each test use a unique phone.

Both fail ONLY in the cross-module full suite; both pass when `test_register` runs alone.
Neither is caused by ITEM-005 or ITEM-006.

**Regression-test feasibility:** MEDIUM — loop the full suite N× in CI and require 0
failures; add a unit test that `content_guard.check_text` does not block plain hex/ascii
identifiers; assert the phone test is isolation-safe (no pre-existing user for its number).

## Plan

<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes
Surfaced during ITEM-006 build: two consecutive full-suite runs produced two *different*
single failures, both green in isolation — the signature of flakiness, not a regression.
The content_guard angle may be a real product bug (over-blocking benign strings), not just
test data — worth confirming during scope.

## Resolution
- closed: YYYY-MM-DD
- branch:
- files changed:
- tests added:
