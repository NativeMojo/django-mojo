---
# id is assigned by /scope on pickup — leave it blank
id:
type: feature
title: Geofence hardening — opt-in strict/compliance enforcement posture
priority: P1
effort:
owner:
opened: 2026-06-30
depends_on: []
related: [ITEM-009, ITEM-010]
links: []
---

# Geofence hardening — opt-in strict/compliance enforcement posture

## What & Why
The geofence engine defaults **fail-open** with several allow-by-default paths (a deliberate
"resilience" choice). That's unsafe for deployments that **require** geofence for jurisdictional
compliance — where "can't verify location" must mean **deny/step-up, not allow**. Now that the IP
input is trustworthy (ITEM-009 HTTP + ITEM-010 WS read the proxy-authoritative `X-Real-IP`),
enforcement is finally meaningful; this item makes the enforcement itself compliance-grade —
**opt-in**, so existing fail-open deployments are unaffected.

Four allow-by-default paths (verified in `mojo/apps/account/services/geofence/engine.py`):
bypass permission (199-205), no-rules fast path (211-213), lookup-failure fail-open (228-234 —
`GEOFENCE_FAIL_CLOSED` default `False`), private-IP allow (236-242 — `GEOFENCE_ALLOW_PRIVATE_IPS`
default `True`). Plus a reliability blocker: the geo/threat lookup makes **live external fetches per
request** (MaxMind web service + uncached Tor exit-list + blocklist.de, 3-5s timeouts) — so naively
flipping fail-closed trades "attacker slips through on failure" for "real users denied on every
provider hiccup." (This is also the separately-noted 7-second-404 latency.)

## Acceptance Criteria
- [ ] An **opt-in strict posture** that, when enabled, fails **closed** on lookup failure, **denies**
      private/reserved IPs, and **denies when geofencing is enabled but no rules are configured** (no
      silent allow-all). Default-off — existing deployments keep current fail-open behavior.
- [ ] The strict posture is settable **per-group** (some groups strict, others permissive),
      overriding the global default.
- [ ] The Tor exit-list and blocklist.de are **cached with periodic refresh** (no per-request
      external fetch), so fail-closed is operationally safe (a provider hiccup does not deny real
      users) and per-request geo latency drops.
- [ ] **Operational visibility:** a way to list who holds `bypass_geofence`; it's documented as a
      high-privilege grant.
- [ ] Docs (both tracks) + `CHANGELOG.md` updated; the compliance posture and the deployment
      requirements are documented.

## Investigation (feature — what exists / what changes / constraints)

### A. Strict posture (settings)
- **Exists:** engine reads settings via `_bool_setting_with_header` / `_int_setting_with_header`
  (`mojo/helpers/settings/helper.py:63-79`) → `settings.get(name, default, kind=...)`, which chains
  test-header override → DB-backed `Setting.resolve()` (group/parent/global, `account/models/setting.py:168-216`)
  → Redis → `django.conf`. Fail-open defaults in `engine.py`: `GEOFENCE_ENABLED` True (194),
  `GEOFENCE_FAIL_CLOSED` False (231), `GEOFENCE_ALLOW_PRIVATE_IPS` True (239), no-rules allow (211-213).
- **Change:** add an opt-in `GEOFENCE_STRICT_POSTURE` (bool, default False); when True → fail-closed +
  deny-private + require-rules. **HARD CONSTRAINT:** do NOT change the global defaults of
  `FAIL_CLOSED`/`ALLOW_PRIVATE_IPS` unconditionally — that silently breaks existing fail-open
  deployments. Strict must be opt-in. Test headers (`X-Mojo-Test-Geofence-*`) already override.

### B. Per-group posture
- **Exists:** group geofence rules live in `group.metadata["geofence"]` (JSONField, `group.py:41`),
  read by `_group_rules()` (`engine.py:90-94`) as `md.get("geofence")`. Write-time validation pattern
  exists for `auth_config` (`group.py:616-622`). Parent-chain inheritance via `get_top_most_parent()`.
- **Change (Option 1, recon-preferred):** extend `metadata["geofence"]` with `strict` / `fail_closed`
  / `allow_private_ips` / `require_rules` sub-keys, validated in `Group.on_rest_pre_save`; the engine
  honors them over the global default. (Option 2: per-group `Setting` entries — more scalable, adds a
  DB call.)
- **Constraint:** `metadata["geofence"]` currently **IS** the DSL rules dict; restructuring to
  `{rules: ..., strict: ...}` is a backwards-compat concern (existing groups store the rules dict
  directly) → needs a read-shim or data migration.

### C. Threat-list caching
- **Exists (uncached):** Tor exit-list `mojo/helpers/geoip/detection.py:34` (`requests.get(..., timeout=3)`
  per lookup); blocklist.de `mojo/helpers/geoip/threat_intel.py:174` (`timeout=5` per check — the code
  comment literally says "cache this list and refresh periodically"); AbuseIPDB per-IP `threat_intel.py:135`.
- **Reuse:** the incident app's **IPSet** pattern — `IPSet` model (`incident/models/ipset.py`) stores
  bulk lists in `.data`/`.cidrs`, refreshed via `refresh_from_source()`, scheduled weekly via
  `@schedule` in `incident/cronjobs.py:46-51` → `incident/asyncjobs.refresh_ipsets`. Existing caches:
  `GeoLocatedIP` TTL (90 days) + geofence decision cache (Redis 300s, `geofence/cache.py`).
- **Change:** create IPSet records for `tor_exits` + `blocklist_de`; replace the per-request fetches in
  `detect_tor` / `check_blocklist_de` with cached IPSet queries; add refresh cron (Tor weekly OK;
  blocklist.de updates ~hourly → shorter TTL, e.g. 6h).
- **Constraint / open Q:** **layering** — `mojo/helpers/geoip/*` is a helper; `IPSet` is an
  incident-app model. A helper importing an app model is a layering inversion — confirm acceptable or
  site the cache elsewhere. Tor format is `ExitAddress {ip} {ts}` (parse on refresh, store IPs only).

### D. bypass_geofence visibility
- **Exists:** check at `engine.py:201` (`user.has_permission("bypass_geofence")`). Permissions live in
  `User.permissions` JSONField (`account/models/user.py:492-543`: `has_permission`/`add_permission`/
  `remove_permission`). There is **no** Django permission model and **no** existing "who holds perm X"
  query. Permission add/remove is already audited via `user.log()`.
- **Change:** add an admin-gated REST endpoint (e.g. `GET geo/bypass-holders`) listing users holding
  the perm (`permissions__bypass_geofence=True` JSONField lookup — PostgreSQL supports it), optional
  group filter. Document `bypass_geofence` as a high-privilege grant.
- **Enforcement surface (reference):** `@md.requires_geofence` decorator (`mojo/decorators/geofence.py:20-68`,
  403 on deny) and `GET /api/geo/check` (`account/rest/geofence.py:18`).

### Open questions (for scope)
- Strict posture: a single `GEOFENCE_STRICT_POSTURE` switch (sets fail-closed + deny-private +
  require-rules) vs. operators flipping individual flags?
- Per-group config shape (Option 1 `metadata["geofence"]` sub-dict vs Option 2 `Setting` model) +
  migration of existing `metadata["geofence"]` rule dicts.
- Threat-list cache: reuse the incident `IPSet` model despite the helper→app layering, or a
  geoip-local cache? Refresh cadence (blocklist.de hourly upstream).
- **deny vs step-up:** the decorator hard-denies (403). Is MFA **step-up** in scope, or deny-only?
  (Affects `decorators/geofence.py` and the decision contract.)
- **Likely SPLIT:** (C) caching is a self-contained chore that also fixes the 7s-latency item and
  de-risks fail-closed — could land first/independently. (B) per-group is a feature. (A)+(D) are smaller.
  Recommend scope decides the split.

## Plan
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

_Write a complete, self-contained design here — enough that a fresh session can
`/build` it cold, without re-deriving anything. Fill every subsection._

### Goal
[One sentence.]

### Context — what exists
[Paths, `file:line`, current behavior, helpers/patterns to reuse.]

### Changes — what to do
1. `path` — [exact change and why]

### Design decisions
- [decision] — [rationale; alternatives rejected]

### Edge cases & risks
- [case] — [how it's handled]

### Tests
- [scenario] -> `test file`

### Docs
- `doc` — [what changes]

### Open questions
- [blocking unknowns, or "none"]

## Notes
[Scratch space — anything not part of the plan.]

## Resolution
- closed: YYYY-MM-DD
- branch:
- files changed:
- tests added:
