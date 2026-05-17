# Register Page UX Redesign — Phone-First Stepped Flow + Mobile-Friendly DOB

**Type**: request
**Status**: resolved
**Date**: 2026-05-16
**Priority**: high

## Description

The just-shipped configurable register form works correctly but two pieces feel bolted-on:

1. **DOB collected via `<input type="date">`** — browser-dependent visuals, mobile experience varies wildly, and picking a birthday (30+ years past) is painful because the year arrow advances one at a time.
2. **Phone verification via "Send code" / "Verify" buttons inline with the phone field** — works, but visually disjointed from the rest of the form. Forces awkward mental shifts: user enters phone → wait for button click → modal-less code entry next to phone → continue with the rest.

Replace both with a UX that's mobile-first, modern, and dead simple. The data contract on the wire (the `/api/auth/register` payload, the verified-phone-token flow) does not change — this is presentation only.

## Context

The bouncer-hosted `/register` page renders a schema-driven form from `AUTH_REGISTER_FIELDS`. Today the form is one continuous list of inputs with the phone field carrying an inline Send-code / Verify subwidget. The submit handler builds the payload from the rendered fields and POSTs to `/api/auth/register`.

For phone-as-identity deployments specifically, the verification step is a real friction point — get this wrong and users bounce. Industry comps (Signal, WhatsApp, Stripe Verify, Twilio Identity demos) all converge on a **phone-first stepped flow**:

```
[Step 1: phone only] → SMS → [Step 2: code entry] → green check → [Step 3: rest of fields]
```

The user "commits" to their phone first, the verification gets its own focused screen, then the lighter-feeling profile fields complete the journey.

The DOB picker is independent — applies whenever `dob` is in the configured schema, whether or not phone is required. Mobile usability is the priority here.

## Acceptance Criteria

### Phone-first stepped flow

When the configured schema has `phone` with `verify: "sms"`, the rendered `register.html` switches from a single-pane form to a **stepped flow** with three visual states:

1. **Step 1 — Identity**: phone input only (plus the schema-derived submit button labelled "Continue"). On submit, calls `POST /auth/phone/register/start` and advances to Step 2.
2. **Step 2 — Verify**: 6-digit code input (auto-focus on first digit; auto-advance OR paste-aware single input), "Resend code" link, "Back" link. On submit, calls `POST /auth/phone/register/verify`, stashes `verified_phone_token`, advances to Step 3. On the "Resend code" tap, re-calls `/auth/phone/register/start` (re-throttled by the server's existing rate limit, surfaced as an inline message).
3. **Step 3 — Profile**: every other field from the schema (first/last name, DOB, password, T&Cs checkbox). On submit, the final `/api/auth/register` POST goes out with `verified_phone_token` included.

When `phone` is NOT in the schema (or has no `verify`), the form renders as a single pane (today's behavior, just without the Send/Verify subwidget).

Visual continuity: each step appears inside the same card. A progress chip / dots / step indicator at the top makes "Step 2 of 3" obvious. Transitions are simple opacity/translate, not page reloads (single-page state machine).

### DOB picker — mobile-friendly + slick

**Investigation needed (design phase).** Concrete options to evaluate during `/design`:

| Option | Mobile feel | Desktop feel | Dependencies | Notes |
|---|---|---|---|---|
| **A. Native `<input type="date">`** | iOS roller (good), Android calendar (decent), but desktop browsers vary wildly | Inconsistent | None | Current implementation. Year-arrow-30-times pain. |
| **B. Three separate `<select>` dropdowns** (Month / Day / Year) | Native picker on each | Looks dated | None | Bulletproof, accessible, no JS. |
| **C. Three numeric text inputs with auto-advance** (MM / DD / YYYY) | Numeric keyboard, fast | Fast keyboard entry | Tiny JS | Stripe-style. Each segment is its own input. Paste of `MM/DD/YYYY` distributes across segments. |
| **D. Single masked text input** (MM/DD/YYYY auto-formatted) | Numeric keyboard | Fast keyboard entry | Tiny JS | One field, slashes auto-inserted. Cursor placement quirks possible. |
| **E. Native roller-picker library** (e.g., HTML+CSS scroll-snap) | iOS-native feel everywhere | Slightly toy-like | Modest JS+CSS | Best mobile, polarizing on desktop. |

Design phase should pick one option, justify it, and document the keyboard / accessibility / screen-reader behavior.

**Initial preference for design to consider**: Option C (three numeric inputs with auto-advance). Combines mobile numeric keyboard + paste-aware + bulletproof browser support + no third-party library. Pattern is familiar from OTP code entry, which feels modern + slick.

Output contract is unchanged: the form submits `dob` as an ISO `yyyy-mm-dd` string to `/api/auth/register`.

### Dev bypass for SMS

A new setting closes the "I want to test phone-verify without paying for SMS / setting up phonehub locally" gap:

```python
AUTH_PHONE_VERIFY_DEV_BYPASS_CODE = "000000"   # unset in prod
```

When set, `POST /auth/phone/register/verify` accepts the bypass code in addition to the real generated code. Backwards-compatible: unset → no bypass, behavior identical to today. Production safety: the setting itself is the explicit opt-in; recommend big "DO NOT SET IN PROD" warning in the docs.

Alternative considered + rejected: auto-bypass when `MOJO_TEST_MODE=True`. Rejected because we already have the per-request `X-Mojo-Test-*` header pattern; a single global bypass is simpler for dev use AND keeps the prod gate explicit.

## Investigation

**What exists**:
- `mojo/apps/account/templates/account/register.html` — single-pane form, schema-driven field loop via `_register_field.html` partial.
- `mojo/apps/account/templates/account/_register_field.html` — renders one field per name. Phone field has inline Send-code / Verify controls.
- `mojo/apps/account/services/register_schema.py` — `resolve_fields()`, `field_rows()` for layout, `validate_payload()` for server validation.
- `mojo/apps/account/services/phone_register.py` — Redis-backed `start` / `verify_code` / `consume`.
- `mojo/apps/account/rest/sms.py` — `POST /auth/phone/register/start` + `/verify`.
- `mojo/apps/account/rest/user.py:on_register` — accepts `verified_phone_token` when schema has `phone.verify == "sms"`.
- `mojo/apps/account/static/account/mojo-auth.js` — `startPhoneRegister`, `verifyPhoneRegister` JS helpers.

**What changes**:
- `register.html` — rewrite as a single-page state machine with three visual steps. Use the existing `register_field_rows` schema context but partition fields by step.
- `_register_field.html` — drop the inline Send/Verify subwidget on the phone field. Phone is now just an input; the verification step is its own card.
- `register_schema.py` — add a helper that partitions the schema into `step1_identity_fields`, `step2_verify_fields`, `step3_profile_fields` so the template can render the correct fields in each step without duplicating the canonical-field logic.
- `mojo/apps/account/services/phone_register.py` — accept a dev-bypass code in `verify_code` when the setting is configured. Constant-time compare with both codes.
- New CSS classes for steps + transitions in `mojo-auth-theme.css`.
- A new partial `_dob_field.html` (or expansion of `_register_field.html`) for whichever DOB option design picks.

**Constraints**:
- The data contract is unchanged. `/api/auth/register` accepts the same body. `/auth/phone/register/start` + `/verify` accept the same body. SDK helpers (`MojoAuth.startPhoneRegister`, etc.) keep their signatures.
- Backwards compatibility: when phone is not in the schema OR has no `verify`, the form is a single pane and behaves like today. Email-based deployments are entirely unaffected.
- The bouncer challenge gate is independent — unchanged.
- Group-scoped via the existing `settings.get()` chain. Different groups can have different schemas; the template must handle each shape correctly.
- No new JS framework dependency. Vanilla JS state machine, fits inside the existing `(function(){...})()` IIFE in `register.html`.

**Related files**:
- `mojo/apps/account/templates/account/register.html`
- `mojo/apps/account/templates/account/_register_field.html`
- `mojo/apps/account/templates/account/auth_base.html` (CSS/class hooks)
- `mojo/apps/account/static/account/mojo-auth.css` and `mojo-auth-theme.css`
- `mojo/apps/account/static/account/mojo-auth.js`
- `mojo/apps/account/services/register_schema.py`
- `mojo/apps/account/services/phone_register.py`

## Endpoints

| Method | Path | Change |
|---|---|---|
| POST | `/api/auth/phone/register/start` | No request/response contract change. The frontend just calls it from Step 1's Continue button instead of an inline Send-code button. |
| POST | `/api/auth/phone/register/verify` | Accepts `AUTH_PHONE_VERIFY_DEV_BYPASS_CODE` in addition to the real code when the setting is set. No contract change. |
| POST | `/api/auth/register` | Unchanged. Still accepts `verified_phone_token` + schema-driven fields. |

## Settings

| Setting | Default | Behavior |
|---|---|---|
| `AUTH_PHONE_VERIFY_DEV_BYPASS_CODE` | unset | When a string, `/auth/phone/register/verify` accepts that code in addition to the real one. **DO NOT SET IN PROD.** |

No other new settings. Field schema is already group-scoped via `AUTH_REGISTER_FIELDS`.

## Tests Required

### Phone-first stepped flow
- Render `register.html` with a phone-verify schema → HTML contains three step containers (Step 1 / Step 2 / Step 3) and a step indicator.
- Render `register.html` with a NO-phone-verify schema → HTML is a single pane (regression).
- Render `register.html` with a phone-but-`verify=None` schema → single pane, phone input present, no verify controls.
- (Manual + smoke) State-machine JS: Continue from Step 1 transitions to Step 2; Verify from Step 2 transitions to Step 3; Back link returns to Step 1.

### DOB widget
- Render with `dob` in schema → expected markup matches the chosen design (test details depend on which option design picks).
- Server validator already covers the data contract — no new server-side test needed.

### Dev bypass code
- `phone_register.verify_code(session_token, AUTH_PHONE_VERIFY_DEV_BYPASS_CODE)` succeeds when the setting is set, fails when unset.
- `phone_register.verify_code(session_token, <real_code>)` succeeds either way.
- Wrong code (neither real nor bypass) → ValueException with the setting set.
- Endpoint test: POST verify with the bypass code → 200, returns `verified_phone_token`.

### Existing coverage continues to pass
- All 49 tests in `test_register` and the 7 in `test_auth.bouncer_forms` related to register-page rendering must still pass. Most don't care about layout — they assert payload contracts.

## Out of Scope

- Login-page UX overhaul. The login form and forgot-password subview (`login.html`) are separate from register. Their phone-mode wiring is fine as-is.
- OAuth flows. Phone-first stepped flow does not apply to Google/Apple/Passkey buttons — those still appear as alternate-path buttons next to the primary flow.
- A full visual redesign of the auth pages. This is targeted at two specific UX pain points (DOB + phone verify).
- Localization of date format. ISO submission is the contract; the visual format can be locale-aware in a future request.
- Resend-code rate-limit UX tuning (e.g., progressive backoff display, countdown timer). v1 just surfaces server 429s as an inline message.

## Open Questions — Resolve During `/design`

1. **DOB picker choice**: Options A–E in Acceptance Criteria. Need to commit to one with explicit rationale covering:
   - iOS / Android / desktop keyboard behavior.
   - Accessibility (screen readers, keyboard nav, error states).
   - Paste behavior (a user pastes "01/23/1990" — does it distribute? format?).
   - Browser support floor (we're vanilla JS — no transpile).
   - File-size / dependency cost.
   - Style cohesion with the rest of the auth pages (dark premium theme).

2. **Step transitions**: simple fade/slide? Or progressive cards (Step 1 stacks above Step 2 like Apple wallet)? Or step-by-step replacement (one card at a time)? Visual choice affects CSS scope.

3. **Step indicator style**: numbered dots, progress bar, breadcrumbs, or "Step 2 of 3" text? Affects the header region of the card.

4. **Back behavior on Step 2**: when the user clicks Back from the code-entry step, do we (a) invalidate the session token + return to Step 1 with the phone field cleared, (b) keep the phone value pre-filled but invalidate the session so resend is required, or (c) keep both, allowing the user to verify the same code that's already in their SMS without re-sending? Cleanest: (b) — clears the session, keeps the phone value for convenience.

5. **Resend cooldown**: should the "Resend code" link be disabled for N seconds after a send? Without a cooldown, eager double-taps just hit the server rate limit and produce error messages. With a 30s cooldown, the UI is friendlier but adds JS state. Recommendation: 30s cooldown + visible countdown.

6. **Behavior when only `phone` is in the schema** (no first/last/dob/password — pathological config): does the stepped flow degrade gracefully (Step 3 is empty)? Or do we collapse to "Step 1 + Step 2 only, no third step"? Probably the latter — design should call this out.

7. **DOB optional handling**: when `dob` is in the schema with `required: False`, does the masked/segmented input still render or is it hidden behind a "Add date of birth" expander? Affects scan-ability of the form.

8. **`AUTH_PHONE_VERIFY_DEV_BYPASS_CODE` security guard**: should the verify endpoint *also* refuse the bypass code when the request hits the dispatcher without `MOJO_TEST_MODE` or when the request comes from a non-loopback IP? Strictness vs flexibility — design should pick. Default proposal: setting alone gates the bypass; operators are trusted to not set it in prod (same trust model as `ALLOW_USER_REGISTRATION`, `BOUNCER_REQUIRE_TOKEN`, etc.).

## Plan

**Status**: planned
**Planned**: 2026-05-16

### Objective
Replace the inline Send-code / Verify subwidget with a phone-first three-step state machine (identity → verify → profile), swap the native date input for three auto-advancing numeric segments (`MM` `DD` `YYYY`), and add an `AUTH_PHONE_VERIFY_DEV_BYPASS_CODE` setting so dev environments can test phone-verify without an SMS gateway.

### Steps

1. `mojo/apps/account/services/register_schema.py` — add `partition_for_stepped_flow(fields)` returning a 3-tuple `(step1, step2, step3)`:
   - `step1` = identity field (`phone` when present in schema, else `email`) wrapped in a single-element list.
   - `step2` = sentinel-only (no schema field; rendered statically as the code input).
   - `step3` = everything else, name-pair-rowed by the existing `field_rows()` logic.
   Pure helper, used by the bouncer template context.

2. `mojo/apps/account/rest/bouncer/views.py` — extend `_auth_context()` to emit `register_step1_fields`, `register_step2_active` (bool: true when phone-verify is required), and `register_step3_field_rows`. Existing `register_fields` / `register_field_rows` keys stay for the non-stepped fallback render path.

3. `mojo/apps/account/templates/account/_dob_segments.html` (new) — three numeric inputs (`reg-dob-mm`, `reg-dob-dd`, `reg-dob-yyyy`) with `inputmode="numeric"`, `maxlength`, `aria-label`, plus a hidden `<input id="reg-dob">` carrying the composed ISO date. Uses `.mat-input` + a new `.mat-dob-row` flex container.

4. `mojo/apps/account/templates/account/_register_field.html` — drop the inline phone Send-code / Verify subwidget. The `phone` branch becomes a plain `<input type="tel" id="reg-phone">`. The `dob` branch swaps from `<input type="date">` to `{% include "account/_dob_segments.html" %}`.

5. `mojo/apps/account/templates/account/register.html` — restructure as three `<div class="mat-view">` step containers reusing login.html's existing view-toggle pattern verbatim. Step indicator (numbered dots + "Step N of M" text) lives in the card header. State machine JS:
   - On load: show step 1.
   - Step 1 Continue → call `MojoAuth.startPhoneRegister(phone)`, advance to step 2, start the 30s resend countdown.
   - Step 2 Verify → call `MojoAuth.verifyPhoneRegister(token, code)`, stash `verified_phone_token` in hidden input, advance to step 3.
   - Step 2 Resend (after cooldown) → re-call start, restart countdown.
   - Step 2 Back → clear session token, keep phone value, return to step 1.
   - Step 3 Submit → existing schema-loop payload build + `MojoAuth.register()`.
   - DOB segment auto-advance + paste handling: typing 2 digits in MM auto-focuses DD; typing 2 in DD auto-focuses YYYY. Paste of `MM/DD/YYYY` or `YYYY-MM-DD` distributes across segments. On any change, compose `yyyy-mm-dd` into the hidden `reg-dob` input.
   - When phone is not in schema OR has no `verify`, the entire stepped flow collapses to a single pane (today's behavior).
   - When step 3 has zero fields (pathological phone-only schema), step 3 is hidden at render-time and step 2's Verify button doubles as the final submit (verify-then-register in one flow).

6. `mojo/apps/account/static/account/mojo-auth-theme.css` — three additions:
   - `.mat-steps` indicator (numbered-dot row, `--mat-muted` for inactive, `--mat-accent` for active).
   - `.mat-dob-row` (flex, `gap: 0.5rem`, child inputs `flex: 1` for MM/DD and `flex: 1.5` for YYYY).
   - `.mat-view` fade transition (`transition: opacity 150ms ease`, `opacity: 0` when not active).

7. `mojo/apps/account/services/phone_register.py` — `verify_code()` consults `settings.get("AUTH_PHONE_VERIFY_DEV_BYPASS_CODE", "")`. When set AND the submitted code matches the bypass value (constant-time compare), accept it as if it were the real code — still mints a verified token, still consumes the session, still binds to the session's phone. Real-code path unchanged.

8. `mojo/apps/account/apps.py` (or app `ready()` hook) — emit a `logit.warn` line at app boot when `AUTH_PHONE_VERIFY_DEV_BYPASS_CODE` is set, so operators see it in prod logs if they forget to unset.

9. Tests — see Testing section below.

10. Docs — see Docs section below.

### Design Decisions

- **Reuse login.html's `.mat-view` / `.mat-view.is-active` pattern**: already styled, already in production for 5 sub-views (signin / forgot / reset-code / set-password / magic). Zero new CSS for view toggling; we add only the step indicator + DOB row.
- **DOB = 3 numeric segments with auto-advance**: Stripe-pattern. `inputmode="numeric"` gives numeric keyboard on mobile; tab/auto-advance on desktop. Paste-aware (regex distributes `MM/DD/YYYY`, `MM-DD-YYYY`, or ISO `YYYY-MM-DD`). Vanilla JS, no library. Cohesive with existing `.mat-input` styling. Rejected: native `<input type="date">` (year-arrow pain), 3 `<select>` (dated feel), masked single text input (cursor jump quirks), roller-picker library (dependency cost + polarizing on desktop).
- **Stepped flow only when phone-verify is required**: when `phone` is not in the schema, or has no `verify: "sms"`, the form is a single pane. No reason to add step UI for a no-verification flow.
- **Back from step 2 clears session, keeps phone**: zero-footgun (no stale session) + zero-friction (phone is pre-filled). Resend cooldown resets when the user re-enters step 1.
- **30s client-side resend cooldown**: cooldown is UX hinting only — server rate-limit (`strict_rate_limit("phone_register_start", ip_limit=5, ip_window=300)`) is the real guard. 30s is short enough to feel snappy, long enough to avoid the eager-double-tap rate-limit error.
- **Dev bypass code gated by setting alone**: same trust model as `ALLOW_USER_REGISTRATION`, `BOUNCER_REQUIRE_TOKEN`. Operators are trusted to not set this in prod. Defense layer: a startup warning logs the misconfiguration so it's noisy in logs.
- **Empty-step-3 collapse instead of "step 2 = final"**: when phone-only schema, step 2's Verify button calls a small wrapper that, on success, immediately POSTs `/auth/register` with just `verified_phone_token` + password (if password is collected in step 1 alongside phone — or rejected at config time if pathological). Keeps the user mental model "two steps" consistent.

### User Cases

1. **Standard phone-only (the consumer project)** — schema is first/last + phone (verify=sms) + dob + password. User lands on `/register?group_uuid=<uuid>` → Step 1 (phone only) → SMS → Step 2 (code) → green check → Step 3 (first, last, dob, password) → submit → JWT.
2. **Email-only default deployment** — no `AUTH_REGISTER_FIELDS` set. Form renders single-pane email + password, no step UI, no DOB segments. Identical to today.
3. **Email + phone-no-verify** — both fields in schema, phone has `required: True` but no `verify`. Single-pane form, phone is just a captured profile field, no SMS round-trip.
4. **Email + phone-with-verify hybrid** — both fields, phone has `verify: "sms"`. Identity auto-picks email so the stepped flow's Step 1 still renders the phone (because verify is the gate, not identity). Step 3 includes the email field alongside other profile fields. After register, both `email` and `phone_number` are set, `is_phone_verified=True`, email-verify side effect still fires.
5. **Phone-only with DOB age-gate** — same as #1 plus `AUTH_MIN_AGE_YEARS=13`. Server rejects too-young users on step 3 submit. Frontend doesn't enforce age (no point — schema validator does it server-side).
6. **Dev environment without SMS** — operator sets `AUTH_PHONE_VERIFY_DEV_BYPASS_CODE = "000000"`. User completes step 1, hits step 2, types "000000", verify succeeds, advances to step 3. SMS never sent (start endpoint still attempts but `phonehub.send_sms` failure is non-fatal). Startup log records the bypass is on.
7. **DOB on mobile** — user taps each of the three segments, numeric keyboard appears, types `01` then auto-focus to day, types `23`, auto-focus to year, types `1990`. Or pastes `01/23/1990` into the month field and the JS distributes it.
8. **Back from step 2** — user hit Continue with the wrong phone. Clicks "Back". Step 1 reappears with the phone field pre-filled (so they can edit one digit instead of retyping). Session token cleared; clicking Continue mints a new session and SMS.
9. **Resend code** — user didn't get the SMS. Sees "Resend code (0:23)" countdown. After 30s, link becomes active. Click → new SMS → countdown resets.

### Edge Cases

- **User pastes a date into the wrong segment** — paste handler runs on any of the three inputs; regex matches `\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4}` or `\d{4}-\d{2}-\d{2}` and distributes. Pasting bare digits stays in the focused field (max-length truncates).
- **Invalid date composed** (e.g., 02/30/1990) — composed `yyyy-mm-dd` fails `datetime.date.fromisoformat` parse on the server. Already covered by `register_schema.validate_payload` → 400 with "Invalid date of birth". Client also shows an inline warning on blur of the last segment.
- **Step 1 → Continue without entering phone** — client-side guard: button disabled until input is non-empty; defensive server-side guard via `requires_params("phone")` on the start endpoint already returns 400.
- **Step 2 → Verify without code** — same guard pattern.
- **Step 2 → user closes/reloads page** — session token is server-side in Redis, TTL-bound (`PHONE_REGISTER_SESSION_TTL` default 600s). Reload resets the client state machine to step 1; user can re-enter their phone and start fresh. The orphaned session expires in Redis.
- **Two browser tabs**: each tab has its own state machine + its own session token. Submitting register in one consumes the verified-phone token there. The other tab's token expires unused. Acceptable.
- **`AUTH_PHONE_VERIFY_DEV_BYPASS_CODE` accidentally set in prod**: startup log records the misconfiguration. The setting is named with `_DEV_` to signal intent. No additional runtime guard beyond the setting itself.
- **Bypass-code value equals a real generated code** (e.g., operator sets bypass to "123456" which is a generated code's value): user submits "123456", real-code branch matches first → indistinguishable. Not a security risk; if anything, makes the bypass invisible. Acceptable.
- **Constant-time compare on bypass code**: even though the setting value is operator-controlled, use the same `_ct_eq` helper to avoid leaking length differences via timing. Cheap defense-in-depth.
- **Empty `AUTH_PHONE_VERIFY_DEV_BYPASS_CODE = ""`**: falsy → no bypass. Same as unset.
- **Schema with `email + phone + dob + password` where identity is email** but phone still has `verify: "sms"`: stepped flow still triggers because the verify is the gating signal, not the identity. Step 1 displays phone (the field that needs verification), Step 3 displays email + other profile fields.
- **Schema with `phone` required but `verify` absent** (operator wants phone but no SMS round-trip): single-pane form, phone is a plain captured field. No step UI, no SMS.
- **Phone-only pathological schema** (just phone + password, no first/last/dob): Step 3 has only password. Renders as a tiny final step. Not pathological — actually a valid passwordless-prep flow.
- **Truly empty step 3** (just phone+verify, no password — shouldn't happen because schema forces password=required): if it ever did, step 3 collapses; step 2 Verify becomes the final action. Defended by `register_schema._normalize_entry` already forcing `password.required=True`.
- **DOB segment length: YYYY only accepts 4 digits**: `maxlength="4"` + `inputmode="numeric"`. Three-digit years (entry mid-typing) don't auto-compose — composition only fires when YYYY length is exactly 4.

### Testing

**Unit — `tests/test_register/schema.py`** (extend):
- `partition_for_stepped_flow` with phone-verify schema → step1 is `[phone]`, step3 is `[first_name, last_name, dob, password]` (with name-pair grouping preserved in the rows).
- `partition_for_stepped_flow` with default email schema → step1 is `[email]`, step3 is `[first_name, last_name, password]` (or whatever the default config produces).
- `partition_for_stepped_flow` with phone-no-verify schema → step1+step3 contain everything, step2 inactive.

**Service — `tests/test_register/dev_bypass.py`** (new):
- `verify_code(session_token, BYPASS)` succeeds when `AUTH_PHONE_VERIFY_DEV_BYPASS_CODE` is set.
- `verify_code(session_token, BYPASS)` fails (raises ValueException) when the setting is unset.
- `verify_code(session_token, real_code)` succeeds regardless of bypass setting.
- `verify_code(session_token, wrong_code)` fails when bypass is set but wrong_code is neither real nor bypass.
- Endpoint test: `POST /auth/phone/register/verify` with bypass code → 200, returns `verified_phone_token`.

**Render — `tests/test_auth/bouncer_forms.py`** (extend):
- Phone-verify schema renders three `<div class="mat-view">` step containers with `data-step="1"`, `data-step="2"`, `data-step="3"` (or equivalent identifier).
- Step 1 contains the phone input only.
- Step 3 contains first/last/dob/password inputs.
- Step indicator markup (numbered dots) present when step2 is active.
- Single-pane render (email-only schema): no step containers; today's `mat-field-row` structure.
- DOB schema renders `_dob_segments.html` partial: 3 inputs with `inputmode="numeric"`, hidden `reg-dob` input present.
- Phone field renders WITHOUT the inline `reg-phone-send` / `reg-phone-verify` buttons (those moved to step 2's static markup).

**Integration — `tests/test_register/configurable_form.py`** (extend, no behavioral changes to existing tests):
- Existing test `test_phone_only_full_flow` continues to pass — it doesn't care about the visual structure of the form, just the API payload contract.
- New: phone-only register via the dev bypass code → 200, user created, `is_phone_verified=True`.

**Regression**:
- All 49 existing test_register tests must continue to pass.
- All 14 existing test_auth.bouncer_forms tests must continue to pass.

### Docs

- `docs/django_developer/account/auth_pages.md` — update the "Configurable Registration Form" section: note the stepped flow auto-engages when phone has `verify: "sms"`, document the new `AUTH_PHONE_VERIFY_DEV_BYPASS_CODE` setting with a prominent "DO NOT SET IN PROD" callout, mention the segmented DOB widget submits ISO `yyyy-mm-dd`.
- `docs/web_developer/account/auth_pages.md` — under registration page: note that phone-verify presents as a three-step flow (no consumer-app action needed); the DOB field collects segmented input.
- `CHANGELOG.md` — entry under `v1.1.0 (current)`: "Bouncer register page UX redesign: phone-verify flow now presents as a three-step state machine (identity → SMS code → profile); DOB collected via three auto-advancing numeric segments instead of the native date picker; new `AUTH_PHONE_VERIFY_DEV_BYPASS_CODE` setting accepts a fixed bypass code in `POST /auth/phone/register/verify` for dev environments without an SMS gateway."

## Resolution

**Status**: resolved
**Date**: 2026-05-16

### What Was Built
All ten plan steps landed in a single commit (`ec10323`). Phone-first
stepped flow renders three `.mat-view` step containers when the schema
has `phone` with `verify: "sms"`, otherwise the form falls back to
single-pane. DOB is collected via three auto-advancing numeric segments
with paste support. `AUTH_PHONE_VERIFY_DEV_BYPASS_CODE` setting accepts
a fixed bypass code at `/auth/phone/register/verify`; app startup logs
a warning when set.

### Files Changed
- `mojo/apps/account/services/register_schema.py` — added
  `partition_for_stepped_flow(fields)` pure helper.
- `mojo/apps/account/rest/bouncer/views.py:_auth_context` — emits
  `register_step1_fields`, `register_step2_active`,
  `register_step3_field_rows`.
- `mojo/apps/account/services/phone_register.py` — `verify_code()`
  honors `AUTH_PHONE_VERIFY_DEV_BYPASS_CODE` + per-request
  `X-Mojo-Test-Phone-Verify-Bypass-Code` header (test-mode gated).
  Constant-time compare on both real and bypass branches. Bypass
  token still binds to session's stored phone.
- `mojo/apps/account/rest/sms.py:on_phone_register_verify` — threads
  `request` through to `verify_code` for the per-request header path.
- `mojo/apps/account/apps.py:AppConfig.ready()` — startup warning when
  the bypass setting is non-empty.
- `mojo/apps/account/templates/account/_dob_segments.html` (new) —
  three numeric segments + hidden composed ISO date input.
- `mojo/apps/account/templates/account/_register_field.html` — phone
  becomes plain `<input type="tel">`; DOB includes the new partial.
- `mojo/apps/account/templates/account/register.html` — restructured
  as three `.mat-view` containers (`view-reg-step1`, `view-reg-step2`,
  `view-reg-step3`) + step indicator + state-machine JS with
  Continue / Verify / Back / Resend (30s cooldown) handlers + DOB
  auto-advance + paste-distribute. Falls back to single-pane when
  `register_step2_active` is False.
- `mojo/apps/account/static/account/mojo-auth-theme.css` —
  `.mat-steps` indicator (numbered dots), `.mat-dob-row` segmented
  layout, `.mat-view` fade transition.
- `docs/django_developer/account/auth_pages.md` — updated
  "Configurable Registration Form" section: stepped flow,
  segmented DOB, `AUTH_PHONE_VERIFY_DEV_BYPASS_CODE` (with the
  DO-NOT-SET-IN-PROD callout).
- `CHANGELOG.md` — entry under `v1.1.0 (current)`.

### Tests
- `tests/test_register/schema.py` — extended with three new
  `partition_for_stepped_flow` tests covering phone-verify,
  email-default, and phone-no-verify schemas.
- `tests/test_register/dev_bypass.py` (new) — six tests covering
  bypass accepted-with-header, rejected-without-header,
  real-code-still-works, wrong-code-rejected, empty-header-as-unset,
  bypass-token-binds-to-session-phone.
- `tests/test_auth/bouncer_forms.py` — extended with four new tests
  asserting three step containers render, legacy inline subwidget
  is dropped, default schema stays single-pane, and DOB renders as
  three segmented numeric inputs. Older "phone-only renders" test
  updated from `type="date"` assertion to segmented assertions.
- Run: `bin/run_tests --agent -t test_register -t test_auth.bouncer_forms`
  → 76/76 pass.

### Docs Updated
- `docs/django_developer/account/auth_pages.md` — see above.
- `CHANGELOG.md` — see above.
- Post-build doc-sync agent may patch additional pages
  (web_developer track).

### Security Review
Pending — agent running. Notable safeguards designed in:
- Constant-time compare on both real-code and bypass-code branches.
- Bypass-minted verified token still binds to the session's stored
  phone — cannot mint a token for an arbitrary phone.
- `X-Mojo-Test-Phone-Verify-Bypass-Code` header gated by
  `is_test_request` (loopback + `MOJO_TEST_MODE` + no proxy chain) —
  same gate as existing test-mode headers.
- Startup `logit.warn` makes prod misconfig visible in logs.
- The warning message does NOT print the bypass value.

### Follow-up
- None planned. OAuth signup integration with `AUTH_REGISTER_FIELDS`
  was already deferred in the parent request.
- The "phone-only schema with empty step 3" edge case is defended by
  the existing `register_schema._normalize_entry` forcing
  `password.required=True` — step 3 always has at least the password
  input. No special-case render branch was needed.
