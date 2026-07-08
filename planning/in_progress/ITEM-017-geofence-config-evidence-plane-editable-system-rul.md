---
# id is assigned by /scope on pickup — leave it blank
id: ITEM-017
type: feature
title: Geofence config + evidence plane — editable system rules, validation, simulate, incident events
priority: P1
effort: L
owner: backend
opened: 2026-07-07
depends_on: []
related: []
links: []
---

# Geofence config + evidence plane — editable system rules, validation, simulate, incident events

## What & Why

The geofence engine (`mojo/apps/account/services/geofence/`) is complete as a
*decision* engine but has no **config plane** (rules are a raw settings dict +
unvalidated `Group.metadata['geofence']`) and no **evidence plane** (blocks
403 silently — no event, no audit trail). Downstream products (MojoVerify,
WMX) now need both: payment-processor compliance teams ask for "geofencing
rules in an active state" with evidence, and **legal/business staff — not
engineers — will maintain the jurisdiction lists** via an admin UI (web-mojo
item, filed separately). That UI needs backend support that doesn't exist yet.

Owner rulings driving this (2026-07-07):
1. System rules must be **editable in the admin portal** (not deploy-file only).
2. Fail posture is **per-endpoint-scope**: fail-closed on money endpoints,
   fail-open on auth.
3. Rule content is set by legal/business in the portal — so validation,
   attribution, and a safe self-serve test surface are mandatory, and raw-JSON
   editing is not an acceptable interface.
4. Developer/office IPs must be allowlistable — a **full** geofence exemption
   (jurisdiction + abuse flags) so internal staff are never locked out of the
   live system — with auditor-grade guardrails: who is exempt, why, until
   when, and evidence whenever an exemption actually bypasses a block.

What exists (recon, verified 2026-07-07):
- Engine `engine.py:186-265`, DSL `dsl.py` (`validate_rule` / `evaluate_rule`),
  Redis decision cache `cache.py` (key `geofence:dec:{ip}:{group_id}`).
- `@md.requires_geofence(scope=)` decorator (`mojo/decorators/geofence.py`) on
  23 auth endpoints; `GET /api/geo/check` pre-flight (`rest/geofence.py`).
- `settings.get` already chains DB-backed `Setting.resolve()` → Redis →
  django.conf, so a Setting-backed `GEOFENCE_SYSTEM_RULES` needs no engine
  read-path change.
- `Group.metadata['geofence']` has **no write-time validation** (a typo'd rule
  surfaces only at evaluation, as `rule_invalid`).
- `incident.report_event(details, title=, category=, level=, request=)`
  (`mojo/apps/incident/reporter.py:4`) is the established evidence sink;
  passing `request=` captures IP/UA/path context.
- Blocks currently emit **nothing** (only `logit.error` on lookup failure /
  invalid rule).

## Acceptance Criteria

- [ ] **Editable system rules**: `GEOFENCE_SYSTEM_RULES` manageable as a
      DB-backed `Setting` row via perm-gated REST (admin-level permission),
      with `validate_rule` enforced on write and change attribution
      (who/when) queryable for the admin UI's change history.
- [ ] **Group-rule validation on save**: writing `Group.metadata['geofence']`
      through REST validates the rule and rejects malformed shapes with a
      human-readable error (no more lazy `rule_invalid` at request time).
- [ ] **Cache invalidation on any rule change** (system or group): an
      emergency rule edit must not serve stale allow decisions for up to
      `GEOFENCE_CACHE_TTL`; invalidation is automatic on write, not an ops
      step.
- [ ] **Effective-rules endpoint** (perm-gated): returns the merged
      system+group ruleset plus posture (enabled, fail mode, cache TTL,
      last-changed metadata) — the machine-readable "rules in an active
      state" artifact.
- [ ] **Simulate endpoint** (perm-gated): arbitrary IP or geo dict (+ optional
      group) → full uncached `GeoDecision`, so a non-engineer can demonstrate
      "a WA IP is blocked" without owning a WA IP. Distinct from the public
      self-check `GET /api/geo/check`.
- [ ] **Block events → incidents**: every geofence block calls
      `incident.report_event(category="geofence_block", request=request)`
      with a level scheme — 3 auth-endpoint block · 5 money-endpoint block or
      abuse-flag (VPN/Tor) block · 6 lookup-failure-while-fail-open ·
      7 invalid-rule-at-evaluation (crosses typical `INCIDENT_LEVEL_THRESHOLD`
      and pages). Per-`(ip, reason)` hourly dedupe via cache so a blocked
      state hammering login cannot flood events; aggregate metrics
      (`geofence:blocks`, `geofence:blocks:region:{code}`) recorded on every
      block including deduped ones (mirror the existing
      `firewall:blocks:country:{code}` pattern).
- [ ] **Per-scope fail posture**: decorator `scope` maps to posture (e.g.
      `GEOFENCE_FAIL_CLOSED_SCOPES = ["payments"]`) so money endpoints
      fail-closed while auth stays fail-open. Reconcile with
      `geofence-hardening.md` (inbox) rather than duplicating it — that item
      owns the strict-posture allow-by-default paths; this one owns the
      scope-map shape.
- [ ] **Bypass visibility**: an endpoint listing users holding
      `bypass_geofence` (also called for in `geofence-hardening.md` — build
      once).
- [ ] **IP allowlist (full exemption)**: checked after `bypass_geofence`,
      before rule evaluation; allowlisted requests pass jurisdiction *and*
      abuse rules with reason `ip_allowlisted`. Two sources: per-IP
      `GeoLocatedIP.is_whitelisted` (existing `/api/system/geoip` whitelist
      action) and a Setting-backed `GEOFENCE_ALLOWLIST` of CIDRs for
      office/VPN egress ranges. Guardrails: entries support expiry
      (`whitelisted_until`, mirroring the `blocked_until` pattern); every
      allowlisted pass that would otherwise have blocked emits an evidence
      event; active exemptions are listable with reason for auditors;
      allowlist changes invalidate the decision cache like any rule change.
- [ ] Tests extend `tests/test_geofence/` (validation, invalidation, events,
      dedupe, scope posture, simulate perms, allowlist exemption + expiry).

## Plan

