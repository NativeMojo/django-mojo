---
# id is assigned by /scope on pickup — leave it blank
id: DM-008
type: bug
title: Phone signup may fail to sign in an existing account on a correct code (verified-token single-use)
priority: P2
effort: M
owner: backend
opened: 2026-06-28
depends_on: []
related: [DM-005, DM-006, DM-007]   # phone-auth cluster; DM-005 = same consume-before-success class
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
reports were **DM-005** (one wrong code burned the register session) — now fixed. What remains is
one **confirmed** residual bug: candidate B below (a per-group registration handler that raises
burns the verified token).

## Acceptance Criteria
- [ ] An already-registered phone, after a correct code on the first try, is signed in
      reliably (no "Invalid or expired phone verification") — including under a double-submit /
      network retry.
- [ ] A failing per-group registration handler does not permanently burn the verified-phone
      token (the user can retry).
- [ ] Anti-enumeration and the existing happy path are preserved (see DM-006 + user memory
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
- **Same consume-before-success class as DM-005.** See `## Plan` for the chosen fix —
  **restore-on-failure**: keep `consume` first (preserves single-use + the dup-user / no-double-fire
  guards) and re-mint the token if the registration work fails, so the user can retry. Both
  `consume()` callers (existing-account `user.py:382`, new-user `user.py:453`) are fixed. (A
  validate-then-delete reorder was considered and rejected — it would double-fire the handler on a
  double-submit and break the new-user dup guard.)

**Regression-test feasibility: HIGH.** Endpoint test mirroring the repro: existing user + new group
+ a raising `USER_REGISTERED_HANDLER` → first attempt fails (5xx) but must NOT burn the token; a
retry with the same token (handler no longer raising) returns 200 and signs the user in. Fails on
`main`, passes after the consume-on-success fix.

## Plan

### Goal
Stop a failed registration from permanently burning the `verified_phone_token`: when the
registration work fails *after* the token was consumed, restore the token so the user can
retry (instead of being left un-logged-in with a dead token). Fix **both** `consume()`
callers in `on_register`.

### Context — what exists
`mojo/apps/account/rest/user.py` → `on_register` consumes the verified-phone token in two
places, each BEFORE the work that can fail (CONFIRMED root cause; repro in `## Repro`):

**(1) Existing-account fast path (lines 362-408).** Verbatim core:
```python
382  if not phone_register.consume(verified_token, norm_phone):
383      raise merrors.ValueException("Invalid or expired phone verification")
384  if not existing.is_active:
385      raise merrors.PermissionDeniedException("Account is disabled", 403, 403)
386  if not existing.is_phone_verified:
387      existing.is_phone_verified = True
388      existing.save(update_fields=["is_phone_verified", "modified"])
394  from mojo.apps.account.models.member import GroupMember
395  if group is not None and not GroupMember.objects.filter(user=existing, group=group).exists():
397      with transaction.atomic():
398          GroupMember.objects.get_or_create(user=existing, group=group)
399          account_extensions.fire_user_registered(user=existing, request=request, group=group, source="sms", extra=extra)
402  existing.report_incident(... "register:existing_account_login")
408  return jwt_login(request, existing, source="sms", is_new_user=False)
```
If `fire_user_registered` (399) raises, the `transaction.atomic()` rolls back the GroupMember
but `consume` (382) already Redis-`getdel`'d the token (not transactional) → `jwt_login` (408)
never runs → not logged in, and the token is gone → retry 400s.

**(2) New-user path (lines 444-540).** Consumes at 453, deliberately BEFORE the atomic block:
```python
449  if "phone" in by_name and by_name["phone"].get("verify") == "sms":
450      verified_token = request.DATA.get("verified_phone_token", "")
453      if not phone_register.consume(verified_token, phone):
454          raise merrors.ValueException("Invalid or expired phone verification")
455      phone_was_verified = True
461  with transaction.atomic():
462      user = User(...); ...; user.save()           # create the user
506      if group is not None: GroupMember.objects.get_or_create(user=user, group=group)
510      account_extensions.fire_user_registered(user=user, ..., source="password", extra=extra)
     # --- side effects OUTSIDE atomic ---
520-527  email-verify send (exceptions already CAUGHT/swallowed)
540  return jwt_login(request, user, source="password", is_new_user=True)
```
The comment at 444-447 ("Consumed here, not inside the atomic block, so a misplaced retry
can't roll back a real user row") shows consume-first is **intentional** — it prevents a retry
from creating a DUPLICATE user. But if the atomic block (462-512) itself raises (handler/validator/
DB), no user is created yet the token is burned → candidate-B again.

**The token mechanism** (`mojo/apps/account/services/phone_register.py`): `verify_code` mints the
verified key with `setex(f"{_VERIFIED_PREFIX}{token}", verified_ttl(), json.dumps({"phone": phone}))`
(~lines 152-157); `consume(token, phone)` (149-168) is `getdel` + constant-time phone match.
`user.py` already imports `phone_register`, `transaction`, `account_extensions`, `jwt_login`.

### Changes — what to do
1. **`mojo/apps/account/services/phone_register.py` — add `restore(verified_token, phone)`** (place
   right after `consume`): re-mint the same verified key so a retry can succeed.
   ```python
   def restore(verified_token, phone):
       """Re-mint a verified token that was consumed but whose registration then
       failed, so the caller's retry can succeed without re-verifying the phone.
       Best-effort; no-op on an invalid token/phone. Resets the TTL to verified_ttl()."""
       if not _valid_token_hex(verified_token) or not phone:
           return
       get_connection().setex(
           f"{_VERIFIED_PREFIX}{verified_token}",
           verified_ttl(),
           json.dumps({"phone": phone}),
       )
   ```
2. **`user.py` existing-account path** — wrap the work *from `is_active` through the group-join
   atomic block* (384-401) in `try/except`; on any exception, `restore` then re-raise. Leave
   `report_incident` (402) and `jwt_login` (408) OUTSIDE the try:
   ```python
   if not phone_register.consume(verified_token, norm_phone):
       raise merrors.ValueException("Invalid or expired phone verification")
   try:
       if not existing.is_active:
           raise merrors.PermissionDeniedException("Account is disabled", 403, 403)
       if not existing.is_phone_verified:
           existing.is_phone_verified = True
           existing.save(update_fields=["is_phone_verified", "modified"])
       from mojo.apps.account.models.member import GroupMember
       if group is not None and not GroupMember.objects.filter(user=existing, group=group).exists():
           with transaction.atomic():
               GroupMember.objects.get_or_create(user=existing, group=group)
               account_extensions.fire_user_registered(user=existing, request=request, group=group, source="sms", extra=extra)
   except Exception:
       # Registration failed after the single-use token was consumed — restore it
       # so the user can retry without re-verifying the phone.
       phone_register.restore(verified_token, norm_phone)
       raise
   existing.report_incident(... unchanged ...)
   return jwt_login(request, existing, source="sms", is_new_user=False)
   ```
3. **`user.py` new-user path** — wrap ONLY the `with transaction.atomic()` block (461-512) in
   `try/except`; restore on failure (guarded by `phone_was_verified`). Post-atomic steps
   (email/jwt) stay outside, so a post-creation failure keeps the token consumed (no duplicate
   user on retry):
   ```python
   try:
       with transaction.atomic():
           ... user creation + GroupMember + fire_user_registered (unchanged) ...
   except Exception:
       if phone_was_verified:
           phone_register.restore(verified_token, phone)
       raise
   # atomic committed → token stays consumed (a retry must not create a duplicate user)
   ... email send / jwt_login (unchanged) ...
   ```
4. **Docs + CHANGELOG** (see Docs).

### Design decisions
- **Restore-on-failure, NOT validate-then-delete-on-success.** Keeping `consume` first preserves
  three properties: single-use, **no double-fire** of `USER_REGISTERED_HANDLER` on a concurrent
  double-submit (the 2nd call's `consume` returns False → 400 before the handler), and the
  new-user **dup-user guard**. Validate-then-delete would re-introduce both candidate A
  (double-fire) and dup users → rejected.
- **Restore boundary = up to and including the handler-firing atomic block.** Failures *before the
  handler completes* are safe to retry → restore. Failures *after* (jwt_login, email) must NOT
  restore: the side-effects/user already exist, and a restored retry would double-fire the handler
  (existing-account) or duplicate the user (new-user). Hence `report_incident`/`jwt_login`/email are
  outside the `try`.
- **`restore` resets TTL to `verified_ttl()`** (full window) rather than the remaining TTL — a tiny
  over-extension, not worth a Redis `TTL` round-trip. The token is still single-use and phone-bound.
- **`phone_was_verified` guards the new-user restore** so email-identity registrations (no token)
  don't call restore with an empty token.

### Edge cases & risks
- **Existing-account, jwt_login fails after a successful group-join** (very rare — local JWT mint):
  token stays consumed, handler already fired, user not logged in; retry 400s. Acceptable — the user
  IS in the group and can sign in via SMS; restoring would double-fire the handler (worse).
- **Concurrent double-submit**: first `consume` wins and fires the handler once; the second 400s.
  Unchanged from today. Not separately unit-tested (concurrency), preserved by construction.
- **`except Exception` is broad** — intcentional: any post-consume failure should restore. It
  re-raises, so the original error/HTTP status is preserved (e.g. the handler 500, is_active 403).
- **Handler partially completed external side-effects before raising** → retry re-runs it; that's the
  handler's own idempotency responsibility, unchanged by this fix.
- **`restore` is best-effort** (no-op on bad token) — never masks a real error.

### Tests
testit; home `tests/test_register/configurable_form.py` (has `_start_and_verify_phone`,
`_register_headers`, `_fresh_phone`, and the existing-account + handler tests). Reuse the raising
handler `tests.test_register._capture.raising_register` via header
`X-Mojo-Test-User-Registered-Handler` (see `test_phone_existing_joins_new_group`, lines 241-282).
Add:
1. **Existing account — a raising handler must NOT burn the token.** Existing user + new Group;
   start+verify → token. POST `/api/auth/register` with `{phone, verified_phone_token, group_uuid}`
   + the raising handler → assert 5xx and user NOT a member. Then **retry the same token** WITHOUT
   the raising handler → assert **200**, `access_token` issued, signs into the existing account,
   GroupMember now exists. (Fails on `main`: retry 400 "Invalid or expired phone verification".)
2. **New user — a raising handler must NOT burn the token.** Fresh phone (no account) + new Group;
   start+verify → token. POST register with the raising handler → assert 5xx and NO user created
   (`User.objects.filter(phone_number=...)` empty). Then **retry the same token** without the raising
   handler → assert **200**, user created + signed in. (Fails on `main`.)
3. **Regression guards (must still pass, unchanged):** `test_phone_existing_logs_in`,
   `test_phone_existing_joins_new_group`, `test_phone_existing_already_member`, and the new-user
   phone-register happy paths.

### Docs
- `docs/django_developer/` — wherever `phone_register` / the register verified-token flow is
  documented: note the verified-phone token is restored (retryable) if registration fails after
  consumption; single-use on success is unchanged.
- `CHANGELOG.md` — "account: a failed phone registration (e.g. a per-group registration handler that
  raises) no longer burns the verified-phone token — the user can retry without re-verifying. Applies
  to both the existing-account login and new-user registration paths."
- `docs/web_developer/` — no contract change (a failed register still returns 4xx/5xx); optional one
  line that the `verified_phone_token` remains valid for retry after a failed `/api/auth/register`.

### Open questions
None blocking. Priority is filed P2 but the trigger is narrow (requires a per-group
`USER_REGISTERED_HANDLER`/validator to actually raise) — consider P3 if the deployment's handlers are
reliable. Candidate A (FE double-submit) is intentionally out of scope (not a backend bug).

## Notes
**Build baseline (2026-06-29, `bin/run_tests --agent`):** `status: passed` — total 2259, passed
2203, **failed 0**, skipped 56. GREEN (reliably, post-DM-007). Every post-change failure is mine.

Cluster: DM-005 (wrong-code burns session) and DM-006 (sms-login dead-end) are the siblings;
this is the third phone-auth issue from the same report thread. **Repro done (2026-06-29):** the
clean existing-account path works (3 passing tests); candidate B (per-group handler raise burns the
token) is CONFIRMED; candidate A is FE-only. Scope as a backend **consume-on-success** fix (mirrors
DM-005). Narrower than first feared — it only bites when a per-group `USER_REGISTERED_HANDLER`
raises, so consider P3.

## Resolution
- closed: 2026-06-29
- branch: main
- files changed: mojo/apps/account/services/phone_register.py, mojo/apps/account/rest/user.py, tests/test_register/configurable_form.py, docs/web_developer/account/authentication.md, docs/django_developer/account/auth.md, CHANGELOG.md   (close.sh stamp trimmed of intervening unrelated commits)
- tests added: `tests/test_register/configurable_form.py` — `test_existing_account_handler_raise_keeps_token` + `test_new_user_handler_raise_keeps_token` (a raising USER_REGISTERED_HANDLER fails the first attempt; the SAME token works on retry).
- verification: full suite green — 2205 passed, 0 failed. security review: passed (restore only in `except`→`raise`; no token abuse / double-fire / dup user; input-safe; new-user atomic block is a pure re-indent). docs: web `authentication.md` + django `auth.md` updated.
