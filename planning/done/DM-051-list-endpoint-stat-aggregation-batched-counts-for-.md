---
id: DM-051
type: feature
title: "List-endpoint stat aggregation — batched counts for web-mojo TableView stat strips"
priority: P2
effort: M
owner: backend
opened: 2026-07-18
depends_on: []
related: [nativemojo/web-mojo#WM-037]
links: []
---

# List-endpoint stat aggregation — batched counts for web-mojo TableView stat strips

## What & Why
web-mojo is adding a `stats:` option to TableView (WM-037 in web-mojo's
pipeline): a strip of KPI chips above heavy admin tables showing live counts
under the table's current filters — e.g. an Incidents table showing
"Open 12 · High 3 · Stale 5", where each count is the row count that a named
filter bundle *would* return, and clicking a chip applies that bundle.

The frontend needs a backend aggregation contract: given a list endpoint and
the request's current filter params, return counts for N additional named
filter bundles **in one batched request** (one round trip per table, not one
per chip).

Frontend consumer details live in web-mojo `WM-037`; this item owns the
API contract and implementation. WM-037 is blocked on this item.

## Acceptance Criteria
- [ ] A list endpoint can be asked for counts of N named filter bundles in a
      single request, evaluated on top of (AND-ed with) the request's normal
      filter params — so counts always describe what the caller would see.
- [ ] Works through the standard CRUD list endpoints with query params — no
      separate admin-scoped endpoints (per REST conventions); permissions
      apply exactly as they do to the list itself.
- [ ] Response shape is stable and documented (e.g. counts keyed by the
      caller-supplied bundle keys), designed together with the web-mojo side
      so `WM-037` can consume it directly.
- [ ] Bounded cost: cap on number of bundles per request; counts are simple
      filtered `count()`s (no arbitrary aggregation language).
- [ ] Graceful behavior for endpoints/models that don't support it (clean
      error or capability flag — web-mojo degrades to label-only chips).
- [ ] Tests covering: counts respect base filters, permission scoping,
      bundle cap, unsupported-endpoint path.
- [ ] API docs updated.

## Repro — bugs only
1.
- Expected:
- Actual:

## Plan

### Goal
Extend `_mode=count` on the standard RestMeta list pipeline to accept `_stats` —
N named filter bundles evaluated as counts AND-ed onto the request's fully
scoped+filtered queryset — one batched request per TableView stat strip (WM-037).

### Context — what exists
**The feature is a small enhancement to an existing aggregation surface — do not
build a new one.**

- **List pipeline** — `mojo/models/rest.py`:
  - `on_rest_handle_list` (rest.py:569-627) applies permission scoping BEFORE
    anything else: happy path `rest_check_permission(request, "VIEW_PERMS")` →
    `on_rest_list(request)` (:580); owner fallback filters
    `{OWNER_FIELD: request.user}` (:587-593); group fallback filters
    `{group__in: get_groups_with_permission(...)}` (:598-605); unauthenticated
    → 401; `MOJO_REST_LIST_PERM_DENY` (default True, :21) → 403, legacy False →
    `on_rest_list_response(request, cls.objects.none())` (:628) which **bypasses
    the aggregation branch entirely** (safe: no stats key, no leak).
  - `on_rest_list` (rest.py:800-833): sets `request.QUERY_PARAMS =
    request.GET.copy()` (:815), applies the `request.group` tenant filter
    (:816-825), then `on_rest_list_filter` (:826),
    `on_rest_list_date_range_filter` (:827), then the aggregation branch:
    ```python
    mode = request.DATA.get("_mode")            # rest.py:828
    if mode and mode != "list":
        from mojo.models.rest_aggregation import on_rest_list_aggregate
        return on_rest_list_aggregate(cls, request, queryset)
    ```
    The queryset at that branch is already permission-, tenant-, filter-, and
    date-range-scoped — exactly the base every bundle must AND with.
  - `on_rest_list_filter` (rest.py:1004-1126): iterates
    `request.QUERY_PARAMS.items()`; the **parse loop at :1029-1115** builds
    `filters`/`excludes` dicts. It skips `_`-prefixed keys (:1030-1034 —
    framework-reserved namespace, so `_stats`/`_mode` never hit field
    filtering), skips `reserved_keys = ["start", "size", "download_format",
    "dr_start", "dr_end", "limit", "offset"]` (:1023), maps `.`→`__`
    (:1036-1037), has a relation branch (:1052-1067) and an attr branch with
    partial-date expansion (:1068-1087). Operators: `__in` (comma-split),
    `__not` → exclude, `__not_in` → `__in`-exclude, `__isnull` (bool coerce),
    literal `"null"` → None; date-component lookups int-coerced via
    `normalize_rest_value` (:1113-1115, fn at :960-1002 — already tolerates
    typed values: bools preserved, datetime passthrough, int passthrough).
    Application: `on_rest_list_search(request, queryset)` then
    `.filter(**filters)` / `.exclude(**excludes)` (:1120-1126).
    **There is no field whitelist on this path** — a field is filterable if it
    is a model field / attr. Bundles inherit exactly this (deliberate — see
    Design decisions).