### Goal
Give the geofence engine a config plane (validated, attributable, cache-coherent
editing of system + group rules via perm-gated REST) and an evidence plane
(incident events + metrics for every enforcement outcome), plus per-scope fail
posture and bypass visibility — the backend for the admin-portal geofencing UI.

### Context — what exists (verified 2026-07-07, v1.2.41)

**Engine** — `mojo/apps/account/services/geofence/` contains only `__init__.py`,
`engine.py`, `dsl.py`, `cache.py`. Entry point:

```python
# engine.py:186-190
class GeoFenceEngine:
    @classmethod
    def check(cls, request, group=None, user=None):
        ip = getattr(request, "ip", None) or request.META.get("REMOTE_ADDR", "")
```

Flow in `check()` (186-265): `GEOFENCE_ENABLED` kill-switch (193-196) →
`bypass_geofence` user perm (199-205, no cache write) → `_system_rules(request)`
(82-87: `settings.get("GEOFENCE_SYSTEM_RULES", {}) or {}` — **no `kind="dict"`**,
so a DB-backed Setting (a JSON *string*) would break; today it only works because
the value comes from django.conf) → `_group_rules(group)` (90-94:
`(group.metadata or {}).get("geofence") or {}`) → both-empty fast path
`no_rules` (212-213) → cache get (215-223, TTL `GEOFENCE_CACHE_TTL` default 300,
`cache_enabled = ttl > 0`) → `_resolve_geo` (101-132: `X-Mojo-Test-Geo` header →
`GEOFENCE_TEST_OVERRIDE` setting → `GeoLocatedIP.geolocate(ip)`) → geo None ⇒
`lookup_failed` with `GEOFENCE_FAIL_CLOSED` default False (229-234) → no
country_code ⇒ `private_ip` / `GEOFENCE_ALLOW_PRIVATE_IPS` default True (237-242)
→ rule eval system-then-group (245-260: `validate_rule` per level, `ValueError` ⇒
`rule_invalid` deny; `evaluate_rule(rule, geo)` ⇒ `(ok, reason)`) → `passed`
(263-265). `_maybe_cache(ip, group_id, dec, ttl)` (268-272) writes only if
`ttl > 0`. Settings are read via `_bool_setting_with_header` /
`_int_setting_with_header` wrappers (63-87) that honor `X-Mojo-Test-Geofence-*`
headers (test-mode-gated — see `tests/test_geofence/test_mode_gate.py`).

`GeoDecision` = `objict` (engine.py:180), built by `_build_decision` (135-154):
`allowed, reason, detail, ip, country, country_code, region, region_code,
abuse{tor,vpn,datacenter,proxy}, checked_at, rule_level`. Reason codes
(`_DETAIL_MAP` 157-172): `no_rules, disabled, bypass, passed, lookup_failed,
private_ip, country_not_allowed, region_not_allowed, tor_detected, vpn_detected,
proxy_detected, datacenter_detected, rule_invalid, group_inactive`.

**DSL** — `dsl.py`: `validate_rule(rule)` (25-42) checks dict shape, top keys
`{country, region, abuse}`, ops `{in, not_in, eq}`, abuse keys
`{tor,vpn,datacenter,proxy}`; raises plain `ValueError` with a human message,
returns None on success. `evaluate_rule(rule, geo)` (84-122) → `(allowed,
reason|None)`; empty rule ⇒ `(True, None)`.

**Cache** — `cache.py`: key `f"geofence:dec:{ip}:{group_id if group_id is not
None else '_'}"` (:14); `get` (:18), `set` (:33, setex JSON),
`invalidate(ip, group_id=None)` (:41-46) — **single-key delete only**, no
group-wide or global invalidation. scan_iter precedent for pattern deletes:
`mojo/decorators/limits.py:411,422` and `Group._invalidate_auth_domain_cache`
(`group.py:646-659`). Redis client: `from mojo.helpers.redis import
get_connection` (`client.py:74-122`).

**Decorator** — `mojo/decorators/geofence.py`: `requires_geofence(scope=None)`
(21-33) supports bare and `scope=` forms; `_apply_geofence` (36-44) records
`SECURITY_REGISTRY[key]["geofence"] = {"scope": scope}` and sets
`func._mojo_geofence_scope` — **scope is metadata-only, never passed to the
engine**. Calls `GeoFenceEngine.check(request, group=getattr(request, "group",
None), user=getattr(request, "user", None))` (51-55); allow ⇒
`request.geofence_decision = decision` (56-58); deny (61-66) ⇒
`JsonResponse({"error": "geofence_blocked", "code": 403, "reason":
decision.reason, "detail": decision.detail}, status=403)`. Applied
`scope="auth"` on ~23 endpoints (`user.py` ×11, `totp.py:145,180,221`,
`passkeys.py:156,214`, `sms.py:113`). **Blocks emit nothing today.**

**Geo REST** — `mojo/apps/account/rest/geofence.py` has one endpoint,
`on_geo_check` (18-49): `@md.GET("geo/check")` + `@md.public_endpoint` +
`@md.rate_limit("geo_check", ip_limit=30)`; takes `group_uuid` from
`request.DATA`, unknown uuid ⇒ `merrors.ValueException("Unknown group")` (400),
inactive group ⇒ system-only eval with `group_inactive` hint (38-46). Account
app mounts at prefix `""` (`mojo/apps/account/rest/__init__.py:1` sets
`APP_NAME = ""`; `mojo/urls.py:40-67`), so paths are `/api/geo/...`.

**Setting model** — `mojo/apps/account/models/setting.py`: `class
Setting(MojoSecrets, MojoModel)` (:11); fields `key` CharField, `value`
**TextField** (JSON dumped manually — not JSONField, no `kind` column),
`is_secret`, `group` FK; `unique_together ("key","group")` (:33). **No
created_by/modified_by.** `RestMeta` (:36-48): `VIEW_PERMS = SAVE_PERMS =
["manage_settings", "groups"]`. `resolve(name, group=None, default=None)`
(:167-216): group→parent chain (Redis-hash cache `settings:g:{group_id}` →
DB backfill) → global (`settings:global`) → default. `save()` (:249-251) →
`push_to_cache()`; `delete()` (:253-255) → `remove_from_cache()`. Programmatic
write: `Setting.set(key, value, is_secret=False, group=None)` (:222-233),
`Setting.remove(key, group=None)` (:235-243). REST: `mojo/apps/account/rest/setting.py`
→ `/api/settings[/<pk>]` via `on_rest_request`. Hook available:
`on_rest_pre_save(self, changed_fields, created)` exists (:83-88, re-encrypts
secrets). **`Setting.set()` does NOT run `on_rest_pre_save`** (plain save), only
the REST flow does (`mojo/models/rest.py:1253`).

