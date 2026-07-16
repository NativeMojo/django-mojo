---
# id is assigned by /scope on pickup — leave it blank
id: DM-009
type: bug
title: get_remote_ip() trusts client-supplied X-Forwarded-For (IP spoofing)
priority: P1
effort: S
owner: backend
opened: 2026-06-30
depends_on: []
related: []
links: []
---

# get_remote_ip() trusts client-supplied X-Forwarded-For (IP spoofing)

## What & Why
`mojo/helpers/request.py:40-46` `get_remote_ip()` derives the client IP by taking the
**leftmost** `X-Forwarded-For` entry (`x_forwarded_for.split(',')[0]`). The leftmost
entry is the value nearest to — and fully controlled by — the client. There is no
trusted-proxy boundary, no hop count, no `.strip()`, and no IP normalization. The
result is stored as `request.ip` (`mojo/middleware/mojo.py:34`) and consumed across the
framework, so a single attacker-supplied request header sets the IP the whole system
treats as authentic.

This is exploitable today and was **proven against production** (see Repro). Because
`request.ip` feeds jurisdictional geofencing, JWT/API-key `allowed_ips` enforcement,
rate limiting, audit logging, and login-anomaly geo, the consequences include an
**auth-control bypass** (forge an allowed IP to satisfy an IP-restricted token), a
**jurisdictional-compliance bypass** (claim any country — compounded by the geofence
failing open on unknown IPs), rate-limit evasion, and a poisoned audit trail.

Why now: a trivially exploitable spoof in a security-focused framework, and it
underpins the IP-geolocation compliance work.

## Acceptance Criteria
- [ ] A client-supplied `X-Forwarded-For` can no longer set `request.ip` to an arbitrary
      value; `request.ip` reflects nginx's authoritative `X-Real-IP`/`$remote_addr` (the
      true client), not the leftmost client-supplied XFF entry.
- [ ] The spoof repro below no longer changes the echoed `request.ip` away from the real
      client.
- [ ] `get_remote_ip()` normalizes the value: `.strip()`, strip an `IP:port` suffix,
      unwrap bracketed IPv6 `[::1]`, collapse IPv4-mapped IPv6 `::ffff:1.2.3.4`. Returns a
      clean address (or a well-defined empty/None) for bare IPv4, bare IPv6, IPv4-mapped
      IPv6, bracketed IPv6, and `IP:port`.
- [ ] A regression test asserts the spoof is blocked (a forged `X-Forwarded-For` does NOT
      become `request.ip` when `X-Real-IP` is set) and that normalization handles the
      IPv6/port/bracket cases.
- [ ] Defense-in-depth follow-ups (nginx XFF overwrite, uvicorn `--forwarded-allow-ips`)
      are filed/noted as separate, non-blocking items (see Investigation → Fix is standalone).

## Repro — bugs only
Authorized live test against `api.mojoverify.com/api/version` (echoes `request.ip`).
Script: `scratchpad/xff_spoof_test.py` (stdlib only; RFC 5737 documentation IPs).
1. `GET /api/version` with no forwarded header → `{"ip":"<real client>"}`.
2. `GET /api/version` with `X-Forwarded-For: 203.0.113.7` → `{"ip":"203.0.113.7"}`.
3. `GET /api/version` with `X-Forwarded-For: 203.0.113.7, 198.51.100.9` → `{"ip":"203.0.113.7"}` (leftmost wins).
- Expected: the server reports the true client IP, unaffected by a client-supplied `X-Forwarded-For`.
- Actual: the server reports whatever IP the client places in the leftmost `X-Forwarded-For` position.

Controls (confirm the single vector): `X-Real-IP` is NOT reflected (nginx overwrites it
with `$remote_addr`); RFC 7239 `Forwarded` is NOT reflected (the HTTP helper doesn't
parse it — only the WS handler does).

## Investigation
**Root cause — confidence: confirmed** (read the source directly + empirically exploited in prod).
`mojo/helpers/request.py:40-46`:
```python
def get_remote_ip(request):
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]      # <-- leftmost = client-controlled
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip
```

