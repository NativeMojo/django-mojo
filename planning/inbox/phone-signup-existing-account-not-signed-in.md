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

**UPDATE (2026-06-29, reproduced):** The *clean* existing-account magic-login **WORKS** — verified
by three passing tests (`configurable_form::test_phone_existing_logs_in`,
`test_phone_existing_joins_new_group`, `test_phone_existing_already_member`). So the bulk of the
reports were **ITEM-005** (one wrong code burned the register session) — now fixed. What remains is
one **confirmed** residual bug: candidate B below (a per-group registration handler that raises
burns the verified token).

## Acceptance Criteria
- [ ] An already-registered phone, after a correct code on the first try, is signed in
      reliably (no "Invalid or expired phone verification") — including under a double-submit /
      network retry.
- [ ] A failing per-group registration handler does not permanently burn the verified-phone
      token (the user can retry).
- [ ] Anti-enumeration and the existing happy path are preserved (see ITEM-006 + user memory
      `auth-no-account-enumeration`).

## Repro
- **Candidate B (handler burns token) — CONFIRMED (2026-06-29).** Existing-account phone joining a
  NEW group whose `USER_REGISTERED_HANDLER` raises. Reproduced with a throwaway test injecting
  `tests.test_register._capture.raising_register` via the `X-Mojo-Test-User-Registered-Handler`
  header (then deleted — not committed):
  - 1st `POST /api/auth/register` (phone + verified_phone_token + group_uuid, raising handler) →
    **500** "test register handler raised"; GroupMember rolled back; user **NOT logged in**.
  - retry with the **same** token → **400** "Invalid or expired phone verification" — the token was
    already burned by the failed attempt → user **stuck** (must restart SMS verification).
  Recipe: create an existing User with a phone + a new `Group`; `phone/register/start` + `/verify`
  to mint a token; POST `/api/auth/register` with the raising-handler header; retry with same token.
- **Candidate A (double-submit) — NOT a backend bug.** If `register()` fires twice, the FIRST call
  consumes the token and logs the user in (200); the SECOND 400s. The user IS logged in from the
  first call — any "not signed in" symptom is a FE promise-handling race, not a server bug. (A FE
  in-flight guard is a nice-to-have, out of this item's backend scope.)

## Investigation
**Confidence: CONFIRMED (reproduced 2026-06-29).** Root cause in
`mojo/apps/account/rest/user.py` → `on_register`, existing-phone fast path (lines 362-408):
- `phone_register.consume(verified_token, norm_phone)` (line 382) consumes the token **before** the
  atomic `GroupMember.get_or_create` + `fire_user_registered` block (lines 397-401).
- `consume` (`phone_register.py:149-168`) is a Redis `getdel` — single-use and **NOT** part of the
  Django transaction. So when `fire_user_registered` raises inside `transaction.atomic()`, the DB
  GroupMember rolls back but the token deletion does not. The exception propagates → `jwt_login`
  (line 408) is never reached → not logged in; and the token is gone → retry 400s.
- The comment at lines 392-393 ("Atomic so a raising handler does not leave a dangling membership")
  shows the DB rollback was intended — but the already-consumed token was missed.
- **Same consume-before-success class as ITEM-005.** Fix mirrors it: validate the token WITHOUT
  deleting (add a non-deleting `phone_register.peek/validate(token, phone)`, or move the delete),
  run the registration/group-join/handler, and `consume` (delete) the token only AFTER success
  (just before `jwt_login`). The verified-token TTL still bounds replay within the window. Re-check
  the OTHER `consume()` caller (the new-registration path) for the same reorder.

**Regression-test feasibility: HIGH.** Endpoint test mirroring the repro: existing user + new group
+ a raising `USER_REGISTERED_HANDLER` → first attempt fails (5xx) but must NOT burn the token; a
retry with the same token (handler no longer raising) returns 200 and signs the user in. Fails on
`main`, passes after the consume-on-success fix.

## Plan

<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes
Cluster: ITEM-005 (wrong-code burns session) and ITEM-006 (sms-login dead-end) are the siblings;
this is the third phone-auth issue from the same report thread. **Repro done (2026-06-29):** the
clean existing-account path works (3 passing tests); candidate B (per-group handler raise burns the
token) is CONFIRMED; candidate A is FE-only. Scope as a backend **consume-on-success** fix (mirrors
ITEM-005). Narrower than first feared — it only bites when a per-group `USER_REGISTERED_HANDLER`
raises, so consider P3.

## Resolution
- closed: YYYY-MM-DD
- branch:
- files changed:
- tests added:
