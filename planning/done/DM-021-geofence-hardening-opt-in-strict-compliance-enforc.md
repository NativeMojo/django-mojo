---
# id is assigned by /scope on pickup — leave it blank
id: DM-021
type: feature
title: Geofence hardening — opt-in strict/compliance enforcement posture
priority: P1
effort: L
owner: backend
opened: 2026-06-30
depends_on: []
related: [DM-009, DM-010, DM-017]
links: []
---

# Geofence hardening — opt-in strict/compliance enforcement posture

## What & Why
The geofence engine defaults **fail-open** with several allow-by-default paths (a deliberate
"resilience" choice). That's unsafe for deployments that **require** geofence for jurisdictional
compliance — where "can't verify location" must mean **deny/step-up, not allow**. Now that the IP
input is trustworthy (DM-009 HTTP + DM-010 WS read the proxy-authoritative `X-Real-IP`),
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
- [x] **Operational visibility:** a way to list who holds `bypass_geofence`; it's documented as a
      high-privilege grant. — **Already delivered by DM-017** (`GET /api/geo/bypass_holders`,
      `mojo/apps/account/rest/geofence.py:270-301`). Out of scope here; do not rebuild.
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

### Goal
Add an opt-in strict/compliance geofence posture — one bundled switch, global
(`GEOFENCE_STRICT_POSTURE`) with a per-group tri-state override
(`Group.metadata["geofence_strict"]`) — that fails closed on lookup failure,
denies private IPs, and denies when no rules are configured; and make the
Tor/blocklist.de threat lists cached-with-periodic-refresh (IPSet-backed,
refresh-only, never firewall-synced) so enforcement is operationally safe.

### Context — what exists (verified 2026-07-08, post-DM-017)

The Investigation section above predates DM-017 and its line refs are stale.
Current state:

**Engine** — `mojo/apps/account/services/geofence/engine.py` (483 lines).
`GeoFenceEngine.check(request, group=None, user=None, scope=None)`
(engine.py:350). Pipeline: kill-switch `GEOFENCE_ENABLED` default True
(engine.py:356-359) → `bypass_geofence` perm (engine.py:361-368) →
`_system_rules(request)` / `_group_rules(group)` (engine.py:370-371; helpers at
100-106 / 109-113) → **no-rules fast path** `_both_empty` → allow `no_rules`
(engine.py:374-376, helper 116-117) → cache lookup, TTL `GEOFENCE_CACHE_TTL`
default 300 (engine.py:378-386) → **IP allowlist step 4b** `_ip_allowlisted`,
full exemption with shadow evaluation for evidence (engine.py:388-396; matcher
128-192; `_allowlisted_decision` 327-343) → `_evaluate()` (engine.py:399).

`_evaluate(request, ip, system, group_r, scope=None, geo=_UNSET)`
(engine.py:436-475) — shared by check/simulate/shadow, never caches:
- lookup failure (engine.py:443-452): `fail_closed = GEOFENCE_FAIL_CLOSED`
  (default False, 445-446) **OR** `scope in GEOFENCE_FAIL_CLOSED_SCOPES`
  (447-451, from DM-017). `lookup_failed` decisions are NEVER cached (caller
  check at engine.py:403 — scope isn't in the cache key).
- private IP (engine.py:454-458): `allow_priv = GEOFENCE_ALLOW_PRIVATE_IPS`
  default True.
- rule eval system→group (engine.py:460-472), passed (474-475).

Settings are read via `_bool/_int/_list_setting_with_header` wrappers
(engine.py:73-97) honoring test-mode-gated `X-Mojo-Test-Geofence-*` headers.
`_build_decision` (engine.py:275-294) builds the GeoDecision objict;
`_DETAIL_MAP` reason→detail (engine.py:297-313).
`simulate(request, ip=None, geo=None, group=None, scope=None)`
(engine.py:407-434) has its own `_both_empty` fast path (426-427).

**Decorator** — `mojo/decorators/geofence.py:26-84`. Passes `scope` to
`check()` (57-62); deny → `evidence.report_block` + 403 (74-82); fail-open
`lookup_failed` allow and exercised allowlist exemptions also emit (63-72).
No changes needed here — evidence hooks already cover every outcome.

**Evidence** — `mojo/apps/account/services/geofence/evidence.py`.
`_block_level` (evidence.py:123-130): `rule_invalid`→7, fail-open
lookup_failed→6, abuse reason or fail-closed scope→5, else 3.

**Setting model** — `mojo/apps/account/models/setting.py`.
`GEOFENCE_KEYS = ("GEOFENCE_SYSTEM_RULES", "GEOFENCE_ALLOWLIST")`
(setting.py:86) is the extension point: `on_rest_pre_save` →
`_validate_geofence_value` (setting.py:89-127) rejects group-scoped rows and
validates by key; `save()`/`delete()` → `_invalidate_geofence_decisions` →
`gf_cache.invalidate_all()` for global rows with a key in `GEOFENCE_KEYS`
(setting.py:288-308). Adding a key to the tuple wires validation routing AND
cache invalidation automatically.

**Group model** — `mojo/apps/account/models/group.py`.
`metadata["geofence"]` is still the **raw rules DSL dict** (unchanged by
DM-017 — no sub-keys possible without a compat break). `on_rest_pre_save`
validates `metadata["geofence"]` via `validate_rule` (group.py:633-650);
`on_rest_saved` unconditionally invalidates the group's cached decisions on
every non-created save (group.py:656-671) — a new metadata key gets cache
invalidation for free.

**Config-plane REST** — `mojo/apps/account/rest/geofence.py` (DM-017).
`GET geo/rules` returns a `posture` dict (geofence.py:142-148) and an optional
`group` section (153-160); `POST geo/simulate` (199-217). All config endpoints
use `@md.requires_global_perms(...)` (global grants only — see DM-017's
security-review note).

**Threat lookups** — current reality (corrects this item's premise):
- `detect_tor(ip)` — `mojo/helpers/geoip/detection.py:18-46`: live
  `requests.get(TOR_EXIT_NODE_LIST_URL, timeout=3)` (line 34) downloading the
  full exit list **per call**. Called from `geolocate_ip`
  (`mojo/helpers/geoip/__init__.py:172`) — i.e. it IS on the geofence hot path,
  but only when a `GeoLocatedIP` row is created/refreshed (90-day TTL,
  `geolocated_ip.py:801-802`), not on every request. URL/flag:
  `mojo/helpers/geoip/config.py:14,17` (`GEOIP_ENABLE_TOR_DETECTION` default
  True; URL default `https://check.torproject.org/exit-addresses`).
- `check_blocklist_de(ip)` — `mojo/helpers/geoip/threat_intel.py:163-186`:
  live `requests.get('https://lists.blocklist.de/lists/all.txt', timeout=5)`
  (line 174; enabled by `THREAT_INTEL_BLOCKLIST_DE_ENABLED` default True,
  threat_intel.py:27). **NOT on the automatic geofence path** — reached only
  via `check_threats=True` flows: the `refresh`/`threat_analysis` REST actions
  (`geolocated_ip.py:738-742`) and explicit `perform_threat_check` calls
  (`geolocated_ip.py:283,293`). Still worth caching (each admin threat check
  downloads a ~30k-line list), but it does not gate request latency.
- Tor exit-list format: `ExitAddress {ip} {ts}` lines (parsed at
  detection.py:37-41).

**IPSet pattern** — `mojo/apps/incident/models/ipset.py`. Bulk CIDR lists in a
`TextField` (`data`, line 44; `cidrs` property 75-80; `set_data` 82-85).
`refresh_from_source()` (129-152) dispatches by `source` to `_fetch_ipdeny`
(154-175) / `_fetch_abuseipdb` (177-190), both `timeout=30`.
`SOURCE_CHOICES` (13-17), `KIND_CHOICES` (6-11). **CRITICAL**: `sync()`
(111-122) broadcasts the list to every instance's **kernel firewall** (real
network-level blocking), and the weekly cron `refresh_ipsets`
(`incident/cronjobs.py:46-51` → `incident/asyncjobs.py:278-293`) auto-refreshes
**and syncs** every `is_enabled=True, source != "manual"` row. `sync()` is a
no-op when `is_enabled=False` (ipset.py:113-114) — that guard is what keeps
cache-only rows out of the firewall.

**Layering precedent** — `mojo/helpers/geoip/threat_intel.py:48` already
lazily imports `mojo.apps.incident.models.event.Event` inside a function, so a
helper→incident-app import for IPSet reads follows an established pattern.

**Related open bug** — `planning/inbox/geofence-settings-write-validation-gap.md`:
`kind="bool"` coercion of unrecognized strings is truthy (`bool("typo") is
True`) and `GEOFENCE_FAIL_CLOSED_SCOPES`/`GEOFENCE_ALLOW_PRIVATE_IPS` lack
write validation. That item owns the general fix; THIS item must write-validate
its own new key (`GEOFENCE_STRICT_POSTURE`) so it doesn't widen the gap. Note
the failure direction for strict is safe: garbage coercing to True means MORE
enforcement, not less.

**Tests** — `tests/test_geofence/` (`_helpers.py` has geo fixtures + a
`headers(...)` builder for the `X-Mojo-Test-Geofence-*` headers; parallel-safe
per-request style, no `th.server_settings`). In-process IPSet model tests
exist at `tests/test_incident/test_ipset.py` (no-network style: call the
method, catch the HTTP failure, assert persisted state).

### Changes — what to do

**Part A+B — strict posture (one bundled switch, global + per-group)**

1. `mojo/apps/account/services/geofence/engine.py`
   - Add `_strict_posture(request, group)` helper next to `_group_rules`
     (~engine.py:109): per-group override wins when present, else global —
     ```python
     def _strict_posture(request, group):
         if group is not None:
             md = getattr(group, "metadata", None) or {}
             override = md.get("geofence_strict")
             if override is not None:
                 return bool(override)
         return _bool_setting_with_header(
             request, "X-Mojo-Test-Geofence-Strict",
             "GEOFENCE_STRICT_POSTURE", False)
     ```
     New test header `X-Mojo-Test-Geofence-Strict` ("0"/"1") — document in the
     module docstring header table (engine.py:27-35).
   - `check()` (engine.py:350-405): compute `strict = _strict_posture(request,
     group)` after the bypass step; change the no-rules fast path condition
     (engine.py:375) to `if _both_empty(system, group_r) and not strict:` so a
     strict deployment with no rules falls through to cache/allowlist/evaluate;
     pass `strict=strict` into both `_evaluate` calls (shadow at 393, main
     at 399).
   - `simulate()` (engine.py:407-434): same — compute `strict`, apply the same
     fast-path condition change (426-427), pass `strict` through.
   - `_evaluate(..., strict=False)` (engine.py:436-475):
     - **New first step** (before geo resolution — cheap, no geoip call):
       `if strict and _both_empty(system, group_r): return
       _build_decision(False, "no_rules_strict", ip=ip, strict=True)`.
       Placement inside `_evaluate` (i.e. AFTER the allowlist step in
       `check()`) is deliberate: an allowlisted developer IP must still get in
       under strict, with the shadow pass recording
       `would_block=True, would_block_reason="no_rules_strict"`.
     - Lookup failure (443-452): extend the OR-chain —
       `fail_closed = fail_closed or strict` (after the existing scope check).
     - Private IP (454-458): `allow_priv = allow_priv and not strict`.
     - Stamp `strict_posture` on every decision `_evaluate` returns while
       strict is active, via a new `_build_decision(..., strict=False)` kwarg
       that sets `dec.strict_posture = strict` (engine.py:275-294) — evidence
       needs it for leveling, and geo/check / simulate callers see posture.
   - `_DETAIL_MAP` (engine.py:297-313): add `"no_rules_strict": "Geofencing is
     required but no rules are configured; access denied."`.
   - Caching: `no_rules_strict`, strict `private_ip`, and strict rule denials
     are deterministic → cacheable as today (invalidation below covers config
     flips). Strict `lookup_failed` denials stay uncached via the existing
     engine.py:403 guard — no change needed.

2. `mojo/apps/account/models/setting.py`
   - Add `"GEOFENCE_STRICT_POSTURE"` to `GEOFENCE_KEYS` (setting.py:86) —
     this auto-wires the group-scoped-row rejection (global-only; the engine
     reads this setting globally, per-group posture lives in Group.metadata)
     AND `invalidate_all()` on save/delete (setting.py:288-308).
   - `_validate_geofence_value` (setting.py:96-127): the current dispatch is
     `if GEOFENCE_SYSTEM_RULES ... else validate_allowlist` — convert to
     if/elif/else and add the strict branch: after the JSON-parse step,
     accept only a JSON boolean (`isinstance(parsed, bool)`), else
     `merrors.ValueException(f"{self.key} must be a JSON boolean
     (true/false)")`. This closes the `bool("typo") is True` write-path hole
     for the new key (see the settings-validation-gap bug for the general fix).

3. `mojo/apps/account/models/group.py`
   - `on_rest_pre_save` (group.py:633-650): after the geofence-rule block, add
     — `gf_strict = (self.metadata or {}).get("geofence_strict")`; if not None
     and not `isinstance(gf_strict, bool)` → `merrors.ValueException(
     "metadata.geofence_strict must be a boolean (true/false) or null to
     inherit the global posture")`. Runs on the post-merge metadata (REST JSON
     merge happens before pre_save). Clearing the override = writing `null`
     (merge sets the key to None; `_strict_posture` treats None as inherit).
   - No `on_rest_saved` change: group decision-cache invalidation is already
     unconditional on every update (group.py:656-671).

4. `mojo/apps/account/services/geofence/evidence.py`
   - `_block_level` (evidence.py:123-130): add `decision.get("strict_posture")`
     to the level-5 disjunction — any block under strict posture is a
     compliance-grade denial:
     `if decision.reason in _ABUSE_REASONS or decision.get("strict_posture")
     or _scope_fails_closed(request, scope): return 5`.
     (`rule_invalid`→7 is checked first and still wins.)
   - Module docstring level table: add the strict clause.

5. `mojo/apps/account/rest/geofence.py`
   - `on_geo_rules_get` posture dict (geofence.py:142-148): add
     `"strict_posture": settings.get("GEOFENCE_STRICT_POSTURE", False,
     kind="bool")`.
   - Group section (geofence.py:153-160): add
     `"strict_posture": (group.metadata or {}).get("geofence_strict")` (raw
     tri-state: None/true/false) and `"strict_posture_effective": <bool>`
     (resolved against the global) so the admin UI shows both the override and
     the outcome.
   - No new endpoints. `simulate` and `geo/check` need no signature changes —
     strict flows in via engine internals; both will surface
     `strict_posture` on the returned decision.

**Part C — threat-list caching (IPSet-backed, refresh-only)**

6. `mojo/apps/incident/models/ipset.py`
   - `SOURCE_CHOICES` (ipset.py:13-17): add `("tor", "Tor Exit List")` and
     `("blocklist_de", "blocklist.de")`.
   - `refresh_from_source()` dispatch (ipset.py:134-140): add
     `elif self.source == "tor": data = self._fetch_tor()` and
     `elif self.source == "blocklist_de": data = self._fetch_blocklist_de()`.
   - `_fetch_tor()`: GET `self.source_url` or the geoip config default
     (`from mojo.helpers.geoip.config import TOR_EXIT_NODE_LIST_URL` — app→
     helper import, always fine), `timeout=30`, parse `ExitAddress {ip} {ts}`
     lines → list of bare IPs (mirror detection.py:37-41). Extract the parsing
     into a pure `_parse_tor_exit_list(text)` module function so it's testable
     without HTTP.
   - `_fetch_blocklist_de()`: GET `self.source_url` or
     `https://lists.blocklist.de/lists/all.txt`, `timeout=30`, one IP per
     line, strip blanks/comments.
   - Class constants + bootstrap helper:
     ```python
     THREAT_CACHE_SETS = {
         "tor_exits": {"kind": "abuse", "source": "tor"},
         "blocklist_de": {"kind": "abuse", "source": "blocklist_de"},
     }
     @classmethod
     def ensure_threat_caches(cls): ...
     ```
     `get_or_create` each by name with `is_enabled=False` and
     `description="Cache-only list for geoip detection — do NOT enable;
     enabling would kernel-block every listed IP fleet-wide."` — **`is_enabled=
     False` is load-bearing**: it keeps these rows out of the weekly
     `refresh_ipsets` cron's refresh-AND-`sync()` path
     (asyncjobs.py:285 filters `is_enabled=True`) and out of the kernel
     firewall (`sync()` no-ops when disabled, ipset.py:113-114). Never flip
     the flag for these rows.

7. `mojo/apps/incident/cronjobs.py` + `mojo/apps/incident/asyncjobs.py`
   - New cron (cronjobs.py, next to `refresh_ipsets`):
     ```python
     # Every 6h — refresh the cache-only threat lists (tor_exits, blocklist_de)
     # used by geoip detection. refresh_from_source() ONLY — never sync():
     # these rows must never reach the kernel firewall.
     @schedule(minutes="30", hours="*/6")
     def refresh_threat_lists(force=False, verbose=False, now=None):
         jobs.publish(
             func="mojo.apps.incident.asyncjobs.refresh_threat_lists",
             payload={})
     ```
   - New asyncjob `refresh_threat_lists(job)` (asyncjobs.py, next to
     `refresh_ipsets` at 278): `IPSet.ensure_threat_caches()`, then for each
     of the two rows call `refresh_from_source()` (which persists data +
     `sync_error` itself) and `job.add_log` the outcome. **No `.sync()`
     call anywhere in this job.** One 6h cadence for both lists (blocklist.de
     updates ~hourly upstream; the Tor list changes continuously — 6h is fresh
     enough for both and one cron is simpler than two).

8. `mojo/helpers/geoip/detection.py`
   - Add a module-level cached-list reader:
     ```python
     def _cached_ip_set(name):
         """IPs from the IPSet-backed threat cache, or None when the row is
         missing/empty (fall back to the live fetch). Lazy app import —
         precedent: threat_intel.check_internal_threats."""
         try:
             from mojo.apps.incident.models.ipset import IPSet
             row = IPSet.objects.filter(name=name).first()
             if row is not None and row.data:
                 return set(row.cidrs)
         except Exception:
             return None
         return None
     ```
   - `detect_tor(ip)` (detection.py:18-46): before the live fetch, `cached =
     _cached_ip_set("tor_exits")`; if not None → `return ip_address in cached`.
     Live per-call fetch remains ONLY as the fallback for a fresh deploy
     before the first cron tick / an incident app not installed — no behavior
     regression, and the hot path stops re-downloading the list once the cache
     exists. Also replace the `print` at detection.py:44 with
     `logit.error` (core rule: no print-based logging in framework code —
     match the logit import style used elsewhere in helpers).
   - Note: `_cached_ip_set` does a single indexed `name=` lookup per call —
     acceptable, `detect_tor` only runs on GeoLocatedIP create/refresh
     (90-day TTL), not per request.

9. `mojo/helpers/geoip/threat_intel.py`
   - `check_blocklist_de(ip)` (threat_intel.py:163-186): same pattern —
     `cached = detection._cached_ip_set("blocklist_de")` (import the helper
     from detection.py); if not None → build the result dict from membership
     without the HTTP call; else keep the live fetch. Delete the "in
     production you'd cache this" comments (163-173) — they're now false.
     Replace the `print`s at threat_intel.py:161,184 with `logit.error` while
     in there.

No schema changes anywhere (IPSet rows are data, `metadata` is an existing
JSONField, settings are rows) → no `bin/create_testproject` needed.

### Design decisions
- **One bundled `GEOFENCE_STRICT_POSTURE` switch**, not three independent
  flags — "compliance posture" is a single stance; three toggles triple the
  config/docs/test surface and invite a half-strict misconfiguration. The
  existing granular flags (`GEOFENCE_FAIL_CLOSED`, `GEOFENCE_ALLOW_PRIVATE_IPS`,
  `GEOFENCE_FAIL_CLOSED_SCOPES`) remain for surgical control; strict ORs on
  top and never loosens anything.
- **Per-group override = flat `metadata["geofence_strict"]` sibling key**, NOT
  a sub-key of `metadata["geofence"]` — that key is (verified) still the raw
  rules DSL dict; nesting would break every existing group and `validate_rule`.
  Tri-state (absent/None = inherit global; true/false = explicit override in
  either direction) satisfies "some groups strict, others permissive."
  Rejected: per-group `Setting` rows (`_bool_setting_with_header` reads
  globally; DM-017 deliberately rejects group-scoped rows for geofence keys).
- **Strict composes with, never replaces, DM-017's scope map** — effective
  fail-closed = `GEOFENCE_FAIL_CLOSED OR scope∈GEOFENCE_FAIL_CLOSED_SCOPES OR
  strict`, exactly as the overlap-resolution note requires.
- **The strict no-rules deny lives in `_evaluate()`, after the allowlist step**
  — first drafted as an early return in `check()` before the allowlist, which
  would have locked out allowlisted developer/office IPs on strict deployments
  with unconfigured rules (the exact outage the allowlist exists to prevent).
  In `_evaluate` it also lands in the shadow pass for free, so exemption
  evidence records `would_block_reason="no_rules_strict"`.
- **Hard constraint honored**: global defaults of `GEOFENCE_FAIL_CLOSED` /
  `GEOFENCE_ALLOW_PRIVATE_IPS` / no-rules-allow are untouched; with
  `GEOFENCE_STRICT_POSTURE` unset and no group overrides, every decision is
  bit-for-bit what it is today (suite baseline must stay green).
- **Any block under strict posture is evidence level 5** (same tier as
  abuse-flag / fail-closed-scope blocks) via `decision.strict_posture` —
  simpler and more defensible than attributing which flag "caused" the deny;
  under a compliance posture every denial is compliance-grade. `rule_invalid`
  still escalates to 7.
- **Reuse IPSet for the threat caches, but `is_enabled=False` + a dedicated
  refresh-only cron** — the model/refresh/cron machinery is exactly right, but
  the existing weekly cron both refreshes AND `sync()`s enabled rows into the
  kernel firewall; naively adding tor/blocklist.de rows would silently
  firewall-block every Tor exit and blocklist.de IP fleet-wide (a policy
  decision nobody made). Disabled rows are inert to both the weekly cron
  (filters `is_enabled=True`) and `sync()` (no-ops when disabled). Rejected:
  a new geoip-local cache model (third bulk-list store, migration, no reuse)
  and Redis-only caching (lost on flush → thundering live fetches).
- **Helper→app lazy import for the cache read** — precedent already exists at
  `threat_intel.py:48` (imports incident's Event); the fallback path keeps
  geoip fully functional when the incident app isn't installed. This resolves
  the item's "layering inversion" open question: acceptable, with the lazy
  import + graceful fallback.
- **Corrected premise, recorded**: blocklist.de is NOT on the automatic
  geofence request path (only explicit `check_threats=True` flows reach it);
  only `detect_tor` gates GeoLocatedIP creation/refresh latency. Caching both
  is still right (reliability + admin-action latency + the Tor hot path), but
  the "7-second-404" fix is mostly the Tor half.
- **Bypass visibility (D) dropped** — built by DM-017
  (`GET /api/geo/bypass_holders`), verified live.

### Edge cases & risks
- **Strict + no rules + allowlisted IP** → allowed (`ip_allowlisted`), exempt
  evidence carries `would_block_reason="no_rules_strict"` — the allowlist is a
  full exemption by design (DM-017 owner ruling); covered by a test.
- **Strict + lookup failure** → deny, and the decision is NOT cached (existing
  engine.py:403 guard) — a transient provider outage on a strict deployment
  recovers on the next successful lookup rather than pinning a cached deny
  for `GEOFENCE_CACHE_TTL`. The Tor-cache half of this item reduces how often
  lookups fail in the first place.
- **Cached pre-flip decisions when strict is toggled**: global toggle → the
  `GEOFENCE_KEYS` save/delete hook `invalidate_all()`s; per-group toggle →
  `Group.on_rest_saved` already `invalidate_group()`s unconditionally. Shell
  writes of `group.metadata` bypass `on_rest_saved` (documented existing
  limitation, same as rules edits).
- **Garbage written to `GEOFENCE_STRICT_POSTURE`**: REST writes are rejected
  (JSON-boolean validation, change 2). A pre-existing/shell-written garbage
  string coerces truthy at read time — the failure direction is deny (more
  enforcement), never a silent allow. General coercion observability belongs
  to the `geofence-settings-write-validation-gap` bug.
- **Group metadata merge**: `geofence_strict` set via partial REST metadata
  writes merges as a scalar (no dict-merge surprise); clearing = write `null`.
  Validation runs post-merge in `on_rest_pre_save`.
- **`no_rules_strict` cached under (ip, group_id)**: deterministic and
  invalidated on any rules/posture write — safe. Strict `private_ip` denials
  likewise.
- **Threat-cache rows accidentally enabled by an operator** (REST `enable`
  action) → they'd enter the weekly firewall sync and kernel-block every
  listed IP. Mitigated by the explicit "do NOT enable" description on the row
  and docs; not hard-blocked in code (an operator with `manage_security` can
  already create arbitrary firewall sets — this adds no new capability).
  Called out in docs as the one sharp edge.
- **First deploy before the first cron tick / incident app absent** →
  `_cached_ip_set` returns None → live-fetch fallback, exactly today's
  behavior. No flag day.
- **blocklist.de list size (~30-40k lines)**: parsed into a set per
  `check_blocklist_de` call — only on explicit admin threat checks, not per
  request; acceptable. `detect_tor`'s cache (~1-2k entries) parses on
  GeoLocatedIP create/refresh only.
- **Tor URL override respected**: `_fetch_tor` honors `source_url` when set on
  the row, falling back to the `TOR_EXIT_NODE_LIST_URL` setting default — a
  deployment already overriding the URL keeps working through the cache.

### Tests
All geofence tests: testit, per-request-header style (extend
`tests/test_geofence/_helpers.py` `headers(...)` with `strict=None` →
`X-Mojo-Test-Geofence-Strict`).

New `tests/test_geofence/strict_posture.py`:
- Strict + empty rules (`system_rules={}`, `strict=1`) on a decorated auth
  endpoint → 403 `no_rules_strict`; same request without strict → 200
  (`no_rules`) — pins opt-in.
- Strict + `geo="fail"` + rules present → 403 `lookup_failed`; non-strict
  default → allowed (existing behavior unchanged).
- Strict + `GEO_PRIVATE` + rules present → 403 `private_ip`; non-strict → 200.
- Strict + allowlist header covering the IP + empty rules → **200**,
  decision `ip_allowlisted`, `would_block_reason == "no_rules_strict"` (the
  outage-prevention case).
- Per-group override via real DB group: group A `metadata.geofence_strict=
  true`, global unset → `geo/check?group_uuid=A` denies `no_rules_strict`;
  group B (no override) allows. Reverse: global strict via header, group C
  `geofence_strict=false` → allows (override loosens).
- Evidence: strict block → Event `category="geofence_block"` level **5** with
  `strict_posture` visible in the decision; non-strict jurisdiction block
  stays level 3 (existing evidence tests unchanged).
- Group REST write `metadata.geofence_strict="yes"` (non-bool) → 400 with the
  human-readable message; `true` → 200; `null` → 200 and back to inherit.
- `POST /api/settings` `GEOFENCE_STRICT_POSTURE` non-boolean JSON → 400;
  `true` → 200; group-scoped row → 400 (global-only).
- Cache invalidation: prime a cached allow (cache_ttl header > 0, empty rules
  NOT via header — real DB Setting flow), write `GEOFENCE_STRICT_POSTURE=true`
  via REST, re-check same IP → denied (a stale cache would still allow).
  NOTE (from DM-017's build): prime cache-test decisions only under
  group-scoped keys — a poisoned `(127.0.0.1, no-group)` cache entry leaks
  into parallel unheadered tests.
- `GET geo/rules` → `posture.strict_posture` present; with `group_uuid` →
  `group.strict_posture` (tri-state) + `group.strict_posture_effective`.
- `POST geo/simulate` with strict header + empty rules → decision
  `no_rules_strict`, `strict_posture` true.

New `tests/test_incident/test_threat_list_cache.py` (in-process model tests,
no network — style of `test_ipset.py`):
- `IPSet.ensure_threat_caches()` creates `tor_exits` + `blocklist_de` with
  `is_enabled=False`, correct source/kind; idempotent on second call; does
  NOT flip `is_enabled` back if an operator changed it (get_or_create only
  sets defaults on create).
- Seed `tor_exits` row with known IPs via `set_data` → `detect_tor(<listed>)`
  is True and `detect_tor(<unlisted>)` is False **without network** (cached
  path short-circuits the fetch).
- Seed `blocklist_de` row → `check_blocklist_de` returns
  `{'source': 'blocklist.de', 'is_listed': True/False}` without network.
- Missing/empty row → `_cached_ip_set("tor_exits") is None` (fallback signal).
- `_parse_tor_exit_list(text)` pure-function test: sample ExitAddress text →
  bare IP list (no HTTP).
- Weekly-cron exclusion: with both cache rows present (disabled), the
  refresh_ipsets selection `IPSet.objects.filter(is_enabled=True)
  .exclude(source="manual")` does not include them.

Existing suites (`tests/test_geofence/engine.py`, `decorator.py`,
`endpoint.py`, `config_plane.py`, `evidence_plane.py`) must pass unchanged —
strict is opt-in, defaults untouched.

Per build-baseline rule: run `bin/run_tests --agent` BEFORE any edit and
record the baseline in `## Notes`.

### Docs
- `docs/django_developer/account/geofence.md` — new "Strict / compliance
  posture" section: the bundled switch semantics (fail-closed + deny-private +
  require-rules), composition formula (`fail_closed = GEOFENCE_FAIL_CLOSED OR
  scope∈SCOPES OR strict`), per-group `metadata.geofence_strict` tri-state,
  `no_rules_strict` reason code, evidence level-5 escalation, new test header;
  add `GEOFENCE_STRICT_POSTURE` to the Settings Reference table (lines
  270-279).
- `docs/django_developer/account/geoip.md` — threat-list caching: the two
  cache-only IPSet rows, the 6h refresh cron, the live-fetch fallback, and
  the **do-not-enable** warning.
- `docs/django_developer/helpers/settings_reference.md` — add
  `GEOFENCE_STRICT_POSTURE`.
- `docs/web_developer/account/geofence.md` — `geo/rules` response additions
  (`posture.strict_posture`, `group.strict_posture`,
  `group.strict_posture_effective`), the `no_rules_strict` reason code on
  `geo/check`/403 bodies, and the group-metadata write shape
  (`metadata.geofence_strict`, 400 on non-bool).
- `CHANGELOG.md` — feature block (strict posture + threat-list caching),
  current top-block format.

### Open questions
None blocking. Decisions locked during scope with the owner (2026-07-08):
(1) single work item, not split; (2) one bundled strict switch, not three
flags — design re-verified end-to-end after the owner asked for a review pass
(the review caught and fixed the allowlist-ordering bug noted in Design
decisions). Out of scope, noted: MFA step-up vs deny (this item is deny-only —
the decorator 403 contract is untouched); general settings-coercion
observability (owned by `geofence-settings-write-validation-gap` in inbox).

## Notes
- **Baseline (2026-07-08, `bin/run_tests --agent`)**: status passed — total 2332,
  passed 2276, failed 0, skipped 56 (plus test_incident/test_security modules
  skipped entirely: "requires --extra slow"). All green; no pre-existing
  failures. (`testproject/var/test_failures.json`)
- **Plan deviation (build, 2026-07-08)**: threat-cache tests go in
  `tests/test_geofence/threat_cache.py`, NOT `tests/test_incident/` as planned —
  test_incident is an opt-in module skipped by the default suite, so tests
  there would never run in routine work.
- **Build outcome (2026-07-08/09)**: implemented in commit `3aff6c0`; post-build
  hardening in a follow-up commit. Full default suite after all changes:
  2354 total / 2298 passed / **0 failed** / 56 skipped — baseline invariant
  held (green → green). One unrelated flake observed once in
  test_verification (token-TTL timing); passed on rerun.
- **Post-build security review (2026-07-08)**: no CRITICAL. One WARNING
  fixed per owner ruling ("platform admins only"): changing
  `metadata.geofence_strict` now requires the global `manage_geofence`/
  `security` permission (Group.on_rest_pre_save compares against the DB
  value — JSONField merges don't populate changed_fields — and 403s
  otherwise) + regression test. Two INFO items also fixed: geofence_strict
  flips now emit `geofence_config` evidence (target `group:<id>`), and the
  cache-only IPSet rows got a hard code breaker (`is_cache_only`: enable
  action 400s, `sync()` no-ops even if the flag is force-set). Deferred
  (INFO, pre-existing pattern): SSRF-hardening helper for the `_fetch_*`
  source_url fetchers — worth a future chore; `no_rules_strict`/
  `strict_posture` visibility on public geo/check 403s accepted (consistent
  with the documented reason/detail exposure policy).
- **Overlap resolution (2026-07-07, DM-017 scope):** DM-017 (geofence config
  + evidence plane) builds **(D) bypass visibility** (`GET /api/geo/bypass_holders`)
  and the **per-scope fail posture map** (`GEOFENCE_FAIL_CLOSED_SCOPES`, decorator
  scope passed into `GeoFenceEngine.check`). Drop (D) from this item when scoping;
  keep (A) strict posture, (B) per-group posture, (C) threat-list caching. Strict
  posture must **compose** with the scope map (effective fail_closed = global flag
  OR strict posture OR scope ∈ fail-closed scopes) — don't replace it.

## Resolution
- closed: 2026-07-09
- branch: main
- files changed: CHANGELOG.md,docs/django_developer/account/geofence.md,docs/django_developer/account/geoip.md,docs/django_developer/helpers/settings_reference.md,docs/django_developer/logging/incidents.md,docs/django_developer/security/README.md,docs/django_developer/testit/Overview.md,docs/web_developer/account/geofence.md,docs/web_developer/account/geoip.md,docs/web_developer/account/login_events.md,docs/web_developer/security/README.md,memory.md,mojo/__init__.py,mojo/apps/account/models/group.py,mojo/apps/account/models/setting.py,mojo/apps/account/rest/geofence.py,mojo/apps/account/services/geofence/engine.py,mojo/apps/account/services/geofence/evidence.py,mojo/apps/incident/asyncjobs.py,mojo/apps/incident/cronjobs.py,mojo/apps/incident/migrations/0031_alter_ipset_source.py,mojo/apps/incident/models/ipset.py,mojo/helpers/geoip/detection.py,mojo/helpers/geoip/threat_intel.py,planning/.next_id,planning/done/DM-020-geofence-evidence-metrics-dual-write-group-scoped-.md,planning/in_progress/DM-021-geofence-hardening-opt-in-strict-compliance-enforc.md,planning/inbox/dispatcher-group-param-is-active-asymmetry.md,planning/inbox/geofence-hardening.md,planning/inbox/geofence-member-visibility-group-scoped-policy-events.md,pyproject.toml,tests/test_geofence/_helpers.py,tests/test_geofence/evidence_plane.py,tests/test_geofence/strict_posture.py,tests/test_geofence/threat_cache.py,uv.lock
- tests added: tests/test_geofence/strict_posture.py (15 tests — strict
  no-rules/lookup-failure/private-IP denials + opt-in defaults, allowlist
  exemption with no_rules_strict shadow, strict_posture decision flag,
  per-group tri-state override (tighten + loosen), level-5 evidence, group
  metadata write validation, global-perm gate on geofence_strict (tenant
  admin 403), flip audit event + no-op dedupe, /api/settings JSON-boolean +
  global-only validation, group posture-flip cache invalidation, geo/rules
  posture fields, simulate strict) and tests/test_geofence/threat_cache.py
  (7 tests — ensure_threat_caches disabled/idempotent/operator-safe,
  ExitAddress parser, detect_tor + check_blocklist_de cached reads without
  network, missing/empty-row fallback signal, enable-action rejection +
  sync() hard breaker, weekly-cron exclusion);
  tests/test_geofence/_helpers.py extended (strict header)