**Consumers** (all read `request.ip`, set once at `mojo/middleware/mojo.py:34`):
- Geofence / jurisdiction: `mojo/apps/account/services/geofence/engine.py:188` (engine also
  fails open on unknown IPs at `:228-234` — compounds this).
- JWT / API-key `allowed_ips`: `mojo/models/auth.py:99`, `mojo/apps/account/models/user.py:1591,1609`
  → **auth bypass** (forge an allowed IP). NB: only bites keys that actually set `allowed_ips`
  (default is empty → check skipped).
- Rate limiting: `mojo/decorators/limits.py:113,173,256`.
- Audit log: `mojo/apps/logit/models/log.py:101`.
- Login-anomaly geo: `mojo/apps/account/models/login_event.py:123`.

**Topology — CONFIRMED via production access logs (2026-06-30), BOTH deployment modes:**
nginx's `$remote_addr` is the real public client in both the direct deployment
(`api.mojoverify.com`) and the load-balanced deployment (`lb.inovopay.net`). Proof: requests
to a fake path with a forged `X-Forwarded-For: 203.0.113.7` logged `$remote_addr =
70.184.70.39` (the test client's real egress), and the forged header did NOT change it.
Behind the LB, BOTH backend nodes (`s2`, `s3`) logged the real client `70.184.70.39`, NOT a
private LB IP — so the LB preserves/recovers the client IP (IP-preserving L4 LB, or nginx
`realip` already configured there). So `$remote_addr` is the unspoofable true client in BOTH
modes; the spoof is purely a Django-side bug (reads leftmost `X-Forwarded-For` instead of the
authoritative `$remote_addr`/`X-Real-IP`). The nginx access logs already record the true
client; only the app-level `request.ip` (audit/geofence/etc.) is poisoned.

**Fix is uniform across both deployments and standalone in this repo.** `get_remote_ip()`
should read `HTTP_X_REAL_IP` (normalized), falling back to `REMOTE_ADDR`. Since `$remote_addr`
is the true client in both modes (see Topology), reading the nginx-set `X-Real-IP` kills the
spoof in both. Prefer `X-Real-IP` over `REMOTE_ADDR`: over the unix socket, uvicorn
(`--proxy-headers --forwarded-allow-ips=*`, `systemd/mojo-asgi.service:21`) rewrites
`scope["client"]`/`REMOTE_ADDR` from XFF, so `REMOTE_ADDR` is itself spoofable; the
`X-Real-IP` header is untouched by uvicorn.
**PRECONDITION — satisfied for all deployments.** The fix relies on nginx setting
`X-Real-IP $remote_addr` AND overwriting any client-supplied `X-Real-IP`. This lives in the
shared `asgi.inc` (`asgi.inc:8`), which is the default include in ALL deployments (confirmed
by the team 2026-06-30) — so every deploy sets `X-Real-IP` from `$remote_addr` and discards
client-supplied values (verified empirically: a client `X-Real-IP` is not reflected). Combined
with `$remote_addr` = the true client in both tested topologies (direct + LB), reading
`X-Real-IP` yields the real client everywhere. Residual assumption: any LB tier preserves the
client into `$remote_addr` as the tested `lb.inovopay.net` does; a terminating LB that neither
preserves client IP nor runs `realip` would surface the LB's IP and need `realip` added there.
**Defense-in-depth (separate, non-blocking, per-deployment nginx/uvicorn):** overwrite inbound
XFF (`proxy_set_header X-Forwarded-For $remote_addr;`) and narrow uvicorn
`--forwarded-allow-ips` from `*`, so XFF and `REMOTE_ADDR` become trustworthy for any other
reader (e.g. the WS handler). The earlier realip / `set_real_ip_from` plan is NOT needed —
`$remote_addr` is already the true client in both modes.

**Related (likely separate items):**
- WebSocket resolver has its own independent leftmost-XFF parsers at
  `mojo/apps/realtime/handler.py:83,117,122` (and prefers `scope["client"]`, which uvicorn
  rewrites from XFF under `--forwarded-allow-ips=*`).
- Geofence fail-open default (`GEOFENCE_FAIL_CLOSED=False`) — separate compliance item that
  amplifies this one.

**Regression-test feasibility:** straightforward. A testit unit test builds a request with a
forged `HTTP_X_FORWARDED_FOR` and a set `HTTP_X_REAL_IP` and asserts `get_remote_ip()` returns
the `X-Real-IP` value, not the forged leftmost XFF entry; plus table-driven normalization
cases for IPv6 / bracket / port. No live network needed — the helper reads `request.META`.

## Plan
### Goal
Stop `get_remote_ip()` trusting the client-supplied `X-Forwarded-For`; derive `request.ip`
from the proxy-authoritative `X-Real-IP` (fallback `REMOTE_ADDR`), normalized — killing the
spoof across both deployment topologies (direct + load-balanced).

### Context — what exists
- `mojo/helpers/request.py:40-46` — current `get_remote_ip()`:
  ```python
  def get_remote_ip(request):
      x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
      if x_forwarded_for:
          ip = x_forwarded_for.split(',')[0]   # leftmost = client-controlled
      else:
          ip = request.META.get('REMOTE_ADDR')
      return ip
  ```
- Set once as `request.ip` at `mojo/middleware/mojo.py:34` (`request.ip = rhelper.get_remote_ip(request)`).
  Consumers of `request.ip`: geofence `mojo/apps/account/services/geofence/engine.py:188`;
  `allowed_ips` `mojo/models/auth.py:99`, `mojo/apps/account/models/user.py:1591,1609`; rate
  limits `mojo/decorators/limits.py:113,173,256`; audit `mojo/apps/logit/models/log.py:101`;
  login geo `mojo/apps/account/models/login_event.py:123`.
- Proxy layer (confirmed 2026-06-30): the shared `asgi.inc:8` sets
  `proxy_set_header X-Real-IP $remote_addr` (overwrites any client value) and is the default
  include on ALL deployments; `$remote_addr` = real client in both direct (`api.mojoverify.com`)
  and load-balanced (`lb.inovopay.net`) topologies. So `HTTP_X_REAL_IP` is the true client.
- No existing IP-normalization helper in `mojo/helpers/` (must write fresh). `ipaddress` is
  already imported at `mojo/helpers/geoip/__init__.py:10`. No existing test references `get_remote_ip`.
- Test mechanics: testit pure unit tests use `@th.unit_test("name")` + `def test_x(opts):`,
  under `tests/test_<domain>/`, imports inside the fn, every assert needs a message
  (`docs/django_developer/testit/Overview.md:208-234,269`). Fake-request precedent:
  `tests/test_geofence/test_mode_gate.py:15-25` (`_FakeRequest` with `.META`); `objict` works as
  a request stand-in. Header-injection integration precedent:
  `tests/test_public_messages/1_submit.py:328-345` (`opts.client.post(..., headers={...})`).

### Changes — what to do
1. `mojo/helpers/request.py` — add `import ipaddress`; add module-private `_normalize_ip(value)`;
   rewrite `get_remote_ip(request)` to read `HTTP_X_REAL_IP` (normalized), fall back to
   `REMOTE_ADDR` (normalized), and **stop reading `HTTP_X_FORWARDED_FOR`**. Leave `get_ip_sources()`
   untouched (diagnostic only). Target shape:
   ```python
   import ipaddress

   def _normalize_ip(value):
       if not value:
           return None
       ip = value.strip()
       if ip.startswith('[') and ']' in ip:        # [::1]:443 -> ::1
           ip = ip[1:ip.index(']')]
       elif ip.count(':') == 1:                     # 1.2.3.4:5678 -> 1.2.3.4
           ip = ip.split(':', 1)[0]
       try:
           addr = ipaddress.ip_address(ip)
       except ValueError:
           return None
       if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
           addr = addr.ipv4_mapped                  # ::ffff:1.2.3.4 -> 1.2.3.4
       return str(addr)

   def get_remote_ip(request):
       # nginx (asgi.inc) sets X-Real-IP to the true client ($remote_addr) and overwrites any
       # client-supplied value; trust that. Never parse the client-controlled X-Forwarded-For.
       # Fall back to REMOTE_ADDR only when X-Real-IP is absent.
       ip = _normalize_ip(request.META.get('HTTP_X_REAL_IP'))
       if ip is None:
           ip = _normalize_ip(request.META.get('REMOTE_ADDR'))
       return ip
   ```
2. `tests/test_helpers/test_get_remote_ip.py` (new; create `tests/test_helpers/` if absent) —
   pure `@th.unit_test` regression + normalization (see Tests).
3. `CHANGELOG.md` — security entry: client IP now derived from `X-Real-IP` (set by the trusted
   proxy); `X-Forwarded-For` no longer trusted; deployments must ensure their reverse proxy sets
   `X-Real-IP` to the real client (the shipped `asgi.inc` does).
4. Docs — any `docs/django_developer/` middleware/request/client-IP page; brief
   `docs/web_developer/` note that clients can't influence their recorded IP via `X-Forwarded-For`.
   CHANGELOG is the required minimum; the post-build docs-updater agent syncs both tracks.

### Design decisions
- **Read `X-Real-IP`, not a re-parsed XFF** — `asgi.inc` makes it authoritative everywhere and
  `$remote_addr` is the real client in both topologies, so X-Real-IP is the simplest correct
  source. Rejected: re-parsing XFF by trusted-hop count / realip-in-Python — unnecessary (no
  proxy chain hides the client) and higher-maintenance.
- **Prefer `X-Real-IP` over `REMOTE_ADDR`** — over the unix socket, uvicorn
  (`--proxy-headers --forwarded-allow-ips=*`) rewrites `scope["client"]`/`REMOTE_ADDR` from XFF,
  so `REMOTE_ADDR` is itself spoofable today; the `X-Real-IP` header is untouched by uvicorn.
- **Hardcode the header (no setting)** — KISS; matches the universal `asgi.inc`. A configurable
  trusted-header setting was considered and rejected for now.
- **Normalize to clean-or-None** — safer for the `GenericIPAddressField`/16-char `source_ip`
  consumers the audit flagged (a malformed value would otherwise raise on save); downstream
  consumers already guard `request.ip` with `or`.
- **Pure `@th.unit_test`** — `get_remote_ip` only reads `request.META`; no ORM/server needed
  (Overview.md:208-234). `@th.django_unit_test()` is an acceptable drop-in.

### Edge cases & risks
- **Operator/behavior change (main risk):** a deployment whose proxy does NOT set `X-Real-IP`
  falls back to `REMOTE_ADDR` (which, over the unix socket with `--forwarded-allow-ips=*`, can be
  the uvicorn-XFF value). Mitigated for the fleet by the universal `asgi.inc`; documented in
  CHANGELOG as a proxy requirement. Residual: a terminating LB that neither preserves client IP
  nor runs `realip` would surface the LB IP — none in evidence.
- IPv6/bracket/port forms (`::ffff:1.2.3.4`, `[::1]`, `host:port`) — handled by `_normalize_ip`;
  covered by tests.
- `request.ip` may be `None` in pathological cases (both sources missing/garbage) — consumers
  already tolerate falsy `request.ip` (e.g. `limits.py` `... or request.META.get("REMOTE_ADDR","unknown")`,
  geofence `engine.py:188` `... or ""`).
- **Out of scope (separate items):** WebSocket resolver `mojo/apps/realtime/handler.py:83,117,122`;
  geofence fail-open default; nginx XFF-overwrite + uvicorn `--forwarded-allow-ips` narrowing
  (defense-in-depth, `mverify_api/aws`).

### Tests
New `tests/test_helpers/test_get_remote_ip.py` (create `tests/test_helpers/` if absent), pure
`@th.unit_test`, fake request via minimal `objict(META={...})` (deterministic — avoids
`get_mock_request`'s default-META merge), imports inside the test fn. Every assert carries a message.
- **Regression (the spoof):** `META={'HTTP_X_FORWARDED_FOR':'203.0.113.7','HTTP_X_REAL_IP':'70.184.70.39','REMOTE_ADDR':'10.0.0.1'}`
  → `get_remote_ip(req) == '70.184.70.39'`. Old code returns `203.0.113.7` → fails; fixed → passes.
- **XFF not trusted even as fallback:** `META={'HTTP_X_FORWARDED_FOR':'203.0.113.7','REMOTE_ADDR':'198.51.100.42'}`
  (no X-Real-IP) → `== '198.51.100.42'`. Old returns spoof → fails; fixed → passes.
- **REMOTE_ADDR fallback:** `META={'REMOTE_ADDR':'198.51.100.42'}` → `== '198.51.100.42'`.
- **`_normalize_ip` table:** `' 1.2.3.4 '→'1.2.3.4'`, `'1.2.3.4:5678'→'1.2.3.4'`,
  `'::ffff:1.2.3.4'→'1.2.3.4'`, `'[2001:db8::1]'→'2001:db8::1'`, `'2001:db8::1'→'2001:db8::1'`,
  `'garbage'→None`, `''→None`.
- Run: `bin/run_tests --agent -t test_helpers.test_get_remote_ip`. Capture the green default-suite
  baseline BEFORE editing, per `.claude/rules/build-baseline.md`.
- (Optional, not required) integration via `opts.client` sending `X-Real-IP` + `X-Forwarded-For`
  (precedent `tests/test_public_messages/1_submit.py:328-345`) — only if a clean `request.ip`-echo
  endpoint exists; skip otherwise.

### Docs
`CHANGELOG.md` (required — security fix + proxy requirement). `docs/django_developer/`
middleware/request/IP page if one exists; `docs/web_developer/` brief note that `X-Forwarded-For`
no longer sets the recorded client IP. Post-build docs-updater agent syncs both tracks.

### Open questions
None blocking. WebSocket handler, geofence fail-open, and the nginx/uvicorn defense-in-depth are
tracked as separate items.

## Notes
- Build baseline (2026-06-30, `bin/run_tests --agent`, default suite): **GREEN** — 2261 total,
  2205 passed, 0 failed, 56 skipped (`testproject/var/test_failures.json`, `"failures": []`).
  Opt-in `test_incident` (243) and `test_security` (82) skipped (`requires --extra slow`). Any
  failure after this change is attributable to DM-009.
- Live proof script: `scratchpad/xff_spoof_test.py` (full 5-case) and `scratchpad/spoof_probe.py`
  (logged-line probe used to confirm topology).
- Topology confirmed 2026-06-30 for BOTH deployment modes (direct `api.mojoverify.com` and
  load-balanced `lb.inovopay.net`): nginx `$remote_addr` is the real client in both, and the
  shared `asgi.inc` (default in all deploys) sets `X-Real-IP $remote_addr`. The fix is a
  uniform, low-risk single-repo change (read `X-Real-IP` + normalize) that holds in all tested
  topologies — no open preconditions.
- Priority rationale: severity is high (auth-control bypass + compliance bypass) and the fix is
  now cheap/low-risk — suggest P1, or P0 given it's both high-impact and low-effort.
- This item is the django-mojo code core only. Defense-in-depth follow-ups belong in
  `mverify_api/aws` (nginx XFF overwrite, uvicorn `--forwarded-allow-ips`); the geofence
  fail-closed default is a separate compliance item.
- Post-build agents (2026-06-30): full default suite green (2210 passed, 0 failed); docs updated
  in both tracks (commit 87644c7 — also corrected pre-existing stale "respects X-Forwarded-For"
  docs); security-review confirmed the spoof is closed and `_normalize_ip` is robust (never
  raises). One out-of-scope finding deferred to a follow-up item: `request.ip` can be `None` on
  garbage input, and `UserLoginEvent.ip_address` is non-nullable while the caller
  (`account/rest/user.py:637`) swallows the error, so a login event would be **silently dropped**
  in a misconfigured deployment (production-unreachable behind the X-Real-IP proxy; fix needs a
  model migration → separate item).

## Resolution
- closed: 2026-06-30
- branch: main
- files changed: CHANGELOG.md,docs/django_developer/core/middleware.md,docs/django_developer/helpers/request.md,docs/web_developer/core/request_response.md,mojo/helpers/request.py,planning/.next_id,tests/test_helpers/test_get_remote_ip.py,uv.lock
- tests added: tests/test_helpers/test_get_remote_ip.py (5 cases — X-Real-IP beats forged XFF; XFF ignored; REMOTE_ADDR fallback; normalization table; malformed X-Real-IP falls back)
