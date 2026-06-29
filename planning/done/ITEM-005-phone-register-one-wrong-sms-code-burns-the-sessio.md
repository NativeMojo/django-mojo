---
# id is assigned by /scope on pickup — leave it blank
id: ITEM-005
type: bug
title: Phone register — one wrong SMS code burns the session; correct code then always fails
priority: P1
effort: S
owner: backend
opened: 2026-06-21
depends_on: []
related: []          # sibling from same report: sms-login-unknown-number-dead-ends
links: []
---

# Phone register — one wrong SMS code burns the session; correct code then always fails

## What & Why
On the phone-first hosted signup (the bouncer stepped flow), entering the SMS
code **wrong even once** permanently blocks that verification session. The user
cannot recover by re-typing the correct code — every subsequent attempt on the
same session returns **"Invalid or expired verification session."** The only way
forward is to restart verification (Back → Continue, or wait out the Resend
cooldown) to mint a fresh session. A single mistyped digit dead-ends the core
signup step, so this is a high-impact acquisition blocker.

Origin: relayed FE bug report (signing up via MojoVerify-hosted auth pages),
2026-06-21. Root cause independently verified against the live framework code
(see Investigation) and against the installed copy wmx-api actually runs.

## Acceptance Criteria
- [ ] A wrong code attempt does **not** consume/invalidate the verification
      session — the user can immediately re-enter the correct code on the **same**
      `session_token` and succeed (within the session TTL).
- [ ] The session is consumed only on **successful** verification (and/or after a
      bounded max-attempts count or TTL expiry).
- [ ] Brute-force protection is preserved — the existing per-IP rate limit
      (`phone_register_verify`, 10/60s) plus, if desired, a bounded attempt
      counter; security is not regressed by the change.
- [ ] The "account already exists → sign straight in" fast path is unaffected.

## Repro — bugs only
1. Configure a group whose register schema is phone-first (SMS verify) so the
   stepped flow renders.
2. Load the hosted register page, enter a phone, Continue → an SMS code is sent
   and a `session_token` is issued (`POST /api/auth/phone/register/start`).
3. On step 2, enter a **wrong** 6-digit code, Verify.
4. Now enter the **correct** code and Verify again (same screen, same session).
- Expected: the correct code verifies and registration proceeds.
- Actual: step 3 returns "Invalid code"; step 4 returns "Invalid or expired
  verification session" — and every retry on this session fails. Only restarting
  verification (new `session_token`) works.

## Investigation
**Root cause — confidence: confirmed.**
`mojo/apps/account/services/phone_register.py` → `verify_code()`
(`phone_register.py:104`) calls `get_connection().getdel(...)`
(`phone_register.py:119`) — an atomic GET-**and-DELETE** of the Redis session —
**before** the submitted code is ever compared (`phone_register.py:134`). The
docstring states it outright: *"the session is deleted whether the code matches
or not"* (`phone_register.py:108-110`). So the first verify call consumes the
session unconditionally; after a wrong code there is no session left, and the
next (correct) submission on the same `session_token` hits a missing key and
raises "Invalid or expired verification session" (`phone_register.py:121`).

**Code path.**
- Endpoint `POST /api/auth/phone/register/verify` → `on_phone_register_verify`
  (`mojo/apps/account/rest/sms.py:229`) → `phone_register.verify_code`.
- Hosted page `mojo/apps/account/templates/account/register.html:344-369` shows
  the error but reuses the now-dead `session_token` (no refresh on failure).
  **Resend** (`register.html:384-396`) *does* mint a fresh session via
  `startPhoneRegister` (`register.html:316-322`), but the button starts disabled
  and sits behind a cooldown (`register.html:46`, `:290`), so it is not the
  obvious recovery; **Back** clears the token to restart (`register.html:372-382`).

**Why it's a defect, not necessary hardening.**
- Brute force is already bounded by the per-IP rate limit on verify
  (`sms.py:231`, `phone_register_verify`, ip_limit=10/60s) — so deleting the
  session on a *wrong* code buys no real protection while breaking the happy path.
- The codebase's other OTP flows are retry-safe and clear the secret only on
  success: `_verify_otp` (`sms.py:60`, cleared via `_clear_otp` at `sms.py:141`)
  and `verify_phone_verify_code` (`mojo/apps/account/utils/tokens.py:255`). Only
  the pre-registration Redis flow burns on any attempt — consistent with the
  report being specific to *signing up*.

**Regression-test feasibility: HIGH (backend).** In `tests/test_register/`
(`phone_verify.py` / `phone_endpoints.py` are the established homes): start →
verify with a wrong code (expect 4xx) → verify with the correct code on the
**same** `session_token` → expect success. Fails on `main` (second call 4xx),
passes after the fix. The dev-bypass header / Redis code-read pattern for
obtaining the real code is already used in the existing phone-register tests.

## Plan

### Goal
Make `phone_register.verify_code()` consume the Redis verification session **only on a
successful code match**, so a wrong code no longer burns the session and the user can
re-submit the correct code on the same `session_token` within its TTL.

### Context — what exists
All paths below are **verified against current `main`** (this working copy).

**The bug — `mojo/apps/account/services/phone_register.py`** (`verify_code`, lines 104-146):
```python
104  def verify_code(session_token, code, request=None):
105      """Atomically consume the session and mint a verified token.
...
108      on missing session, expired session, or mismatched code. Single-use:
109      the session is deleted whether the code matches or not (rate-limit at
110      the endpoint layer prevents brute force).
...
115      if not _valid_token_hex(session_token):
116          raise merrors.ValueException("Invalid or expired verification session")
117      if not code:
118          raise merrors.ValueException("Invalid code")
119      raw = get_connection().getdel(f"{_SESSION_PREFIX}{session_token}")   # ← BUG: GET+DELETE before compare
120      if not raw:
121          raise merrors.ValueException("Invalid or expired verification session")
122      try:
123          data = json.loads(raw)
124      except (TypeError, ValueError):
125          raise merrors.ValueException("Invalid or expired verification session")
126      stored_code = data.get("code")
127      submitted = str(code).strip()
128      bypass = _dev_bypass_code(request=request)
129      real_match = stored_code and _ct_eq(submitted, stored_code)
130      # Constant-time compare both branches ...
133      bypass_match = bypass is not None and _ct_eq(submitted, str(bypass))
134      if not (real_match or bypass_match):
135          raise merrors.ValueException("Invalid code")          # ← session already gone here
136      phone = data.get("phone")
137      if not phone:
138          raise merrors.ValueException("Invalid or expired verification session")
139      verified_token = uuid.uuid4().hex
140      ttl = verified_ttl()
141      get_connection().setex(f"{_VERIFIED_PREFIX}{verified_token}", ttl, json.dumps({"phone": phone}))
142      return verified_token, phone, ttl
```
- `_SESSION_PREFIX = "phone:register:session:"` (line 37). Session payload (written by `start()`, lines 57-78) is `{"phone","code","ip","ts"}` with TTL `session_ttl()` = `PHONE_REGISTER_SESSION_TTL` default **600s** (line 42). **No attempts counter** in the payload.
- Redis client: `from mojo.helpers.redis import get_connection` (line 33). It returns a raw redis-py client — `.get()`, `.getdel()`, `.delete()`, `.setex()` are all available. There is **no** atomic get-compare-delete helper to reuse; the fix is hand-written `get` + conditional `delete`.
- `_ct_eq` (constant-time compare) and `_dev_bypass_code` (lines 81-101, honors `AUTH_PHONE_VERIFY_DEV_BYPASS_CODE` + the `X-Mojo-Test-Phone-Verify-Bypass-Code` test header) are **left exactly as-is** — the only change is *when* the delete happens.
- Module docstring (lines 13-14) also describes the old behavior: *"verify_code(token, code) ... Atomic getdel of session; on success mints a verified_token."* — update this line too.

**Endpoint — `mojo/apps/account/rest/sms.py`** (`on_phone_register_verify`, line ~229):
`@md.strict_rate_limit("phone_register_verify", ip_limit=10, ip_window=60)` → **10 verify attempts / 60s per IP**. This is the brute-force bound we rely on (no code change here). The endpoint reads `request.DATA`, calls `verify_code`, returns `verified_phone_token`.

**The retry-safe pattern this fix mirrors** (already in the codebase): `_verify_otp` / `_clear_otp` (`sms.py:60` / `:72`) and `verify_phone_verify_code` (`mojo/apps/account/utils/tokens.py:255`) all **read → compare → clear only on success**. `phone_register` is the lone outlier.