**Settings helper** — `mojo/helpers/settings/helper.py`
`get(name, default, group, kind)` (:122-143): dict-root → DB
(`Setting.resolve`) → django.conf. `_convert_value` (:69-120) coerces `kind` ∈
int/float/bool/dict/list — `dict` json-parses strings (:96-105).

**Group save path** — `mojo/apps/account/models/group.py`: `metadata =
JSONField(default=dict)` (:41), in the `default` graph ⇒ REST-writable. JSON
fields **merge** on REST write (`mojo/models/rest.py:1421-1461`; replace only
via `"__replace": true` or `RestMeta.JSON_REPLACE_FIELDS`; `"protected"` root
key guarded :1445-1447). Write-time validation precedent (the template to
follow), `group.py:623-629`:

```python
def on_rest_pre_save(self, changed_fields, created):
    # Reject an invalid auth config at write time so a bad
    # metadata.auth_config surfaces as a 400 here, not a render error later.
    auth_cfg = (self.metadata or {}).get("auth_config")
    if auth_cfg is not None:
        from mojo.apps.account.services.auth_config import validate_auth_config
        validate_auth_config(auth_cfg)
```

`validate_auth_config` raises `merrors.ValueException` ⇒ clean 400
(`mojo/errors`: `ValueException` default code/status 400; `MojoException`
rendered by `mojo/decorators/http.py:142-167`; bare `ValueError` also → 400
:181-195). `Group.on_rest_saved` → `_invalidate_auth_domain_cache`
(:634-659) is the "invalidate external cache after save" precedent. REST save
flow: `on_rest_save` (`rest.py:1204-1266`) — custom `set_<field>` wins
(:1270-1279), JSONField → `on_rest_update_jsonfield` (:1287-1288),
`on_rest_pre_save` (:1253) → `atomic_save` → `on_rest_saved` (:1257);
`POST_SAVE_ACTIONS` dispatch to `on_action_<key>` (:1258-1262).

**Incident sink** — `mojo/apps/incident/reporter.py:4-10`:
`report_event(details, title=None, category="api_error", level=1, request=None,
scope="global", **kwargs)`; group auto-resolved from `request.group`; extra
kwargs land in `Event.metadata` (sanitized). Event model
(`incident/models/event.py`): fields incl. `level, category, source_ip,
country_code, title, details, metadata JSONField, group FK, incident FK`
(:37-64); `VIEW_PERMS=["view_security","security"]` (:68-70).
`INCIDENT_LEVEL_THRESHOLD = settings.get_static(..., 7)` (:12); `publish()`
creates/bundles an Incident when a RuleSet matches **or `level >=` threshold**
(:227), so level 7 pages by default and 3/5/6 stay as queryable events unless a
deployment adds RuleSets. Existing "once per window" primitive: Redis
`set(key, val, ex=ttl, nx=True)` (`mojo/cache/redis.py:82-95`,
`mojo/apps/jobs/scheduler.py:169`).

**Metrics** — the pattern to mirror, `mojo/apps/account/models/geolocated_ip.py:509-511`:

```python
metrics.record("firewall:blocks", category="firewall")
if self.country_code:
    metrics.record(f"firewall:blocks:country:{self.country_code}", category="firewall")
```

(`from mojo.apps import metrics` — `record(slug, when=None, count=1,
category=None, account="global", min_granularity="hours", ...)`,
`metrics/redis_metrics.py:70-72`.)

**GeoLocatedIP whitelist surface** — `mojo/apps/account/models/geolocated_ip.py`:
`ip_address = GenericIPAddressField(db_index=True, unique=True)` (:28);
block/whitelist fields (:74-83): `is_blocked` (:75, indexed), `blocked_at`
(:76), `blocked_until` (:77, indexed, null = permanent), `blocked_reason`
(:78), `block_count` (:79), `is_whitelisted` (:82, indexed + Meta index :100),
`whitelisted_reason` (:83) — **no `whitelisted_until` today**. The expiry
idiom to mirror, `block_active` (:160-169):

```python
@property
def block_active(self):
    if not self.is_blocked:
        return False
    if self.is_whitelisted:
        return False
    if self.blocked_until and dates.utcnow() > self.blocked_until:
        return False
    return True
```

`whitelist(reason)` (:576-623) sets flag + reason, clears block state, saves,
logs `firewall:whitelist`; `unwhitelist()` (:625-638) clears both. REST:
`@md.URL('system/geoip')` in `account/rest/device.py:30-34` →
`/api/system/geoip`, `POST_SAVE_ACTIONS = ["refresh", "threat_analysis",
"block", "unblock", "whitelist", "unwhitelist"]` (:107).
`on_action_whitelist(value)` (:652-656) takes a bare reason string;
`on_action_block` (:640-644) shows the dict pattern `{reason, ttl}` with
`block()` converting ttl → `blocked_until` (:470). Expired blocks are swept by
a minute-cron (`incident/asyncjobs.py:210-224`). `is_whitelisted` readers that
must honor expiry once it exists: `block_active` (:165),
`update_threat_from_incident` (:417), `block()` (:449); assistant tools also
set/serialize it (`assistant/services/tools/security/ips.py:38,:147,:174,:207`).
Federation sync hard-rejects whitelist/block state
(`_GEOIP_SYNC_FORBIDDEN_FIELDS`, `device.py:71-75`; strip lists
`mojo/helpers/geoip/mojo.py:6,:28`). Row fetch by ip:
`cls.objects.filter(ip_address=ip).first()` inside `geolocate` (:693).

