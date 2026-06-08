---
id: ITEM-003
type: bug
title: Register page — Enter on phone/OTP field fires step-3 submit → false "agree to Terms" error
priority: P2
effort: S
owner: backend
opened: 2026-06-07
depends_on: []
related: []
links: [wmwx/docs/orchestrate/inbox/REQ-030.md]
---

# Register page — Enter on phone/OTP field fires step-3 submit → false "agree to Terms" error

## What & Why
On the hosted register page (the django-mojo "bouncer" stepped/phone-first flow),
a user on **step 1** who types a phone number and presses **Enter** (instead of
clicking **Continue**) gets the error **"Please agree to the Terms & Conditions."**
— a checkbox that lives on the hidden **step 3**. Same on the step-2 OTP field.
It makes registration feel broken and adds acquisition friction for keyboard /
reflexive-Enter users. Clicking Continue / Verify works, so it only bites Enter.

Origin: `losers-revenge` FE QA (2026-06-07), filed upstream as REQ-030. The root
cause was independently verified against the live template (see Plan → Context).

## Acceptance Criteria
- [ ] Pressing Enter on the step-1 phone field starts phone verification (same as
      clicking Continue), not the final submit
- [ ] Pressing Enter on the step-2 OTP field runs the verify action (same as
      clicking Verify), including the existing "account already exists" fast path
- [ ] "Please agree to the Terms & Conditions." can only surface on step 3, when
      the terms checkbox is actually visible and unchecked
- [ ] Continue / Verify / Create-account buttons behave unchanged
- [ ] The non-stepped (single-pane email) register layout is completely unaffected

## Repro — bugs only
1. Configure a group whose register schema is phone-first (SMS verify) so the
   stepped flow renders (`register_step2_active = True`).
2. Load the hosted register page; on step 1 type a phone number.
3. Press **Enter** (do not click Continue).
- Expected: phone verification starts (SMS code sent, advance to step 2).
- Actual: error "Please agree to the Terms & Conditions." appears; the
  phone-verify step never runs.

## Plan

### Goal
On the stepped register flow, pressing **Enter** should do the current step's
action (Continue on step 1, Verify on step 2) instead of firing the step-3 final
submit — which is what surfaces the false "agree to Terms" error today.

### Context — what exists
All three steps share one `<form id="form-register">`
(`mojo/apps/account/templates/account/register.html:19`); steps are shown/hidden
with an `is-active` CSS class, never removed from the DOM.

- Step-1 **Continue** (`register.html:26`) and step-2 **Verify**
  (`register.html:40`) are `type="button"` with working click handlers wired in
  the `if (STEPPED)` block (`register.html:330-333` Continue, `:334-369` Verify).
- Step-3 **Create account** (`register.html:70`) is the form's only
  `type="submit"` button, so it's the **default button**: pressing Enter in any
  field submits the form. `display:none` on the hidden step-3 panel does NOT stop
  this (`display:none` ≠ `disabled`).
- The `submit` handler (`register.html:415`) runs the Terms check first
  (`register.html:418`) → the misleading error. Nothing intercepts Enter earlier.
- `showStep(n)` (`register.html:260`) switches steps but does **not** record which
  step is active.

### Changes — what to do
Two small edits, both in `register.html`'s `{% block page_script %}`:

1. **Track the active step.** Add `var currentStep = 1;` near the state-machine
   vars (`register.html:257-258`), and set `currentStep = n;` as the first line of
   `showStep(n)` (`register.html:261`).

2. **On Enter, click the current step's button.** At the top of the `submit`
   handler, right after `e.preventDefault();` (`register.html:416`), before the
   Terms check:
   ```js
   if (STEPPED && currentStep === 1) { $("btn-reg-continue").click(); return; }
   if (STEPPED && currentStep === 2) { $("btn-reg-verify").click(); return; }
   ```
   Step 3 (`currentStep === 3`) and the single-pane form (`STEPPED === false`)
   fall through to the unchanged Terms-check + `MojoAuth.register(...)`.

### Design decisions
- **Click the existing buttons instead of calling the verify logic directly.**
  Continue and Verify already have working click handlers; reusing them means
  Enter literally does what the button does — no code moved, no duplication, the
  `account_exists` fast path stays exactly where it is. (KISS; chosen over
  extracting a `doVerifyCode()` function, which was heavier with no benefit.)
- **Guard on `STEPPED &&`** so the single-pane email form is provably untouched.

### Edge cases & risks
- **Empty phone / empty OTP on Enter** → the Continue/Verify click handlers
  already show their own "Enter your phone number." / "Enter the 6-digit code."
  messages. Same as clicking. Correct.
- **Single-pane flow** (`register_step2_active` false): `STEPPED` is false, guards
  skip, button ids aren't referenced → identical behavior. No regression.
- **Step 3 Enter** → `currentStep === 3`, guards fall through → Terms check runs,
  now correct because the Terms box IS visible on step 3.

### Tests
Add to `tests/test_auth/bouncer_forms.py` (the established pattern: render the
template, assert on the rendered JS — the repo has no JS/browser runtime). Render
the stepped flow via `_render_with_test_register_fields('account/register.html',
PHONE_ONLY_FIELDS_JSON, group=opts.group)`:

- `test_register_enter_runs_current_step_not_submit` — assert the HTML contains
  both `if (STEPPED && currentStep === 1) { $("btn-reg-continue").click(); return; }`
  and the step-2 `$("btn-reg-verify").click()` guard, AND that
  `html.find('currentStep === 1')` < `html.find('Please agree to the Terms & Conditions.')`
  (the step dispatch must come before the Terms gate). Fails on current `main`,
  passes after the fix.
- Regression guard: assert the stepped HTML still contains
  `"Please agree to the Terms & Conditions."` and `MojoAuth.register(` — the
  step-3 final path must remain.

Every assert carries a descriptive failure message (testing rule).

### Docs
- None (behavior-only JS fix; no API/model/permission/config change). Add a
  `CHANGELOG.md` line per the delivery checklist.

### Open questions
- none

## Notes
Upstream source: `wmwx/docs/orchestrate/.../REQ-030.md`. Root cause re-verified
against the live template; REQ-030's line refs and mechanism check out. REQ-030's
suggested fix referenced a `currentStep` variable that doesn't exist yet — change
(1) adds it.

Baseline (build-baseline rule) — `bin/run_tests --agent --full` BEFORE first edit:
total **2571**, passed **2524**, failed **8**, skipped **39**. Baseline is RED;
user explicitly authorized proceeding ("begin 003 now"). All 8 failures are
pre-existing and in unrelated domains (none in `test_auth`/register):
- `test_incident`: check_by_category priority; LLM full-agent-loop (ruleset
  disabled); handler_map_has_all_types; ticket_is_llm_ticket_detection
- `test_security`: pii_anonymize; public_endpoints_security;
  route_security_comprehensive; generate_security_report
The `test_security` reds are already tracked by inbox item
`test-security-full-suite-red`. Acceptance bar for this build: end state shows the
SAME 8 and nothing new (esp. nothing in `test_auth`).

Post-build full suite (`--full`): total 2573, passed 2526, failed 8 (identical
pre-existing set — 4 `test_incident`, 4 `test_security`), skipped 39. No new
failures; both new tests pass; all `test_auth` green. docs-updater: no doc change
needed. security-review: clean (Enter routes within `preventDefault`, never
submits; phone token still server-issued/validated; Terms gate unreachable from
steps 1/2; no XSS/auth surface).

## Resolution
- closed: 2026-06-07
- branch: main
- files changed: CHANGELOG.md,mojo/apps/account/templates/account/register.html,tests/test_auth/bouncer_forms.py (commit a657dfb; the close.sh auto-stamp diffed origin/main and also listed the unpushed ITEM-002 commit's files — trimmed to this item's actual change)
- tests added: tests/test_auth/bouncer_forms.py — test_register_enter_runs_current_step_not_submit (regression: Enter on step 1/2 routes to Continue/Verify, dispatch precedes the Terms gate); test_register_step3_final_path_intact (step-3 Terms + MojoAuth.register path preserved)