- **Aggregation surface** — `mojo/models/rest_aggregation.py`:
  - Entry `on_rest_list_aggregate(cls, request, queryset)` (:80-103): validates
    `_mode` ∈ `VALID_MODES` (:28, `("list","count","top","distinct","summary",
    "histogram")`) else `me.ValueException` → 400 (:82-85); dispatches
    `_agg_count(cls, request, queryset)` (:110-111) which returns
    `{"count": queryset.count()}`; entry adds `took_ms` rounded to 10ms
    (timing-oracle damping) + `status: True` (:99-103); returns
    `mojo.helpers.response.JsonResponse`.
  - Caps pattern (:65-73): `def _cap(name, default): return
    int(settings.get_static(name, default))`; module-level
    `TOP_CAP = _cap("MOJO_REST_AGG_TOP_CAP", 100)`, `DISTINCT_CAP`,
    `HISTOGRAM_CAP`. Overridable in tests via `th.server_settings(...)` (server
    reload re-imports the module).
  - `_validate_field`/`SENSITIVE_FIELDS`/`AGGREGATION_FIELDS` (:330-400) guard
    the **value-exposing** modes (top/distinct/summary/histogram). Counts are
    not value-exposing — bundles deliberately do NOT use this guard.
- **`request.DATA`** (`mojo/helpers/request_parser.py`): a query param
  `?_stats={...}` arrives as a raw JSON **string** (`_process_query_params`
  :48-56 — no JSON parsing of query values); a JSON POST body
  `{"_stats": {...}}` arrives as a real **dict** (`_process_json_data` :76-95).
  Both shapes must be accepted. Cross-source duplicates: later source wins
  whole-value (DM-024) — no special handling needed.
- **Error convention**: raise `mojo.errors.ValueException(msg)` (defaults
  code=400); the dispatcher (`mojo/decorators/http.py:158-183`) converts
  `MojoException` to a JSON 400. This is how every `_mode` guard fails today.
- **Prior-art tests**: `tests/test_account/test_aggregation_permissions.py`
  (default suite — the scoping-inheritance proof to mirror: two users/two
  groups, group-stamped rows, per-caller counts); `tests/test_models/
  date_filtering.py` (default suite — drives `mojo.apps.shortlink.models.
  ShortLink` + `mojo.apps.account.models.User` through `opts.client`; mirror
  its seeding/login/cleanup style). `tests/test_incident/
  test_event_aggregation.py` is the richest aggregation example **but
  test_incident is opt-in `--full` — do NOT put the new tests there.**

### Changes — what to do
1. `mojo/models/rest.py` — **pure refactor**: extract the parse loop
   (:1029-1115) into a new classmethod
   `build_rest_filters(cls, request, params)` → returns `(filters, excludes)`.
   `params` is any query-param-shaped mapping (`request.QUERY_PARAMS` or a
   `_stats` bundle dict). Keep the `__rest_field_names__` lazy-init
   (:1026-1027) inside the helper (both callers need it).
   `on_rest_list_filter` becomes: `filters, excludes =
   cls.build_rest_filters(request, request.QUERY_PARAMS)` + the existing
   search/filter/exclude application (:1120-1126) — behavior unchanged.
   One tolerance fix inside the extracted loop: the `__in`/`__not_in`
   comma-split becomes `if isinstance(value, str): value = value.split(",")`
   so JSON-native lists pass through (query-param strings: unchanged behavior).
2. `mojo/models/rest_aggregation.py` — enhance count mode:
   - Module-level `STATS_CAP = _cap("MOJO_REST_AGG_STATS_CAP", 12)` next to the
     other caps (:71-73).
   - New `_parse_stats_bundles(request)`: read `request.DATA.get("_stats")`;
     absent/empty → `None`; `str` → strict stdlib `json.loads` in
     try/except `(TypeError, ValueError)` → `me.ValueException("_stats must be
     a JSON object mapping bundle names to filter dicts")` (NEVER
     `objict.from_json(..., ignore_errors=True)` — DM-023). Validate
     (structural errors are LOUD 400s): parsed value is a dict; every key is a
     non-empty str with `len <= 64`; every value is a dict;
     `len(bundles) <= STATS_CAP` else 400 (`f"_stats supports at most
     {STATS_CAP} bundles"`).
   - New `_count_bundles(cls, request, queryset, bundles)`: for each
     `(name, params)`: `try:` `filters, excludes =
     cls.build_rest_filters(request, params)`; `qs =
     queryset.filter(**filters)`; `if excludes: qs = qs.exclude(**excludes)`;
     `stats[name] = qs.count()`; `except (me.MojoException, FieldError,
     ValidationError, ValueError, TypeError): stats[name] = None` (soft
     per-bundle failure — a bad chip must not 400 the strip). `FieldError`/
     `ValidationError` from `django.core.exceptions`.
   - `_agg_count` becomes:
     ```python
     def _agg_count(cls, request, queryset):
         body = {"count": queryset.count()}
         bundles = _parse_stats_bundles(request)
         if bundles is not None:
             body["stats"] = _count_bundles(cls, request, queryset, bundles)
         return body
     ```
   - Wire contract (documented, consumed by WM-037):
     `GET <list-endpoint>?<current filters>&_mode=count&_stats=<urlencoded
     JSON>` (or POST body dict) →
     `{"count": <total under current filters>, "stats": {"open": 12,
     "broken": null}, "took_ms": 20, "status": true}`.
     Capability rule: **`stats` key present ⟺ supported** (old servers serve
     `_mode=count` without it → label-only chips, zero console errors).
3. Docs + CHANGELOG — see Docs.
4. Tests — see Tests.

### Design decisions
- **Enhance `_mode=count`, not a new list-envelope `stats` key, not a new
  `_mode=stats`** (owner ruling 2026-07-18, overturning the item's original
  sketch): keeps the whole feature inside the aggregation surface that owns the
  `_` namespace; zero changes to envelope/serializer/`on_rest_list` flow; the
  rows fetch never waits on N count queries (envelope piggyback would delay row
  rendering behind chip counts); softest degradation (a new mode would 400 on
  every old-server table load; `_mode=count` just lacks the key).
- **Bundles parse through the SAME machinery as URL filter params**
  (`build_rest_filters`): guarantees chip count == post-click table count by
  construction (same operators, same coercion, same silent-skip of
  unknown/reserved/`_`-prefixed keys — a typo'd bundle field drops the filter
  exactly as the equivalent URL param would), and adds **zero new security
  surface**: any bundle count is already obtainable today as a plain list
  request with those params, reading envelope `count`. Do NOT route bundles
  through `_validate_field` — that guard exists for value-exposing modes;
  applying it here would make chips reject filters the table itself accepts.