**CIDR matching & dates** — there is **no ip-in-CIDR-list helper anywhere in
mojo/** (incident `IPSet` stores CIDR strings; matching happens in the kernel
via ipset; `firewall.py:31-39` is regex format validation only). Stdlib
`ipaddress` is already imported at `helpers/request.py:1` (normalize_ip),
`geolocated_ip.py:1`, `helpers/geoip/__init__.py:10` (`is_private` check
:78-79) — single-IP uses only. Dates: `mojo.helpers.dates.parse_datetime`
(:78-89, ISO → aware UTC), `add(when, seconds=...)` (:206-215), `utcnow()`
(:199-203); request idiom `request.DATA.get_typed("when",
typed=datetime.datetime)` (`metrics/rest/values.py:204`).

**Perms conventions** — geofence perms today: **only `bypass_geofence`**
(engine.py:201; stored in `User.permissions` JSONField,
`user.py:492-543` `has_permission`). Security-domain convention:
`["view_security","security"]` / `["manage_security","security"]`
(incident/event models). Non-RestMeta endpoints:
`@md.requires_perms(...)` (e.g. `account/rest/group.py:51`).

**Tests** — `tests/test_geofence/` (`__init__.py` declares
`TESTIT = {"requires_apps": ["mojo.apps.account"]}`); `_helpers.py` has geo
fixtures (`GEO_US, GEO_US_FL, GEO_RU, GEO_TOR, GEO_VPN, GEO_DATACENTER,
GEO_PRIVATE`) and `headers(*, geo=None, system_rules=None, enabled=None,
fail_closed=None, allow_private=None, cache_ttl=0)` building
`X-Mojo-Test-Geo` / `X-Mojo-Test-Geofence-System` / `-Enabled` / `-Fail-Closed`
/ `-Allow-Private` / `-Cache-Ttl`. Files: `dsl.py` (pure DSL), `engine.py`
(via `GET /api/geo/check`), `decorator.py` (real login/register 403s, bypass
perm), `endpoint.py` (geo/check shapes), `test_mode_gate.py`. Tests are
parallel-safe via per-request headers — keep that style; no `th.server_settings`.

**Docs** — `docs/django_developer/account/geofence.md` exists but is **drifted**
(shows old reason values `system_rule`/`group_rule` and a different 403 body);
web track has **no geofence doc** (`/api/geo/check` undocumented). Index rows:
`docs/django_developer/account/README.md:17`, `docs/web_developer/account/README.md`,
`docs/web_developer/README.md:31`.

### Changes — what to do

**1. `mojo/apps/account/services/geofence/engine.py`**
- `_system_rules` (82-87): read with `kind="dict"` so a DB-backed Setting (JSON
  string) parses — required for the whole config plane. If coercion fails
  (malformed stored JSON), fall back to `{}`; eval-time `validate_rule` still
  backstops as `rule_invalid`.
- Add `_list_setting_with_header(request, header, name, default)` alongside the
  bool/int wrappers (63-87), header `X-Mojo-Test-Geofence-Fail-Closed-Scopes`
  (comma-separated), setting kind `list`.
- `check(cls, request, group=None, user=None, scope=None)`: in the
  `lookup_failed` branch (229-234), effective posture =
  `GEOFENCE_FAIL_CLOSED` **or** (`scope` in `GEOFENCE_FAIL_CLOSED_SCOPES`,
  default `[]`).
- **Never cache `lookup_failed` decisions** (today `check()` DOES cache them —
  `engine.py:233` — stop): scope isn't in the cache key, so a cached fail-open
  allow from an auth endpoint must not be replayed to a fail-closed scope;
  caching transient failures also prolongs outages. `rule_invalid` may stay
  cacheable (deterministic; write-path invalidation clears it).
- **IP allowlist step** (new): wins over rule evaluation (decision priority is
  bypass → allowlist → rules), physically placed after the cache lookup
  (:220-223, so cached `ip_allowlisted` decisions stay fast) and before geo
  resolution (:226). `_ip_allowlisted(request, ip)` returns
  `(matched, source, entry_reason, until)`, checking:
  1. Setting `GEOFENCE_ALLOWLIST` (kind `list`, default `[]`; entries are
     `"CIDR-or-IP"` strings or `{cidr, reason, until}` objects) — match via
     stdlib `ipaddress` (`ip_address(ip) in ip_network(cidr, strict=False)`;
     family mismatch or bad entry ⇒ skip + `logit.error`, never raise — **no
     CIDR-membership helper exists in mojo/, build the small loop here**);
     `until` (ISO) parsed with `mojo.helpers.dates.parse_datetime`, expired ⇒
     no match. Test header `X-Mojo-Test-Geofence-Allowlist` (JSON list,
     mirroring the `-System` wrapper).
  2. `GeoLocatedIP.objects.filter(ip_address=ip, is_whitelisted=True).first()`
     (unique+indexed `ip_address` :28, indexed `is_whitelisted` :82), honoring
     the new `whitelist_active` expiry semantics (change 6).
  On match: **shadow-evaluate** the remaining pipeline (geo resolve +
  private-ip + rule eval) to compute `would_block` / `would_block_reason`,
  then return `allowed=True, reason="ip_allowlisted"` carrying
  `allowlist_source`, `allowlist_reason`, `would_block`, `would_block_reason`
  (extra objict keys; `_DETAIL_MAP` gains `ip_allowlisted`). Shadow lookup
  failure ⇒ `would_block=None` (and no level-6 event — the allowlist made the
  lookup moot). The decision is cacheable; allowlist-change invalidation
  (changes 2/6/7) covers staleness.
- To keep one pipeline, extract steps 5-10 (`engine.py:226-265`) into an
  internal `_evaluate(...)` used by both the normal path and the shadow pass.
- Add `simulate(cls, request, ip=None, geo=None, group=None, scope=None)`
  classmethod: same rule pipeline as `check` but — no bypass-perm shortcut, no
  cache read/write, evaluates even when `GEOFENCE_ENABLED` is false (report
  `enabled` alongside so staff can stage rules before enabling), geo comes from
  the explicit `geo` dict or `GeoLocatedIP.geolocate(ip)`; lookup failure ⇒
  the posture-applied `lookup_failed` decision. `request` is passed only so the
  `_*_with_header` wrappers keep working (test-mode-gated). Simulate includes
  the allowlist step when `ip` is given (a geo-dict-only simulate has no IP to
  match), so staff can verify exemptions end-to-end too.
- Test vector for forced lookup failure: if none exists, extend
  `_resolve_geo`'s `X-Mojo-Test-Geo` handling to accept literal `"fail"` ⇒
  return None (test-mode-gated like the rest).

**2. `mojo/apps/account/services/geofence/cache.py`**
- Add `invalidate_group(group_id)` (pattern `geofence:dec:*:{group_id}`),
  `invalidate_ip(ip)` (pattern `geofence:dec:{ip}:*` — for per-IP whitelist
  changes), and `invalidate_all()` (pattern `geofence:dec:*`) using
  `get_connection().scan_iter(...)` + delete, per `limits.py:411` /
  `group.py:646-659` precedent.

**3. NEW `mojo/apps/account/services/geofence/evidence.py`** — the evidence plane.
- `report_block(request, decision, scope=None)`:
  - Compute level: `rule_invalid` → **7**; `lookup_failed` and
    `decision.allowed` (fail-open pass-through) → **6**; blocked and (abuse
    reason ∈ {tor,vpn,proxy,datacenter}_detected **or** `scope` ∈
    `GEOFENCE_FAIL_CLOSED_SCOPES`) → **5**; any other block → **3**.
  - Metrics on every **block** (deduped or not; not on the level-6 fail-open
    allow): `geofence:blocks`, `geofence:blocks:country:{country_code}`, and
    `geofence:blocks:region:{country_code}-{region_code}` when region present —
    all `category="geofence"`, mirroring the firewall pattern.
  - Dedupe events per `(ip, reason)` hourly: `get_connection().set(
    f"geofence:evt:{ip}:{reason}", 1, ex=3600, nx=True)` — emit only when the
    SET wins. On Redis error: emit anyway (evidence beats dedupe), never raise.
  - Emit `incident.report_event(details, title=..., category="geofence_block",
    level=level, request=request, reason=..., rule_level=..., scope=...,
    country_code=..., region_code=..., abuse=..., detail=...)` — kwargs land in
    `Event.metadata`; `request=` captures ip/UA/path/group.
  - Entire function wrapped so it can never break the request path
    (`try/except` + `logit.error`).
- `report_exempt(request, decision, scope=None)`: when an `ip_allowlisted`
  decision has `would_block` truthy — category `"geofence_exempt"`, level 3,
  same hourly `(ip, reason)` dedupe, plus
  `metrics.record("geofence:exempt", category="geofence")` on every
  occurrence; metadata carries `allowlist_source`, `allowlist_reason`,
  `would_block_reason`, `scope`. A pass that would NOT have blocked emits
  nothing (developers in allowed regions don't spam the evidence stream).
- `report_config_change(target, old, new, request=None, user=None)` (request
  optional so model-level hooks can call it; attribution falls back to
  `user`/`active_user`): category `"geofence_config"`, level 3, never
  deduped; metadata carries `target` (`"system"`, `"allowlist"`,
  `"group:<id>"`, or `"ip:<addr>"`), `old`, `new`, and the acting user
  (username/id). This event stream **is** the change history the admin UI
  queries (via existing `/api/incident/event` REST, `view_security`-gated).

**4. `mojo/decorators/geofence.py`**
- Pass `scope` into the engine: `GeoFenceEngine.check(request, group=...,
  user=..., scope=scope)` (51-55).
- On deny (61-66): call `evidence.report_block(request, decision, scope)`
  before returning the 403 (body unchanged).
- On allow with `decision.reason == "lookup_failed"`: also call
  `report_block` (that's the level-6 fail-open evidence), then proceed.
- On allow with `decision.reason == "ip_allowlisted"` and a truthy
  `would_block`: call `evidence.report_exempt(request, decision, scope)`,
  then proceed.

**5. `mojo/apps/account/rest/geofence.py`** — the config-plane REST (all under
`/api/geo/*`; account APP_NAME=""). New perm strings: `view_geofence` /
`manage_geofence`; `security` is the domain category.
- `on_geo_check`: accept optional `scope` in `request.DATA`, pass to
  `check()` so the pre-flight can preview posture. **geo/check emits no
  events** (advisory; would double-fire with the real request).
- `GET geo/rules` — `@md.requires_perms("view_geofence", "manage_geofence",
  "security")`. Returns the machine-readable "rules in an active state"
  artifact:
  `{system: {rule, source: "setting"|"conf"|"none", modified: <Setting.modified
  or null>}, group: {id, uuid, rule} (only when ?group_uuid=), posture:
  {enabled, fail_closed, fail_closed_scopes, allow_private_ips, cache_ttl},
  allowlist_summary: {setting_entries, geoip_active},
  evaluation_order: ["system", "group"], enforced_endpoints: [{endpoint, scope}]}`
  — endpoints enumerated from `SECURITY_REGISTRY` entries with a `geofence` key
  (`decorators/geofence.py:36-44`).
- `POST geo/rules` — `@md.requires_perms("manage_geofence", "security")`. Body
  `{rule: {...}}`; `validate_rule` first (ValueError → wrap in
  `merrors.ValueException` so the admin UI gets the human-readable message);
  **full replace** of the system rule via
  `Setting.set("GEOFENCE_SYSTEM_RULES", <json>, group=None)`; decision-cache
  invalidation happens in the Setting hook (change 6); then
  `report_config_change(request, "system", old, new)`. Returns the saved rule +
  modified stamp.
- `DELETE geo/rules` — same perms as POST; `Setting.remove(
  "GEOFENCE_SYSTEM_RULES")` (falls back to django.conf value), config-change
  event, `invalidate_all()` explicitly (delete path bypasses the save hook —
  verify; `Setting.delete` :253-255 only touches the settings cache).
- `POST geo/simulate` — `@md.requires_perms("view_geofence",
  "manage_geofence", "security")`. Body: `ip` (string) **xor** `geo` (dict,
  same shape as `X-Mojo-Test-Geo`), optional `group_uuid`, optional `scope`.
  Runs `GeoFenceEngine.simulate(...)`; returns the **full** GeoDecision (all
  fields — perm-gated, so no info-leak concern) plus
  `posture: {enabled, fail_closed_effective, scope}`. Uncached, no events, no
  metrics. Distinct from public `geo/check` (which evaluates the caller's IP).
- `GET geo/bypass_holders` — `@md.requires_perms("view_geofence",
  "manage_geofence", "security")`. Queries
  `User.objects.filter(permissions__bypass_geofence__isnull=False)`, then
  Python-filters truthy values **matching `User.has_permission` semantics**
  (user.py:492-543). Rows: `{id, username, display_name, email, is_active,
  value}` + total count; cap the list (e.g. 200) defensively. Built here
  **once** — dropped from `geofence-hardening` (its file is annotated).
- `GET geo/allowlist` — `@md.requires_perms("view_geofence",
  "manage_geofence", "security")`. The auditor's "who is exempt" artifact:
  `{setting: [{cidr, reason, until, active}], geoip: [{ip, reason, until,
  active}]}` — setting entries from `GEOFENCE_ALLOWLIST`, geoip entries from
  `GeoLocatedIP.objects.filter(is_whitelisted=True)`; expired entries are
  listed with `active: false`, not hidden. Together with `bypass_holders`
  this answers "who is exempt" across all three exemption kinds.
- `POST geo/allowlist` — `@md.requires_perms("manage_geofence", "security")`.
  Full-replace of the `GEOFENCE_ALLOWLIST` Setting; validates every entry
  (parseable CIDR/IP via `ipaddress`, optional `reason` string, optional
  `until` ISO datetime) ⇒ `merrors.ValueException` naming the first bad
  entry; config-change event (`target="allowlist"`); decision-cache
  invalidation via the Setting hook (change 7). Per-IP entries are NOT
  managed here — they stay on the existing `/api/system/geoip`
  whitelist/unwhitelist actions (change 6).

**6. `mojo/apps/account/models/geolocated_ip.py`** — whitelist expiry + hooks.
- Add `whitelisted_until = models.DateTimeField(null=True, blank=True,
  db_index=True, help_text="When the whitelist expires (null = permanent)")`
  next to `whitelisted_reason` (:83), mirroring `blocked_until` (:77).
  **This is the item's one schema change.**
- Add a `whitelist_active` property mirroring `block_active` (:160-169):
  `is_whitelisted` and (`whitelisted_until` is null or
  `dates.utcnow() <= whitelisted_until`).
- Switch the three internal `is_whitelisted` readers to `whitelist_active` so
  an EXPIRED whitelist stops suppressing firewall blocking too: `block_active`
  (:165), `update_threat_from_incident` (:417), `block()` (:449). Same
  semantics everywhere — otherwise geofence would honor expiry while the
  firewall didn't. Permanent (null-until) whitelists behave exactly as before.
- `whitelist(reason)` (:576-623): accept optional expiry — `ttl` seconds
  (mirroring `block()`'s ttl → `blocked_until` conversion at :470) or `until`
  datetime; sets `whitelisted_until` (None = permanent). `on_action_whitelist`
  (:652-656): accept a dict value `{reason, ttl, until}` like
  `on_action_block` (:640-644), keeping the bare-string form
  backward-compatible; parse `until` via `dates.parse_datetime`.
- In `whitelist()` and `unwhitelist()` (:625-638): after save, call
  `geofence.cache.invalidate_ip(self.ip_address)` and
  `evidence.report_config_change(target=f"ip:{self.ip_address}", ...,
  user=self.active_user)` (imports inside the methods). Hooking the model
  methods covers the REST actions AND the assistant tools
  (`ips.py:147,:174`) in one place.
- Federation must not sync the new field: add `whitelisted_until` to
  `_GEOIP_SYNC_FORBIDDEN_FIELDS` (`account/rest/device.py:71-75`) and the
  strip lists in `mojo/helpers/geoip/mojo.py:6,:28`.

**7. `mojo/apps/account/models/setting.py`**
- `on_rest_pre_save` (:83-88): add — if `self.key == "GEOFENCE_SYSTEM_RULES"`:
  json-parse `self.value` (string) and `validate_rule` it; if `self.key ==
  "GEOFENCE_ALLOWLIST"`: json-parse and validate each entry (parseable
  CIDR/IP, optional `reason`/`until` shapes). Failure ⇒
  `merrors.ValueException`. Covers writes through the generic `/api/settings`
  REST so every write path validates. (Imports inside the method, mirroring
  the group auth_config precedent.)
- `save()` (:249-251) and `delete()` (:253-255): after the existing
  settings-cache push/remove, if `key in {"GEOFENCE_SYSTEM_RULES",
  "GEOFENCE_ALLOWLIST"}` and `group_id is None` →
  `geofence.cache.invalidate_all()` (import inside). Putting it here covers
  `Setting.set()` (used by the geo endpoints), generic REST saves, and shell
  writes — invalidation is automatic on write, not an ops step, satisfying
  the emergency-edit criterion.

**8. `mojo/apps/account/models/group.py`**
- `on_rest_pre_save` (:623-629): after the auth_config block, validate
  `(self.metadata or {}).get("geofence")` when not None via `validate_rule`
  (import from `services/geofence/dsl.py` inside the method); `ValueError` ⇒
  `merrors.ValueException(f"Invalid geofence rule: {exc}")`. Note REST JSON
  merge runs **before** pre_save, so the merged result is validated. `{}` is
  valid (= no rules; `_group_rules` treats it as empty).
- `on_rest_saved`: alongside `_invalidate_auth_domain_cache` (:634-659), when
  `metadata` is among the changed fields →
  `geofence.cache.invalidate_group(self.id)`. Mild over-invalidation (any
  metadata change) is acceptable — decisions rebuild on next request. (Builder:
  mirror however the auth_domain invalidation gates on changed fields; if
  changed-field info isn't available there, invalidate unconditionally on save
  — still correct, just less precise.)

**9. Tests — extend `tests/test_geofence/`** (see Tests section).

**10. Docs + CHANGELOG** (see Docs section).

One schema change (`GeoLocatedIP.whitelisted_until`, change 6) → run
`bin/create_testproject` after the model edit, before the test suite.

### Design decisions
- **System rules stay in `Setting`** (key `GEOFENCE_SYSTEM_RULES`, global row)
  — the engine read path needs only `kind="dict"`; resolve/Redis/REST already
  exist; no new model or migration. Rejected: a dedicated GeofenceConfig model
  (schema + migration for one value).
- **Dedicated `geo/rules` endpoints rather than pointing the admin UI at
  `/api/settings`** — key-specific validation with human-readable errors,
  attribution events, and an *effective*/merged view; and legal/business staff
  get `manage_geofence` without `manage_settings` (which exposes every setting
  incl. secrets). The generic settings REST is still validated (change 6) so
  there is no unvalidated back door.
