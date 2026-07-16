---
id: DM-001
type: feature
title: Render allowlisted extra registration fields (promo) per-group
priority: P2
effort: S
owner: backend
opened: 2026-06-06
depends_on: []
related: []
source_reqs: [REQ-029]
links: [/Users/ians/Projects/wmwx/docs/orchestrate/done/REQ-029.md]
---

# Render allowlisted extra registration fields (promo) per-group

## What & Why
The django-mojo hosted register page (`account/templates/account/register.html`)
renders only the canonical schema fields (`first_name`, `last_name`, `email`,
`phone`, `dob`, `password`). It renders no input for the allowlisted *extra*
fields (`REGISTRATION_EXTRA_FIELDS = ["promo", "ref", "tracking", "channel"]`),
and the submit JS builds its payload from the canonical `register_fields` schema
only — so even an allowlisted extra never reaches `POST /api/auth/register`.

The backend already accepts these extras: `on_register` reads
`REGISTRATION_EXTRA_FIELDS` into the `extra` dict passed to the
`USER_REGISTERED_HANDLER`. The downstream wmx-api validate + grant path
(`registration_codes._apply_promo_code` → `PlayerProfile.metadata["promo_code"]`
→ `promotions.asyncjobs.redeem_registration_promo`) already shipped (`a4f1df0`).
So the only gap is **capture** — rendering the extra input and wiring it into the
payload.

This lands in the **shared** register template, so it must NOT hardcode a promo
`<input>` (that would force the field on every tenant). The render + submit must
be driven by **per-group config** (default off), so other brands are unaffected.
Build the generic "render the allowlisted extra fields this group declares"
mechanism, not a wmx-specific one.

This is the django-mojo half of REQ-029 (wmx-api half shipped in `a4f1df0`).

## Acceptance Criteria
- [ ] Extra registration inputs (e.g. `promo`) render on the hosted register page,
      driven by per-group config, default OFF.
- [ ] Submit JS adds the value(s) to the register payload under the field name(s).
- [ ] `POST /api/auth/register` with `promo=WELCOME100` lands the value in the
      `extra` dict reaching `USER_REGISTERED_HANDLER` (the shipped validate/grant
      path then runs).
- [ ] Per-group gating — a brand that does not enable the toggle sees no extra
      field and no behavior change (default config unchanged).

## Repro — bugs only
N/A (feature).

## Plan

### Goal
Add a per-group, default-off mechanism that renders allowlisted *extra*
registration fields (e.g. `promo`) on the shared hosted register page — silently
captured from the URL when present, asked for as a plain text input when not —
wires their values into the register payload, captures them server-side, and
persists them onto the new user at `user.metadata["registration"]`.

### Context — what exists
The `configurable-register-form` work (done) made registration schema-driven via a
per-group `auth_config`. The extension points are all in place; we add a sibling
config key and a render/capture path for *extras*.

- **Per-group config source** — `mojo/apps/account/services/auth_config.py`.
  `resolve_auth_config(group, request)` deep-merges: code `DEFAULT_AUTH_CONFIG`
  ([auth_config.py:49](mojo/apps/account/services/auth_config.py:49)) → global
  `AUTH_CONFIG` setting → each ancestor group's `metadata["auth_config"]` →
  `X-Mojo-Test-Auth-Config` header (test-mode only). The `registration` block
  ([auth_config.py:65](mojo/apps/account/services/auth_config.py:65)) holds
  `enabled`, `fields`, `identity_field`, `min_age`, `methods`, `passkey_prompt`.
  A public-subset whitelist re-emits these fields
  ([auth_config.py:169](mojo/apps/account/services/auth_config.py:169)).
  `validate_auth_config` ([auth_config.py:226](mojo/apps/account/services/auth_config.py:226))
  validates a group's config on save; its `registration` branch calls
  `register_schema.validate_fields_config(fields)` ([auth_config.py:267](mojo/apps/account/services/auth_config.py:267)).
