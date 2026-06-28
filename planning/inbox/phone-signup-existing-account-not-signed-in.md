---
# id is assigned by /scope on pickup — leave it blank
id:
type: bug
title: Phone signup may fail to sign in an existing account on a correct code (verified-token single-use)
priority: P2
effort:
owner:
opened: 2026-06-28
depends_on: []
related: [ITEM-005, ITEM-006]   # same phone-auth subsystem / same report cluster
links: []
---

# Phone signup may fail to sign in an existing account on a correct code (verified-token single-use)

## What & Why
Users report that the phone-first **sign-up** flow sometimes does not log them in after
they enter the correct SMS code, when the phone already has an account. Expected: the
"magic login" — an already-registered phone, once its code is verified, is signed straight
in (profile step skipped). Reports are **unconfirmed / not yet reproduced**; captured from a
relayed report (2026-06-28) plus a code review.

**Most likely this is ITEM-005** (one wrong code permanently burns the register session, so
the subsequent correct code fails). Fix ITEM-005 first and re-check whether any reports
remain. This item covers the *residual, distinct* failure modes below that can occur on a
**clean, correct-first-try** attempt.

## Acceptance Criteria
- [ ] An already-registered phone, after a correct code on the first try, is signed in
      reliably (no "Invalid or expired phone verification") — including under a double-submit /
      network retry.
- [ ] A failing per-group registration handler does not permanently burn the verified-phone
      token (the user can retry).
- [ ] Anti-enumeration and the existing happy path are preserved (see ITEM-006 + user memory
      `auth-no-account-enumeration`).

## Repro — bugs only (UNCONFIRMED hypotheses)
- **Candidate A (double-submit):** existing-account phone → correct code → the verify handler
  calls `MojoAuth.register()`; if it fires twice (double-click / retry), the second call
  re-consumes the single-use `verified_phone_token` and 400s "Invalid or expired phone
  verification" → not signed in.
- **Candidate B (handler burns token):** existing-account phone joining a new group → the token
  is consumed *before* the atomic GroupMember / `USER_REGISTERED_HANDLER` block; if the handler
  raises, the membership rolls back but the token is already gone → retry fails.

## Investigation
**Confidence: code-plausible, unconfirmed (~60%).** From a read of current `main`:
- Existing-account fast path: `mojo/apps/account/rest/user.py` → `on_register`, existing-phone
  branch (~lines 362-408). It consumes the verified token via
  `phone_register.consume(verified_token, norm_phone)` (~line 382) **before** the atomic
  `GroupMember.get_or_create` + `fire_user_registered` block (~lines 395-401), then
  `jwt_login(...)` (~line 408).
- `phone_register.consume` (`mojo/apps/account/services/phone_register.py:149-168`) is `getdel`
  — single-use; a second call returns False → "Invalid or expired phone verification".
- FE: `mojo/apps/account/templates/account/register.html` step-2 verify handler (~lines 343-359)
  calls `MojoAuth.register(p)` with no apparent double-submit guard.
- Two fix levers to weigh in /scope: (1) guard the verify→register call against double-submit
  (disable button / in-flight flag); (2) reorder so the token is consumed only once
  registration/login is known to succeed, or make the existing-account login idempotent for a
  still-valid token within its TTL.

**Regression-test feasibility: MEDIUM** — endpoint test that double-submits `register` with the
same `verified_phone_token` for an existing account and asserts the user is still signed in; and
a test that a raising registration handler does not strand the token.

## Plan

<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes
Cluster: ITEM-005 (wrong-code burns session) and ITEM-006 (sms-login dead-end) are the siblings;
this is the third phone-auth issue from the same report thread. **Confirm-repro before building**
— if ITEM-005's fix clears the reports, this may reduce to just the double-submit guard.

## Resolution
- closed: YYYY-MM-DD
- branch:
- files changed:
- tests added:
