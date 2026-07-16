---
# id is assigned by /scope on pickup — leave it blank
id: DM-010
type: bug
title: WebSocket IP resolver trusts client-spoofable sources (X-Forwarded-For / Forwarded)
priority: P2
effort: M
owner: backend
opened: 2026-06-30
depends_on: []
related: [DM-009]
links: []
---

# WebSocket IP resolver trusts client-spoofable sources (X-Forwarded-For / Forwarded)

## What & Why
The realtime WebSocket handler resolves the client IP with its OWN resolver — independent of
the HTTP `get_remote_ip()` hardened in DM-009 — and it prefers client-spoofable sources over
the proxy-authoritative `X-Real-IP`. In `mojo/apps/realtime/handler.py`, `resolve_remote_ip()`
and `get_remote_ip(scope)` try, in order: `scope["client"]`, the RFC 7239 `Forwarded: for=`
first entry, the **leftmost** `X-Forwarded-For`, and only then `X-Real-IP`. So a WS client can
set `X-Forwarded-For`/`Forwarded` and control the IP the server records — the same bug DM-009
fixed for HTTP, in a second, independently-coded resolver.

The resolved IP (`self.remote_ip`, handler.py:42) is **not** a direct auth gate on the WS
handshake (WS auth is token/message-based with `request=None`, so no HTTP `allowed_ips`/geofence
runs at connect) — so this is lower-severity than DM-009. But it IS a security-forensics
surface: it flows into the incident/security `Event` audit trail (`source_ip`/`request_ip` via
`report_incident()` → `incident/reporter.py` → `Event.source_ip`), drives geolocation and
per-country security metrics, lands in connection logs and the CSV audit export, and is passed
to the app's optional `on_realtime_connection()` hook (which an app could gate on). A spoofed WS
IP therefore pollutes the security audit trail, skews geo metrics, and can mislead incident
investigation (attacker-chosen origin country) — and it leaves `request.ip` semantics
inconsistent across transports right after DM-009 hardened the HTTP side.

Why now: same-class sibling of the just-closed DM-009; closing the WS half makes the
framework's IP handling consistent and the security-event trail trustworthy.

## Acceptance Criteria
- [ ] The WS resolver prefers the proxy-authoritative `X-Real-IP` (normalized) over
      `X-Forwarded-For`, the RFC 7239 `Forwarded` header, and `scope["client"]`. A forged
      `X-Forwarded-For`/`Forwarded` no longer changes the resolved WS IP when `X-Real-IP` is present.
- [ ] Leftmost `X-Forwarded-For` parsing (handler.py:83, 122) and the RFC 7239 `Forwarded for=`
      parse (handler.py:117) are removed as trusted sources.
- [ ] The resolved IP is normalized consistently with the HTTP path (same helper).
- [ ] `_normalize_ip` is shared — promoted to a public helper in `mojo/helpers/request.py` and
      used by BOTH `get_remote_ip` (HTTP) and the WS resolver; no duplicated normalization.
- [ ] A regression test reproduces the WS spoof (forged `X-Forwarded-For`/`Forwarded` must not
      win over `X-Real-IP`) and fails before the fix.
- [ ] The unix-socket transport case (no `X-Real-IP`, empty `scope["client"]`) has a defined,
      non-spoofable fallback.

## Repro — bugs only
1. Open a WebSocket to `/ws/` through the proxy with `X-Real-IP: <real>` and
   `X-Forwarded-For: 203.0.113.7` (or `Forwarded: for=203.0.113.7`).
2. Observe the resolved `self.remote_ip` — via a connection log line (handler.py:62), the
   `realtime:connections:{id}` Redis record (handler.py:206), or a reported incident's `source_ip`.
- Expected: the recorded WS IP is the real client (`X-Real-IP` / true transport peer),
  unaffected by the client-supplied `X-Forwarded-For`/`Forwarded`.
- Actual: the resolver returns the forged `X-Forwarded-For`/`Forwarded` value (preferred over
  `X-Real-IP`), so the spoofed IP is logged, stored in Redis, and written into security `Event`
  records.

## Investigation
**Root cause — confidence: confirmed** (read `mojo/apps/realtime/handler.py:67-129`).
- `resolve_remote_ip()` (handler.py:67-94): calls `get_remote_ip(scope)` first; then the
  request_headers fallback reads leftmost XFF (handler.py:82-83) BEFORE X-Real-IP
  (handler.py:84-85); final fallback transport peername (handler.py:87-91).
- `get_remote_ip(scope)` (handler.py:96-129) priority: (1) `scope["client"][0]` (98-100);
  (2) RFC 7239 `Forwarded for=` first entry (111-117); (3) leftmost XFF (120-122);
  (4) `X-Real-IP` (124-127). The authoritative header is LAST, behind three client-controllable ones.

**Blast radius (consumers of `self.remote_ip`, set handler.py:42):**
- Connection/access logging — handler.py:62 (`_log`).
- Redis connection metadata — handler.py:206 (`register_connection`), 229 (`update_connection_auth`):
  `realtime:connections:{id}`, TTL 300s.
- **Security/incident audit** — handler.py:721-722 (`report_incident` sets `source_ip` AND
  `request_ip`) → `mojo/apps/incident/reporter.py` → `Event.source_ip`
  (`mojo/apps/incident/models/event.py:42`) → geolocation (`Event.geo_ip` → `GeoLocatedIP.geolocate`),
  per-country security metrics, CSV audit export, security dashboard.
- App hook — handler.py:404 passes `remote_ip` to `user.on_realtime_connection()` (an app may gate on it).
- NOT a direct handshake gate: WS auth is token/message-based with `request=None`; no
  `allowed_ips`/geofence runs on connect. So no direct auth bypass — impact is forensic/observability
  (plus any app-hook enforcement).

**Open question for scope:** how `scope["client"]` is populated for WS over the **unix socket** —
uvicorn `--proxy-headers --forwarded-allow-ips=*` may rewrite it from XFF (→ spoofable), or it may
be the (empty, for a unix socket) transport peer. The shim `mojo/apps/realtime/asgi.py:120-127`
only wraps `scope.get("client")`; it does not rewrite it. The fix (prefer `X-Real-IP`) is correct
either way; scope should pin down whether `scope["client"]` is a safe fallback or another spoof vector.

**Fix shape (mirrors DM-009):** invert priority to prefer `X-Real-IP` (normalized) in both
`resolve_remote_ip()` and `get_remote_ip(scope)`; drop the leftmost-XFF and RFC 7239 `Forwarded`
parses as trusted sources; keep the transport peer only as a last-resort fallback. Promote
`mojo/helpers/request.py` `_normalize_ip` → public `normalize_ip` and reuse it (DRY) rather than
duplicating the normalization in the realtime app.

**Regression-test feasibility:** unit-testable. `get_remote_ip(scope)` takes a plain ASGI `scope`
dict — build `scope` with `headers` carrying a forged `x-forwarded-for`/`forwarded` plus an
authoritative `x-real-ip` and assert the resolver returns the `x-real-ip` value, not the forged
one (fails before the fix). `resolve_remote_ip()` needs a small fake `self.websocket` with
`.scope`/`.request_headers`; fake-object precedent in `tests/test_geofence/test_mode_gate.py`.
Existing realtime tests live in `tests/test_realtime/basic.py` (none cover IP resolution).

## Plan
### Goal
Make the WebSocket IP resolver prefer the proxy-authoritative `X-Real-IP` (normalized) and stop
trusting client-spoofable `X-Forwarded-For` / RFC 7239 `Forwarded` / `scope["client"]`, sharing
one normalizer with the HTTP path (DM-009).

### Context — what exists
- `mojo/apps/realtime/handler.py` — class `WebSocketHandler` (`__init__(self, websocket, path)`,
  line 32); `self.remote_ip = self.resolve_remote_ip()` at line 42 (only caller of it);
  `get_remote_ip(self, scope)` (line 96) is called only by `resolve_remote_ip` (line 74) and uses
  ONLY `scope`, not `self`.
  - `get_remote_ip(scope)` (96-129) current priority: `scope["client"][0]` → `Forwarded for=` →
    leftmost `X-Forwarded-For` → `X-Real-IP` (last).
  - `resolve_remote_ip()` (67-94): `get_remote_ip(scope)`; then `request_headers` reads leftmost
    XFF (82-83) BEFORE X-Real-IP (84-85); then `transport.get_extra_info("peername")` (87-91).
- `mojo/helpers/request.py` `_normalize_ip` (added in DM-009, ~lines 42-60) — the normalizer;
  its ONLY caller is `get_remote_ip` (HTTP) in the same file (grep `_normalize_ip` to confirm).
  DM-009's test (`tests/test_helpers/test_get_remote_ip.py`) exercises the PUBLIC `get_remote_ip`,
  not `_normalize_ip` directly — so renaming the helper does not break it. No other IP helper exists.
- Blast radius of `self.remote_ip` (handler.py:42): logging (62), Redis `realtime:connections:{id}`
  (206, 229), incident `Event.source_ip` via `report_incident()` (721-722) → `incident/reporter.py`
  → `Event.source_ip` (`incident/models/event.py:42`) → geolocation, per-country security metrics,
  CSV export; and `on_realtime_connection()` hook (404). NO IP gate runs on the WS handshake
  (token/message auth, `request=None`) — so this is forensic/audit-integrity, not an auth bypass.
- Tests: `tests/test_realtime/basic.py` drives full WS via `WsClient` (`testit/ws_client.py`); NONE
  cover IP resolution. `@th.django_unit_test` has Redis/Django available. ASGI `scope["headers"]` is
  a list of `(bytes, bytes)` pairs; `scope["client"]` is `(ip, port)` or None.

### Changes — what to do
1. `mojo/helpers/request.py` — rename `_normalize_ip` → **`normalize_ip`** (public); update the one
   caller `get_remote_ip` to call `normalize_ip`. Grep-confirm no other `_normalize_ip` references.
2. `mojo/apps/realtime/handler.py` — add `from mojo.helpers.request import normalize_ip` (top-level;
   no circular import — `helpers/request` does not import realtime). Rewrite both methods to prefer
   `X-Real-IP`, drop the XFF/`Forwarded` parses, normalize every return, keep transport peer last:
   ```python
   def get_remote_ip(self, scope):
       headers = {}
       for k, v in scope.get("headers", []):
           try: headers[k.decode().lower()] = v.decode()
           except Exception: pass
       ip = normalize_ip(headers.get("x-real-ip"))   # proxy-authoritative; never XFF/Forwarded
       if ip:
           return ip
       client = scope.get("client")                  # last-resort transport peer
       if client and client[0]:
           return normalize_ip(client[0])
       return None

   def resolve_remote_ip(self):
       try:
           scope = getattr(self.websocket, "scope", None)
           if scope:
               ip = self.get_remote_ip(scope)
               if ip: return ip
           headers = getattr(self.websocket, "request_headers", None)
           if headers:
               ip = normalize_ip(headers.get("x-real-ip") or headers.get("X-Real-IP"))
               if ip: return ip
           transport = getattr(self.websocket, "transport", None)
           if transport and hasattr(transport, "get_extra_info"):
               peer = transport.get_extra_info("peername")
               if peer:
                   raw = peer[0] if isinstance(peer, (tuple, list)) else str(peer)
                   return normalize_ip(raw)
       except Exception:
           self._log_exception("resolve_remote_ip")
       return None
   ```
3. `tests/test_realtime/ip_resolution.py` (new) — regression tests (see Tests).
4. `CHANGELOG.md` — security entry: WS client IP now from `X-Real-IP`; XFF/`Forwarded` no longer
   trusted on the realtime path; consistent with DM-009.
5. Docs — `docs/django_developer/realtime/` note on WS client-IP derivation; `helpers/request.md`
   gains the now-public `normalize_ip`. Post-build docs-updater syncs both tracks.

### Design decisions
- **Prefer `X-Real-IP`, demote `scope["client"]` to a fallback** — sidesteps the open question of
  whether uvicorn rewrites `scope["client"]` from XFF for WS over the unix socket: since `X-Real-IP`
  (set by `asgi.inc` on `/ws/`) always wins when present, a spoofable `scope["client"]` can never
  override it. XFF/`Forwarded` are dropped entirely (always client-controlled). Rejected: keeping
  `scope["client"]` first (current) — that's the spoof vector.
- **Promote `_normalize_ip` → public `normalize_ip`** — DRY; both transports use one normalizer.
  Required enabler for this fix (in the acceptance criteria), not a gratuitous refactor. Rejected:
  importing the private `_normalize_ip` across modules (uglier; underscore = private).
- **Transport peer as last resort** — the WS analog of DM-009's `REMOTE_ADDR` fallback; over the
  unix socket it's empty → `None`; behind `asgi.inc` never reached.
- **Pure-unit test via `object.__new__(WebSocketHandler)`** — `__init__` needs Redis
  (`get_connection()`, line 46), but the resolver methods only touch `self.websocket`, so bypassing
  `__init__` gives a dependency-free, deterministic test. `get_remote_ip(scope)` uses no `self`.

### Edge cases & risks
- `self.remote_ip` can be `None` (already possible pre-fix). Consumers tolerate it (logging prints
  it; Redis stores it; `Event.source_ip` pre-existing behavior). Same `None` posture as DM-009's
  Watch List item — not newly introduced here.
- Behavior change: WS-recorded IP now matches HTTP (real client, not the spoof). A deployment whose
  `/ws/` route doesn't set `X-Real-IP` falls back to transport peer/`None`; `asgi.inc`'s `/ws/`
  include sets it.
- Out of scope (separate items): geofence fail-open; nginx/uvicorn DiD; the DM-009 `UserLoginEvent`
  None-IP watch item.

### Tests
New `tests/test_realtime/ip_resolution.py`, `@th.django_unit_test` (safe import of the app handler),
imports inside the test fn. Build a minimal handler via `object.__new__(WebSocketHandler)`; a
`_FakeWS` with `.scope`/`.request_headers`/`.transport`; `scope["headers"]` as `(bytes, bytes)` pairs;
a `_FakeTransport` with `get_extra_info("peername")`. Every assert carries a message.
- **Spoof regression — forged XFF loses to X-Real-IP:** scope headers `x-real-ip=70.184.70.39` +
  `x-forwarded-for=203.0.113.7`, `client=None` → `get_remote_ip(scope) == "70.184.70.39"`.
  (old → `203.0.113.7`, fails; fixed → passes.)
- **Forged `Forwarded` loses:** `x-real-ip=70.184.70.39` + `forwarded=for=203.0.113.99` →
  `== "70.184.70.39"`. (old → `203.0.113.99`, fails.)
- **`scope["client"]` demoted below X-Real-IP:** `x-real-ip=70.184.70.39`, `client=("203.0.113.7",0)`
  → `== "70.184.70.39"`. (old → `203.0.113.7`, fails.)
- **`resolve_remote_ip` request_headers branch:** `scope=None`,
  `request_headers={"x-forwarded-for":"203.0.113.7","x-real-ip":"70.184.70.39"}` (via `_FakeWS`) →
  `resolve_remote_ip() == "70.184.70.39"`. (old → `203.0.113.7`, fails.)
- **Transport fallback (guard):** no X-Real-IP anywhere, `transport.peername=("1.2.3.4",5678)` →
  `resolve_remote_ip() == "1.2.3.4"`.
- **Normalization:** scope `x-real-ip=::ffff:1.2.3.4` → `get_remote_ip(scope) == "1.2.3.4"` (shared
  `normalize_ip` applied).
- Run: `bin/run_tests --agent -t test_realtime`. Capture the green default-suite baseline BEFORE
  editing, per `.claude/rules/build-baseline.md`.

### Docs
`CHANGELOG.md` (required). `docs/django_developer/realtime/` WS client-IP note; `helpers/request.md`
`_normalize_ip` → `normalize_ip` rename. web_developer minimal (WS). Post-build docs-updater syncs.

### Open questions
None blocking. The `scope["client"]`-over-unix-socket question (uvicorn-XFF-derived vs empty peer) is
neutralized by X-Real-IP-first (see Design) and does not gate the build.

## Notes
- Build baseline (2026-06-30, `bin/run_tests --agent`, default suite, HEAD includes DM-009):
  **GREEN** — 2266 total, 2210 passed, 0 failed, 56 skipped (`testproject/var/test_failures.json`,
  `"failures": []`). `test_realtime` 13/13. Any failure after this change is attributable to DM-010.
- Post-build agents (2026-06-30): full default suite green (2216 passed, 0 failed); docs updated in
  both tracks (commit 01d137e); security-review confirmed the spoof is closed, the `_normalize_ip` →
  `normalize_ip` rename is behavior-neutral (no stale callers), and `None` is handled safely by all
  WS-IP consumers. Acceptable residual: the `scope["client"]` fallback is only reached when
  `X-Real-IP` is absent (dead behind `asgi.inc`) — documented as a deployment requirement. One
  out-of-scope finding deferred to a follow-up: `incident/models/event.py:42`
  `Event.source_ip = CharField(max_length=16)` silently truncates native IPv6 — the new WS path
  normalizes/stores IPv6, making it more likely to surface (also flagged in the DM-009 audit).
  Fix needs a model migration → separate item.

## Resolution
- closed: 2026-06-30
- branch: main
- files changed: CHANGELOG.md,docs/django_developer/core/middleware.md,docs/django_developer/helpers/request.md,docs/django_developer/realtime/architecture.md,docs/web_developer/core/request_response.md,docs/web_developer/realtime/websocket.md,memory.md,mojo/apps/realtime/handler.py,mojo/helpers/request.py,planning/.next_id,planning/done/DM-009-get-remote-ip-trusts-client-supplied-x-forwarded-f.md,tests/test_helpers/test_get_remote_ip.py,tests/test_realtime/ip_resolution.py,uv.lock
- tests added: tests/test_realtime/ip_resolution.py (6 cases — X-Real-IP beats forged XFF; X-Real-IP beats forged Forwarded; scope[client] demoted below X-Real-IP; X-Real-IP normalization; resolve_remote_ip request_headers prefers X-Real-IP; transport-peer fallback)