**The "existing phone → sign in" fast path is downstream of `verify_code`** — it lives in `on_register` (see `tests/test_register/phone_endpoints.py:43-45`: *"on_register turns it into a login for the proven owner"*). `verify_code` is unchanged in shape, so this fast path is unaffected.

### Changes — what to do
1. **`mojo/apps/account/services/phone_register.py`** — in `verify_code()`:
   - Line 119: `get_connection().getdel(...)` → `get_connection().get(...)` (read, **don't** delete).
   - After the phone is validated (after line 138, before minting at line 139) insert:
     ```python
     # Code verified — consume the session now (single-use ON SUCCESS only).
     # A wrong code above raised without deleting, so the user can retry the
     # correct code on the same session_token until it succeeds or the TTL expires.
     get_connection().delete(f"{_SESSION_PREFIX}{session_token}")
     ```
   - Update the `verify_code` docstring (lines 105-110): the session is consumed **only on a successful match**; a wrong/empty code leaves it intact for retry within the TTL; brute force is bounded by the endpoint rate limit (`phone_register_verify`, 10/60s) and the session TTL.
   - Update the module docstring line 14 (`Atomic getdel of session; ...`) to read e.g. `Reads the session; on a matching code, mints a verified_token and deletes the session (consume-on-success).`
2. **`tests/test_register/phone_verify.py`** — rewrite `test_verify_code_wrong_code` (see Tests).
3. **`tests/test_register/phone_endpoints.py`** — add the endpoint-level wrong-then-correct regression (see Tests).
4. **Docs + CHANGELOG** (see Docs).

### Design decisions
- **Rate-limit only — no per-session attempts counter** (user-approved). The 6-digit code has 1e6 possibilities; verify is capped at 10/60s per IP and the session lives 600s → ≤~100 guesses per session against a fresh random code (~0.01%). Every other OTP flow here (`_verify_otp`, `verify_phone_verify_code`, email OTP/change flows) relies on rate-limit + TTL with **no** counter — KISS + Trust Order #3 (match existing patterns). Acceptance criterion #2's "and/or … TTL expiry" is satisfied by the existing 600s TTL. *Rejected:* adding an attempt counter to the payload — extra Redis round-trips, diverges from the codebase, marginal security gain.
- **Delete only after the phone is validated** (after line 138), not immediately after the code matches. Keeps "session consumed ⇒ we actually minted a verified token from it." A corrupt payload missing `phone` raises without consuming — harmless, the TTL reaps it.
- **`get` + `delete` is intentionally non-atomic** (vs. the old atomic `getdel`). For a *correct* code, two concurrent verifies could both succeed and mint two verified tokens for the **same** phone — benign (both lead to the same registration). For a *wrong* code we now never delete. No security regression.

### Edge cases & risks
- **Wrong then correct, same session** → the fix's whole point: wrong raises `"Invalid code"` without deleting; correct then succeeds. ✓
- **Retry window** is the remaining session TTL (≤600s). After expiry the key is gone and the user must restart — unchanged, acceptable. ✓
- **Dev bypass** (`AUTH_PHONE_VERIFY_DEV_BYPASS_CODE` / test header) still works and a non-matching code is still rejected — compare logic untouched. ✓
- **Brute force** bounded by `phone_register_verify` 10/60s per IP + 600s TTL. ✓ (no regression — the old code's per-attempt delete bought nothing the rate limit didn't already provide.)
- **Malformed token / empty code** (lines 115-118) still raise before any Redis access. ✓
- **"Existing phone → sign in" fast path** in `on_register` is untouched (criterion #4). ✓
- **Concurrent correct verifies** → benign double-mint (see Design decisions). ✓

### Tests
Framework: testit (`from testit import helpers as th`, `@th.django_unit_test(...)`, `def test_x(opts)`). Tests read the real code straight from Redis with `get_connection().get(...)` (the session is shared with the test process) — this is the established pattern in `phone_endpoints.py:114-116`.

1. **Rewrite** `tests/test_register/phone_verify.py::test_verify_code_wrong_code` (lines 53-71) — it currently asserts the **bug** (*"session must be consumed even when the code is wrong"*). New behavior (service-level regression, fails on `main`):
   - rename description to e.g. *"phone_register.verify_code rejects a wrong code but keeps the session for retry"*.
   - `start("+14155550222")` → wrong = `"000000" if code != "000000" else "111111"`.
   - `verify_code(session_token, wrong)` raises `merrors.ValueException` (assert it raises).
   - **assert the session key still exists**: `get_connection().get(f"phone:register:session:{session_token}")` is **not None** ("a wrong code must NOT consume the session").
   - then `verify_code(session_token, code)` (correct) **succeeds**, returns a 32-char `verified_token` and the right phone.
   - assert the session key is **now gone** after the successful verify (consumed on success). Clean up the minted verified key.
2. **Add** `tests/test_register/phone_endpoints.py::test_verify_wrong_then_correct_same_session` — HTTP-level repro mirroring `test_full_phone_register_flow` (lines 94-126):
   - `_clear_register_limits()`; `start` with a fresh phone e.g. `"+14155557006"`; read `code` from Redis.
   - POST `/api/auth/phone/register/verify` with a **wrong** code → assert `status_code in (400, 401, 422)`.
   - POST verify again with the **correct** `code` on the **same** `session_token` → assert `status_code == 200` and `verify.response.data.verified_phone_token` is a 32-char hex. (Fails on `main`: second call is 4xx "Invalid or expired verification session".)
3. **No change needed** (confirmed): `test_verify_code_happy_path` (still consumes on success), `dev_bypass.py::test_wrong_code_rejected_with_header` (asserts only 4xx, not consumption), `test_start_accepts_existing_phone`.

Run: `bin/run_tests --agent -t test_register.phone_verify` and `-t test_register.phone_endpoints`; read `var/test_failures.json`. (Capture the green baseline with `bin/run_tests --agent` before editing, per build-baseline rule.) No model/schema changes, so `bin/create_testproject` is **not** required.

### Docs
- `docs/web_developer/` — if the phone register/verify endpoint is documented, update the behavior note: a wrong code is rejected but the `session_token` stays valid for retry until success or TTL expiry (no longer single-attempt). Builder: grep docs for `phone/register/verify`.
- `docs/django_developer/` — if the `phone_register` service is documented, note consume-on-success semantics. (The in-code docstring updates above are the primary source of truth.)
- `CHANGELOG.md` — add a bug-fix entry: "Phone-register verification: a wrong SMS code no longer invalidates the session; the correct code can be retried on the same `session_token` within the TTL."

### Open questions
None — root cause confirmed against current code, design approved (rate-limit only).

## Notes
**Build baseline (2026-06-28, `bin/run_tests --agent`):** `status: passed` — total
2251, passed 2195, **failed 0**, skipped 56. GREEN → every post-change failure is
mine to fix. (The terminal also shows `test_incident` 243 + `test_security` 82 at
0.0s; these are opt-in/excluded modules NOT in the agent suite or the JSON report —
2576−2251 = 325 = 243+82 — pre-existing and tracked in
`planning/inbox/test-security-full-suite-red.md`. Out of scope.) Target area
`test_register` = 93/93.

Sibling bug from the same report: `sms-login-unknown-number-dead-ends`
(unrecognized number on sign-in dead-ends instead of routing to signup). The two
are in the same phone-auth subsystem but independent fixes.

A likely-minimal fix: replace `getdel` with `get`, compare, and `delete` only on
a successful match — leaving brute-force protection to the existing rate limit
(optionally add a small max-attempts counter on the session payload). /scope owns
the final design.

## Resolution
- closed: 2026-06-28
- branch: main
- files changed: mojo/apps/account/services/phone_register.py, tests/test_register/phone_verify.py, tests/test_register/phone_endpoints.py, docs/web_developer/account/authentication.md, docs/django_developer/account/auth_pages.md, CHANGELOG.md   (the close.sh auto-stamp over-reported intervening, unrelated commits — corrected to ITEM-005's actual files)
- tests added: `test_register/phone_verify.py::test_verify_code_wrong_code` (rewritten — was asserting the bug; now requires the session survive a wrong code and the correct code then succeed); `test_register/phone_endpoints.py::test_verify_wrong_then_correct_same_session` (new HTTP regression)
- security review: passed (no regressions; brute force bounded by rate limit + TTL). docs: web + django tracks updated.