- **Schema service** — `mojo/apps/account/services/register_schema.py`.
  `CANONICAL_FIELDS = ("first_name","last_name","email","phone","dob","password")`,
  closed. `resolve_fields(group, request)` reads
  `auth_config.resolve_auth_config(...).registration.fields`, supports the
  `X-Mojo-Test-Register-Fields` header, and normalizes via `_normalize_field_list`
  (drops unknown names). `validate_fields_config(fields)` validates shape. Extras
  are deliberately **not** modeled here.
- **Register endpoint** — `mojo/apps/account/rest/user.py:on_register`
  ([user.py:249](mojo/apps/account/rest/user.py:249)). Extras are read from the
  global `REGISTRATION_EXTRA_FIELDS` allowlist into an `extra` dict
  ([user.py:343-346](mojo/apps/account/rest/user.py:343)):
  ```python
  extras_allow = account_extensions.list_setting_with_header(
      request, "X-Mojo-Test-Registration-Extra-Fields",
      "REGISTRATION_EXTRA_FIELDS", [])
  extra = {key: request.DATA.get(key) for key in extras_allow if key in request.DATA}
  ```
  `extra` is forwarded to `USER_REGISTERED_HANDLER` via
  `account_extensions.fire_user_registered(... extra=extra)` on both the
  existing-account phone path ([user.py:385](mojo/apps/account/rest/user.py:385))
  and the new-user atomic block ([user.py:487](mojo/apps/account/rest/user.py:487)).
  The new user is built and saved in `transaction.atomic()`
  ([user.py:447-489](mojo/apps/account/rest/user.py:447)); `user.save()` is at
  [user.py:481](mojo/apps/account/rest/user.py:481).
- **User.metadata** — `JSONField(default=dict)`
  ([models/user.py:116](mojo/apps/account/models/user.py:116)); update pattern used
  throughout is `meta = self.metadata or {}; meta[...] = ...; self.metadata = meta`
  (e.g. [models/user.py:1378](mojo/apps/account/models/user.py:1378)).
- **Bouncer context** — `mojo/apps/account/rest/bouncer/views.py:_auth_context`
  ([bouncer/views.py:254](mojo/apps/account/rest/bouncer/views.py:254)) emits
  `register_fields`, `register_field_rows`, `identity_field`, `forgot_channel`,
  etc. into the template context.
- **Template** — `mojo/apps/account/templates/account/register.html`.
  Canonical fields render via `{% for row in register_field_rows %}` (line 79);
  empty extension hooks exist at `{% block extra_fields_stepped %}` (line 62, step 3
  of the stepped/SMS flow) and `{% block extra_fields %}` (line 89, single-pane
  flow). The schema is handed to JS as JSON at line 127
  (`{{ register_fields|json_script:"reg-fields-data" }}`), parsed into `REG_FIELDS`
  at line 137; `valueFor(name)` reads `#reg-{name}` (line 149). **Two** submit
  sites build a payload:
  - Single-pane final submit ([register.html:373-398](mojo/apps/account/templates/account/register.html:373)):
    loops `REG_FIELDS` into `payload`, then adds `verified_phone_token` and
    `group_uuid` before `MojoAuth.register(payload)`.
  - Stepped existing-account fast-path ([register.html:308-311](mojo/apps/account/templates/account/register.html:308)):
    builds `p = { phone, verified_phone_token }` (+`group_uuid`) then
    `MojoAuth.register(p)`.
  `MojoAuth.register(payload)` ([static/account/mojo-auth.js:243](mojo/apps/account/static/account/mojo-auth.js:243))
  forwards **all** payload keys untouched; the server silently drops unknowns.
- **Test patterns** — render tests call `_auth_context(request, group=group)` then
  `render(...)` (`tests/test_auth/bouncer_forms.py` `_render`, lines 41-53) and
  assert on raw HTML substrings. Register integration tests build per-request
  test-mode headers via `_reg_headers(...)` (`tests/test_register/register.py`
  lines 55-89), including `X-Mojo-Test-Registration-Extra-Fields` (JSON list) and a
  capture `USER_REGISTERED_HANDLER`. Test-mode headers are gated by
  `extensions._read_test_header` (loopback + `MOJO_TEST_MODE_ENABLED` + no proxy).

### Changes — what to do
1. **`mojo/apps/account/services/register_schema.py`** — model extras.
   - Add `resolve_extra_fields(group=None, request=None)` mirroring `resolve_fields`:
     read `auth_config.resolve_auth_config(group=group, request=request).registration.extra_fields`
     (default `[]`); honor an `X-Mojo-Test-Register-Extra-Fields` header (JSON list)
     for parity with `resolve_fields`; normalize each entry via a new
     `_normalize_extra_field_list` to `{"name", "label", "required"}`:
     - drop entries without a non-empty string `name`;
     - **drop any `name` in `CANONICAL_FIELDS`** (prevents `#reg-extra-{name}` /
       `#reg-{name}` id and capture collisions);
     - `label` defaults to a humanized `name` (e.g. `name.replace("_"," ").title()`);
     - `required` coerced to bool, default `False`.
   - Add `extra_field_names(extra_fields)` → `[ef["name"] for ef in extra_fields]`.
   - Add `validate_extra_fields_config(extra_fields)`: must be a list; each item a
     dict with a non-empty string `name` not in `CANONICAL_FIELDS`; optional
     `label` (str) and `required` (bool). Raise `ValueException` on violation
     (mirrors `validate_fields_config`).
2. **`mojo/apps/account/services/auth_config.py`** — register the new key.
   - Add `"extra_fields": []` to `DEFAULT_AUTH_CONFIG["registration"]`
     ([auth_config.py:65](mojo/apps/account/services/auth_config.py:65)).
   - Add `"extra_fields": list(registration.get("extra_fields") or [])` to the
     public subset ([auth_config.py:171](mojo/apps/account/services/auth_config.py:171)).
   - In `validate_auth_config`'s registration branch
     ([auth_config.py:267](mojo/apps/account/services/auth_config.py:267)), after the
     `fields` check, validate extras:
     ```python
     extra_fields = registration.get("extra_fields")
     if extra_fields:
         from mojo.apps.account.services import register_schema
         register_schema.validate_extra_fields_config(extra_fields)
     ```
3. **`mojo/apps/account/rest/bouncer/views.py:_auth_context`** — emit context.
   - `register_extra_fields = register_schema.resolve_extra_fields(group=group, request=request)`.
   - Add `'register_extra_fields': register_extra_fields` to the returned dict
     (next to `register_fields`, ~[bouncer/views.py:330](mojo/apps/account/rest/bouncer/views.py:330)).
4. **`mojo/apps/account/templates/account/_register_extra_field.html`** — new partial.
   - Renders one extra text field, hidden by default so the URL-param case never
     flashes; JS reveals it when there is no URL param:
     ```django
     <div class="mat-field" id="reg-extra-row-{{ ef.name }}" style="display:none">
       <input type="text" id="reg-extra-{{ ef.name }}" placeholder=" "
              {% if ef.required %}data-required="1"{% endif %} />
       <label for="reg-extra-{{ ef.name }}">{{ ef.label }}</label>
     </div>
     ```
     (Match the existing `mat-field` markup used by `_register_field.html`; the
     `{{ ef.label }}` is auto-escaped by Django.)
5. **`mojo/apps/account/templates/account/register.html`** — render + wire.
   - Render the loop in **both** extra-field hook locations so extras appear in the
     single-pane flow and step 3 of the stepped flow. Just before the
     `{% block extra_fields %}` at line 89 (and likewise before
     `{% block extra_fields_stepped %}` at line 62):
     ```django
     {% for ef in register_extra_fields %}{% include "account/_register_extra_field.html" %}{% endfor %}
     ```
   - Emit the JSON after line 127:
     `{{ register_extra_fields|json_script:"reg-extra-fields-data" }}`.
   - In the JS init (near line 137) add:
     ```javascript
     var REG_EXTRA_FIELDS = JSON.parse(document.getElementById("reg-extra-fields-data").textContent || "[]");
     var URL_PARAMS = new URLSearchParams(window.location.search);
     // For each declared extra: if present in the page URL, capture silently
     // (stays hidden, value pre-set); otherwise reveal the text input to ask.
     for (var i = 0; i < REG_EXTRA_FIELDS.length; i++) {
       var ef = REG_EXTRA_FIELDS[i];
       var fromUrl = URL_PARAMS.get(ef.name);
       var el = document.getElementById("reg-extra-" + ef.name);
       var row = document.getElementById("reg-extra-row-" + ef.name);
       if (fromUrl !== null && fromUrl !== "") {
         if (el) el.value = fromUrl;            // captured; row stays display:none
       } else if (row) {
         row.style.display = "";                // no URL value → ask for it
       }
     }
     ```
   - Add a shared collector used by both submit sites:
     ```javascript
     // Returns true if all required extras are present; false (after showing a
     // message) otherwise. URL param wins over a (hidden) input value.
     function collectExtras(payload) {
       for (var i = 0; i < REG_EXTRA_FIELDS.length; i++) {
         var ef = REG_EXTRA_FIELDS[i];
         var fromUrl = URL_PARAMS.get(ef.name);
         var el = document.getElementById("reg-extra-" + ef.name);
         var v = (fromUrl !== null && fromUrl !== "")
                   ? fromUrl
                   : (el ? (el.value || "").trim() : "");
         if (ef.required && !v) {
           m.showMessage("Please fill in all required fields.", "error");
           return false;
         }
         if (v) payload[ef.name] = v;
       }
       return true;
     }
     ```
   - Call it in the single-pane submit after the `REG_FIELDS` loop (after line 389,
     before `group_uuid`): `if (!collectExtras(payload)) return;`.
   - Call it in the stepped fast-path after building `p` (after line 310, before
     `MojoAuth.register(p)`): `collectExtras(p);` (no required-block here — this is
     a returning-user sign-in; just forward any present extras).
