---
# id is assigned by /scope on pickup — leave it blank
id: DM-006
type: bug
title: SMS sign-in with an unrecognized number dead-ends — no code sent, no route to sign-up
priority: P2
effort: S
owner: frontend
opened: 2026-06-21
depends_on: []
related: []          # sibling from same report: phone-register-wrong-code-blocks-session
links: []
---

# SMS sign-in with an unrecognized number dead-ends — no code sent, no route to sign-up

## What & Why
On the hosted sign-in page, a person who enters a phone number that has **no
account** is shown "Enter the 6-digit code we sent to …" — but no code is ever
sent, and nothing routes them to sign-up. They wait on a code-entry screen
forever and conclude the product is broken. This silently loses prospective users
who reach for "Sign in" before "Sign up". We should detect the unrecognized
number and send the person down the sign-up path instead of dead-ending.

Origin: relayed FE bug report (signing up via MojoVerify-hosted auth pages),
2026-06-21. Root cause verified against the live framework code (see
Investigation).

## Acceptance Criteria
- [ ] Entering an unrecognized phone number on SMS sign-in leads to a path that
      lets the user create an account — no silent "code sent" screen waiting on a
      code that never arrives.
- [ ] Behavior is consistent with the existing phone-register flow, which already
      handles both new and existing numbers (always sends; `verify` returns
      `account_exists`; existing accounts are signed straight in).
- [ ] The anti-enumeration posture is an explicit, documented decision (see Open
      questions) — not an accidental side effect.
- [ ] Known-number sign-in is unchanged (code sent, verify, JWT).

## Repro — bugs only
1. On the hosted sign-in page, choose SMS-code (passwordless) sign-in.
2. Enter a phone number that has no MojoVerify account; submit.
- Expected: the user is offered / routed into sign-up (or otherwise told no
  account exists), so they can proceed to create one.
- Actual: the UI reveals the code-entry block ("Enter the 6-digit code we sent
  to …") and waits; no SMS is ever sent; the only escape is the static
  "Create one" link, which the user has no reason to click.

## Investigation
**Root cause — confidence: confirmed.**
`mojo/apps/account/rest/sms.py` → `on_sms_login()` (`sms.py:160`): when
`User.lookup_from_request(...)` returns `None` for an unrecognized number
(`sms.py:170-179`), it returns a generic `{"status": True, "message": "If the
account exists, a code was sent."}` and sends **no** SMS — and gives the frontend
no signal that the number is unknown. The hosted page
`mojo/apps/account/templates/account/login.html:317-321` calls
`MojoAuth.startSmsLogin` and then **unconditionally** reveals the code-entry
block, so the user waits for a code that never comes. The only path to signup is
the static "Don't have an account? Create one" link (`login.html:195`); there is
no automatic routing. The SDK documents the generic-resolve contract at
`mojo/apps/account/static/account/mojo-auth.js:593`.

**The fix lever already exists.** The *register* entry point handles both cases
gracefully: `POST /auth/phone/register/start` (`sms.py:190`) always sends a code
regardless of account existence, `/auth/phone/register/verify` returns
`account_exists` (`sms.py:255`), and the hosted register page auto-signs-in an
existing account (`register.html:348`). So routing an unrecognized sign-in number
into the phone-register flow (or returning an explicit "no account" signal the FE
can act on) is a natural fix.

**Regression-test feasibility: MEDIUM.** The backend behavior is testable via
testit (`tests/test_register/` or a `test_auth` sibling); the template-routing
portion can be asserted against rendered HTML (the repo has no JS/browser runtime
— same approach as DM-003). Exact assertions depend on the approach chosen
below, so they're left to the plan.

## Plan

### Goal
Fix the SMS sign-in dead-end for unrecognized numbers **without revealing whether an
account exists** — keep `on_sms_login`'s generic response identical for every number,
and make the hosted login page honest (a static "you'll only get a code if you already
have an account" disclaimer + a visible sign-up link) so an account-less user has a
clear path forward instead of waiting on a code that never arrives.

> **Product decision (RESOLVED, locked with the user):** preserve strong
> anti-enumeration. We do **NOT** add a "no account found" / `account_exists` signal to
> sign-in. The original report's "detect the unrecognized number and route to sign-up"
> framing is **rejected** — detecting/branching per number is exactly the leak. Explicit
> threat model: a snooping spouse must not be able to learn whether a phone number has an
> account. Existence is only ever revealed *after* the user proves phone ownership by
> entering the texted code. See user memory `auth-no-account-enumeration`.

### Context — what exists
- **Backend — `mojo/apps/account/rest/sms.py`, `on_sms_login` (lines 160-182):** already
  privacy-correct; **stays unchanged**. Unknown number → logs an incident and returns the
  SAME generic body as the known path; only a real account is texted:
  ```python
  170      user = User.lookup_from_request(request, phone_as_username=True)
  171      if not user:
  172          User.class_report_incident("SMS login attempt with unknown account",
  173                                      event_type="sms:login_unknown", level=8, request=request)
  179          return JsonResponse({"status": True, "message": "If the account exists, a code was sent."})
  181      _send_otp(user, request)
  182      return JsonResponse({"status": True, "message": "If the account exists, a code was sent."})
  ```
- **Template — `mojo/apps/account/templates/account/login.html`, SMS view (lines 139-173):**
  - `#sms-help` sub-text (line 146): `Enter your phone number and we'll text you a 6-digit code.` ← the pre-submit copy to harden.
  - `#sms-code-block` hidden div (line 152) holds the code input; "Send Code" button (line 158).
  - Bottom switcher (line 195): `Don't have an account? <a href="{{ register_url|default:'/register' }}">Create one</a>` — present, but users "have no reason to click" it mid-login.
- **Template JS handler (lines 306-342):** on submit calls `MojoAuth.startSmsLogin(phone, { group_uuid: cfg.groupUuid })` (318), then **unconditionally** reveals the code block (321), sets `#sms-help` to `"Enter the 6-digit code we sent to " + phone + "."` (322) and the button to "Verify & Sign In" (324). **Line 322 is the false certainty to fix.**
- **Render path:** `_serve_login()` in `mojo/apps/account/rest/bouncer/views.py` (lines 353-362) renders `login.html` with context from `_auth_context()` (lines 254-350): provides `login_methods` (list incl. `'sms'`), `register_url`, `group_uuid`, branding. The SMS block only renders when `'sms' in login_methods`.
- **SDK — `mojo/apps/account/static/account/mojo-auth.js`, `startSmsLogin` (lines 593-597):** "always resolves" (generic). **No change.**
- **Anti-enumeration is already the documented system posture:** identical generic
  responses in password reset (`user.py:742`), magic login (`user.py:842`), email verify
  (`user.py:877`), documented in `docs/web_developer/account/mfa_sms.md` & `magic_login.md`,
  locked by tests like `tests/test_auth/forgot_password_phone.py::test_forgot_unknown_user_no_enumeration`.

### Changes — what to do
**Template-copy / markup only — no backend or SDK behavior change.**
1. **`login.html` — pre-submit disclaimer (line 146):** change `#sms-help` default sub-text
   to a generic, honest, always-shown line, e.g.
   `Enter your phone number. We'll text a 6-digit code only if it's already linked to an account.`
   (Static — identical for everyone; carries no per-number info, so it leaks nothing.)
2. **`login.html` — honest post-submit message (JS line 322):** replace the false certainty,
   e.g. `"If " + phone + " has an account, we just texted a code — enter it below. No code? You may not have an account yet."`
3. **`login.html` — visible in-flow sign-up affordance:** surface a "Don't have an account?
   **Create one**" link **inside** the SMS view (near the code block / help text), not only in
   the bottom switcher, using the same `register_url`. Forward tenant context — include
   `group_uuid` (mirror how `register.html` forwards `cfg.groupUuid`) so sign-up lands in the
   right group. Verify whether `register_url` already encodes the group; if not, append
   `?group_uuid=…`.
4. **Docs + CHANGELOG** (see Docs).

> The code box still appears for everyone — the accepted privacy trade-off (hiding it for
> account-less numbers would itself reveal existence). The fix is honest copy + a visible path
> to sign-up, NOT a behavioral branch.

### Design decisions
- **Preserve anti-enumeration; reject "detect & route".** Any per-number branch (different
  message / auto-redirect for unknown numbers) is the leak. Locked with the user (spouse threat).
  Rejected alternatives: returning `account_exists`/"no account found" on sign-in; unifying
  sign-in into `register/start` (changes the known-number path, risks AC4, larger surface).
- **Copy/affordance fix only, server untouched** — smallest change that resolves the dead-end;
  keeps known-number sign-in byte-for-byte identical (AC4).
- **Forward `group_uuid` to the sign-up link** so multi-tenant routing + the register bouncer
  token resolve correctly.

### Edge cases & risks
- **Account-less user still sees a code box** — accepted; mitigated by the honest sub-text +
  visible "Create one". (AC1 satisfied via a clear path, not auto-routing.)
- **Known-number sign-in unchanged** — we touch only copy/markup, not `_send_otp`/verify/JWT (AC4).
- **Tenant routing** — if `register_url` lacks group context, the sign-up link must carry
  `group_uuid`, else a multi-tenant user lands on the wrong/blank sign-up.
- **Disclaimer must be static** — never rendered conditionally on existence (no such data exists
  at render time anyway); a reviewer should confirm no per-number logic crept in.

### Tests
Rendered-HTML assertions (no JS runtime), mirroring `tests/test_auth/bouncer_forms.py` — its
`_render(template, group)` helper (lines 41-53) builds context via `_auth_context()` and returns
`response.content.decode()`; a `@th.django_unit_setup()` creates the test `Group`. The SMS block
only renders when `login_methods` includes `'sms'` — enable it for the test via the group's
auth_config or the `X-Mojo-Test-Auth-Config` header that `_auth_context` honors (bouncer/views.py:267).
New tests in `tests/test_auth/` (e.g. `sms_login_copy.py`, or extend `bouncer_forms.py`):
- **Disclaimer present:** login.html (sms enabled) renders the static "only if it's already linked
  to an account" copy in the SMS view.
- **In-flow sign-up link:** a `register_url` anchor / "Create one" appears within the SMS view and
  carries `group_uuid` (mirror `bouncer_forms.py`'s group_uuid assertion).
- **Anti-enumeration regression (backend):** `POST /api/auth/sms/login` returns the **identical**
  generic JSON for a known vs. an unknown number (no `account_exists`/leak added) — locks AC3 and
  guards against a future "helpful" regression. (Create a known user like
  `phone_endpoints.py::test_start_accepts_existing_phone`.)
- Assert the corrected feature is **present** (honest copy + link), not that old text is absent
  (per testing rules).

### Docs
- `docs/web_developer/account/mfa_sms.md` (+ `authentication.md` if it covers SMS sign-in):
  document the sign-in UX explicitly — generic response is intentional anti-enumeration; the page
  tells users a code only arrives if an account exists and points account-less users to sign-up.
  (Satisfies AC3.)
- `CHANGELOG.md`: "Hosted SMS sign-in: honest messaging — a code is only sent if the number has an
  account — with a visible sign-up link; preserves anti-enumeration (no account-existence signal)."

### Open questions
- Exact copy wording is proposed above — fine to refine during build; none of it is blocking.
- **Out of scope (tracked separately):** the "signup didn't log me in" reports are NOT this item.
  Most likely DM-005 (one wrong code burns the register session). A code review also surfaced a
  *possible distinct* signup bug (verified-phone token is single-use and consumed *before* the
  group-membership transaction, so a double-submit or a failing per-group `USER_REGISTERED_HANDLER`
  burns the token and blocks the existing-user login: `user.py` existing-account path ~lines 362-408
  + `phone_register.consume` `phone_register.py:149`). Captured as a separate inbox draft —
  `phone-signup-existing-account-not-signed-in` — do NOT fold into DM-006.

## Notes
**Build baseline (2026-06-28, `bin/run_tests --agent`):** `status: passed` — total
2252, passed 2196, **failed 0**, skipped 56 (post-DM-005 HEAD). GREEN → every
post-change failure is mine. (Opt-in `test_incident`/`test_security` excluded as
before — out of scope.) `register_url` already carries group context
(`views.py:335`); default `login_methods` includes `sms` (`auth_config.py:79`), so
`_render('account/login.html')` renders the SMS view. Backend anti-enumeration is
already guarded by `tests/test_phone/sms.py:133-135` → change stays TEMPLATE-ONLY.

Sibling bug from the same report: `phone-register-wrong-code-blocks-session`
(one wrong SMS code permanently blocks the signup verify session). Independent fix.

## Resolution
- closed: 2026-06-29
- branch: main
- files changed: mojo/apps/account/templates/account/login.html, tests/test_auth/bouncer_forms.py, docs/web_developer/account/auth_pages.md, docs/django_developer/account/auth_pages.md, docs/web_developer/account/mfa_sms.md, CHANGELOG.md   (close.sh stamp trimmed of the planning-meta files it also listed)
- tests added: `tests/test_auth/bouncer_forms.py` — `test_login_sms_discloses_code_only_if_account`, `test_login_sms_offers_signup_link`, `test_login_sms_post_submit_message_is_honest` (rendered-HTML; all pass deterministically)
- security review: passed (no enumeration leak; `register_url` autoescaped; JS uses `textContent`). docs: web auth_pages + django auth_pages + mfa_sms updated.
- note: full-suite flakiness (content_guard false-positive + uncleaned phone user) is PRE-EXISTING and unrelated — DM-006's own tests are green. Filed `planning/inbox/flaky-full-suite-content-guard-and-phone-cleanup.md`.