- **N small `count()` queries (N ≤ cap), not one
  `aggregate(Count("id", filter=Q(...)))`** — filtered aggregates go wrong on
  multi-valued joins (`.exclude()` subquery semantics ≠ `~Q`; join-row
  inflation needs `distinct=True` care), and the per-bundle count is exactly
  the query the table runs post-click. Bounded: 1 + N ≤ 13 cheap COUNTs.
- **Structural errors loud (400), per-bundle errors soft (`null`)** —
  malformed `_stats` JSON / non-dict bundle / oversize name / cap exceeded =
  frontend bug, fail loud; one failing bundle must not break the table fetch
  (WM-037 renders that chip label-only on `null`).
- **`_stats` outside `_mode=count` is inert** — already skipped by the filter
  parser (`_` prefix); no other mode reads it; plain list ignores it. No
  conflict rule needed. Document as count-mode-only.
- **No per-model opt-in/opt-out** — zero new surface ⇒ nothing to gate;
  available wherever `_mode=count` is.
- **Empty bundle `{}` allowed** → equals base `count` (the "All" chip idiom).

### Edge cases & risks
- Query-param `_stats` = JSON string; body `_stats` = dict —
  `_parse_stats_bundles` handles both (`isinstance(str)` → strict parse).
- JSON-native bundle values (int/bool/list/null): `normalize_rest_value`
  already tolerates typed values; the `__in` split gains the isinstance-str
  guard; JSON `null` value → Python None → Django `exact=None` ≡ isnull (same
  as the URL `"null"` sentinel).
- Multi-value/relation lookups, partial dates, date-component lookups: all
  inherited verbatim from the shared parser — no bundle-specific handling.
- Scoping inheritance is free: the `_mode` branch runs after owner/group/tenant
  narrowing; the two-tenant test proves bundle counts never leak cross-tenant.
- Legacy `MOJO_REST_LIST_PERM_DENY=False` deny-path (rest.py:628) bypasses
  aggregation entirely → empty envelope, no stats key, no leak.
- `took_ms` 10ms rounding (timing-oracle damping) inherited from the entry
  point — applies to the whole 1+N batch.
- Cap override in tests: `th.server_settings(MOJO_REST_AGG_STATS_CAP=2)` works
  because server reload re-imports the module (same pattern as the existing
  DISTINCT_CAP test).
- If seeding groups in tests: `Group.objects.create()` leaves `uuid=None` —
  call `grp.get_uuid()` before driving `group_uuid` params (known trap).

### Tests
New **default-suite** module `tests/test_models/list_stats.py` (seed/auth/clean
style from `tests/test_models/date_filtering.py` using ShortLink; two-tenant
scoping proof mirroring `tests/test_account/test_aggregation_permissions.py`).
Every assert carries a message; setup deletes its rows before creating
(long-lived DB). Scenarios:
1. **Consistency contract**: base URL filter + `_mode=count&_stats={...}` —
   each stat equals the `count` of the equivalent plain list request with the
   bundle's params merged in (assert literally against a second request).
2. **Scoping**: two users/two groups, group-stamped rows — tenant-A caller's
   bundle counts cover only tenant-A rows; unauthenticated → 401;
   no-perm authed caller → 403 (default `MOJO_REST_LIST_PERM_DENY=True`).
3. **Cap**: 13 bundles → 400; under `th.server_settings(
   MOJO_REST_AGG_STATS_CAP=2)`: 3 bundles → 400, 2 → 200.
4. **Malformed (loud)**: `_stats=not-json` → 400; `_stats='["a"]'` → 400;
   bundle value non-dict (`{"open": "x"}`) → 400; 65-char name → 400.
