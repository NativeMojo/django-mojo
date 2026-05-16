# Mojo-as-GeoIP-Provider

**Type**: request
**Status**: resolved
**Date**: 2026-05-16
**Priority**: medium

## Description

Two related capabilities:

1. **Pull**: a new `mojo` GeoIP provider in `mojo/helpers/geoip/` lets one django-mojo instance fetch fully-enriched geolocation data from another django-mojo's authed `GET /api/system/geoip/lookup` endpoint, using a group-scoped `ApiKey`. Because the upstream record is already enriched with third-party detection (Tor / VPN / proxy / cloud / datacenter / mobile) and external blocklists, the downstream skips local third-party overlays for these records and trusts the upstream values.

2. **Push back**: when a record sourced from the `mojo` provider has an abuse signal raised locally — `threat_level` escalation, or `is_known_attacker` / `is_known_abuser` flipping to True from observed local event patterns — the new state is pushed back to the upstream via a new `POST /api/system/geoip/sync` endpoint. This federates observed abuse across a mesh of mojo instances and grows the shared threat picture — without leaking per-fleet firewall state (`is_blocked`, `is_whitelisted`, etc.).

## Context

- Today the GeoIP providers (`maxmind`, `ipinfo`, `ipstack`, `ip-api`) return raw location + ASN, then `geolocate_ip()` runs `detection.detect_tor`, `detection.detect_vpn_proxy_cloud`, and optionally `threat_intel.perform_threat_check` to fill in the security/threat fields.
- `/api/system/geoip/lookup` was changed from public to requiring an authenticated user / API key, so it's now suitable as an internal provider for other deployments.
- A "hub" mojo deployment with paid MaxMind and a populated `GeoLocatedIP` cache can serve as the upstream IP-intelligence source for smaller "spoke" deployments. The spokes inherit the hub's already-computed detection, ASN data, and threat metadata, and feed observed threat escalations back to the hub.
- Per-fleet decisions (firewall blocks, whitelists) stay local — federation is for *abuse signal* (threat_level, known_attacker/abuser observations), not enforcement state. Whitelists are local trust ("our office IP"), and blocks are local enforcement; neither belongs in the shared abuse list. But `is_known_attacker` / `is_known_abuser` are derived from observed event patterns and ARE genuine abuse signals — sharing them is exactly the point.
- `block()` currently does not bump `threat_level` — only `update_threat_from_incident()` does, and only on incident creation. Manual admin blocks, LLM-tool blocks, and direct programmatic blocks leave `threat_level` unchanged. We will centralize the escalation in `block()` itself so every entry point benefits and every block feeds the federation loop.

## Acceptance Criteria

- New provider module `mojo/helpers/geoip/mojo.py` exposing `fetch(ip_address, api_key=None)`, registered in `PROVIDERS` as `'mojo'`.
- The provider calls `GET {GEOIP_MOJO_PROVIDER_URL}/api/system/geoip/lookup?ip=<ip>&graph=detailed` with `Authorization: apikey <token>` sourced from `GEOIP_API_KEY_MOJO`.
- Returned dict includes every field a `GeoLocatedIP` instance absorbs via `setattr` (location, ASN/ISP, `is_tor/vpn/proxy/cloud/datacenter/mobile`, `mobile_carrier`, `is_known_attacker/abuser`, `threat_level`, `data`), plus `provider='mojo'`.
- **Firewall fields stripped at provider boundary**: `is_blocked`, `is_whitelisted`, `blocked_at`, `blocked_until`, `blocked_reason`, `block_count`, `whitelisted_reason` are removed from the returned dict.
- In `geolocate_ip()`: when the chosen provider is `mojo`, **skip** `detect_tor()` and `detect_vpn_proxy_cloud()`. Trust the upstream values for those flags.
- `threat_intel.perform_threat_check()` gains a `skip_external` parameter. When `provider == 'mojo'` AND `check_threats=True`, only local internal-event analysis runs; external blocklist calls (AbuseIPDB, blocklist.de) are skipped because the upstream already covered them.
- `GeoLocatedIP.block()` escalates `threat_level` to at least `high` when below, in the same atomic update as the block. Never downgrades.
- New endpoint `POST /api/system/geoip/sync` accepts `{ip, threat_level?, is_known_attacker?, is_known_abuser?}` (any subset of the three abuse-signal fields), requires `geoip_sync` permission on the calling `ApiKey`, and applies updates via MAX-for-`threat_level` and OR-for-the-booleans semantics with `from_sync=True` (no outbound re-push from the receiver).
- Push-back: when a `GeoLocatedIP` record with `provider == 'mojo'` has any abuse signal raised (`threat_level` strictly rises, or `is_known_attacker`/`is_known_abuser` flips False→True) AND `GEOIP_MOJO_PROVIDER_URL` is set AND the change did not arrive via `from_sync=True`, an **async job** (via `jobs.publish()`) posts the changed fields to the upstream sync endpoint. The local call (`block()`, `check_threats()`, `update_threat_from_incident()`) must NEVER perform the HTTP POST inline — it enqueues and returns immediately. Local action is never blocked by push-back failure or latency.
- No federation of `is_blocked`, `is_whitelisted`, `blocked_*`, `whitelisted_*`.

## Investigation

