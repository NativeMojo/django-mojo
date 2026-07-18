---
id: DM-049
type: chore
title: UserLoginEvent.track() should also snapshot region_code (ISO 3166-2) alongside the region name
priority: P3
effort: XS
owner: backend
opened: 2026-07-16
depends_on: []
related: []
links: []
build_strategy: delegate
build_model: opus
---

# Snapshot region_code on UserLoginEvent

## What & Why
`UserLoginEvent` denormalizes geo from `GeoLocatedIP` at `track()`
(`mojo/apps/account/models/login_event.py:92-106`) but copies only the
subdivision NAME (`region="California"`) — `GeoLocatedIP.region_code`
(`US-CA`, `geolocated_ip.py:38`) is dropped. Downstream consumers that
compare login geo against code-based policy lists must maintain their own
name→code mapping (wmx_api's geolocation compliance report builder now
does exactly that — `apps/wmx/reports/services/builders/geolocation.py`
`_STATE_CODE_BY_NAME`, WMX-API-131).

Add a `region_code` column, populate it in `track()` from
`geo.region_code`, expose it in the RestMeta graphs. Historical rows stay
name-only (consumers keep their mapping for history); new rows become
directly joinable against ISO code lists.

## Acceptance Criteria
- [ ] `region_code` CharField (indexed, nullable) populated by `track()`.
- [ ] Included in `basic`/`list` graphs + CSV format if present.
- [ ] Migration + test (track() writes both name and code).

## Plan

**Build routing**: `build_strategy: delegate`, `build_model: opus` — rubric floor
is sonnet (XS chore, exact in-repo precedent), opus chosen by the user for the
first delegate-mode build.

### Goal
Copy `GeoLocatedIP.region_code` (ISO 3166-2, e.g. `"US-CA"`) onto `UserLoginEvent`
at `track()` time and expose it in the REST graphs (and thereby CSV export), so
downstream consumers can join login geo against code-based policy lists instead of
maintaining a name→code map (WMX-API-131).

### Context — what exists
- `mojo/apps/account/models/login_event.py`
  - Field block (13–42): `region = models.CharField(max_length=100, db_index=True,
    null=True, blank=True)` at line 23, between `country_code` (22) and `city` (24).
    No `region_code` today.
  - `RestMeta` (44–79): `SEARCH_FIELDS = ['ip_address', 'country_code', 'region',
    'city']` (49). `region` appears in all three `GRAPHS` field lists — `basic`
    (53), `list` (60), `default` (70). No `FORMATS` key.
  - `track()` (84–148): inits `region = None` (96); copies `region = geo.region`
    (103) when a `GeoLocatedIP` row exists for `request.ip`; passes `region=region`
    into `cls.objects.create(...)` (128). Metrics block (138–146) keys off the
    region NAME.
- `mojo/apps/account/models/geolocated_ip.py:34-38` — the source field (mirror it
  exactly):
  ```python
  region = models.CharField(max_length=100, db_index=True, null=True, blank=True)
  # ISO 3166-2 subdivision code, e.g. "US-FL". Populated lazily on refresh()
  # from providers that expose it (MaxMind, ip-api, ipstack). For geofence DSL
  # region matching.
  region_code = models.CharField(max_length=10, db_index=True, null=True, blank=True)
  ```
- CSV export needs no extra work once graphs are updated: `on_rest_list_response`
  (`mojo/models/rest.py:847-859`) checks `RestMeta.FORMATS` (absent here) then
  falls to `get_rest_meta_graph(["basic", "default"])` — first-match
  (`mojo/models/rest.py:98-107`) → `basic`'s `fields` list verbatim.
- Filtering needs no work: `on_rest_list_filter` (`mojo/models/rest.py:1005`)
  derives exact-match filters from real model fields — `?region_code=US-CA` works
  as soon as the column exists.
- Migrations: latest is `mojo/apps/account/migrations/0046_geolocatedip_whitelisted_until.py`.
  Exact precedent for this change: `0042_geolocatedip_region_code.py` — a single
  `AddField` of the identical field type on the sibling model.
- Production call site: `jwt_login()` (`mojo/apps/account/rest/user.py:665-666`)
  → `UserLoginEvent.track(...)`. No other code constructs rows.
- Tests: `tests/test_account/test_login_event.py` — `setup_login_event` (7–50)
  creates `opts.geo_ip` (28–36: `country_code="US", region="California"`, no
  `region_code`); `test_track_creates_event_with_geo` (53–72) asserts each
  denormalized field incl. `event.region == "California"` (66);
  `test_unknown_ip_creates_event_with_null_geo` (150–163) asserts
  `event.region is None` (161).

### Changes — what to do
1. `mojo/apps/account/models/login_event.py`
   - Add after line 23:
     ```python
     # ISO 3166-2 subdivision code, e.g. "US-CA" (see GeoLocatedIP.region_code)
     region_code = models.CharField(max_length=10, db_index=True, null=True, blank=True)
     ```
   - `track()`: add `region_code = None` beside line 96; `region_code =
     geo.region_code` beside line 103; `region_code=region_code,` in the
     `create(...)` call beside line 128.
   - `RestMeta.GRAPHS`: insert `'region_code'` immediately after `'region'` in all
     three field lists (`basic` 53, `list` 60, `default` 70).
2. Migration: run `bin/create_testproject` after the model edit (it runs
   makemigrations + migrate). Do NOT hand-author the file. Expected result:
   `0047_userloginevent_region_code.py` with one `AddField` mirroring 0042's
   shape; verify the generated file and dependency chain before committing.
3. `tests/test_account/test_login_event.py`
   - `opts.geo_ip` fixture (28–36): add `region_code="US-CA",`.
   - `test_track_creates_event_with_geo`: after the `region` assertion (66) add
     `assert event.region_code == "US-CA", f"Expected region_code US-CA, got {event.region_code}"`.
   - `test_unknown_ip_creates_event_with_null_geo`: after the `region is None`
     assertion (161) add
     `assert event.region_code is None, f"Expected None region_code, got {event.region_code}"`.
4. Docs (both tracks):
   - `docs/django_developer/account/login_events.md`: add a `region_code` row to
     the Fields table after `region` (`CharField(10)` — ISO 3166-2 subdivision
     code from GeoLocatedIP, nullable, indexed); add `region_code` to the `list`
     row of the Graphs table (the `default` row says "All list fields + …" and
     inherits).
   - `docs/web_developer/account/login_events.md`: add `"region_code": "US-CA",`
     after `"region"` in both JSON examples (`graph=list` and `graph=default`);
     add a `region_code` query-param row (exact-match filter, e.g. `US-CA`).
5. `CHANGELOG.md`: add a `**chore**` entry (DM-049) at the top of the current
   rolling block, matching the style of recent DM entries.

### Design decisions
- Field params mirror `GeoLocatedIP.region_code` exactly (`max_length=10,
  db_index=True, null=True, blank=True`) — proven precedent; max ISO 3166-2
  length is 6 chars so 10 is ample.
- Include in the `default` graph too (AC names only basic/list): `default`
  already carries `region`; omitting the code there would be an inconsistency in
  the fullest graph.
- No `FORMATS` block: CSV inherits `basic`'s fields (see Context), so the graph
  edit satisfies the CSV criterion for free.
- `SEARCH_FIELDS` untouched: the stated use case is exact-match joining, already
  covered by the generic `?region_code=` filter; `search=` is free-text and not
  the use case.
- No backfill/data migration — ticket explicitly keeps historical rows name-only.
- **Non-goal**: metrics slugs and `is_new_region` semantics stay keyed on the
  region NAME (renaming slugs would break downstream metric consumers).
- **Non-goal**: `GeoLocatedIP.geolocate()`'s subnet-fallback
  (`mojo/apps/account/models/geolocated_ip.py:781`) copies `region` but not
  `region_code` when creating a row from a subnet match — pre-existing gap in the
  SOURCE model; do not fix it here. Consequence: some new login events may
  legitimately have `region` set but `region_code=None`.
- **Non-goal**: aggregation endpoints (`mojo/apps/account/rest/login_event.py:49`
  — `logins/summary` / `logins/user`) keep drilling down by region NAME.

### Edge cases & risks
- No `GeoLocatedIP` row for the IP → `region_code` stays `None`, identical to
  `region`/`city`/lat/long today. No new code path.
- `geo.region_code is None` while `geo.region` is set (older cached rows,
  providers without subdivision codes, subnet-fallback rows) → event gets
  name-only. Expected; field is nullable.
- Migration number may differ from 0047 if something else lands first —
  `bin/create_testproject` resolves it; verify the generated dependency chain,
  don't hardcode.

### Tests
Run `bin/run_tests --agent -t test_account.test_login_event` (plus the default
suite baseline per `.claude/rules/build-baseline.md`).
- Extend `test_track_creates_event_with_geo`: `region_code == "US-CA"` alongside
  `region == "California"`.
- Extend `test_unknown_ip_creates_event_with_null_geo`: `region_code is None`
  alongside `region is None`.

### Docs
Both tracks as listed in Changes 4; CHANGELOG per Changes 5.

### Open questions
None.

## Notes

- Baseline (`bin/run_tests --agent`, default suite, before any edit): total 2537 /
  passed 2481 / failed 0 / skipped 56. All green — `failures: []`. Opt-in modules
  `test_incident` (243) and `test_security` (82) skipped (require `--extra slow`),
  out of scope for the default baseline. Any failure after this change is mine.

## Resolution
- closed: YYYY-MM-DD
- branch:
- files changed:
- tests added:
