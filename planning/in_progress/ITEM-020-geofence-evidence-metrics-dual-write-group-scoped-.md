---
# id is assigned by /scope on pickup — leave it blank
id: ITEM-020
type: feature
title: Geofence evidence metrics — dual-write group-scoped accounts (group-<id>) alongside global
priority: P2
effort: S
owner: backend
opened: 2026-07-08
depends_on: []
related: [ITEM-017]
links: []
---

# Geofence evidence metrics — dual-write group-scoped accounts alongside global

## What & Why

ITEM-017's evidence plane records geofence metrics (`geofence:blocks`,
`geofence:blocks:country:{CC}`, `geofence:blocks:region:{code}`,
`geofence:exempt`) with the default `account="global"` only
(`mojo/apps/account/services/geofence/evidence.py::_record_block_metrics`).
Consumer portals chart per-tenant metrics with `account="group-<id>"`
(established convention across mverify/wmx dashboards) — for geofence
that account is always empty, so per-group block charts are impossible.

Owner ruling (2026-07-08): add group-based metrics, `group-<id>` style.
The group is already in hand at record time (block metadata carries
`group_id/group_name` when present).

## Acceptance Criteria

- [ ] When a group is associated with the decision, block/exempt recording
      dual-writes the **base** slugs (`geofence:blocks`, `geofence:exempt`)
      to `account=f"group-{group.id}"` in addition to `global`. Global
      recording is unchanged (platform dashboards keep working).
- [ ] **Cardinality guard**: per-country/per-region suffixed slugs stay
      global-only unless measured cheap — do not create a
      groups × countries × regions key cross-product by default. If /scope
      decides to include them per-group, justify the key growth.
- [ ] No-group decisions record exactly as today (global only).
- [ ] Tests extend `tests/test_geofence/evidence_plane.py`: group present →
      both accounts incremented; no group → global only; suffixed slugs
      unchanged.
- [ ] Doc touch: `docs/web_developer/account/geofence.md` metrics note
      gains the group-account line so portal builders know both exist.

## Plan

### Goal

When a geofenced request carries a group, dual-write the **base** evidence
metrics (`geofence:blocks`, `geofence:exempt`) to `account=f"group-{group.pk}"`
alongside the unchanged global records; country/region suffixed slugs stay
global-only.

### Context — what exists

**Recording site** — `mojo/apps/account/services/geofence/evidence.py`:

- `report_block(request, decision, scope)` (`evidence.py:33`) →
  `_report_block` (`evidence.py:41`), which gates on `blocked = decision.allowed is False`
  and calls `_record_block_metrics(decision)` (`evidence.py:44`). `request` is in
  scope there but **not** threaded into the metrics function.
- `_record_block_metrics(decision)` (`evidence.py:154-166`), current body:

  ```python
  def _record_block_metrics(decision):
      """Aggregate counters — recorded for EVERY block, including deduped ones.
      Mirrors the firewall:blocks pattern (geolocated_ip.block)."""
      try:
          metrics.record("geofence:blocks", category="geofence")
          cc = decision.get("country_code")
          if cc:
              metrics.record(f"geofence:blocks:country:{cc}", category="geofence")
          rc = decision.get("region_code")  # ISO 3166-2, already country-prefixed (US-WA)
          if rc:
              metrics.record(f"geofence:blocks:region:{rc}", category="geofence")
      except Exception as exc:
          logit.error("geofence", f"block metrics failed: {exc}")
  ```

- `report_exempt(request, decision, scope=None)` (`evidence.py:65-89`) records
  `metrics.record("geofence:exempt", category="geofence")` at `evidence.py:69`,
  deliberately **before** the `_dedupe_wins` early-return (metrics count every
  occurrence; dedupe only gates events).
- All records use the implicit default `account="global"`.

**Where the group is** — the decision objict carries NO group fields
(`_build_decision`, `engine.py:275-294`: allowed/reason/ip/country/region/abuse/…).
The group is **`request.group`**: a `Group` instance or `None`, resolved by the
REST dispatcher (`mojo/decorators/http.py:74-111`) from the `group` (int) or
`group_uuid` params **before** the view/decorator chain runs (`http.py:116`).
Default is `request.group = None` (`mojo/middleware/mojo.py:31`). The enforcement
decorator (`mojo/decorators/geofence.py:52-84`) already passes
`group=getattr(request, "group", None)` into `GeoFenceEngine.check` and calls
`evidence.report_block/report_exempt(request, decision, scope)` — so the group
that participated in the decision is exactly `request.group`, available at both
entry points. Evidence fires from the decorator for real enforcement only;
`/api/geo/check` and simulate emit nothing (by design, ITEM-017).

**Metrics API** — `mojo/apps/metrics/redis_metrics.py:70-94`:

```python
def record(slug, when=None, count=1, category=None, account="global",
           min_granularity="hours", max_granularity="years", timezone=None,
           expires_at=None, disable_expiry=False):
```

Reader: `metrics.fetch_values(slugs, granularity=..., account="global")`
(`redis_metrics.py:142`). Keys per (account, slug): one counter per granularity
per time bucket, hash-tagged per account, e.g.
`{mets:group-42}:mets:group-42::geofence|blocks:hr:2026-07-08T14`, plus small
per-account index sets (`mets:<account>:slugs`, `:c:<category>`, `:cats`).
TTLs (`utils.py:17-24`): hours 3d, days/weeks 360d, **months/years never expire**.

**Established convention** — hand-built `f"group-{group.pk}"`; no shared helper
exists anywhere. Record-side precedents: `mojo/apps/account/models/member.py:154`
(`member_activity_day`) and the global+group dual-record idiom in
`@md.endpoint_metrics` (`mojo/decorators/limits.py:462-479`). Read side already
works with zero changes: `GET /api/metrics/fetch` (`mojo/apps/metrics/rest/base.py:126-174`)
gates group accounts via `_check_group_account_permission`
(`mojo/apps/metrics/rest/helpers.py:8-23`) — a group member holding
`view_metrics`/`metrics` on that group can read `account="group-<id>"`; parent
groups aggregate children read-side via `child_kind` fanout
(`fetch_group_fanout`, `helpers.py:94-208`). There is **no record-side fanout**
(metrics-parent-group-fanout shipped fetch-side only) — none needed here.

**Tests** — `tests/test_geofence/evidence_plane.py`: `_login_attempt(opts, **header_kwargs)`
(`:30-42`) posts `/api/auth/login` with test-mode headers from
`tests/test_geofence/_helpers.py::headers()` (defaults `cache_ttl=0` — nothing
cached, no decision-cache poisoning); `_blocks_metric()` (`:45-52`) reads the
current-hour `geofence:blocks` via `metrics.fetch_values`; the template test is
`test_block_metrics_count_deduped` (`:203-216`, count-delta asserts). Today no
evidence test sends a group, so `request.group is None` throughout the module.

**Docs** — `docs/django_developer/account/geofence.md:241-264` ("Evidence Plane —
Incident Events + Metrics") is the only place the metric slugs are documented
(global implied). `docs/web_developer/account/geofence.md` has **no metrics
section at all** — the AC's "metrics note" must be added, not amended. Fetch
request shape for the web doc: crib from `docs/web_developer/metrics/metrics.md`.

### Changes — what to do

1. **`mojo/apps/account/services/geofence/evidence.py`** (only code file):
   - `_report_block` (`:44`): thread the group —
     `_record_block_metrics(decision, getattr(request, "group", None))`.
   - `_record_block_metrics(decision, group=None)`: append **after** the region
     record, inside the existing try (new code strictly after the established
     global writes, so a failure in it can't skip them):

     ```python
     if group is not None:
         # base slug only — per-group country/region would cross-product keys
         metrics.record("geofence:blocks", category="geofence",
                        account=f"group-{group.pk}")
     ```
   - `report_exempt`: right after the global record (`:69`), before the dedupe
     early-return (metrics must count deduped occurrences too):

     ```python
     group = getattr(request, "group", None)
     if group is not None:
         metrics.record("geofence:exempt", category="geofence",
                        account=f"group-{group.pk}")
     ```

2. **`tests/test_geofence/evidence_plane.py`** — extend (see Tests).

3. **Docs + CHANGELOG** (see Docs).

No model changes, no migrations, no settings — `bin/create_testproject` not needed.

### Design decisions

- **Group source = `request.group` only; no `request.user.org` fallback** (unlike
  `endpoint_metrics`). The dual-write must attribute blocks to the group whose
  context the engine actually evaluated (`geofence.py:55` passes `request.group`);
  geofence blocks are mostly pre-auth (login), and attributing a block to an org
  that didn't participate in the decision would mis-chart tenant dashboards.
  This also satisfies the AC "no-group decisions record exactly as today".
- **Hand-build `f"group-{group.pk}"`** — matches every existing call site
  (`member.py:154`, `limits.py:466`); no helper exists and two call sites don't
  justify inventing one (KISS). `.pk` == `.id`.
- **Suffixed slugs stay global-only** (cardinality guard, AC #2): one
  (slug, account) pair costs ~480 live keys in year one (~72 hourly + ~360 daily
  + ~51 weekly, TTL'd; months/years **permanent**, +13/yr forever). Base slugs
  add a bounded 2 × ~480 keys/group/yr. Per-group country (~200 values) ×
  region (thousands) would be an unbounded groups × geography cross-product with
  a permanent months/years residue — rejected. Tenants get per-group totals;
  geographic breakdown stays platform-wide.
- **Keep `category="geofence"` on the group-account records** so the slug lands
  in the group account's category index (`mets:<account>:c:geofence`) and stays
  discoverable by the portal/assistant metrics tooling — same as global.
- **No record-side parent fanout** — parent dashboards already aggregate child
  groups at read time via `child_kind` (`fetch_group_fanout`).
- **Cost**: one extra `metrics.record()` (its own small redis pipeline) per
  group-scoped block/exempt — negligible on a 403 path.

### Edge cases & risks

- **No group** → guard `is not None`; behavior byte-identical to today.
- **`lookup_failed` fail-open allows** record no block metrics today (gated by
  `blocked` at `evidence.py:42-44`) — unchanged.
- **Cache-hit decisions** still emit evidence (decorator-level, ITEM-017
  invariant); `request.group` is per-request, so group attribution is correct on
  cache hits too.
- **Redis/metrics failure**: swallowed by the existing try/excepts (`nothing in
  this module may raise into the request path`). Worst case the group write
  fails after global succeeded — acceptable; a redis outage fails both anyway.
- **Group later deleted**: `group-<pk>` metric keys persist, same as every other
  group metric (`member_activity_day`, endpoint metrics); pks aren't reused.
- **ApiKey confinement**: the dispatcher already 403s an api_key not allowed for
  the resolved group (`http.py:80-81,109-111`) before the view runs; reading
  group accounts is already permission-gated (`helpers.py:8-23`). No new exposure.
- **Test hygiene** (memory, ITEM-017): never prime cached denies under the shared
  `(127.0.0.1, no-group)` key — tests keep the `headers()` default `cache_ttl=0`
  and scope their group via `group_uuid`; `finally:` invalidates the group's
  decision cache and deletes the group.

### Tests (`tests/test_geofence/evidence_plane.py`)

- Add a general reader next to `_blocks_metric` (leave `_blocks_metric` alone):

  ```python
  def _metric_value(slug, account="global", granularity="hours"):
      from mojo.apps import metrics
      resp = metrics.fetch_values([slug], granularity=granularity, account=account)
      try:
          return int((resp.get("data") or {}).get(slug) or 0)
      except (TypeError, ValueError):
          return 0
  ```
- Extend `_login_attempt` with an optional body merge:
  `def _login_attempt(opts, extra=None, **header_kwargs)` →
  `payload = {"username": ..., "password": ...}; if extra: payload.update(extra)`.
- **`test_block_metrics_group_account`** — group present → both accounts; no
  group → global only; suffixed slugs stay off the group account:
  1. `_clear_evt("region_not_allowed")`; create
     `Group.objects.create(name=f"Geofence Evidence {uuid4().hex[:8]}", is_active=True)`;
     `account = f"group-{grp.pk}"`.
  2. Blocked login WITH group: `_login_attempt(opts, extra={"group_uuid": str(grp.uuid)},
     geo=GEO_US, system_rules={"country": {"in": ["US"]}, "region": {"in": ["US-FL"]}})`
     → assert 403; assert `_metric_value("geofence:blocks", account=account,
     granularity="days") == 1` (fresh per-run group → exact; days granularity to
     dodge hour-boundary flakes); assert global `geofence:blocks` delta ≥ 1
     (capture before/after via `_metric_value`); assert
     `_metric_value("geofence:blocks:country:US", account=account, granularity="days") == 0`
     (suffixed slugs global-only).
  3. Blocked login WITHOUT group → 403; group-account counter still `== 1`
     (no-group writes never touch the group account).
  4. `finally:` `from mojo.apps.account.services.geofence import cache as gf_cache;
     gf_cache.invalidate_group(grp.pk)`; `grp.delete()`.
- **`test_exempt_metrics_group_account`** — `_clear_evt("ip_allowlisted")`; own
  fresh group; allowlisted pass that would block, WITH group:
  `_login_attempt(opts, extra={"group_uuid": str(grp.uuid)}, geo=GEO_RU,
  system_rules={"country": {"in": ["US"]}}, allowlist=[f"{IP}/32"])` → assert 200;
  assert group-account `geofence:exempt` (days) `== 1`; assert global delta ≥ 1.
  Same `finally` cleanup. (A bare group with no metadata doesn't alter the login
  flow; dispatcher resolves it from the body param before the decorator runs.)
- Existing tests (`test_block_metrics_count_deduped` etc.) double as the
  no-group regression — they must pass unchanged.
- Run: `bin/run_tests --agent -t test_geofence.evidence_plane` (baseline first,
  per `.claude/rules/build-baseline.md`).

### Docs

- `docs/django_developer/account/geofence.md` (~`:259-263`): in the Evidence
  Plane metrics paragraph, add: when the request carries a group
  (`request.group` via `group`/`group_uuid`), the base slugs `geofence:blocks`
  and `geofence:exempt` are **also** recorded under `account="group-<id>"`
  (same convention as `member_activity_day`/endpoint metrics); country/region
  breakdown slugs stay global-only to avoid a groups × geography key
  cross-product.
- `docs/web_developer/account/geofence.md`: add a short **Metrics** section
  (none exists): the four slugs; base slugs available per-tenant under
  `account=group-<id>` (requires group `view_metrics`/`metrics` permission);
  fetched via `GET /api/metrics/fetch` (crib the exact param shape from
  `docs/web_developer/metrics/metrics.md`); country/region breakdowns are
  global-only.
- `CHANGELOG.md`: entry under the rolling block — geofence base evidence metrics
  dual-write to `group-<id>` accounts when a group is on the request.

### Open questions

None.

## Notes

- **Baseline (2026-07-08, pre-edit)**: `bin/run_tests --agent` GREEN —
  total 2330 / passed 2274 / failed 0 / skipped 56 (opt-in `test_incident` 243
  + `test_security` 82 skipped as usual; smaller in-module skips are
  environmental). No pre-existing failures; anything red after this build is
  attributable to it.
- Consumer rider: mverify_portal ITEM-014 (scoped) ships its charts with
  `account:'global'` and a note to flip/augment to the active group's
  account once this lands — a one-line widget change portal-side, plus an
  optional global/group toggle.
- Same convention as the rest of the platform's metrics
  (`account='group-<id>'`, e.g. VerifyDashboardPage) — no new account
  naming scheme.