**What exists**:
- Provider plugin pattern: `mojo/helpers/geoip/{maxmind,ipinfo,ipstack,ipapi}.py` each expose `fetch(ip_address, api_key=None)`, registered in `PROVIDERS` at `mojo/helpers/geoip/__init__.py:20-25`.
- Post-fetch overlay in `mojo/helpers/geoip/__init__.py:139-196` runs Tor/VPN/proxy/cloud detection and (when `check_threats=True`) `threat_intel.perform_threat_check()`.
- `mojo/helpers/geoip/config.py:20-32` exposes `get_api_key(provider)`. Add a `GEOIP_API_KEY_MOJO` mapping.
- `mojo/helpers/geoip/threat_intel.py:224-252` — `perform_threat_check()` runs both `check_internal_threats` (queries local `Event` table, line 35-112) and `check_all_blocklists` (third-party HTTP calls, line 189-221). Needs a `skip_external` switch.
- `mojo/apps/account/models/geolocated_ip.py:200-228` — `refresh()` writes any matching key from the returned dict via `setattr`. Already handles richer payloads.
- `mojo/apps/account/models/geolocated_ip.py:258-292` — `update_threat_from_incident()` is the only place that currently raises `threat_level`. Uses `THREAT_LEVEL_ORDER` for non-downgrade comparisons.
- `mojo/apps/account/models/geolocated_ip.py:294-376` — `block()` does NOT touch `threat_level`. It does an atomic conditional `update()` to be race-safe.
- Block call sites (none need to change once `block()` is centralized):
  - `mojo/apps/incident/handlers/event_handlers.py:362` — rule-engine `BlockHandler.run`
  - `mojo/apps/incident/handlers/llm_agent.py:619` — LLM `block_ip` tool
  - `mojo/apps/incident/asyncjobs.py:42` — `broadcast_block_ip` iptables apply (downstream of `block()`)
  - `mojo/apps/account/models/geolocated_ip.py:472` — `on_action_block` admin REST
- Server side: `GET /api/system/geoip/lookup` at `mojo/apps/account/rest/device.py:37-45` is already authed and accepts `graph` via `request.DATA` (REST framework default). `detailed` graph returns the full record including `data` and `provider`.
- API key auth: `Authorization: apikey <token>` validated at `mojo/apps/account/models/api_key.py:204-235`. Per-key `permissions` dict gated by `has_permission()` (line 91-110).
- Async job primitive: `mojo.apps.jobs.publish(func_path, payload, idempotency_key=..., max_retries=..., backoff_base=...)` at `mojo/apps/jobs/__init__.py:39-100`.

**What changes**:
- **New**: `mojo/helpers/geoip/mojo.py` — `fetch(ip_address, api_key=None)` HTTP GET to upstream lookup endpoint; normalize + strip firewall fields.
- **New**: `mojo/apps/account/asyncjobs/__init__.py` (if missing) and `geoip_sync.py` — `push_threat_level(payload)` job posts to upstream sync endpoint with retries.
- **Modified**: `mojo/helpers/geoip/__init__.py` — register `mojo` in `PROVIDERS`; skip `detect_tor` / `detect_vpn_proxy_cloud` when `provider == 'mojo'`; pass `skip_external=True` to `perform_threat_check` when `provider == 'mojo'` AND `check_threats=True`.
- **Modified**: `mojo/helpers/geoip/config.py` — add `GEOIP_MOJO_PROVIDER_URL`, `GEOIP_MOJO_SYNC_ENABLED`, and `GEOIP_API_KEY_MOJO` mapping in `get_api_key()`.
- **Modified**: `mojo/helpers/geoip/threat_intel.py` — add `skip_external` parameter to `perform_threat_check()`; when True, skip `check_all_blocklists` but keep `check_internal_threats`.
- **Modified**: `mojo/apps/account/models/geolocated_ip.py` — add `from_sync=False` to `block()`, `update_threat_from_incident()`; centralize `threat_level` escalation to `high` inside `block()`; add `_maybe_push_threat_level(prev_level)` helper that enqueues the sync job when conditions are met.
- **Modified**: `mojo/apps/account/rest/device.py` — new `POST system/geoip/sync` endpoint requiring API key + `geoip_sync` perm.
- **Modified**: `mojo/apps/account/models/api_key.py` — no code change required; the new `geoip_sync` permission is just a string key set by the user when issuing keys.

**Constraints**:
- **Federation scope is `threat_level` only**: never push `is_blocked`, `is_whitelisted`, `is_known_attacker`, `is_known_abuser`, or any `blocked_*`/`whitelisted_*` field.
- **No downgrade ever**: receiver applies MAX of current and incoming levels using `THREAT_LEVEL_ORDER`.
- **Loop prevention**: receiver applies with `from_sync=True` so its own escalation paths do not trigger an outbound push. Combined with strict-rise gating, the federation reaches a natural fixed point.
- **Failure isolation**: provider HTTP failures, push-back HTTP failures, missing config, and missing API keys all return `None` / log and return — they never raise into the calling `geolocate()` / `block()` paths.
- **Backwards compatible**: existing providers and the existing `geolocate_ip` overlay behavior are unchanged for non-`mojo` providers.

**Related files**:
- `mojo/helpers/geoip/__init__.py`, `config.py`, `detection.py`, `threat_intel.py`, `maxmind.py` (reference shape)
- `mojo/apps/account/models/geolocated_ip.py`
- `mojo/apps/account/rest/device.py`
- `mojo/apps/account/models/api_key.py`
- `mojo/apps/jobs/__init__.py`
- `mojo/apps/incident/handlers/event_handlers.py`, `llm_agent.py` (block-call-site context — no edits)
- `docs/django_developer/account/geoip.md`, `docs/web_developer/account/geoip.md`

## Endpoints

| Method | Path | Description | Auth |
|---|---|---|---|
| GET | `/api/system/geoip/lookup` *(existing, consumed)* | Returns enriched `GeoLocatedIP` record. Use `?graph=detailed` to include raw `data`. | Authed user / API key (already in place) |
| POST | `/api/system/geoip/sync` *(new)* | Receive abuse-signal updates from a downstream mojo. Payload: `{ip, threat_level?, is_known_attacker?, is_known_abuser?}`. Applies MAX for `threat_level` and OR for the booleans, all with `from_sync=True`. | `ApiKey` with `geoip_sync` permission |

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `GEOIP_MOJO_PROVIDER_URL` | none | Base URL of upstream mojo (e.g. `https://hub.example.com`). Required when `mojo` is a configured provider. |
| `GEOIP_API_KEY_MOJO` | none | API key token issued by the upstream. |
| `GEOIP_MOJO_SYNC_ENABLED` | `True` | Master switch for outbound threat_level push-back. Set False to disable federation while still consuming. |
| `GEOIP_PRIMARY_PROVIDER` / `GEOIP_FALLBACK_PROVIDER` | unchanged | Can now be set to `mojo`. |

## Tests Required

- `mojo.fetch()` returns `None` when `GEOIP_MOJO_PROVIDER_URL` is unset.
- `mojo.fetch()` returns `None` when `GEOIP_API_KEY_MOJO` is unset.
- `mojo.fetch()` returns `None` on HTTP error / non-2xx / timeout — does not raise.
- `mojo.fetch()` returns a dict with `provider='mojo'` and full enriched fields on success (mocked HTTP).
- `mojo.fetch()` strips firewall fields from the returned dict.
- `geolocate_ip()` with provider `mojo` skips `detect_tor` and `detect_vpn_proxy_cloud` (regression guard: non-mojo provider still runs them).
- `perform_threat_check(skip_external=True)` runs internal-event analysis but skips external blocklist HTTP calls.
- `GeoLocatedIP.block()` escalates `threat_level` to `high` when current is `None` / `low` / `medium`; never downgrades `critical` or `high`; threat_level write is atomic with block fields.
- `POST /api/system/geoip/sync` requires `geoip_sync` permission (without it → 403).
- `POST /api/system/geoip/sync` applies MAX `threat_level` and OR for `is_known_attacker`/`is_known_abuser`; never downgrades; does NOT trigger outbound push (from_sync semantics).
- `POST /api/system/geoip/sync` rejects payloads containing block/whitelist fields.
- `POST /api/system/geoip/sync` accepts partial payloads (e.g. just `{ip, is_known_attacker: true}` without threat_level).
- Push-back: `block()` on a `provider='mojo'` record enqueues `push_abuse_signals` job when threat_level rises; on a non-mojo record does NOT.
- Push-back: `check_threats()` on a `provider='mojo'` record enqueues `push_abuse_signals` job when `is_known_attacker` or `is_known_abuser` flips False→True.
- Push-back: no change in any abuse-signal field does NOT enqueue.
- Push-back: `from_sync=True` does NOT enqueue.

## Out of Scope

- Federating `is_blocked`, `is_whitelisted`, or any per-fleet firewall/enforcement state.
- Bulk lookup or bulk sync endpoints (single-IP only).
- Two-way real-time push (e.g. WebSocket / SSE) — async job + HTTP POST is enough.
- Gating push-back on `GEOIP_MOJO_PROVIDER_URL` regardless of record provider — confirmed scoped to `provider == 'mojo'`.
- Server-side rate-limit or quota configuration for the sync endpoint (use existing `ApiKey.limits` if needed later).
- A dedicated "abuse mesh" admin UI / dashboard.
- Changes to `bouncer/assess.py`, `bouncer/views.py`, `bouncer/event.py` consumers of `GeoLocatedIP.geolocate()` — they remain provider-agnostic.

---

## Plan

**Status**: planned
**Planned**: 2026-05-16

### Objective
Add a `mojo` GeoIP provider that pulls fully-enriched records from another django-mojo's authed lookup, skip redundant local third-party detection for those records, and push back observed abuse signals (`threat_level` escalations and `is_known_attacker` / `is_known_abuser` flips) to the upstream so a federation of mojo instances builds a shared abuse list — without federating per-fleet firewall state.

### Steps

1. **`mojo/helpers/geoip/mojo.py` (NEW)** — `fetch(ip_address, api_key=None)`:
   - Read `GEOIP_MOJO_PROVIDER_URL` from `config`; return `None` if unset.
   - Read API key via `get_api_key('mojo')`; return `None` if unset.
   - HTTP GET `{url.rstrip('/')}/api/system/geoip/lookup?ip=<ip>&graph=detailed` with header `Authorization: apikey <token>`, 5s timeout.
   - On any exception / non-2xx: log and return `None`.
   - Parse JSON, extract `data` field (the upstream `on_rest_get` returns `{status, data: {...}}`).
   - Build the normalized dict: copy all relevant keys (`country_code`, `country_name`, `region`, `region_code`, `city`, `postal_code`, `latitude`, `longitude`, `timezone`, `asn`, `asn_org`, `isp`, `connection_type`, `mobile_carrier`, `is_tor`, `is_vpn`, `is_proxy`, `is_cloud`, `is_datacenter`, `is_mobile`, `is_known_attacker`, `is_known_abuser`, `threat_level`).
   - Set `provider='mojo'`.
   - Set `data` to the raw upstream payload's `data` JSON field (so consumers can still see the raw provider data the upstream cached).
   - Filter out `is_blocked`, `is_whitelisted`, `blocked_at`, `blocked_until`, `blocked_reason`, `block_count`, `whitelisted_reason` — never returned.

2. **`mojo/helpers/geoip/config.py`** — add settings + key mapping:
   - `MOJO_PROVIDER_URL = settings.get_static('GEOIP_MOJO_PROVIDER_URL', None)`
   - `MOJO_SYNC_ENABLED = settings.get_static('GEOIP_MOJO_SYNC_ENABLED', True, kind='bool')`
   - In `get_api_key()` `key_map`: `'mojo': 'GEOIP_API_KEY_MOJO'`.

3. **`mojo/helpers/geoip/__init__.py`** — register provider + skip local overlay for `mojo`:
   - Import `from . import mojo as mojo_provider` (avoid name collision with module-level `mojo` namespace).
   - Add to `PROVIDERS`: `'mojo': mojo_provider.fetch`.
   - In the post-fetch overlay block (line 139+): branch on `geo_data.get('provider')`. When `'mojo'`:
     - Skip `detect_tor()` and `detect_vpn_proxy_cloud()` entirely — trust upstream values already in `geo_data`.
     - When `check_threats=True`, call `threat_intel.perform_threat_check(ip_address, skip_external=True)` instead of the default.
     - Still calculate `threat_level` via `detection.calculate_threat_level(...)` only if upstream did not provide one; otherwise keep upstream's level. (Upstream always provides one in practice, but be defensive.)
   - For all other providers: behavior is unchanged.

4. **`mojo/helpers/geoip/threat_intel.py`** — add `skip_external` parameter:
   - `def perform_threat_check(ip_address, skip_external=False):`
   - When `skip_external=True`: call `check_internal_threats()` as today, but set `blocklist_results = {'blocklist_hits': [], 'is_blocklisted': False}` without calling `check_all_blocklists()`.
   - Existing callers unchanged (default `skip_external=False`).

5. **`mojo/apps/account/models/geolocated_ip.py`** — block escalation, push-back hook, sync semantics:
   - Add helper `_maybe_push_abuse_signals(self, prev_snapshot)`:
     - `prev_snapshot` is a dict `{threat_level, is_known_attacker, is_known_abuser}` captured before mutation.
     - Guard: `if self.provider != 'mojo': return`
     - Guard: `if not config.MOJO_SYNC_ENABLED: return` (import lazily)
     - Guard: `if not config.MOJO_PROVIDER_URL: return`
     - Build `changed` dict — include each field only if it strictly rose:
       - `threat_level`: include if `THREAT_LEVEL_ORDER.index(self.threat_level) > THREAT_LEVEL_ORDER.index(prev_snapshot['threat_level'])`
       - `is_known_attacker`: include if `self.is_known_attacker and not prev_snapshot['is_known_attacker']`
       - `is_known_abuser`: include if `self.is_known_abuser and not prev_snapshot['is_known_abuser']`
     - If `changed` is empty: return (no rise → no push).
     - Build payload `{ip, **changed}`.
     - Idempotency key derived from sorted `changed` items: `f"geoip_sync:{ip}:{sorted_field_summary}"` — distinct keys for distinct upward transitions.
     - Enqueue `jobs.publish('mojo.apps.account.asyncjobs.geoip_sync.push_abuse_signals', payload, idempotency_key=..., max_retries=5)`.
     - Wrap in try/except — push-back must never raise.
   - Add helper `_abuse_snapshot(self)` returning `{'threat_level': self.threat_level, 'is_known_attacker': self.is_known_attacker, 'is_known_abuser': self.is_known_abuser}` — used to capture state before mutation.
   - Modify `update_threat_from_incident(self, priority, block=False, from_sync=False)`:
     - Capture `prev = self._abuse_snapshot()` before mutation.
     - Compute new level as today.
     - After save: if `not from_sync`, call `self._maybe_push_abuse_signals(prev)`.
   - Modify `block(self, reason="manual", ttl=None, broadcast=True, from_sync=False)`:
     - Capture `prev = self._abuse_snapshot()` before the atomic `update()`.
     - Compute `new_level`: if `THREAT_LEVEL_ORDER.index(self.threat_level or None) < THREAT_LEVEL_ORDER.index('high')`, set to `'high'`; else keep `self.threat_level`.
     - Include `threat_level=new_level` in the same `GeoLocatedIP.objects.filter(...).update(...)` so the bump is atomic with the block.
     - After `refresh_from_db()`: if `not from_sync`, call `self._maybe_push_abuse_signals(prev)`.
   - Modify `check_threats(self, from_sync=False)`:
     - Capture `prev = self._abuse_snapshot()` before running the threat check.
     - After save: if `not from_sync`, call `self._maybe_push_abuse_signals(prev)`.
   - `unblock()`, `whitelist()`, `unwhitelist()`: no push-back, no abuse-signal mutation. Federated scope is rises only.