5. **Soft per-bundle**: one good bundle + `{"created__month": "abc"}` → 200,
   good counted, bad → null.
6. **Operators through bundles**: `__in` (comma-string AND JSON list), `__not`,
   `__isnull`, JSON-native int equality.
7. **Inert elsewhere**: plain list with `_stats` → 200, envelope has NO `stats`
   key; `_mode=distinct&_stats=...` → distinct response unaffected;
   `_mode=stats` still 400s (unknown mode, unchanged).
8. **Empty bundle** `{}` → equals base `count`; empty `_stats={}` → 200 with
   `stats: {}`.
9. **Refactor regression**: full default suite green (date_filtering.py et al.
   exercise the extracted `build_rest_filters` on the URL path).

### Docs
- `docs/web_developer/core/aggregation.md` — extend the `_mode=count` section:
  `_stats` param (JSON object: name → filter dict), GET (urlencoded) + POST
  body examples, response shape `{count, stats, took_ms, status}`,
  per-bundle-null degradation, cap + `MOJO_REST_AGG_STATS_CAP`, capability rule
  (`stats` key absent = unsupported), bundles use identical filter semantics to
  list params (link `filtering.md`; field filters only — `search`/`dr_*`/
  reserved keys inside a bundle are ignored exactly as on the URL).
- `docs/web_developer/README.md` — Common Query Parameters table (:59-72): add
  `_stats` row ("with `_mode=count`: named filter bundles → counts").
- `docs/django_developer/core/mojo_model.md` — aggregation section (:136-232):
  `_stats` behavior, `MOJO_REST_AGG_STATS_CAP` in the caps table,
  `build_rest_filters` as the shared parse helper (extension point).
- `CHANGELOG.md` — feature entry.
- web-mojo WM-037 Notes: contract pinned at scope time (already done).

### Open questions
- none — all decisions above carry owner sign-off (2026-07-18 scope session).

## Notes
- **Build baseline (2026-07-18, default suite `bin/run_tests --agent`, pre-edit):**
  total 2523, passed 2467, **failed 0**, skipped 56 — all-green. Opt-in
  `--full` modules (test_incident 243, test_security 82) not run. Every failure
  after this change is attributable to DM-051.
- Filed from web-mojo's EPIC WM-038 scoping session (2026-07-18). ~~Once this
  item gets its `DM-###` at /scope pickup, update web-mojo `WM-037`'s
  `depends_on`~~ — done at /scope pickup (2026-07-18): WM-037 `depends_on` now
  reads `nativemojo/django-mojo#DM-051`.
- ~~Contract sketch to evaluate at /scope (not binding): request param like
  `_stats={...}` on the list endpoint; response gains `stats: {...}` alongside
  the normal paginated payload, or a `size=0`-style counts-only variant.~~
  **Superseded at /scope (owner ruling 2026-07-18):** don't graft a `stats` key
  onto the list envelope — enhance the existing aggregation surface instead.
  Final contract: `_mode=count&_stats={...}` → `{count, stats, took_ms,
  status}` (see Plan). Chips are a separate debounced call; rows fetch never
  waits on counts; old servers degrade by key absence.
- **Post-build (2026-07-18):** full default suite green — 2537 total, 2481
  passed, **0 failed**, 56 skipped (+14 new list_stats.py tests). test-runner
  surfaced 2 flaky `test_jobs` failures on one run, verified as a pre-existing
  time-of-day flake (minute-`:59` fallback in `test_scheduled_task.py`, untouched
  by DM-051; clean on isolated re-run) → filed
  `planning/inbox/test-scheduled-task-minute-59-boundary-flake.md`.
- **Security-review WARNING fixed (commit f4d46c84):** `_count_bundles` didn't
  catch `django.db.Error`, so a bundle failing at `.count()` execution time
  (invalid regex / overflow / unsupported lookup) 500'd the whole strip +
  fired a level-12 incident — breaking the fail-soft contract. Added
  `django.db.Error`, narrowed the catch from the `MojoException` base to
  `ValueException` (a permission-class error must surface, not null), added a
  debug log. Regression: `test_stats_db_execution_error_is_null_not_500`.
- **Known follow-ons (not blockers):** the pre-existing list-filter sensitive-
  field count oracle (`planning/inbox/list-filter-sensitive-field-count-oracle.md`)
  — DM-051 adds no new value exposure but batches up to 12 probes/request
  (rate-limit amortization noted on that item). Fix belongs at the shared
  `build_rest_filters` choke point (covers list + `_mode=count` + `_stats`).

## Resolution
- closed: 2026-07-18
- branch: main
- files changed: .claude/agents/security-review.md,.claude/rules/git.md,.claude/skills/build/SKILL.md,.claude/skills/request/SKILL.md,.claude/skills/scope/SKILL.md,AI_DEV.md,CHANGELOG.md,CLAUDE.md,bin/create_testproject,docs/django_developer/account/api_keys.md,docs/django_developer/account/auth.md,docs/django_developer/account/disable_lifecycle.md,docs/django_developer/account/geofence.md,docs/django_developer/account/group.md,docs/django_developer/core/decorators.md,docs/django_developer/core/middleware.md,docs/django_developer/core/mojo_model.md,docs/django_developer/core/permissions.md,docs/django_developer/core/rate_limiting.md,docs/django_developer/filevault/README.md,docs/django_developer/helpers/request.md,docs/django_developer/helpers/settings_reference.md,docs/django_developer/logging/incidents.md,docs/django_developer/realtime/README.md,docs/django_developer/realtime/architecture.md,docs/django_developer/rest/permissions.md,docs/django_developer/security/README.md,docs/django_developer/security/abuse_hardening.md,docs/django_developer/security/maestro_board.md,docs/web_developer/README.md,docs/web_developer/account/api_keys.md,docs/web_developer/account/authentication.md,docs/web_developer/account/bouncer.md,docs/web_developer/account/custom_auth_models.md,docs/web_developer/account/geofence.md,docs/web_developer/account/group.md,docs/web_developer/account/user.md,docs/web_developer/account/user_self_management.md,docs/web_developer/core/aggregation.md,docs/web_developer/core/filtering.md,docs/web_developer/core/request_response.md,docs/web_developer/filevault/README.md,docs/web_developer/logging/reporting_events.md,docs/web_developer/realtime/websocket.md,docs/web_developer/security/README.md,docs/web_developer/security/maestro_board.md,docs/web_developer/security/rate_limits.md,memory.md,mojo/apps/account/models/api_key.py,mojo/apps/account/models/group.py,mojo/apps/account/models/user.py,mojo/apps/account/rest/bouncer/assess.py,mojo/apps/account/rest/bouncer/event.py,mojo/apps/account/rest/bouncer/views.py,mojo/apps/account/rest/geofence.py,mojo/apps/account/rest/group.py,mojo/apps/account/rest/oauth.py,mojo/apps/account/rest/passkeys.py,mojo/apps/account/rest/sms.py,mojo/apps/account/rest/totp.py,mojo/apps/account/rest/user.py,mojo/apps/account/services/auth_config.py,mojo/apps/account/services/disable.py,mojo/apps/account/services/geofence/__init__.py,mojo/apps/account/services/geofence/enforcement.py,mojo/apps/account/services/geofence/evidence.py,mojo/apps/filevault/rest/data.py,mojo/apps/filevault/rest/file.py,mojo/apps/incident/asyncjobs.py,mojo/apps/incident/cronjobs.py,mojo/apps/incident/handlers/event_handlers.py,mojo/apps/incident/migrations/0032_maestroboard_maestroboardlink.py,mojo/apps/incident/models/__init__.py,mojo/apps/incident/models/maestro_board.py,mojo/apps/incident/models/maestro_board_link.py,mojo/apps/incident/models/rule.py,mojo/apps/incident/models/ticket.py,mojo/apps/incident/rest/__init__.py,mojo/apps/incident/rest/event.py,mojo/apps/incident/rest/maestro_board.py,mojo/apps/incident/rest/maestro_webhook.py,mojo/apps/incident/services/__init__.py,mojo/apps/incident/services/maestro_sync.py,mojo/apps/realtime/asgi.py,mojo/apps/realtime/handler.py,mojo/decorators/auth.py,mojo/decorators/geofence.py,mojo/decorators/http.py,mojo/decorators/limits.py,mojo/helpers/crypto/vault.py,mojo/helpers/request.py,mojo/models/rest.py,mojo/models/rest_aggregation.py,planning/.config,planning/.next_id,planning/_template.md,planning/confirmed/DM-049-userloginevent-track-should-also-snapshot-region-c.md,planning/done/DM-001-render-allowlisted-extra-registration-fields-promo.md,planning/done/DM-002-step-up-recent-authentication-gate-for-sensitive-o.md,planning/done/DM-003-register-page-enter-on-phone-otp-field-fires-step-.md,planning/done/DM-004-sign-in-alternate-method-button-row-overflows-clip.md,planning/done/DM-005-phone-register-one-wrong-sms-code-burns-the-sessio.md,planning/done/DM-006-sms-sign-in-with-an-unrecognized-number-dead-ends-.md,planning/done/DM-007-full-test-suite-is-flaky-content-guard-false-posit.md,planning/done/DM-008-phone-signup-may-fail-to-sign-in-an-existing-accou.md,planning/done/DM-009-get-remote-ip-trusts-client-supplied-x-forwarded-f.md,planning/done/DM-010-websocket-ip-resolver-trusts-client-spoofable-sour.md,planning/done/DM-011-ip-storage-fields-assume-ipv4-non-null-ipv6-trunca.md,planning/done/DM-012-auth-middleware-500s-on-a-malformed-authorization-.md,planning/done/DM-013-management-command-to-create-initial-users-admins.md,planning/done/DM-014-var-dev-server-conf-overrides-config-dev-server-co.md,planning/done/DM-015-configurable-outbound-webhook-signature-header-use.md,planning/done/DM-016-group-user-has-permission-crashes-on-apikey-identi.md,planning/done/DM-017-geofence-config-evidence-plane-editable-system-rul.md,planning/done/DM-018-audit-requires-perms-group-fallback-on-global-effe.md,planning/done/DM-019-self-minted-group-apikey-with-arbitrary-permission.md,planning/done/DM-020-geofence-evidence-metrics-dual-write-group-scoped-.md,planning/done/DM-021-geofence-hardening-opt-in-strict-compliance-enforc.md,planning/done/DM-022-member-readable-geofence-policy-events-group-scope.md,planning/done/DM-023-geofence-adjacent-settings-bypass-write-time-valid.md,planning/done/DM-024-same-key-in-query-string-json-body-merges-to-a-lis.md,planning/done/DM-025-dispatcher-numeric-group-resolution-skips-is-activ.md,planning/done/DM-026-github-oauth-login-on-the-bouncer-hosted-auth-page.md,planning/done/DM-027-group-rest-save-collapses-to-the-view-check-any-ac.md,planning/done/DM-028-post-api-group-member-invite-returns-a-raw-500-typ.md,planning/done/DM-029-add-explicit-auth-gates-to-the-permission-check-si.md,planning/done/DM-030-jsonfield-replace-bypasses-protected-json-perms-ma.md,planning/done/DM-031-geofence-test-override-mojo-test-mode-are-db-redis.md,planning/done/DM-032-rest-batch-save-skips-instance-level-permission-ch.md,planning/done/DM-033-fileman-initiated-uploads-can-t-be-completed-or-fk.md,planning/done/DM-034-oauth-login-drops-the-redirect-param-user-lands-on.md,planning/done/DM-035-field-action-level-permission-gates-omit-the-base-.md,planning/done/DM-036-apikey-set-permissions-silently-discards-non-dict-.md,planning/done/DM-037-apikey-validate-token-grants-group-context-without.md,planning/done/DM-038-rest-batch-save-ignores-can-update-can-create-flag.md,planning/done/DM-039-get-api-group-pk-member-resolves-touches-any-group.md,planning/done/DM-040-incident-maestroboard-push-link-tickets-into-a-rem.md,planning/done/DM-041-config-driven-item-id-prefixes-dm-canonical-workfl.md,planning/done/DM-042-authenticated-abuse-doom-loop-hardening-default-pe.md,planning/done/DM-043-enforce-login-flow-geofencing-after-credential-ver.md,planning/done/DM-044-auth-decorators-clobber-each-other-s-security-regi.md,planning/done/DM-045-harden-the-dm-037-identity-gates-enforce-the-inact.md,planning/done/DM-046-unguarded-self-active-user-is-superuser-in-user-py.md,planning/done/DM-047-filevault-endpoints-fetch-vaultfile-vaultdata-by-p.md,planning/done/DM-048-group-get-member-for-user-parent-walk-ignores-each.md,planning/future/group-member-deny-timing-side-channel.md,planning/in_progress/DM-051-list-endpoint-stat-aggregation-batched-counts-for-.md,planning/inbox/apikey-group-context-ignores-group-is-active.md,planning/inbox/apikey-parent-key-inactive-descendant-one-way-door.md,planning/inbox/apikey-suspension-residual-surfaces.md,planning/inbox/batch-ignores-can-update-can-create-flags.md,planning/inbox/filevault-sharing-token-hardening.md,planning/inbox/group-me-member-endpoint-oracle-touch.md,planning/inbox/is-request-user-positive-marker.md,planning/inbox/maestro-webhook-replay-timestamp.md,planning/inbox/member-perms-ignore-group-is-active.md,planning/inbox/phone-verify-dev-bypass-code-db-settable.md,planning/inbox/serializer-reverse-onetoone-graph-emits-empty-list.md,planning/inbox/test-security-full-suite-red.md,scripts/intake.sh,scripts/ready.sh,testit/client.py,tests/test_account/test_bouncer_limits.py,tests/test_account/test_disable_kill_switch.py,tests/test_account/test_geolocated_ip_aggregation.py,tests/test_account/test_group_me_member_oracle.py,tests/test_assistant/28_test_fk_perm_check.py,tests/test_email/email_change.py,tests/test_filevault/3_test_rest_scoping.py,tests/test_geofence/config_plane.py,tests/test_geofence/post_auth.py,tests/test_geofence/registry.py,tests/test_global_perms/apikey_group_inactive.py,tests/test_limits/__init__.py,tests/test_limits/api_throttle.py,tests/test_limits/block_dedup.py,tests/test_limits/traffic_concentration.py,tests/test_maestro_board/__init__.py,tests/test_maestro_board/test_maestro_rest.py,tests/test_maestro_board/test_maestro_service.py,tests/test_middleware/group_param_is_active.py,tests/test_models/batch_feature_flags.py,tests/test_models/list_stats.py,tests/test_oauth/oauth.py,tests/test_realtime/connection_limits.py,tests/test_verification/verification.py,uv.lock
- tests added: `tests/test_models/list_stats.py` (14) — counts+AND-with-base,
  operator coverage (incl. JSON-native `__in` list guard), absent/empty/dict-form
  stats key, soft-null per bundle (build-time AND DB-execution error), structural
  400s, cap default + `MOJO_REST_AGG_STATS_CAP` boundary, HTTP envelope +
  query-param JSON-string parsing, `_stats` inert without `_mode=count`,
  owner-scope isolation.