- **New perms `view_geofence` / `manage_geofence`** (+ `security` domain
  category per convention). Rejected: reusing `manage_settings` (over-broad for
  the legal/business persona this item exists for).
- **Change history = incident events (`geofence_config`), not audit columns on
  `Setting`** — queryable via existing `/api/incident/event` REST, zero
  migration, and config changes are themselves compliance evidence.
  "Last-changed when" comes from `Setting.modified`; "who" from the event
  stream.
- **Evidence emission lives in the decorator, not the engine** — cached denials
  still emit (the engine returns early on cache hits), and advisory surfaces
  (`geo/check`, simulate) never emit by construction.
- **`lookup_failed` decisions are never cached** — scope isn't in the cache
  key, so a cached fail-open allow must not satisfy a later fail-closed-scope
  request. Rejected: adding scope to the cache key (cardinality ×scopes for a
  branch only `lookup_failed` cares about).
- **POST geo/rules is full-replace, not merge** — legal-reviewed rulesets are
  replace-by-review; merge invites surprise composites. Group rules keep the
  standard REST metadata merge semantics (documented, with `__replace` for
  clearing).
- **Split vs `geofence-hardening` (inbox)**: this item owns the scope→posture
  map (`GEOFENCE_FAIL_CLOSED_SCOPES`) and bypass visibility; hardening keeps
  strict posture (global/per-group fail-closed-everything, deny-private,
  require-rules) and threat-list caching. Its strict flag will simply OR with
  the scope map. The hardening inbox file is annotated accordingly.