6. **`mojo/apps/account/asyncjobs/geoip_sync.py` (NEW)** — `push_abuse_signals(payload)`:
   - Register via the same pattern used in `mojo/apps/incident/asyncjobs.py`.
   - Payload: `{ip, threat_level?, is_known_attacker?, is_known_abuser?}` — any subset of the three signal fields, plus `ip`.
   - Read `GEOIP_MOJO_PROVIDER_URL` and `get_api_key('mojo')`. If either missing, log + return (no retry).
   - HTTP POST `{url}/api/system/geoip/sync` with `Authorization: apikey <token>`, body = payload, 10s timeout.
   - On 2xx: success. On 4xx (auth / perm / validation): log + return (no retry — won't fix itself). On 5xx / network: raise so the job retries with backoff.
   - Confirm asyncjobs package init exists at `mojo/apps/account/asyncjobs/__init__.py`; create if missing.

7. **`mojo/apps/account/rest/device.py`** — new sync endpoint:
   - Add after the existing geoip endpoints:
     ```python
     @md.POST('system/geoip/sync')
     @md.requires_params('ip')
     @md.requires_perms('geoip_sync')
     def on_geo_located_ip_sync(request):
         ip = request.DATA.get('ip')
         incoming_level = request.DATA.get('threat_level', None)
         incoming_attacker = request.DATA.get('is_known_attacker', None)
         incoming_abuser = request.DATA.get('is_known_abuser', None)

         # Reject any per-fleet enforcement fields up-front
         for forbidden in ('is_blocked', 'is_whitelisted', 'blocked_at',
                           'blocked_until', 'blocked_reason', 'block_count',
                           'whitelisted_reason'):
             if forbidden in request.DATA:
                 return {"status": False, "error": f"Field '{forbidden}' is not federated"}

         if incoming_level is not None and incoming_level not in ('low', 'medium', 'high', 'critical'):
             return {"status": False, "error": "Invalid threat_level"}

         geo = GeoLocatedIP.geolocate(ip, auto_refresh=False)
         update_fields = []
         applied = {}

         if incoming_level is not None:
             order = GeoLocatedIP.THREAT_LEVEL_ORDER
             prev_idx = order.index(geo.threat_level if geo.threat_level in order else None)
             new_idx = order.index(incoming_level)
             if new_idx > prev_idx:
                 geo.threat_level = incoming_level
                 update_fields.append('threat_level')
                 applied['threat_level'] = incoming_level

         # OR semantics for boolean abuse flags — never flip True→False
         if incoming_attacker is True and not geo.is_known_attacker:
             geo.is_known_attacker = True
             update_fields.append('is_known_attacker')
             applied['is_known_attacker'] = True

         if incoming_abuser is True and not geo.is_known_abuser:
             geo.is_known_abuser = True
             update_fields.append('is_known_abuser')
             applied['is_known_abuser'] = True

         if update_fields:
             # Direct save — does NOT call block()/check_threats(), so no outbound push fires.
             geo.save(update_fields=update_fields)

         return {"status": True, "data": {
             "ip": ip,
             "threat_level": geo.threat_level,
             "is_known_attacker": geo.is_known_attacker,
             "is_known_abuser": geo.is_known_abuser,
             "applied": applied,
         }}
     ```
   - Auth: `@md.requires_perms('geoip_sync')` — domain-category permission `geoip_sync`. The downstream's `ApiKey` must include `{"geoip_sync": true}` in its `permissions` dict.
   - No `@md.public_endpoint()` — must be authed.
   - Loop prevention: the endpoint uses raw `geo.save(update_fields=...)` rather than calling `block()`/`check_threats()`/`update_threat_from_incident()`, so the `_maybe_push_abuse_signals` hook is never reached on the receiver.

### Design Decisions

- **Skip local detection for `mojo` provider entirely** (not OR-merge): upstream is authoritative for third-party data. Cleanest semantics; no risk of conflicting flags.
- **`threat_intel.skip_external` flag** rather than a separate "internal-only" function: keeps one call site in `geolocate_ip()`, minimal API surface change, default unchanged.
- **`block()` escalates `threat_level` to `high`**: centralizes the rule so every block path (admin REST, LLM, rule engine, asyncjobs, manual) benefits without touching the incident app. The atomic single-`UPDATE` keeps it race-safe.
- **Federate abuse signals (`threat_level`, `is_known_attacker`, `is_known_abuser`) — never enforcement state**: `is_blocked` and `is_whitelisted` are per-fleet enforcement decisions; sharing them would force one fleet's firewall policy onto another. The three abuse signals are observed-behavior derivations and ARE the shared abuse list. Apply MAX for the level and OR for the booleans — never downgrade.
- **Push-back gated on `provider == 'mojo'`** (per user direction): only records pulled from a mojo upstream send signals back. A record whose location came from MaxMind isn't reported even if blocked locally.
- **Loop prevention via raw-save on receiver + strict-rise gating on sender**: the receiver's sync endpoint uses `save(update_fields=...)` directly rather than calling `block()`/`check_threats()`, so the `_maybe_push_abuse_signals` hook is never reached. Senders only push on strict rise / False→True flips, so chained federation (A→B→C) reaches a natural fixed point.
- **`whitelist()` does NOT touch `threat_level`** (and is not federated): whitelisting is local trust, not abuse signal.
- **Idempotency key derived from the changed-fields summary**: e.g. `geoip_sync:1.2.3.4:level=high|attacker=1` — prevents duplicate sends on job retry but allows distinct sends as different signals fire over time.
- **Async push via `jobs.publish` is mandatory, not optional**: matches existing asyncjob pattern in `mojo/apps/incident/asyncjobs.py`; built-in retries with backoff. The push-back call site MUST enqueue + return — it must not do the HTTP POST inline. Rationale: `block()` is called from rule-engine handlers, admin REST, and asyncjobs that are themselves time-sensitive; a 10s upstream timeout on every block would compound badly under load and could cascade into request timeouts in the calling REST handler.

### User Cases

- **Hub-only operator** (no spokes yet): adds `mojo` provider, sets URL+key — pulls work, push-back is a no-op for upstream-internal records (their `provider` field isn't `'mojo'` on the hub). Nothing to configure on the hub other than issuing keys with `geoip_sync` perm to spokes.
- **Spoke operator**: configures `GEOIP_PRIMARY_PROVIDER='mojo'` + URL + key. Now every IP lookup pulls enriched data from the hub. When an event escalates threat_level (incident system or admin block) or `check_threats()` flips an abuse flag, the hub gets a sync POST.
- **Admin blocks an IP manually** (REST `on_action_block` or LLM tool): `block()` sets `is_blocked=True` AND bumps `threat_level` to `high`. The threat_level rise triggers push-back if `provider=='mojo'`.
- **Incident rule blocks an IP** (`BlockHandler`): same flow as admin block. The incident's `update_threat_from_incident()` already ran first (event.py:401), potentially raising the level; `block()` may raise further; push-back fires once after `block()` completes.
- **Threat re-analysis** (`on_action_threat_analysis` or scheduled `check_threats()`): local Event count crosses `INTERNAL_THREAT_EVENT_THRESHOLD` → `is_known_attacker` flips False→True. Push-back posts `{ip, is_known_attacker: true}` to upstream. Spoke C pulling the same IP from the hub now inherits the attacker flag.
- **Same signal re-asserted**: no push (strict-rise / False→True gate).
- **Inbound sync from a peer spoke** (chained federation A→B): B's sync endpoint applies via direct `save(update_fields=...)`, never calls `block()`/`check_threats()`/`update_threat_from_incident()`, so no outbound push triggered.
- **Hub itself is a spoke of another hub**: hub's local actions push to its own upstream — chain works.
- **Sync POST with `ip=192.168.1.1`** (RFC1918): `GeoLocatedIP.geolocate()` creates an internal record with `provider='internal'`. Endpoint still applies the update. Push-back never triggers from internal records (gated on `provider=='mojo'`).

### Edge Cases

- **Upstream unreachable / returns 500**: `jobs.publish` retries the push job with exponential backoff (`max_retries=5`). Local action and DB state unaffected.
- **Upstream returns 4xx** (auth invalid, perm missing): job logs and returns without retry. Operator must fix config; signals queue up only via fresh escalations.
- **`MOJO_PROVIDER_URL` set but `GEOIP_API_KEY_MOJO` missing**: `fetch()` returns `None` → fallback chain handles it. Push-back job logs + returns.
- **Stale `provider` attribution**: record originally fetched from `maxmind`, later refreshed → now `provider='mojo'`. Push-back applies from that point forward; earlier blocks are not retroactively pushed. Acceptable.
- **Concurrent `block()` calls on the same IP** (race): the atomic `UPDATE ... WHERE (is_blocked=False OR blocked_until<=now)` already prevents double-block. Both callers re-read state; only the winner's `_maybe_push_abuse_signals()` will see a strict rise; the loser sees prev==new and does not push.
- **`threat_level` is `None` on first block**: `THREAT_LEVEL_ORDER` includes `None` as index 0; the bump-to-`high` logic and strict-rise comparison handle it correctly.
- **Push payload size**: `{ip, ...up to 3 signal fields}` is well under 200 bytes — far below `JOBS_PAYLOAD_MAX_BYTES`.
- **Idempotency clash**: identical payload posted twice → duplicate job is filtered by idempotency key. Acceptable: the receiver already has the equal-or-higher state.
- **Mixed-rise scenarios** (threat_level rises AND is_known_attacker flips in same operation): a single push includes both changed fields. Receiver applies both atomically in one save.
- **`is_known_attacker` flips True→False on receiver** (e.g. via direct admin action, not federated): the next downstream observation that flips it True→True won't push (strict-rise gate). Operator should re-trigger `check_threats()` if they want to re-federate. Acceptable.
- **Inbound sync with invalid `threat_level` string**: validated against the four valid values; returns `{status: False, error: ...}` with 200 status (matching existing endpoint patterns).
- **Inbound sync with `is_known_attacker: false` or `is_known_abuser: false`**: ignored (OR semantics — never downgrade). Field is not added to `applied`.
- **Inbound sync with forbidden field** (`is_blocked`, `is_whitelisted`, etc.): rejected with explicit error message.
- **`GEOIP_MOJO_SYNC_ENABLED=False`**: pull still works; push is fully disabled. Provides a kill switch.
- **A whitelisted IP receives an inbound sync**: threat_level/attacker/abuser still update (whitelist suppresses blocking, not threat tracking). Existing behavior of `is_whitelisted` is unchanged.

### Testing

- `tests/test_geoip_mojo_provider.py` — `fetch()` missing URL/key returns None; HTTP error returns None; 200 returns enriched dict with `provider='mojo'`; firewall fields stripped from result.
- `tests/test_geoip_overlay.py` — `geolocate_ip()` with `provider='mojo'` does NOT call `detect_tor`/`detect_vpn_proxy_cloud`; with other providers it still does.
- `tests/test_geoip_threat_intel.py` — `perform_threat_check(skip_external=True)` runs internal but does not call `check_all_blocklists`; default still runs both.
- `tests/test_geoip_block.py` (extends existing if any) — `block()` raises `threat_level` to `high` when below; never downgrades `critical`/`high`; level is part of atomic UPDATE.
- `tests/test_geoip_sync_endpoint.py` — `POST system/geoip/sync` requires `geoip_sync` perm (403 without); applies MAX `threat_level` and OR for booleans; accepts partial payloads (just one of the three signals); rejects invalid levels; rejects payloads containing block/whitelist fields; ignores boolean `False` values (OR semantics); does NOT push outbound when applying.
- `tests/test_geoip_pushback.py` — `block()` on `provider='mojo'` enqueues `push_abuse_signals` on threat_level rise; `check_threats()` on `provider='mojo'` enqueues when `is_known_attacker` or `is_known_abuser` flips False→True; combined rises enqueue ONE job with all changed fields; do NOT enqueue when nothing rose, when `from_sync=True`, when `provider != 'mojo'`, when `GEOIP_MOJO_SYNC_ENABLED=False`, or when `GEOIP_MOJO_PROVIDER_URL` unset.
- `tests/test_geoip_pushback_job.py` — `push_abuse_signals` job POSTs correct body + auth header; 4xx returns without retry; 5xx raises for retry; missing config logs + returns.
- `tests/test_geoip_pushback_async.py` — `block()` returns within a tight time budget even when the mocked HTTP push-back layer would block for seconds; confirms the push happens via `jobs.publish` and not inline.

All tests follow project conventions (`testit`, `@th.django_unit_test()`, `request.DATA`, no `override_settings`; use `th.server_settings(...)` for settings overrides).

### Docs

- `docs/django_developer/account/geoip.md`:
  - Add `mojo` to the providers table.
  - New section: "Federation with another mojo instance" — pull semantics, push-back rules, federation scope table (federated: `threat_level`, `is_known_attacker`, `is_known_abuser`; not federated: `is_blocked`, `is_whitelisted`, `blocked_*`, `whitelisted_*`).
  - Document `block()` now escalates `threat_level` to `high`.
  - Document new `from_sync` parameter on `block()` / `update_threat_from_incident()` / `check_threats()`.
- `docs/web_developer/account/geoip.md`:
  - Document new `POST /api/system/geoip/sync` endpoint with payload (partial subset of `{threat_level, is_known_attacker, is_known_abuser}`), perm (`geoip_sync`), and response shape (`applied` dict showing which fields actually changed).
  - Clarify the existing lookup endpoint now requires auth (already updated per user).
- `docs/django_developer/account/README.md` and `docs/web_developer/account/README.md` — verify the geoip page index links are current; no new files to add.
- `CHANGELOG.md` — entry for: mojo provider, sync endpoint, block→threat_level escalation, abuse-signal federation (threat_level + is_known_attacker + is_known_abuser).

---

## Resolution

**Status**: resolved
**Date**: 2026-05-16
**Commits**: `df658c6` (implementation + tests), `e006df5` (security review fixes)

### What Was Built

A new `mojo` GeoIP provider that lets one django-mojo instance pull fully-enriched
records from another's authed `GET /api/system/geoip/lookup`, plus a federation
loop so observed abuse signals on the downstream get pushed back to the upstream:

- **Pull**: `mojo/helpers/geoip/mojo.py` provider calls the upstream with
  `Authorization: apikey <token>`. Strips per-fleet firewall fields at the
  boundary. The downstream skips local Tor / VPN / proxy / cloud detection for
  `provider == 'mojo'` records (upstream is authoritative). Local-only
  `threat_intel.check_internal_threats()` still runs via the new
  `skip_external=True` flag.
- **`block()` escalation**: every block path now atomically bumps `threat_level`
  to at least `high` in the same UPDATE so the federation loop fires for every
  entry point (admin REST, LLM agent, incident rule engine, asyncjobs, manual)
  without changes to incident code.
- **Push-back**: when a `provider == 'mojo'` record has `threat_level` strictly
  rise OR `is_known_attacker` / `is_known_abuser` flip False→True, an async
  `push_abuse_signals` job posts the change to upstream
  `POST /api/system/geoip/sync`. Mandatory async — never inline HTTP.
- **Sync endpoint**: gated by `geoip_sync` permission on the calling `ApiKey`.
  Applies MAX for `threat_level` and OR for the booleans (never downgrades).
  Explicitly rejects per-fleet firewall fields. Uses raw
  `save(update_fields=...)` so it never re-fires the outbound hook (loop
  prevention).
- **Dedicated `federation` graph** on `GeoLocatedIP` — emits only the federated
  abuse-signal and location fields, never firewall state. Mojo provider
  requests `graph=federation`.

### Federation Scope

| Federated | Not federated |
|---|---|
| `threat_level` (MAX) | `is_blocked`, `blocked_*`, `block_count` |
| `is_known_attacker` (OR, False→True only) | `is_whitelisted`, `whitelisted_reason` |
| `is_known_abuser` (OR, False→True only) | |

### Files Changed

**Source**:
- `mojo/helpers/geoip/mojo.py` (NEW) — `fetch()` provider, requests `federation` graph, strips firewall fields defensively
- `mojo/helpers/geoip/__init__.py` — registered `mojo` in `PROVIDERS`, added `provider == 'mojo'` overlay branch that skips local detection and routes threat checks through `skip_external=True`
- `mojo/helpers/geoip/config.py` — `MOJO_PROVIDER_URL`, `MOJO_SYNC_ENABLED`, `'mojo'` API key mapping
- `mojo/helpers/geoip/threat_intel.py` — `skip_external=False` parameter on `perform_threat_check()`
- `mojo/apps/account/models/geolocated_ip.py` — `block()` atomically escalates `threat_level` to `high`; added `from_sync=False` parameter to `block()`, `update_threat_from_incident()`, `check_threats()`; added `_abuse_snapshot()` and `_maybe_push_abuse_signals()` helpers; added `federation` RestMeta graph
- `mojo/apps/account/rest/device.py` — new `POST /api/system/geoip/sync` endpoint with `geoip_sync` perm and 60/min rate limit
- `mojo/apps/account/asyncjobs.py` — added `push_abuse_signals(job)` handler with 4xx→drop, 5xx→retry semantics

**Tests** (43 new tests, all passing):
- `tests/test_account/test_geoip_mojo_provider.py` — fetch success/failure, firewall-field stripping, `skip_external` flag
- `tests/test_account/test_geoip_block_escalation.py` — `block()` threat_level escalation across all transitions
- `tests/test_account/test_geoip_pushback.py` — push hook fires/doesn't fire under all gating conditions; mandatory async (never inline HTTP); failure isolation
- `tests/test_account/test_geoip_pushback_job.py` — `push_abuse_signals` job body shape, retry vs no-retry, missing config handling, payload field allowlist
- `tests/test_account/test_geoip_sync_endpoint.py` — REST endpoint perm gate, MAX/OR semantics, partial payloads, forbidden-field rejection, loop-prevention guarantee

**Docs** (updated by docs-updater agent):
- `docs/django_developer/account/geoip.md` — providers table, "Federation with Another Mojo Instance" section, `from_sync` parameter, `block()` escalation, new sync endpoint
- `docs/web_developer/account/geoip.md` — `POST /api/system/geoip/sync` contract, response shape, perm requirement, `graph` param on lookup
- `CHANGELOG.md` — unreleased entry

### Settings Added

| Setting | Default | Purpose |
|---|---|---|
| `GEOIP_MOJO_PROVIDER_URL` | `None` | Base URL of upstream mojo |
| `GEOIP_API_KEY_MOJO` | `None` | API key token issued by upstream |
| `GEOIP_MOJO_SYNC_ENABLED` | `True` | Outbound push-back kill switch |

### Test Run

- `bin/run_tests --agent -t test_account.test_geoip_mojo_provider -t test_account.test_geoip_block_escalation -t test_account.test_geoip_pushback -t test_account.test_geoip_pushback_job -t test_account.test_geoip_sync_endpoint` — **43/43 pass**
- Full `bin/run_tests --agent -t test_account` — **133/133 pass** (no regressions in existing geoip tests)
- Full `bin/run_tests --agent` (no `--full`) — **1956/1956 pass** (run by test-runner agent)

### Security Review

The security-review agent flagged seven findings; the actionable ones were fixed
in `e006df5`:

- **HIGH (FIXED)**: `detailed` graph emits firewall state (`is_blocked`,
  `is_whitelisted`, `blocked_*`, `whitelisted_*`) to any authenticated caller —
  not just downstream provider fetches. Introduced a dedicated `federation`
  graph that includes only the federated abuse-signal and location fields, and
  switched the mojo provider to request `graph=federation`. The boundary scrub
  in `fetch()` remains as defense in depth.
- **MEDIUM (FIXED)**: No rate limit on `POST /api/system/geoip/sync`. Added
  `@md.rate_limit("geoip_sync", ip_limit=60)`.
- **INFO (FIXED)**: Loop prevention relied on code-discipline. Added
  `test_sync_does_not_enqueue_pushback` that posts a sync update to a
  `provider='mojo'` record and asserts zero `push_abuse_signals` jobs were
  enqueued.
- **MEDIUM (documented, not changed)**: SSRF surface via admin-controlled
  `GEOIP_MOJO_PROVIDER_URL`. Admin-only risk; mitigation would be a scheme
  allowlist enforced at startup. Tracked as a follow-up.
- **MEDIUM (by design, documented)**: A compromised spoke key with `geoip_sync`
  permission can mark any IP as a known attacker on the hub. This is the
  fundamental federation trust model — spoke keys are as trusted as the hub
  admins who issue them. Recommend hub operators audit issued keys.
- **LOW (by design)**: Idempotency key doesn't include instance identity. Two
  spokes independently observing the same threat will dedupe to one push. This
  is intentional cross-fleet deduplication and acceptable.
- **INFO (deferred)**: `GEOIP_MOJO_SYNC_ENABLED` defaults to `True`. The push
  is also gated on `MOJO_PROVIDER_URL` being set, so an unconfigured instance
  cannot accidentally push. Default kept as `True` for ergonomics.

### Pre-existing Failures (Not Caused By This Change)

`bin/run_tests --agent --full -t test_incident -t test_security` surfaced 8
failures, all unrelated to this commit:

- `test_incident`: 4 failures about ruleset priority matching, ruleset
  disabling in LLM loop, `HANDLER_MAP` containing `resolve`, and `TicketNote`
  attribute access — none touch GeoLocatedIP or the federation paths.
- `test_security`: 3 failures with `KeyError 'type'` in
  `tests/test_security/test_routes.py:235`, plus 1 PII metadata wipe issue.
  The `KeyError 'type'` is a pre-existing framework gap — the
  `requires_geofence` decorator adds a `'geofence'` subkey to
  `SECURITY_REGISTRY` without setting `'type'`, so any route decorated with
  only `requires_geofence` + `requires_bouncer_token` triggers it. Multiple
  such routes exist in `mojo/apps/account/rest/user.py`. Out of scope for this
  request.

### Follow-up

- Optional: add startup validation of `GEOIP_MOJO_PROVIDER_URL` (scheme
  allowlist) to harden the admin-only SSRF surface.
- Optional: change `GEOIP_MOJO_SYNC_ENABLED` default to `False` as a stricter
  feature-flag default.
- Pre-existing framework gap: `requires_geofence` should set
  `SECURITY_REGISTRY['type']` so route-security audit tests don't `KeyError`.
- Pre-existing test_incident/test_security failures should be investigated
  separately.
