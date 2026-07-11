---
id: ITEM-031
type: bug
title: GEOFENCE_TEST_OVERRIDE + MOJO_TEST_MODE are DB/Redis-settable — silent total geofence bypass, no evidence trail
priority: P2
effort: S
owner: backend
opened: 2026-07-09
depends_on: []
related: [ITEM-017, ITEM-023]
links:
  - wmwx/wmx_api WMX-API-121 (security review finding, 2026-07-09)
---

# GEOFENCE_TEST_OVERRIDE / MOJO_TEST_MODE readable from DB Settings — geofence bypass with no audit trail

## What & Why

`GeoFenceEngine._resolve_geo` (engine.py:278-280) reads
`GEOFENCE_TEST_OVERRIDE` through the DB-aware settings helper
(`settings.get`, DB/Redis-first). A single global `Setting` row — writable
via the generic settings REST by platform `manage_settings`, or by anyone
with Redis write access to the settings cache hash — makes **every**
geofence decision resolve to the override: a total, silent bypass of
jurisdiction enforcement.

Unlike the `X-Mojo-Test-Geo` header seam there is no loopback / no-proxy
defense on this path, and the key is not in `Setting.GEOFENCE_KEYS`, so:
no write-time validator, no global-only enforcement, no decision-cache
invalidation, and no `geofence_config` evidence event — the flip leaves no
trail. The same DB-first read applies to `MOJO_TEST_MODE` (though the
header seam itself stays remotely unexploitable — proxies append
X-Forwarded-For, tripping the test-mode gate).

Downstream weight: wmx_api now hangs statutory money/compliance gates on
the engine (WMX-API-121 — 13-state deny list at registration/login/launch/
deposit/redemption), so this bypass is no longer test-plumbing-only.

## Acceptance Criteria

- [ ] `GEOFENCE_TEST_OVERRIDE` and `MOJO_TEST_MODE` are read file-only
      (django.conf / `settings.get_static`) — never from the DB/Redis
      settings plane; OR the override is gated behind
      `test_mode.is_enabled()` with that flag itself file-only.
- [ ] If either key is deliberately kept DB-settable: add to
      `Setting.GEOFENCE_KEYS` (validator + global-only), invalidate the
      decision cache on write, and emit `report_config_change` so the flip
      lands in the geofence_config evidence stream.
- [ ] Regression test: a `GEOFENCE_TEST_OVERRIDE` Setting row (and Redis
      cache entry) does NOT affect engine resolution in a non-test-mode
      process.

## Notes

- Baseline (2026-07-10, pre-edit, `bin/run_tests --agent`): total 2411,
  passed 2355, failed 0, skipped 56 — green. NOTE: the first full run showed 4
  transient errors in `test_metrics/fanout.py` (`account_group_pkey` duplicate
  key, ids 9011-9014) — a shared-DB PK-sequence desync under full-suite
  ordering, NOT a code fault; the module passes 19/19 in isolation and the
  full suite passed clean on immediate re-run. Not mine; not in my change area.
- Found by wmx_api's WMX-API-121 post-build security review (2026-07-09).
- Sibling of ITEM-023 (`GEOFENCE_FAIL_CLOSED_SCOPES` / `ALLOW_PRIVATE_IPS`
  validation gap) — same "adjacent settings bypass the validated plane"
  family; consider fixing together.
- Related smaller finding from the same review (fix here or alongside):
  when an enforcement point overrides a fail-open `lookup_failed` decision
  and blocks anyway, `evidence.report_block` records "fail-open allowed"
  — evidence verb should be able to reflect the enforced outcome (wmx's
  seam now passes a blocked copy as a workaround).

### Validity re-check (2026-07-10, at scope time)

Verified against current code — **bug is still present and unfixed**:
- `engine.py:278` reads `settings.get("GEOFENCE_TEST_OVERRIDE", None)` (DB/Redis-
  first), OUTSIDE the `is_test_request` gate at 272-277 — runs on every request.
- `test_mode.py:43` reads `settings.get("MOJO_TEST_MODE", False, kind="bool")`
  (DB/Redis-first).
- Neither key is in `Setting.GEOFENCE_KEYS` (setting.py:94-97) nor has a
  registered validator, so writing either via `/api/settings` skips validation,
  global-only enforcement, decision-cache invalidation, and evidence.
- Both `docs/django_developer/account/geofence.md:389-390` ("conf-file-only
  `GEOFENCE_TEST_OVERRIDE`") and `test_mode.py:6-8` ("`MOJO_TEST_MODE = True` in
  Django settings") already document these as **file** settings — the code
  contradicts its own docs. The fix makes code match docs.

**Correction to the filed severity narrative (verified in code):** a DB/Redis
`GEOFENCE_TEST_OVERRIDE` row is **not** a silent allow-bypass for that key.
`Setting.get_value()` (setting.py:58-62) returns the raw `value` TextField (a
string) and `Setting.resolve` (setting.py:232+) / `get_cached` (208-218) yield a
string; the engine reads it with **no `kind=`**, so `settings.get` returns the
string unparsed and `dict("<json string>")` at engine.py:280 raises
`ValueError` → caught by `dispatch_error_handler` (`mojo/decorators/http.py`) →
**HTTP 400 on every geofenced request** (an unaudited availability break, with
stale cached allows still served until TTL since the key isn't in
`GEOFENCE_KEYS`). The genuinely dangerous half is **`MOJO_TEST_MODE`**: read
with `kind="bool"`, a DB/Redis row coerces cleanly to `True` and arms the entire
X-Mojo-Test-* header plane process-wide (incl. `X-Mojo-Test-Geo` geo
substitution and dotted-path handler headers that load arbitrary importable
callables), gated then only by loopback + no-proxy — reachable via SSRF or any
internal/localhost caller. Either way the fix (file-only reads) is unchanged;
only the "impact" wording changes.

## Plan

### Goal

Read `GEOFENCE_TEST_OVERRIDE` and `MOJO_TEST_MODE` file-only
(`settings.get_static`), closing the DB/Redis-settable silent geofence bypass
and making both match their already-documented "conf-file-only" contract.

### Context — what exists

**The bypass — `mojo/apps/account/services/geofence/engine.py:265-280`:**
```python
def _resolve_geo(ip, request=None):
    """... Otherwise GEOFENCE_TEST_OVERRIDE setting wins over real lookups."""
    if _tm.is_test_request(request):                        # loopback + MOJO_TEST_MODE + no-proxy
        if _header(request, "X-Mojo-Test-Geo") == "fail":
            return None
        header_override = _json_header(request, "X-Mojo-Test-Geo")
        if header_override is not None:
            return header_override
    override = settings.get("GEOFENCE_TEST_OVERRIDE", None)  # :278 — DB/Redis-first, NO gate
    if override:
        return dict(override)
    # ...real GeoLocatedIP.geolocate(ip) lookup follows...
```
The `X-Mojo-Test-Geo` header path (272-277) is properly gated; the
`GEOFENCE_TEST_OVERRIDE` read at 278 sits *outside* that block and runs
unconditionally for every request, via the DB-aware helper. `line 278 is the
only executable read` of the key (other hits are docstrings/comments).

**The test-mode gate — `mojo/helpers/test_mode.py:41-43`:**
```python
def is_enabled():
    """Module-level: is test-mode enabled in this process at all?"""
    return settings.get("MOJO_TEST_MODE", False, kind="bool")   # :43 — DB/Redis-first
```
`is_enabled()` is the master switch for the entire X-Mojo-Test-* header plane
(`is_test_request` returns False when it's off). The module docstring (lines
6-8, 22-23) already states the flag lives in the Django *settings file* and is
safe "even if MOJO_TEST_MODE accidentally leaks into a production settings
file" — the code's DB read contradicts that stated design. `is_enabled()` is
the only read of the flag.

**The settings helpers — `mojo/helpers/settings/helper.py`:**
- `get(name, default, group, kind)` (157-178): `_get_db_setting` (Redis→DB via
  `Setting.resolve`) first when Django is ready, django.conf only as fallback.
- `get_static(name, default, kind)` (180-198): django.conf **only** —
  `getattr(self._live_django_settings(), name, ...)`, never touches DB/Redis.
  Same `kind=` coercion path as `get`.

**Why the sibling `_*_setting_with_header` helpers (engine.py:86-110) stay on
`get`:** they resolve the *real* geofence config keys (`GEOFENCE_ENABLED`,
`GEOFENCE_ALLOW_PRIVATE_IPS`, etc.) which ARE legitimately DB-settable and live
in `GEOFENCE_KEYS` with validators + cache invalidation (ITEM-023). Only the
two *test-plumbing* keys are being moved to file-only. Do **not** touch those
helpers.

**Docs already describing the target state:**
- `docs/django_developer/account/geofence.md:387,389-390` — table row for
  `GEOFENCE_TEST_OVERRIDE` + the "except the conf-file-only
  `GEOFENCE_TEST_OVERRIDE`" clause. Already correct; no change needed (verify).
- `docs/django_developer/helpers/settings_reference.md:131` — lists
  `GEOFENCE_TEST_OVERRIDE`.

**Test patterns — `tests/test_geofence/`:**
- In-process direct override of a Django setting: `test_mode_gate.py:33-42`
  saves `getattr(dj_settings, name, ...)`, sets on `django.conf.settings`,
  restores in `finally` (NOT `th.server_settings` — memory rule). This is the
  pattern for asserting the file value is honored.
- Real-Setting-row + decision-cache with `finally` cleanup:
  `tests/test_geofence/settings_validation.py` (`Setting.set`/`Setting.remove`
  + `gf_cache.invalidate_all()`); hygiene banner (lines 6-12): never persist a
  non-default global row without restoring, never touch `127.0.0.1`/allowlist.
- Engine driven via `opts.client.get("/api/geo/check", headers=...)`
  (`engine.py:18`, `_helpers.headers`).
- No existing test references `GEOFENCE_TEST_OVERRIDE` as a value — coverage gap.

### Changes — what to do

1. **`mojo/apps/account/services/geofence/engine.py:278`** — change
   `settings.get("GEOFENCE_TEST_OVERRIDE", None)` →
   `settings.get_static("GEOFENCE_TEST_OVERRIDE", None)`. One line. Update the
   `_resolve_geo` docstring (269-270) to note the override is a conf-file-only
   knob.
2. **`mojo/helpers/test_mode.py:43`** — change
   `settings.get("MOJO_TEST_MODE", False, kind="bool")` →
   `settings.get_static("MOJO_TEST_MODE", False, kind="bool")`.
3. **`tests/test_geofence/test_override_file_only.py`** — new regression module
   (see Tests).
4. **Docs:** verify geofence.md:389-390 still reads true (it does) and, if
   `settings_reference.md:131` doesn't already, add a one-word "(conf-file-only)"
   note next to `GEOFENCE_TEST_OVERRIDE`. Add a `MOJO_TEST_MODE` file-only note
   if the reference documents it. `CHANGELOG.md` security-fix entry.

### Design decisions

- **File-only (`get_static`), not the validated-DB-plane treatment.** The AC
  offered two paths; take path 1. These are test/local-dev plumbing knobs with
  no legitimate runtime-flip use case — deploy-time (a settings file) is the
  correct and sufficient bar, and it's exactly what both files' own docs
  already promise. Path 2 (keep DB-settable, add to `GEOFENCE_KEYS` + validator
  + global-only + cache invalidation + a brand-new `report_config_change` hook
  at the `Setting.save/delete` layer — which does **not** exist today; evidence
  currently only fires from the dedicated `rest/geofence.py` endpoints) is
  strictly *more* machinery to make a bypass auditable rather than to remove
  it. Rejected: you don't audit a backdoor you can simply close.
- **Also gating the override behind `is_test_request` was considered and NOT
  added.** The `_resolve_geo` docstring says the override "wins over real
  lookups" generally (a documented staging/dev knob), so requiring test-mode
  would be a behavior change for environments that set the file value without
  `MOJO_TEST_MODE`. `get_static` alone closes the reported vector (DB/Redis) and
  matches the docs; tightening to test-mode-only is a separable hardening, not
  this bug.
- **`report_block` "fail-open allowed" verb sub-finding → separate item, not
  this one.** It's an evidence-fidelity issue, not a bypass; fixing it means an
  API/signature change to `report_block` with a downstream consumer (wmx passes
  a blocked-copy workaround) that deserves its own scoping. Keeping ITEM-031
  tight (two-line security fix + regression) is the KISS call. *Recommend
  filing via `/request`.*

### Edge cases & risks

- **`get_static` coercion parity.** `get_static` runs the same `_convert_value`
  path as `get` for `kind="bool"`, so `MOJO_TEST_MODE`'s bool parsing is
  unchanged; only the *source* (file vs DB) changes.
- **Existing in-process tests stay green.** `test_mode_gate.py` sets
  `django.conf.settings.MOJO_TEST_MODE` directly and passed *because* no DB row
  existed and `get` fell through to file — `get_static` reads that same file
  value, so those tests are unaffected.
- **The real test suite runs with `MOJO_TEST_MODE=True` in its Django conf**
  (the server banner in every run confirms it) — a file value, so `get_static`
  keeps the header plane working for the whole suite. No DB row is relied upon.
- **No behavior change for legitimate deployments** unless one is *currently*
  flipping these two keys via a DB Setting row (the vector being closed) — which
  is precisely the unsupported, undocumented usage this fixes.

### Tests

New `tests/test_geofence/test_override_file_only.py` (testit; import models
inside functions; descriptive asserts; `finally` cleanup per the
`settings_validation.py` hygiene banner):

1. **Regression — DB Setting row does NOT affect resolution.** Persist a global
   `Setting.set("GEOFENCE_TEST_OVERRIDE", {"country_code": "XX", ...})` (+
   `gf_cache.invalidate_all()`), ensure no file value is set, then call
   `_resolve_geo(<ip>)` and assert it (a) does not raise and (b) does not return
   the override dict — the DB row is ignored and resolution proceeds to the real
   lookup path. Remove the row in `finally`. Pre-fix this **raises `ValueError`**
   (`dict("<json string>")` from the DB read — see the corrected severity note),
   so the test errors; post-fix it falls through cleanly. (Assert on the
   no-raise + not-honored behavior rather than a specific return value, so the
   test is robust to whatever the real/mocked lookup yields.)
2. **File value still honored.** With `setattr(django.conf.settings,
   "GEOFENCE_TEST_OVERRIDE", {...})` in try/finally, assert `_resolve_geo`
   returns that dict — proves `get_static` reads the file and the dev/staging
   knob still works.
3. **`MOJO_TEST_MODE` DB row does not enable the header plane.** Persist
   `Setting.set("MOJO_TEST_MODE", True)` with no file value, construct a
   loopback no-proxy fake request carrying `X-Mojo-Test-Geo`, and assert
   `test_mode.is_test_request(req)` is False and the header is ignored. Remove
   in `finally`. (Guards the master switch.)

Baseline: `bin/run_tests --agent` green before edits (build rule). Run targeted
with `bin/run_tests --agent -t test_geofence.test_override_file_only`.

### Docs

- `docs/django_developer/` — geofence.md already states conf-file-only (verify,
  likely no change); settings_reference.md:131 — add "(conf-file-only)" if
  absent, and a `MOJO_TEST_MODE` note if that key is documented there.
- `docs/web_developer/` — none (internal test/config plumbing, no REST surface
  change).
- `CHANGELOG.md` — security-fix entry: `GEOFENCE_TEST_OVERRIDE` / `MOJO_TEST_MODE`
  are now file-only; a DB/Redis Setting row can no longer bypass geofence
  enforcement or enable the test-header plane.

### Open questions

- Confirm the `report_block` verb sub-finding (Notes) is split into its own
  item rather than folded in here. Plan assumes **split** (recommended). If you
  want it in-scope, say so and I'll re-scope to include the `report_block`
  signature change.

## Build log

- Fix applied exactly as planned: `engine.py:278`
  `settings.get(...)` → `settings.get_static(...)` and `test_mode.py:43`
  `is_enabled()` → `settings.get_static("MOJO_TEST_MODE", False, kind="bool")`,
  plus docstring updates on both. No new machinery.
- Regression tests confirmed failing pre-fix exactly as predicted: DB
  `GEOFENCE_TEST_OVERRIDE` row → `ValueError('dictionary update sequence
  element ...')` (the `dict("<json str>")` 400 path); DB `MOJO_TEST_MODE` row →
  `is_enabled()` True with conf False. Both pass post-fix; the conf-honored
  guard passed throughout (proves the override still works, read moved not
  removed).
- Docs: no change needed to `geofence.md` (it already said "conf-file-only
  `GEOFENCE_TEST_OVERRIDE`" — the code now matches). Added a clarifying
  sentence to `testit/Overview.md` security-gate section. `settings_reference.md`
  left untouched (auto-generated static-scan, names-only). CHANGELOG entry added.
- Final suite: total 2414 (+3 new tests), passed 2358, failed 0 — green.
- `report_block` verb sub-finding left OUT of scope (split recommended, per
  the open question) — no code touched for it.
- Post-build agents: test-runner green (2414/2358/0, test_geofence 106/106);
  docs-updater added `get_static` documentation to `helpers/settings.md` and a
  "accepted-but-inert" note to `web_developer/account/admin_portal.md` (both
  verified accurate). Security-review gave the fix a clean bill of health
  (complete for both keys, no fail-open, no re-bypass path) BUT found a
  **CRITICAL pre-existing sibling** outside this diff: `phone_register.py:102`
  reads `AUTH_PHONE_VERIFY_DEV_BYPASS_CODE` via DB-aware `settings.get` — a
  remotely-armable phone-verification bypass (same vector as ITEM-031, worse
  impact). Out of scope here; filed to `planning/inbox/`
  (`phone-verify-dev-bypass-code-db-settable.md`, P1) for separate scoping.

## Resolution
- closed: 2026-07-10
- branch: main
- files changed: CHANGELOG.md,docs/django_developer/account/api_keys.md,docs/django_developer/account/bouncer.md,docs/django_developer/account/geofence.md,docs/django_developer/account/group.md,docs/django_developer/core/decorators.md,docs/django_developer/core/middleware.md,docs/django_developer/core/mojo_model.md,docs/django_developer/helpers/settings.md,docs/django_developer/helpers/settings_reference.md,docs/django_developer/testit/Overview.md,docs/web_developer/account/admin_portal.md,docs/web_developer/account/custom_auth_models.md,docs/web_developer/account/group.md,docs/web_developer/shortlink/README.md,memory.md,mojo/apps/account/rest/group.py,mojo/apps/account/services/geofence/engine.py,mojo/helpers/test_mode.py,mojo/middleware/mojo.py,mojo/models/rest.py,planning/.next_id,planning/done/ITEM-028-post-api-group-member-invite-returns-a-raw-500-typ.md,planning/done/ITEM-029-add-explicit-auth-gates-to-the-permission-check-si.md,planning/done/ITEM-030-jsonfield-replace-bypasses-protected-json-perms-ma.md,planning/in_progress/ITEM-031-geofence-test-override-mojo-test-mode-are-db-redis.md,planning/inbox/phone-verify-dev-bypass-code-db-settable.md,tests/test_account/test_group_invite_anonymous.py,tests/test_account/test_group_protected_metadata.py,tests/test_geofence/test_override_file_only.py
- tests added: tests/test_geofence/test_override_file_only.py — 3 tests
  (DB GEOFENCE_TEST_OVERRIDE row ignored by engine resolution;
  conf-file override still honored; DB MOJO_TEST_MODE row does not enable
  test mode when the conf file says False)