6. **`mojo/apps/account/rest/user.py:on_register`** — union allowlist + persist.
   - Replace the extras-allowlist block
     ([user.py:343-346](mojo/apps/account/rest/user.py:343)) with a union of the
     legacy global setting and the group's declared extra-field names:
     ```python
     extras_allow = account_extensions.list_setting_with_header(
         request, "X-Mojo-Test-Registration-Extra-Fields",
         "REGISTRATION_EXTRA_FIELDS", [])
     group_extra_fields = register_schema.resolve_extra_fields(group=group, request=request)
     allow = set(extras_allow) | set(register_schema.extra_field_names(group_extra_fields))
     extra = {key: request.DATA.get(key) for key in allow if key in request.DATA}
     ```
   - In the atomic new-user block, just before `user.save()`
     ([user.py:481](mojo/apps/account/rest/user.py:481)), persist captured extras to
     `metadata["registration"]`:
     ```python
     if extra:
         meta = user.metadata or {}
         reg = meta.get("registration") or {}
         reg.update(extra)
         meta["registration"] = reg
         user.metadata = meta
     ```
     (No separate save — `user.save()` at line 481 writes it. The existing-account
     phone path is left unchanged: it still forwards `extra` to the handler but does
     not overwrite a returning user's metadata — see Design decisions.)
7. **Docs** — see Docs section.

### Design decisions
- **Reuse `auth_config.registration` as the single per-group knob** (not a new
  `show_promo_code` boolean). REQ-029's non-negotiable constraint is a *generic*
  per-group mechanism; `auth_config` already is the group-scoped, deep-merged,
  validated-on-save config surface for registration. Adding `extra_fields` beside
  `fields` is the lowest-surprise extension.
- **Group config is both the render declaration AND the capture authorization.**
  `on_register` unions the group's declared names with the legacy global
  `REGISTRATION_EXTRA_FIELDS`. Rationale: keeps existing deployments working with
  zero migration (promo/ref/tracking/channel stay captured), while letting a group
  authorize new extras purely through its (perm-gated, validated) `auth_config`
  metadata. Authorization to edit a group's `auth_config` therefore *is* the
  authorization to define its captured extras — intended and contained (extras are
  plain strings handed to the consumer `USER_REGISTERED_HANDLER`, never to
  privileged code).
- **URL param ⇒ silent capture; no param ⇒ ask (text only).** The split is purely
  presentational and decided client-side (the server can't see the page-URL query
  at render time). The partial renders hidden by default; JS reveals the input only
  when no URL value exists. URL value wins over any input value.
- **Extras persist to `user.metadata["registration"]`** (a `name→value` sub-dict),
  merged so other metadata is untouched. This is generic framework storage in
  addition to the existing `extra=` kwarg to `USER_REGISTERED_HANDLER` — the shipped
  wmx promo-grant handler keeps reading `extra` and is unchanged.
- **Persist only on new-user creation, not the returning-user fast path.** The
  existing-account phone path is a sign-in; overwriting an established user's
  `metadata["registration"]` from query params would be surprising. We still
  forward `extra` to the handler there (unchanged) for parity.
- **Extras are text-only and server-non-authoritative.** Canonical fields stay the
  closed, typed, validated set ([register_schema.py CANONICAL_FIELDS]). Extras are
  free-text, `required` is a **client-side UX hint only** — the server keeps the
  existing "silent-drop / no 400 for missing extra" contract. Rationale: extras are
  consumer data, not identity/auth; a hard server gate would be a new failure mode
  for little benefit.

### Edge cases & risks
- **Default off / no behavior change** — `extra_fields` defaults to `[]`; with no
  group config the loop is empty, no `reg-extra-*` markup renders, the allowlist
  union reduces to today's global set. Covered by a render regression test.
- **Id/name collision with a canonical field** — `resolve_extra_fields` drops any
  `name` in `CANONICAL_FIELDS`, and the distinct `reg-extra-` id prefix keeps DOM
  ids disjoint from `reg-{name}`.
- **XSS / injection** — `{{ ef.label }}` is Django auto-escaped; input values are
  set via `el.value` from `URLSearchParams` (text, not `innerHTML`); captured values
  reach the server as plain request data. No new sink.
- **Stepped-flow profile step** — extras render in step 3 too, so a phone-identity
  group still collects/asks for extras; the fast-path forwards URL-param extras.
- **Multi-value query param** — `URLSearchParams.get` takes the first; acceptable
  for these single-value codes.
- **Group `auth_config` with a malformed `extra_fields`** — rejected at Group-save
  time by `validate_extra_fields_config`, not discovered at render.

### Tests
Use testit (`from testit import helpers as th`, `@th.django_unit_test()`); read
`docs/django_developer/testit/Overview.md` first. Setup must delete any records it
creates before inserting (long-lived DB). Run with
`bin/run_tests --agent -t <module>`.

- **Schema unit** → extend `tests/test_register/schema.py`:
  - `resolve_extra_fields` with no config → `[]`.
  - normalization: humanized default label; `required` coerced to bool; entry
    without `name` dropped; a `name` in `CANONICAL_FIELDS` dropped.
  - `validate_extra_fields_config` rejects non-list, item without `name`, and a
    `name` colliding with a canonical field.
- **Render / gating** → extend `tests/test_auth/bouncer_forms.py`:
  - With `opts.group.metadata = {"auth_config": {"registration": {"extra_fields":
    [{"name": "promo", "label": "Promo code"}]}}}` (save, render via `_render(
    'account/register.html', group=opts.group)`): HTML contains
    `id="reg-extra-promo"`, the label "Promo code", the `reg-extra-fields-data`
    `json_script`, and the `collectExtras(` call. (Clean up group metadata in setup.)
  - Default group (no `extra_fields`) → HTML contains **no** `reg-extra-` substring
    (gating regression guard).
- **Capture + persistence (integration)** → new `tests/test_register/extra_fields.py`,
  mirroring `register.py`'s `_reg_headers` + capture-handler pattern:
  - `promo` in body + a capture `USER_REGISTERED_HANDLER`, with promo allowed via
    `X-Mojo-Test-Registration-Extra-Fields=["promo"]` → 200; handler `extra["promo"]`
    equals the sent value **and** the created `User.metadata["registration"]["promo"]`
    equals it.
  - Group-config path: promo authorized via `X-Mojo-Test-Auth-Config`
    (`registration.extra_fields=[{"name":"promo"}]`) with the global extras header set
    to `[]` → promo still captured (proves the union picks up group-declared names).
  - Gating: promo in body, global extras header `["other"]`, no group `extra_fields`
    → `promo` absent from handler `extra` and absent from `metadata["registration"]`.
  - Back-compat: promo via global `REGISTRATION_EXTRA_FIELDS` only (no group config)
    still captured.

### Docs
- `docs/django_developer/account/auth_pages.md` — in the Configurable Registration
  Form section, document `registration.extra_fields` (shape `{name,label,required}`,
  default off), the URL-param-silent-capture vs ask-as-text behavior, the union with
  `REGISTRATION_EXTRA_FIELDS`, and persistence at `user.metadata["registration"]`.
- `docs/web_developer/account/authentication.md` — note `POST /api/auth/register`
  accepts arbitrary allowlisted extra keys; describe the per-group config + the
  `?name=value` URL-capture pattern and `metadata.registration` storage.
- `CHANGELOG.md` — entry: per-group configurable extra registration fields (default
  off), URL-param capture, `metadata.registration` persistence; defaults unchanged.

### Open questions
None blocking. (Two settled by Design decisions: extras are server-non-authoritative
even when `required`; persistence happens only on new-user creation, not the
returning-user fast path.)

## Notes
Source: external request `/Users/ians/Projects/wmwx/docs/orchestrate/confirmed/REQ-029.md`.
The `configurable-register-form` done item deliberately closed the canonical field
set to six User-column fields and routed consumer-specific fields to
`REGISTRATION_EXTRA_FIELDS`. This item delivers the render path for those extras.

## Resolution
- closed: 2026-06-06
- branch: main
- files changed: CHANGELOG.md,docs/django_developer/account/auth_config.md,docs/django_developer/account/auth_pages.md,docs/web_developer/account/auth_config.md,docs/web_developer/account/authentication.md,mojo/apps/account/rest/bouncer/views.py,mojo/apps/account/rest/user.py,mojo/apps/account/services/auth_config.py,mojo/apps/account/services/register_schema.py,mojo/apps/account/templates/account/_register_extra_field.html,mojo/apps/account/templates/account/register.html,tests/test_auth/bouncer_forms.py,tests/test_register/extra_fields.py,tests/test_register/schema.py
- tests added: tests/test_register/schema.py (5 extra-field unit tests — resolve default/normalize/drop-bad incl. format guard, validate_extra_fields_config good+bad shapes); tests/test_register/extra_fields.py (new, 4 integration tests — group-config capture+persist to metadata.registration, undeclared-key drop, legacy global-allowlist back-compat, no-extras→no-metadata); tests/test_auth/bouncer_forms.py (2 render tests — configured promo input + collectExtras wiring, gating regression). Targeted 48/48 green; full test_register 93/93; test_auth has no new regressions (5 pre-existing sms_otp_autofill failures are an unrelated unimplemented WebOTP feature).