- **Level scheme** (per acceptance criteria): `rule_invalid` 7 (≥
  `INCIDENT_LEVEL_THRESHOLD` default 7 ⇒ auto-incident/pages), fail-open
  `lookup_failed` 6, abuse-flag or fail-closed-scope block 5, other block 3,
  allowlisted-pass-that-would-have-blocked 3 (category `geofence_exempt`).
  Escalation of 3/5/6 stays with incident RuleSet bundling — blocked-jurisdiction
  login traffic is the system working.
- **Allowlist = two sources, one decision step** — Setting-backed
  `GEOFENCE_ALLOWLIST` (CIDRs: office/VPN egress ranges, one reviewable list)
  plus per-IP `GeoLocatedIP.is_whitelisted` (already has management REST +
  assistant tooling). Rejected: a new allowlist model (a third store for the
  same concept). Decision priority: bypass → allowlist → rules; full exemption
  covers abuse flags too (owner ruling — developers on VPNs must not be
  blocked as `vpn_detected`).
- **Shadow evaluation on allowlisted requests** — the exemption must still
  produce would-have-blocked evidence (owner guardrail), so the pipeline runs
  anyway and only the decision authority changes. Cost bounded by the decision
  cache + GeoLocatedIP's stored rows. Rejected: fast-path return (loses the
  auditor signal).
- **Expiry is evaluated lazily** (`whitelist_active`, mirroring
  `block_active`) — no new cron; expired entries simply stop matching and are
  listed `active: false`. The firewall-side `is_whitelisted` readers switch to
  the property so expiry means one thing everywhere.
- **Invalidation + config events hook the model layer**
  (`whitelist()`/`unwhitelist()`, `Setting.save`), not the REST handlers —
  every write path (REST action, assistant tool, shell) stays cache-coherent
  and audited.

### Edge cases & risks
- **Malformed JSON already stored in the Setting row** (pre-validation writes):
  `kind="dict"` coercion must degrade to `{}` + `logit.error`, and eval-time
  `validate_rule` still catches bad-but-parseable rules as `rule_invalid`
  (level 7 — loud, as intended). Builder: verify `_convert_value` (:96-105)
  failure behavior.
- **Redis unavailable during dedupe** → emit the event anyway; never raise from
  `evidence.py` into the auth path (`try/except` + `logit.error` around the
  whole reporter).
- **Event flood from a blocked state hammering login** → per-(ip, reason)
  hourly dedupe; metrics still count every block (criterion).
- **scan_iter invalidation cost** — keyspace is bounded (TTL 300s default) and
  scan is incremental; acceptable for an emergency-edit path.
- **Header overrides** — all new headers go through the existing
  `_*_setting_with_header` wrappers, so the `test_mode_gate` protections apply
  unchanged.
- **`bypass_holders` truthiness** must match `User.has_permission` (a perm set
  to `false`/`0` must not list) — filter in Python after the JSONField
  `isnull=False` prefilter.
- **Zero behavior change when unconfigured**: `GEOFENCE_FAIL_CLOSED_SCOPES`
  defaults `[]`; all 23 existing `scope="auth"` endpoints keep fail-open
  defaults; the suite baseline must stay green.
- **Group metadata merge surprise**: partial `metadata.geofence` REST writes
  merge into the existing rule before validation — correct, but document that
  clearing requires `"__replace"` semantics.
- **`Setting.delete` path** doesn't run `save()` — the DELETE endpoint
  invalidates explicitly; verify whether `remove()`/`delete()` need the same
  hook for non-REST deletes.
- **Shadow-eval latency for allowlisted IPs**: the first uncached hit pays geo
  resolution (possibly a live provider fetch); subsequent hits are
  decision-cache hits. Acceptable; threat-list caching (hardening item C)
  reduces it further.
- **Bad allowlist entry at match time** (family mismatch, malformed CIDR
  written pre-validation): skip the entry + `logit.error`, never raise —
  allowlist failure degrades to normal evaluation, not an outage.
- **Expired whitelist previously suppressed firewall blocks** — switching
  `block_active`/`block()`/`update_threat_from_incident` to `whitelist_active`
  is a deliberate behavior fix; permanent (null-`until`) whitelists are
  unaffected.
- **Federation**: `whitelisted_until` joins the forbidden-sync fields — fleet
  members must not receive another node's exemptions (existing policy for all
  whitelist/block state).

### Tests (testit — `tests/test_geofence/`, per-request headers style)
New `config_plane.py`:
- POST `geo/rules` valid rule as user with `manage_geofence` → 200; `Setting`
  row exists; GET `geo/rules` returns it with `source: "setting"` + modified.
- POST `geo/rules` malformed rule (bad op / bad abuse key) → 400 with the
  `validate_rule` message; no Setting write.
- POST `geo/rules` without perm → 403; GET without perm → 403.
- Generic `/api/settings` write of key `GEOFENCE_SYSTEM_RULES` with a bad rule
  → 400 (model-level validation, change 6).
- **Cache invalidation (system)**: prime a cached deny via `geo/check`
  (`X-Mojo-Test-Geo` fixture + `Cache-Ttl` header > 0, rules from the real DB
  Setting — no system-rules header); POST `geo/rules` switching to allow;
  re-check same IP → new decision (stale cache would still deny).
- **Cache invalidation (group)**: same flow with `group_uuid` +
  `metadata.geofence` write through group REST.
- Group REST write of invalid `metadata.geofence` → 400 human-readable; valid
  write → 200.
- DELETE `geo/rules` removes the override (source back to conf/none) + emits
  the config event.
- Config-change events: after POST, an Event with `category="geofence_config"`
  exists carrying old/new/user (query via ORM in-process or events REST).
- POST `geo/allowlist` with valid entries (incl. one carrying `until`) → 200;
  GET returns both sources with `active` flags; Setting row written. Malformed
  CIDR or bad `until` → 400 naming the entry, no write; no-perm → 403.
- Allowlist cache invalidation: prime a cached deny via `geo/check`
  (cache-ttl header > 0); POST an allowlist covering the IP; re-check →
  `ip_allowlisted` (a stale cache would still deny). Same flow through the
  `/api/system/geoip` whitelist action (exercises `invalidate_ip`).
New `evidence_plane.py`:
- Blocked login (decorator path, `GEO_RU` + US-only system rule via header) →
  Event `category="geofence_block"`, level 3, metadata has reason/rule_level/
  scope.
- Same ip+reason again inside the hour → still 403 but **no second event**;
  `geofence:blocks` metric incremented both times (assert via metrics fetch API
  or Redis key).
- Abuse block (`GEO_TOR`) → level 5.
- Invalid rule injected via `X-Mojo-Test-Geofence-System` header (bypasses
  write validation deliberately) → block + level-7 event.
- Forced lookup failure, fail-open (default) → request succeeds + level-6
  event.
- Forced lookup failure with `X-Mojo-Test-Geofence-Fail-Closed-Scopes: auth` →
  403 + level-5 event (scope posture works end-to-end on a real auth endpoint).
- Simulate: geo-dict deny for `manage_geofence` user → full decision incl.
  abuse/geo fields; no Event created, no cache key written; without perm → 403.
- `bypass_holders`: user with truthy `bypass_geofence` listed; user with the
  key set falsy NOT listed; no-perm → 403.
- Effective rules: GET `geo/rules` shape — posture keys, allowlist_summary,
  evaluation_order, enforced_endpoints non-empty (auth endpoints registered).
- Allowlisted IP (setting CIDR via header, and separately a `GeoLocatedIP`
  row) + blocking rule (`GEO_RU` + US-only) → request ALLOWED with reason
  `ip_allowlisted`; Event `category="geofence_exempt"` carrying
  `would_block_reason`; repeat inside the hour → no duplicate event.
- Allowlisted IP + non-blocking geo → allowed, NO exempt event.
- `GeoLocatedIP` whitelist with a past `until` → not exempt (blocked again);
  listed `active: false` in GET `geo/allowlist`.
- In-process model regression: an expired whitelist no longer suppresses
  `block_active`/`block()` (the firewall-side reader switch); a permanent
  (null-`until`) whitelist still does.
Existing `engine.py`/`decorator.py`/`endpoint.py` tests must pass unchanged
(fail-open defaults untouched).

### Docs
- `docs/django_developer/account/geofence.md` — add Config Plane (endpoints,
  perms, the `GEOFENCE_SYSTEM_RULES` Setting row, automatic invalidation),
  Evidence Plane (event categories
  `geofence_block`/`geofence_exempt`/`geofence_config`, level scheme 3/5/6/7,
  dedupe key, metrics slugs), `GEOFENCE_FAIL_CLOSED_SCOPES`,
  `GEOFENCE_ALLOWLIST` + exemption semantics (priority order, expiry,
  evidence), simulate + bypass_holders + allowlist; **fix the noted drift**
  (403 body, reason codes, `rule_level`) while in there.
- `docs/django_developer/account/geoip.md` — document `whitelisted_until`,
  `whitelist_active`, and the extended whitelist action value
  (`{reason, ttl, until}`).
- `docs/web_developer/account/geofence.md` — **new**: `/api/geo/check`,
  `/api/geo/rules` (GET/POST/DELETE), `/api/geo/simulate`,
  `/api/geo/bypass_holders`, `/api/geo/allowlist` (GET/POST) —
  request/response/perms; note the extended `/api/system/geoip` whitelist
  action; plus index rows in `docs/web_developer/account/README.md` and
  `docs/web_developer/README.md`.
- `CHANGELOG.md` — feature block (config plane + evidence plane + scope
  posture + new perms), current top-block format.

### Open questions
None blocking. Five decisions flagged for sign-off (recommendations already
baked into this plan): (1) new `view_geofence`/`manage_geofence` perms instead
of reusing `manage_settings`; (2) change history via incident events instead of
audit columns on `Setting`; (3) bypass visibility + scope map built here and
dropped from `geofence-hardening`; (4) allowlisted requests shadow-evaluate the
rules so would-have-blocked evidence exists (small latency on the first
uncached hit per IP); (5) expired whitelists stop suppressing firewall
auto-blocking too (`whitelist_active` everywhere — a deliberate behavior fix).

## Notes

- **Baseline (2026-07-08, `bin/run_tests --agent`)**: status passed — total 2295,
  passed 2239, failed 0, skipped 56. All green; no pre-existing failures.
  (`testproject/var/test_failures.json`)
- Sibling filing (same program, 2026-07-07): web-mojo
  `admin-geofencing-section.md` (the admin UI consuming these endpoints —
  its scope should pin a hard dependency on this item once IDs exist);
  mverify_api `geofence-enforcement-payments.md`; wmx_api
  `geofence-five-touchpoints-and-loader.md`.
- Overlap warning for /scope: `planning/inbox/geofence-hardening.md` predates
  this item and covers strict posture + bypass visibility. Merge or sequence
  explicitly; do not build twice.
- Evidence-plane volume: blocked-jurisdiction login traffic is the system
  working, not an incident — hence level 3 + dedupe + metrics, with
  escalation left to incident bundling (same-subnet probing, post-rule-change
  spikes).
