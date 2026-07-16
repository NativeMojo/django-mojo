---
# id is assigned by /scope on pickup — leave it blank
id: DM-042
type: feature
title: Authenticated-abuse / doom-loop hardening — default per-identity throttling, traffic-concentration detection, instant account kill switch, WS reconnect limits
priority: P1
effort: L
owner: backend
opened: 2026-07-16
depends_on: []
related: []
links: [/Users/ians/Projects/doom_loop_report.md]
---

# Authenticated-abuse / doom-loop hardening

## What & Why

A sibling system's postmortem (`/Users/ians/Projects/doom_loop_report.md`) describes a
27-hour outage caused not by an exploit but by a **paying customer pointing an AI agent
at their authenticated portal**: machine-rate scraping from two accounts, amplified by
client failure handling (instant websocket reconnect with no backoff + one telemetry
DB-write per failure), until the box collapsed. Two IPs = 1.02M requests = 96% of
traffic, undetected for ~20 hours. Its core policy conclusion: **authenticated abuse is
a first-class threat category** — per-IP throttles, UA sniffing, and WAF signatures
never see it, because every request carries a valid login and looks like Chrome.

django-mojo serves exactly this architecture shape (REST portal API + realtime
websocket feed + telemetry/event ingestion + async jobs), so the same mechanism applies
to every deployment of this framework. A code survey (2026-07-16) found the framework
has strong building blocks — `mojo/decorators/limits.py` (fixed + sliding-window Redis
limiters, 429 + `Retry-After`, per-account `check_account_attempt`), the incident rules
engine with `email://`/`notify://`/`block://` handlers, fleet IP/ipset firewall, and
per-request ApiKey validation — but **five gaps that leave the doom-loop scenario
open**:

1. **No default/global throttle for authenticated users.** Rate limiting is opt-in
   per-endpoint via decorator, and the decorator dimensions are ip/duid/muid/api_key —
   there is **no `user` dimension**. An authenticated JWT user hammering un-decorated
   list endpoints at machine rate is unthrottled, and every GET hits the DB (no
   response caching exists; `Cache-Control: no-store` is forced) so there is no cache
   absorption either.
2. **Traffic concentration by one account is undetectable.** No middleware counts
   requests per user/account; `endpoint_metrics(by=["user"])` exists but only on
   decorated endpoints and only when `API_METRICS` is on. The incident rules engine
   fires on *events* (auth failures, rate-limit trips) — a well-behaved-but-fast
   authenticated agent produces zero events and trips zero rules. The report's #1
   detection signature (one account > a few % of a service's requests) has no data
   source here.
3. **The user kill switch is not instant.** `User.validate_jwt` (user.py:1581) does
   **not check `user.is_active`**, and the admin disable path
   (`services/disable.py:99` / `on_action_disable`) flips `is_active` but does **not
   rotate `auth_key`** — so disabling an abusive user mid-incident leaves their live
   JWTs working until expiry. The real revoke lever (`on_action_revoke_sessions`,
   auth_key rotation) is a separate action an operator must know to also fire.
   (ApiKey suspension IS instant — validated per-request, DM-037.) Realtime
   `manager.disconnect_user` exists but is not wired to disable/revoke.
4. **Websocket reconnect storms are unbounded.** No connection-rate limit, no
   max-concurrent-connections per user/IP, and each connect gets a 30s
   free unauthenticated window (auth-after-accept) plus Redis writes — the exact
   amplifier in the report (sessions living 37s, instant reconnect, connection-setup
   work dominating).
5. **Telemetry/event ingestion is IP-limited only, one DB row per call.** Bouncer
   event/assess (60/min/IP), incident event ingestion — none keyed per
   account/session, no sampling/dedupe. And the rate-limiter's own 429 path calls
   `incident.report_event`, a small secondary write-amplifier under storm conditions.

This item = close those gaps with framework-level controls (the report's guidance
sections A, B-server-side, C, E), plus deployment/ops guidance docs for the parts that
live outside the framework (edge limits, burstable-instance physics, blocking playbook
— sections C/D/F). Client-side backoff (section B, the actual bug in their client) is
web-mojo's side and out of scope here, but the server-side limits below make the server
survive a misbehaving client regardless.

## Acceptance Criteria

- [ ] **Per-user rate limiting exists and can be applied by default.** The limiter
      gains a `user` (authenticated identity) dimension, and there is a way to impose a
      framework-wide default request budget per identity (user / api_key / session)
      without decorating every endpoint — middleware or dispatcher-level, config-driven
      (sane generous default, per-deployment override, per-account/tier override akin
      to `ApiKey.limits`). Over-budget → cheap 429 + `Retry-After` before hitting
      model/DB code. Fail-open on Redis outage (existing limiter convention).
- [ ] **Traffic-concentration detection.** Lightweight per-identity request accounting
      (Redis counters, pipelined — no per-request DB writes) feeding either metrics or
      incident events, such that "one account exceeds N requests/min sustained" and/or
      "one account > X% of requests over a window" raises an incident event that the
      existing rules engine can page on (`notify://`/`email://`). Prebuilt ruleset
      (like `ensure_auth_rules`) so deployments get it by default.
- [ ] **Instant, single-action account kill switch.** Disabling a user
      (`disable_entity` / `on_action_disable`) revokes live access in one step:
      rotates `auth_key` (or `validate_jwt` checks `is_active` — decide in scope),
      and force-disconnects their realtime sockets. Effect within one request, like
      ApiKey suspension. Existing sessions must not survive on token expiry timers.
- [ ] **Websocket connection hardening.** Connection-establishment rate limit
      (per IP and per authenticated identity) and a max-concurrent-connections cap per
      identity, reusing the limits.py primitives; repeat offenders get closes cheap and
      early (before the 30s unauthenticated grace). Storm attempts surface as incident
      events.
- [ ] **Telemetry ingestion bounded per identity.** Bouncer event/assess and incident
      event ingestion endpoints rate-limited per account/session in addition to IP;
      the limiter's own `report_event` on 429 cannot amplify (sampled/deduped or
      exempted). Decide in scope whether row-sampling is needed or per-identity limits
      suffice.
- [ ] **Docs for both audiences.** django_developer: how to enable/tune the default
      throttle, concentration alerts, kill switch; a deployment-hardening page carrying
      the report's ops lessons that live outside the framework (edge rate limits for
      WS-upgrade/telemetry/token endpoints, burstable-instance credit alarms, graceful
      reload doesn't apply deny rules to established connections, CGNAT caveat for IP
      blocks, account-first blocking playbook). web_developer: 429/`Retry-After`
      contract clients must honor + expectation of exponential backoff with jitter.
- [ ] Report's detection signatures that fit the framework (account request share, WS
      session-lifetime collapse, telemetry POST rate) are either implemented or
      explicitly dispositioned in the plan as deployment-level.

## Repro — bugs only
n/a

## Plan

_Approved by owner 2026-07-16: enforcement ON by default (user 240/min, apikey
600/min), concentration alerts ON (>120 req/min sustained 10 min, or >20% share with
total >1000/window), WS pre-auth subscribe deferral IN scope._

### Goal
Make django-mojo survive an authenticated identity issuing machine-rate traffic
(REST scraping, WS reconnect storms, telemetry floods): throttle it by default,
detect and page on traffic concentration, and let one admin action kill an abusive
account instantly — at a hot-path cost of exactly **one pipelined Redis round-trip
per authenticated REST request** and zero for anonymous requests.

### Context — what exists

**Performance baseline (why the budget is what it is).** A warm authenticated GET
today, before the view: ~1 DB query, 0 Redis. Chain: `MojoMiddleware`
(`mojo/middleware/mojo.py:27-89`, pure request setup, **eagerly** parses
`request.DATA` at :56, sets `request.ip/duid/muid/msid` — no I/O) →
`AuthenticationMiddleware` (`mojo/middleware/auth.py:21-56`,
`MiddlewareMixin.process_request`; maps `bearer→User.validate_jwt`,
`apikey→ApiKey.validate_token` via `AUTH_BEARER_NAME_MAP` :15-19, sets
`request.user` + `request.bearer`) → `LoggerMiddleware`
(`mojo/middleware/logging.py:57-98`, async queue+bg thread unless test profile) →
the dispatcher view. JWT: 1 SELECT (`user.py:1612`) + `user.touch()` UPDATE
amortized to 1/300s (`user.py:306-316`, `USER_LAST_ACTIVITY_FREQ` default 300).
ApiKey: SELECT + **unconditional** `last_used` UPDATE (`api_key.py:306,328`) = 2
DB/request. Redis ops go through a warm per-process singleton pool
(`mojo/helpers/redis/client.py:74-122`, `ConnectionPool.from_url`,
`REDIS_MAX_CONN` 500; note `mojo/helpers/redis/__init__.py` imports from
`.client`, NOT `.pool` — pool.py is not on this path). One Redis RTT ≈ 0.1–0.5 ms
vs the ~1–5 ms auth SELECT already paid.

**Dispatcher** (`mojo/decorators/http.py`): `dispatcher(request, ...)` (:68-123) is
the registered Django view for **every** `@md.URL` route (`_register_route`
:222-299). Per request it resolves `request.DATA.group` → `Group.get_active` +
`touch()` (:73-117, 1 SELECT + possible UPDATE — only when `group=` present), then
routes via `URLPATTERN_METHODS` and wraps the view in `dispatch_error_handler`
(:122,126-219). **Hooking the top of `dispatcher()` covers the whole mojo REST
surface with no deployment settings change, and runs before group resolution.**

**Limiter toolkit** (`mojo/decorators/limits.py`): `rate_limit` (:134,
fixed-window `_incr_fixed` :46-54 — `INCR` + `EXPIRE` on first hit, sequential,
1–2 RTT/dimension) and `strict_rate_limit` (:215, sliding `_check_sliding` :57-72
— one `pipeline(transaction=False)` of ZREMRANGEBYSCORE/ZADD/ZCARD/EXPIRE = 1 RTT
of 4 cmds/dimension). Dimensions: ip/duid/muid/api_key (**no `user`**).
`_get_apikey_limits` (:75-107) reads per-key overrides from `ApiKey.limits`
JSONField (`api_key.py:47`; shape `{"key": {"limit": N, "window": minutes}}`,
window in MINUTES ×60 at :103). `check_account_attempt`/`read_account_attempt`
(:298/:337) = manual per-account sliding counters keyed
`srl:{key}:account:{id}`. `clear_rate_limits` (:385) clears `srl:`+`rl:` keys —
testit `client.login()` calls it for key="login" (`testit/client.py:56-67`).
**`_block()` (:28-43) is the current 429 path and an amplifier: on EVERY block it
does `metrics.record` + a SYNCHRONOUS `incident.report_event(level=5)`** →
Event INSERT + rule-engine queries in-request (wrapped in try/except).
Both decorators fail open on Redis errors (:207-210, :290-293).

**Settings machinery** (`mojo/helpers/settings/helper.py`): `settings.get(name,
default, group=, kind=)` (:157-178) resolves DB `Setting` (Redis-cached) → Django
settings → default; `settings.get_static` (:180-198) is file-only (DM-031: only
test/bypass plumbing must be get_static; operational config keys are legitimately
DB-settable). `kind=` coercions at :83-155. **Do not call `settings.get` per
request** — Setting.resolve is a Redis hit; cache resolved throttle config
in-process with a short TTL instead.

**Kill-switch gap.** `User.validate_jwt` (`user.py:1580-1625`), hit on every HTTP
request AND on WS auth, fetches the row (:1612), verifies HMAC vs `user.auth_key`
(:1615-1616), `touch()`es (:1624) — **never checks `user.is_active`**. The
`user_api_key` sub-path (:1591-1610) filters `UserAPIKey.is_active` but not
`user.is_active` (row available via `select_related("user")`).
`on_action_disable` (`user.py:931-947`, perm `users`/`manage_users`, reason ∈
{admin, abuse}) → `disable_entity` (`mojo/apps/account/services/disable.py:99-154`):
writes `metadata.protected.disable`, atomically flips `is_active=False`, emits
incident `account:disabled` level 4 — **no `auth_key` rotation, no WS
disconnect**. `on_action_revoke_sessions` (`user.py:1005-1015`):
`_require_fresh_auth()` then `auth_key = uuid.uuid4().hex` + save — the real JWT
revoker. Precedent for rotate-on-deactivate: `pii_anonymize` (`user.py:1093-1094`)
sets both `auth_key` and `is_active=False`. `manager.disconnect_user(user_type,
user_id)` (`mojo/apps/realtime/manager.py:275-288`) force-closes all of a user's
sockets cross-process (PUBLISH `realtime:messages:{cid}` `{"type":"disconnect"}`
→ `process_redis_message` handler.py:652-654 closes) — **written but has zero
callers**. ApiKey suspension is already instant (validated per-request,
`api_key.py:296-335`, DM-037).

**Realtime WS** (`mojo/apps/realtime/`): `asgi.py:41` **accepts unconditionally**
(path guard only, :32-38); client IP is available pre-accept via
`scope["headers"]` `x-real-ip` (wrapper lowercases headers :69-76; normalize via
`mojo/helpers/request.py normalize_ip`; never trust XFF — DM-009/010). Pre-auth,
each connection costs: `register_connection` SETEX (`handler.py:187-205`), **3
asyncio tasks** (:162-166), and `handle_redis_messages` (:289-302) immediately
opens a **dedicated Redis pub/sub connection** subscribed to
`realtime:messages:{cid}` + `realtime:broadcast`. Auth arrives message-only
(`handle_authenticate` :355-415, token in first message; `auth.py:12`) →
`validate_jwt` (1 SELECT + touch). Unauth timeout: `activity_timeout` task
(:248-263), 30 s hardcoded, then `report_incident(level 6)` + close. Post-auth:
`update_connection_auth` SETEX (:207-229), `register_user_online` SADD+EXPIRE on
`realtime:online:{type}:{id}` (:231-246), topic SADD+subscribe (:593-611).
Cleanup: `cleanup_connection` (:746-771). `get_user_connections` (manager.py:157)
= TYPE+SMEMBERS (cheap); **avoid `get_auth_count`/`get_online_users`
(manager.py:139/242) — they use blocking `KEYS`**. Concurrency substrate for the
cap: `SCARD realtime:online:{user_type}:{user_id}`.

**Telemetry writers.** `account/rest/bouncer/event.py:12-14`
(`@md.public_endpoint` + `@md.rate_limit('bouncer_event', ip_limit=60)`; batched
bulk_create cap 200 :54/85, legacy single create :104) and `assess.py:61-63`
(same shape, ip_limit=60; DB get_or_create+save+signal create :95-153, on-block
report_event+jobs.publish :182-203). Both have `request.muid/duid` available
pre-auth; **neither uses the decorator's existing `muid_limit`/`duid_limit`
params**. `incident/rest/event.py:17-20` routes to `Event.on_rest_request` with
`CREATE_PERMS=["all"]` (`models/event.py:66-70`) and **no rate limit at all**.

**Incident/rules/alerting.** `incident.report_event` (`reporter.py:4-10`) is
synchronous: Event INSERT + `publish()` (`models/event.py:200-319`) → rule
matching = up to 3 `RuleSet.check_by_category` SELECTs (scope→category→`"*"`,
:205-209; rule.py:753-769) + 1 rules SELECT per candidate ruleset (no caching);
match or `level >= INCIDENT_LEVEL_THRESHOLD` (default 7, event.py:12) →
atomic `get_or_create_incident` etc. **Handlers (`notify://`, `email://`,
`block://`…) dispatch via `jobs.publish(...)` to background workers**
(rule.py:148-200, :184) — never inline. Prebuilt-ruleset pattern:
`ensure_auth_rules` (rule.py:467-526) using `_create_ruleset` get_or_create
(:252-264), bootstrapped lazily at runtime (e.g. bouncer: canary-guarded
`_ensure_bouncer_defaults` at assess.py:20-37; health: `incident/cronjobs.py:10-19`).
Rule damping fields: `trigger_count`/`trigger_window`/`retrigger_every`,
`bundle_by`/`bundle_minutes` (rule.py:127-135).

**Cron/jobs.** `@schedule(...)` decorator (`mojo/decorators/cron.py:3-31`) +
`mojo/helpers/cron.py` (`run_now`/`load_app_cron` — driven by the HOST project's
minute ticker, no caller in mojo). Pattern to copy:
`mojo/apps/incident/cronjobs.py` — e.g. `@schedule(minutes="*/3")
check_system_health` :75-82 just `jobs.publish(...)`es the real work to async
workers.

**Metrics.** `metrics.record` (`mojo/apps/metrics/redis_metrics.py:70-94`) = 1
pipelined RTT of ~13 cmds (3 SADD + INCR/EXPIREAT per granularity, default
hours→years) — **too heavy per-request; do NOT use it for accounting**. Metrics
are passive (no threshold/alarm mechanism; `mojo/apps/metrics/__init__.py`) —
the rules engine is the only alerting path.

**Test-suite interaction.** testit's HTTPClient makes real HTTP requests with
real JWTs (`testit/client.py:48-91`); `login()` pre-clears only key="login"
limits. Test profile (`testproject/config/settings/local/__init__.py:101-108`)
already disables adjacent machinery (`INCIDENT_EVENT_METRICS=False`,
`LOGIT_ASYNC_LOGGING=False`). The scaffold that generates it is
`bin/create_testproject:134-144` — settings changes go THERE (regenerated
projects) as well as the checked-in testproject copy. A global throttle at 240/min
WOULD break high-volume test modules → test profile must set
`API_THROTTLE_ENABLED=False`; the throttle test module opts in with low limits
for its own identities.

### Changes — what to do

1. **`mojo/decorators/limits.py` — the throttle core.**
   - Add `check_api_throttle(request)`: returns `None` (allow) or an
     `HttpResponse` 429. Logic:
     - Resolve identity: `request.api_key` → `("apikey", pk)`; elif
       `request.user` is a real User (`hasattr(user, "is_request_user")` — the
       canonical idiom, see memory/DM-016) and authenticated → `("user", pk)`;
       else (anonymous / ANONYMOUS_USER) → **return None immediately, zero Redis**.
     - Exempt-prefix check (in-memory): reuse the `METHOD:/path` prefix-match
       shape from `mojo/middleware/logging.py:100-120` against
       `API_THROTTLE_EXEMPT_PREFIXES`.
     - Config via a module-level cached resolver `_get_throttle_config()`:
       reads `settings.get` keys (below) at most once per
       `API_THROTTLE_CONFIG_TTL` (default 30 s, monotonic-clock stamp) per
       process; per-apikey override via existing `_get_apikey_limits(request,
       "api")` convention (`ApiKey.limits["api"] = {"limit": N, "window":
       minutes}`).
     - Fixed-window count+account in **one**
       `pipeline(transaction=False)`: `INCR rl:api:{kind}:{pk}:{window_start}`,
       `INCR rl:api:total:{window_start}`, plus `EXPIRE` on both **only when**
       the identity INCR is a first-write (can't know pre-INCR → always queue
       the two EXPIREs in the same pipeline; 4 cmds, still 1 RTT — simpler and
       race-free beats clever).
     - Top-talkers sampling: when `count % API_THROTTLE_SAMPLE_EVERY == 0`
       (default 100), one extra pipelined RTT: `ZINCRBY
       traffic:top:{window_start} SAMPLE_EVERY "{kind}:{pk}"` + `ZINCRBY ...
       "ip:{request.ip}"` + `EXPIRE`. Amortized ~1/100 requests.
     - Over limit (`count > limit`) and `API_THROTTLE_ENABLED`: return
       `_block_cheap(...)` — static `JsonResponse({"error": "rate limit
       exceeded", "code": 429}, status=429)` with `Retry-After` = seconds to
       window end. First-engage only (`SET rl:api:blocked:{kind}:{pk}:{window}
       NX EX window` → if set): `metrics.record("rate_limit:api", ...)` +
       `incident.report_event(category="rate_limits", level=5)`. Subsequent
       blocks in the window: nothing but the 429. If enforcement disabled:
       accounting still ran; return None.
     - Entire body in try/except → log + fail open (existing convention).
   - Rework `_block()` (:28-43): same SETNX dedup guard (keyed on
     key+dimension+window) around its `metrics.record` + `report_event` so
     existing decorators stop writing an Event per rejected request under storm.
     429 response shape unchanged.
   - Extend `clear_rate_limits` (:385) to also delete `rl:api:*` keys for the
     given identity (new `user_id=` param) — for tests.

2. **`mojo/decorators/http.py` — enforce at the dispatcher.** At the top of
   `dispatcher()` (:68, before the group-resolution block at :73): `throttled =
   check_api_throttle(request); if throttled is not None: return throttled`.
   Rejected requests never reach group resolve, the view, or the DB (beyond the
   auth SELECT already spent in middleware).

3. **Settings (documented defaults; all operational keys via `settings.get`,
   cached per process as above):** `API_THROTTLE_ENABLED` (bool, default
   **True**), `API_THROTTLE_USER` (int req/window, default **240**),
   `API_THROTTLE_APIKEY` (default **600**), `API_THROTTLE_WINDOW` (seconds,
   default **60**), `API_THROTTLE_EXEMPT_PREFIXES` (list, default `[]`),
   `API_THROTTLE_SAMPLE_EVERY` (default 100), `API_THROTTLE_CONFIG_TTL`
   (default 30). Detection: `TRAFFIC_CONCENTRATION_RPM` (default **120**),
   `TRAFFIC_CONCENTRATION_SUSTAIN_WINDOWS` (default **2** consecutive 5-min
   checks ≈ 10 min), `TRAFFIC_CONCENTRATION_SHARE` (default **0.20**),
   `TRAFFIC_CONCENTRATION_MIN_TOTAL` (default **1000**/window). WS:
   `WS_CONNECT_RATE_LIMIT` (default **30**/min/IP), `WS_MAX_CONNECTIONS`
   (default **10**/identity), `WS_UNAUTH_TIMEOUT` (default **10** s,
   replaces hardcoded 30).

4. **Concentration detector.**
   - `mojo/apps/incident/cronjobs.py`: add `@schedule(minutes="*/5")
     check_traffic_concentration()` → `jobs.publish("mojo.apps.incident.services.traffic.check_concentration",
     ...)` (mirror `check_system_health` :75-82).
   - New `mojo/apps/incident/services/traffic.py`: `check_concentration()` reads
     the current+previous `traffic:top:{window}` zsets (ZREVRANGE, top ~20) and
     `rl:api:total:{window}` counters; computes per-identity req/min and share;
     an identity over `TRAFFIC_CONCENTRATION_RPM` for
     `SUSTAIN_WINDOWS` consecutive checks (state in a small Redis hash), or over
     `SHARE` with total ≥ `MIN_TOTAL`, → one
     `incident.report_event(category="traffic", subcategory/details naming the
     identity, level 6)`. Runs on job workers — zero request-path cost.
   - `mojo/apps/incident/models/rule.py`: add `ensure_traffic_rules()`
     (pattern: `ensure_auth_rules` :467) — RuleSet category `"traffic"`,
     `notify://` handler, `bundle_by` identity, `retrigger_every` damping.
     Bootstrap: call from the detector service on first run (get_or_create is
     idempotent; canary-guard like assess.py:20-37 if needed).

5. **Kill switch.**
   - `mojo/apps/account/models/user.py` `validate_jwt`: after the fetch at
     :1612, `if user is None or not user.is_active: return None, "invalid
     token"` (same generic error string as other failures — no
     account-state oracle). In the `user_api_key` path add `and
     ukey.user.is_active` (row already joined).
   - `mojo/apps/account/services/disable.py` `disable_entity` (User path): also
     rotate `auth_key = uuid.uuid4().hex` in the same atomic UPDATE (extend
     `_write_metadata(atomic_with_active=...)` fields), and after commit call a
     new best-effort `_disconnect_realtime(user)` → `from mojo.apps.realtime
     import manager; manager.disconnect_user("user", user.pk)` wrapped in
     try/except (realtime/Redis unavailability must never fail a disable).
   - `on_action_revoke_sessions` (`user.py:1005`): add the same
     `disconnect_user` call after rotation.
   - Reactivation intentionally does NOT restore old tokens (auth_key rotated)
     — user re-authenticates. Document it.

6. **Realtime hardening** (`mojo/apps/realtime/`).
   - `asgi.py` pre-accept (between path guard :38 and accept :41): parse
     `x-real-ip` from `scope["headers"]` (fallback `scope["client"][0]`;
     `normalize_ip`), fixed-window `INCR ws:connect:{ip}:{window}` (+EXPIRE) via
     `run_in_executor` (sync redis client); over `WS_CONNECT_RATE_LIMIT` →
     `await send({"type": "websocket.close", "code": 4429})` WITHOUT accepting,
     plus first-engage-only `report_event` (same SETNX dedup, category
     "traffic", level 6, run in executor). Fail open on Redis error.
   - `handler.py` `handle_authenticate` (:355): after successful validate and
     before registration, `SCARD realtime:online:{user_type}:{pk}` ≥
     `WS_MAX_CONNECTIONS` → send an `auth_failed` message with reason
     "too many connections" and close (code 4429). 1 cheap op per auth.
   - `handler.py` `handle_connection` (:147-185): **defer
     `handle_redis_messages` task creation until after successful auth**
     (move the `create_task` from :162-166 into the post-auth path in
     `handle_authenticate`); pre-auth the connection only holds the SETEX
     record + timeout/client-message tasks. Verify auth flow needs no pub/sub
     (auth traffic goes over the socket directly — confirmed, delivery channels
     are only used post-auth).
   - `activity_timeout` (:248-263): unauth threshold from `WS_UNAUTH_TIMEOUT`
     (10 s default) read once at connect; authenticated idle stays 30 s.
   - `cleanup_connection`: no change needed, but verify it tolerates the
     never-subscribed pub/sub (guard `pubsub.close()` for None).

7. **Telemetry limits.**
   - `account/rest/bouncer/event.py:14`: `@md.rate_limit('bouncer_event',
     ip_limit=60, muid_limit=30)`.
   - `account/rest/bouncer/assess.py:63`: `@md.rate_limit('bouncer_assess',
     ip_limit=60, muid_limit=30)`.
   - `incident/rest/event.py:17-20`: add `@md.rate_limit('incident_event',
     ip_limit=60, muid_limit=30)` above the route's model dispatch (the global
     throttle also covers authenticated volume; this bounds the heavier
     rule-engine path specifically).

8. **testit / test profile.**
   - `bin/create_testproject` (:134-144 area) + checked-in
     `testproject/config/settings/local/__init__.py` (~:101-108):
     `API_THROTTLE_ENABLED = False`.
   - `testit/client.py login()` (:56-67): also clear the new `rl:api:*` keys for
     the logging-in account (via extended `clear_rate_limits`).

9. **CHANGELOG.md** — new-feature + behavior-change entries (throttle default-on,
   429 dedup, disable now revokes sessions + sockets, WS caps, telemetry muid
   limits).

### Design decisions
- **Dispatcher hook, not new middleware** — every `@md.URL` route is covered
  automatically on upgrade with zero deployment settings edits; runs after
  identity (set in auth middleware) and before group resolution/view, so
  rejected requests cost no DB. Rejected: new MIDDLEWARE entry (silently absent
  in existing deployments — a shipped-but-off defense repeats the report's
  failure mode).
- **Fixed window (INCR), not sliding (zset)** for the global throttle — 1 RTT of
  4 pipelined cmds vs 4-cmd zset ops **plus one zset member per request in
  memory**; at generous machine-rate-only limits the 2× window-boundary burst is
  irrelevant. `strict_rate_limit` remains for security endpoints.
- **Keyed by authenticated identity only; anonymous exempt** — IP-keyed global
  limits are the CGNAT trap the report warns about; anon surfaces keep their
  per-endpoint limiters. Also makes the hot-path cost 0 for anon traffic.
- **Accounting always on, enforcement flag separate** — detection ("96% in
  silence") must not depend on 429 posture; both default ON per owner approval.
- **Config cached in-process 30 s** — `settings.get` per request would add a
  Redis hit; a 30 s staleness on limit changes is acceptable (emergency tighten
  still lands in ≤30 s + window length). Keys stay DB-settable (operational
  config, not bypass plumbing — DM-031 distinction).
- **First-engage-only incident events (SETNX dedup) on every block path** — the
  current per-429 synchronous Event INSERT + rule evaluation is exactly the
  "failure costs more than success" amplifier the report names. Bounded: ≤1
  event per identity per window. Rules engine still sees engagement signal.
- **Top-talkers via sampled ZINCRBY off the INCR return value** — zero extra
  RTTs for 99% of requests; `metrics.record` (~13 cmds) deliberately NOT used
  per-request.
- **Detector is a 5-min cron job, not inline** — request path stays flat;
  alerting latency of ≤10 min is far inside the report's 20-hour detection gap.
- **Disable rotates auth_key (not just an is_active gate)** — re-enabling a user
  must not resurrect tokens minted before the abuse; matches `pii_anonymize`
  precedent. The `validate_jwt` is_active check is still added (free, covers
  any path that flips is_active without rotating).
- **Generic "invalid token" error for disabled accounts** — no account-state
  oracle (memory: no-account-enumeration).
- **WS close pre-accept with code 4429** — refused handshake costs the server
  one Redis INCR; 4xxx range is app-reserved, client can distinguish deliberate
  rejection from network failure (web_developer docs will state the backoff
  contract).
- **`disconnect_user` wired best-effort (try/except)** — a Redis/realtime outage
  must never make disable fail; the auth_key rotation is the guarantee, the
  socket drop is hygiene.

### Edge cases & risks
- **Redis outage** → throttle/accounting/WS checks all fail open (log, allow) —
  matches existing limiter convention; detection silently pauses (documented).
- **Redis cluster mode** — pipelines are `transaction=False` and multi-key;
  `metrics.record` already pipelines heterogeneous keys under cluster
  (`redis_metrics.py:78-94` precedent), redis-py cluster splits by slot. Keep
  `transaction=False`.
- **Fleet/federation apikey traffic** (`requires_global_perms(...,
  allow_api_keys=True)` receivers: jobs control/broadcast, geoip sync, aws
  ingest — see recon list) may legitimately exceed 600/min → per-key
  `ApiKey.limits["api"]` override, plus `API_THROTTLE_EXEMPT_PREFIXES` for
  path-level carve-outs. Document BOTH in the hardening page; call out jobs
  endpoints explicitly.
- **`user_api_key` JWT sub-path** identities throttle under `("user", pk)` —
  same budget as the user; fine.
- **Window-boundary burst (2× limit across a boundary)** — accepted; limits are
  machine-rate-only by design.
- **Two EXPIREs re-queued every request** (can't know if INCR was first without
  a round-trip) — refreshing TTL on a live key is harmless; keys self-expire
  after the window regardless. Keep TTL = 2× window (existing `_incr_fixed`
  convention :50).
- **429 on legit burst** (SPA dashboard storm): 240/min sustained is ~4 rps for
  a full minute from ONE account — a real dashboard burst is 30–60 requests in
  a few seconds, well inside. Per-group/tier raise documented
  (`Setting` row, group-scoped, or ApiKey.limits).
- **Disable while requests in flight** — in-flight requests complete (identity
  resolved before the flip); next request dies at validate_jwt. WS sockets get
  the pub/sub disconnect within ms; if realtime is down, sockets die at next
  reconnect (token now invalid).
- **`_require_fresh_auth` on revoke_sessions** (:1008) — unchanged; disable path
  has its own perm gate and does its own rotation, so an admin without fresh
  auth can still disable (deliberate: incident response must not stall on
  step-up).
- **WS pub/sub deferral regression risk** — the auth exchange uses the socket
  directly, but `send_to_connection` (server→client via
  `realtime:messages:{cid}`) would silently drop for unauthenticated
  connections; audit callers to confirm none target pre-auth connections
  (recon says only post-auth flows use it — verify in build).
  `cleanup_connection` must handle pubsub=None.
- **Test-suite**: throttle disabled in profile; the throttle test module enables
  it via `th.server_settings` (server-process override — correct tool for
  `opts.client` tests) with tiny limits, uses its own dedicated user, and
  clears its keys in setup (never leave global throttle poisoned — geofence
  test-hygiene lesson). Cron detector tested by direct function call, not the
  scheduler.
- **`incident_event` rate limit vs OSSEC fleet volume — RESOLVED 2026-07-16
  (verified against mojo-ossec repo):** OSSEC agents do NOT post to
  `incident/rest/event.py` — they post batches to
  `/api/incident/ossec/alert/batch` with an `X-OSSEC-SECRET` header
  (mojo-ossec `install_ec2.sh:30`, `ossec-webhook-batch.sh:89`), and the
  sender is a single flock'd loop flushing max 10 events/POST every 5 s —
  structurally capped ≈12 POSTs/min/host. So `incident/event` can take the
  ordinary `ip_limit=60, muid_limit=30` with no fleet concern. Do NOT add a
  limit to the ossec alert endpoint in this item: its client treats 429 like
  any error (`curl -f`, no Retry-After handling) and after 3 retries parks
  the batch to a backup dir nothing re-reads — sustained 429 there = silent
  alert loss. If it ever gets a limit, it must stay well above 12/min/host.
- **Clock**: window_start = `int(time.time()) // window * window` — workers need
  NTP-sane clocks (same assumption as existing fixed-window limiter).

### Tests
All testit (`@th.django_unit_test()`, `opts` signature, cleanup-before-create,
descriptive assert messages). New module `tests/test_limits/` (or extend
existing test home for limits if one exists — check first).

- **Throttle**: with server_settings enabling `API_THROTTLE_ENABLED` +
  `API_THROTTLE_USER=5`, dedicated user: 5 requests pass, 6th → 429 with
  `Retry-After` header; anonymous endpoint unaffected; exempt prefix passes at
  limit; enforcement-off still increments counters (read key directly);
  ApiKey with `limits["api"]` override honored; Redis-down fail-open (unit-level:
  call `check_api_throttle` with a broken connection via monkeypatched
  get_connection in-process). → `tests/test_limits/api_throttle.py`
- **Block dedup**: two consecutive 429s in one window → exactly one incident
  Event row (count Events by category before/after). →
  `tests/test_limits/block_dedup.py`
- **Concentration detector** (direct call, no cron): seed
  `traffic:top:{window}` + totals in Redis, run `check_concentration()` twice →
  event emitted for sustained offender, none below floor, none for
  one-window spike; share-based trigger with/without MIN_TOTAL floor. →
  `tests/test_incident/traffic_concentration.py`
- **Kill switch (regression-grade)**: login → disable via
  `on_action_disable` path → same JWT rejected on next request (401, generic
  error); re-enable → old JWT STILL rejected (rotation); fresh login works.
  user_api_key of a disabled user rejected. `revoke_sessions` still rotates. →
  `tests/test_account/test_disable_kill_switch.py`
- **WS caps**: check for an existing WS test harness under `tests/test_realtime*`
  first; if present, drive connect-rate limit (Nth connect refused pre-accept)
  and concurrency cap; otherwise unit-test the check functions directly
  (asgi pre-accept helper + SCARD gate) with seeded Redis. Also: unauth socket
  closed at `WS_UNAUTH_TIMEOUT`; pub/sub not subscribed pre-auth
  (assert connection object state); cleanup with pubsub=None doesn't raise. →
  `tests/test_realtime/connection_limits.py`
- **Telemetry muid limits**: bouncer event endpoint — same muid over
  muid_limit → 429 while a second muid still passes. →
  `tests/test_account/test_bouncer_limits.py` (extend existing bouncer tests if
  present)

### Docs
- `docs/django_developer/` — new page (e.g. `security/abuse_hardening.md`, linked
  from README index): threat model (authenticated abuse), throttle
  config/tuning/exemptions (incl. federation keys + `ApiKey.limits["api"]`),
  concentration alerts + prebuilt ruleset, kill-switch semantics (disable =
  is_active + auth_key rotation + socket drop; reactivation requires re-login),
  WS limits, and a **deployment hardening** section carrying the report's
  out-of-framework lessons: edge `limit_req` for WS-upgrade/telemetry/token
  endpoints, burstable-instance credit alarms, graceful-reload-doesn't-apply-
  deny-rules trap, CGNAT collateral check before IP blocks, account-first
  blocking playbook.
- `docs/web_developer/` — 429/`Retry-After` contract on all endpoints; REQUIRED
  client behavior: exponential backoff with jitter (base 1 s, cap 60 s, give-up
  threshold), never one-telemetry-POST-per-failure; WS close code 4429 =
  deliberate rejection, back off, don't hammer.
- Root README index updates in both tracks; `CHANGELOG.md`.

### Open questions
- none — posture and defaults approved by owner 2026-07-16 (enforcement ON,
  240/600 per min, concentration 120 rpm sustained 10 min or 20% share over
  1000-total floor, WS deferral in scope). The former build-time judgment call
  (`incident_event` limit vs OSSEC fleet volume) is resolved — see edge cases:
  OSSEC posts to a different endpoint, is structurally capped ≈12 POSTs/min/host,
  and its endpoint must NOT get a limit in this item.

## Notes

**Build baseline (2026-07-16, pre-edit):** `bin/run_tests --agent` →
total 2451 / passed 2395 / failed 0 / skipped 56 (opt-in modules test_incident +
test_security excluded per rules). GREEN — all post-change failures are ours.

**Build deviations from the plan (all improvements, same cost budget):**
- Top-talkers accounting: the planned sampled ZINCRBY (every Nth request)
  quantized detection to ~2× the threshold. Replaced with an EXACT flush — on
  the first request of a new window (count == 1), the previous window's final
  count is ZINCRBY'd into the 5-min bucket zset (identities ≥
  `API_THROTTLE_REPORT_FLOOR`, default 60/window, only). Same amortized cost
  (~1 small extra RTT per identity-minute), exact rpm data.
  `API_THROTTLE_SAMPLE_EVERY` was dropped in favor of
  `API_THROTTLE_REPORT_FLOOR`.
- Sustained detection needs no Redis state hash: the detector requires the
  identity's presence over threshold in N consecutive bucket zsets.
- Detector lives in `mojo/apps/incident/asyncjobs.py::run_concentration_check`
  (repo pattern: cronjobs publish → asyncjobs), not a new services/traffic.py.
- Detector + throttle tests live in `tests/test_limits/` — the planned
  `tests/test_incident/` is an opt-in (--full) module that never runs in the
  default suite.
- Throttle tests use a new `X-Mojo-Test-Api-Throttle` header override
  (test_mode-gated, per-request) instead of `th.server_settings` — keeps the
  module parallel-safe and can't poison other modules.
- `incident/event` limits set to ip 240 / muid 120 (not 60/30): the same route
  serves security-dashboard reads; ingest is additionally bounded by the
  global throttle.
- Test profile also sets `WS_CONNECT_RATE_LIMIT = 0` (whole suite shares
  127.0.0.1); the pre-accept 4429 path is exercised in-process by driving the
  real ASGI app with a seeded counter.
- `_block()` dedup key is key+IP with EX 60 (fixed dedup window regardless of
  the caller's rate window) — bounded ≤1 event/min per key+IP.

**Deliberate test-contract updates (full-suite run):** two pre-existing tests
encoded the superseded "inactive user's live JWT still authenticates, view
returns 403" contract — `tests/test_email/email_change.py`
(inactive-user-blocked; now 401 at middleware, restore made try/finally to
stop cascade into 2 later tests) and `tests/test_verification/verification.py`
(banned-account-reactivate; 401 added to accepted rejections, the real
invariant — is_active stays False — unchanged). Both are the kill switch
working as designed, not regressions.

Survey inventory (2026-07-16) — key refs for /scope:
- Limiter toolkit: `mojo/decorators/limits.py` — `rate_limit` (:134, fixed-window),
  `strict_rate_limit` (:215, sliding), `check_account_attempt` (:298, per-account,
  manual call not decorator), `_block` (:28, 429 + Retry-After + metric + incident
  event), `_get_apikey_limits` (:75, per-tenant override via `ApiKey.limits`),
  `endpoint_metrics` (:432, gated on `API_METRICS`; `_get_dimension` :110 already
  supports "user"/"group" for accounting only). Fail-open on Redis errors (:207, :290).
- Canonical wiring example: `mojo/apps/account/rest/user.py:146-192` (login:
  strict ip limit + `check_account_attempt` + `clear_rate_limits` on success).
- Kill-switch gap: `mojo/apps/account/models/user.py` — `validate_jwt` :1581 (no
  `is_active` check; signature vs `auth_key` :1616), `on_action_revoke_sessions`
  :1005 (auth_key rotation = the real revoker), `on_action_disable` :931 →
  `mojo/apps/account/services/disable.py:99` (no rotation). `auth_key` is in
  `NO_SAVE_FIELDS` (:154). ApiKey contrast: `api_key.py:296` validated per-request.
- Realtime: `mojo/apps/realtime/asgi.py:41` (accept-then-auth),
  `handler.py:248-263` (30s auth/idle timeout), `manager.py:139/:157` (connection
  counts exist, unenforced), `manager.py:275` (`disconnect_user` — wire to kill
  switch).
- Telemetry writers: `account/rest/bouncer/event.py:12` + `assess.py:61` (60/min/IP,
  row per call), `incident/reporter.py:4-10` (Event.save + publish per call),
  `incident/rest/event.py:17`.
- Rules engine to reuse for alerts: `incident/models/rule.py` (thresholds :127-135),
  `event.py:200-270` (publish→check), prebuilt pattern `ensure_auth_rules`
  (rule.py:467), handlers `incident/handlers/event_handlers.py` (`notify://`,
  `email://`, `block://`).
- Middleware that could host accounting: `mojo/middleware/mojo.py` (identity setup;
  also forces `Cache-Control: no-store` :65-67), `mojo/middleware/logging.py`.
- Out of framework scope (docs only): burstable credit alarms, nginx edge
  `limit_req`, ops DNS hygiene, blast-domain separation, staggered reloads.
- Possible split: /scope may break this into 2–3 items (e.g. kill switch as its own
  small bug-flavored fix; throttle+detection; WS hardening) — the kill-switch gap
  (`is_active=False` doesn't revoke live JWTs) is arguably a standalone P1 bug.

## Resolution
- closed: 2026-07-16
- branch: main
- files changed: .claude/skills/request/SKILL.md,.claude/skills/scope/SKILL.md,AI_DEV.md,CHANGELOG.md,CLAUDE.md,bin/create_testproject,docs/django_developer/account/api_keys.md,docs/django_developer/account/auth.md,docs/django_developer/account/disable_lifecycle.md,docs/django_developer/account/geofence.md,docs/django_developer/account/group.md,docs/django_developer/core/decorators.md,docs/django_developer/core/middleware.md,docs/django_developer/core/mojo_model.md,docs/django_developer/core/permissions.md,docs/django_developer/core/rate_limiting.md,docs/django_developer/helpers/settings_reference.md,docs/django_developer/logging/incidents.md,docs/django_developer/realtime/README.md,docs/django_developer/realtime/architecture.md,docs/django_developer/rest/permissions.md,docs/django_developer/security/README.md,docs/django_developer/security/abuse_hardening.md,docs/web_developer/account/api_keys.md,docs/web_developer/account/bouncer.md,docs/web_developer/account/group.md,docs/web_developer/account/user.md,docs/web_developer/core/request_response.md,docs/web_developer/logging/reporting_events.md,docs/web_developer/realtime/websocket.md,docs/web_developer/security/README.md,docs/web_developer/security/rate_limits.md,memory.md,mojo/apps/account/models/api_key.py,mojo/apps/account/models/user.py,mojo/apps/account/rest/bouncer/assess.py,mojo/apps/account/rest/bouncer/event.py,mojo/apps/account/rest/group.py,mojo/apps/account/services/disable.py,mojo/apps/incident/asyncjobs.py,mojo/apps/incident/cronjobs.py,mojo/apps/incident/models/rule.py,mojo/apps/incident/rest/event.py,mojo/apps/realtime/asgi.py,mojo/apps/realtime/handler.py,mojo/decorators/auth.py,mojo/decorators/http.py,mojo/decorators/limits.py,mojo/models/rest.py,planning/.config,planning/.next_id,planning/_template.md,planning/confirmed/DM-040-incident-maestroboard-push-link-tickets-into-a-rem.md,planning/done/DM-001-render-allowlisted-extra-registration-fields-promo.md,planning/done/DM-002-step-up-recent-authentication-gate-for-sensitive-o.md,planning/done/DM-003-register-page-enter-on-phone-otp-field-fires-step-.md,planning/done/DM-004-sign-in-alternate-method-button-row-overflows-clip.md,planning/done/DM-005-phone-register-one-wrong-sms-code-burns-the-sessio.md,planning/done/DM-006-sms-sign-in-with-an-unrecognized-number-dead-ends-.md,planning/done/DM-007-full-test-suite-is-flaky-content-guard-false-posit.md,planning/done/DM-008-phone-signup-may-fail-to-sign-in-an-existing-accou.md,planning/done/DM-009-get-remote-ip-trusts-client-supplied-x-forwarded-f.md,planning/done/DM-010-websocket-ip-resolver-trusts-client-spoofable-sour.md,planning/done/DM-011-ip-storage-fields-assume-ipv4-non-null-ipv6-trunca.md,planning/done/DM-012-auth-middleware-500s-on-a-malformed-authorization-.md,planning/done/DM-013-management-command-to-create-initial-users-admins.md,planning/done/DM-014-var-dev-server-conf-overrides-config-dev-server-co.md,planning/done/DM-015-configurable-outbound-webhook-signature-header-use.md,planning/done/DM-016-group-user-has-permission-crashes-on-apikey-identi.md,planning/done/DM-017-geofence-config-evidence-plane-editable-system-rul.md,planning/done/DM-018-audit-requires-perms-group-fallback-on-global-effe.md,planning/done/DM-019-self-minted-group-apikey-with-arbitrary-permission.md,planning/done/DM-020-geofence-evidence-metrics-dual-write-group-scoped-.md,planning/done/DM-021-geofence-hardening-opt-in-strict-compliance-enforc.md,planning/done/DM-022-member-readable-geofence-policy-events-group-scope.md,planning/done/DM-023-geofence-adjacent-settings-bypass-write-time-valid.md,planning/done/DM-024-same-key-in-query-string-json-body-merges-to-a-lis.md,planning/done/DM-025-dispatcher-numeric-group-resolution-skips-is-activ.md,planning/done/DM-026-github-oauth-login-on-the-bouncer-hosted-auth-page.md,planning/done/DM-027-group-rest-save-collapses-to-the-view-check-any-ac.md,planning/done/DM-028-post-api-group-member-invite-returns-a-raw-500-typ.md,planning/done/DM-029-add-explicit-auth-gates-to-the-permission-check-si.md,planning/done/DM-030-jsonfield-replace-bypasses-protected-json-perms-ma.md,planning/done/DM-031-geofence-test-override-mojo-test-mode-are-db-redis.md,planning/done/DM-032-rest-batch-save-skips-instance-level-permission-ch.md,planning/done/DM-033-fileman-initiated-uploads-can-t-be-completed-or-fk.md,planning/done/DM-034-oauth-login-drops-the-redirect-param-user-lands-on.md,planning/done/DM-035-field-action-level-permission-gates-omit-the-base-.md,planning/done/DM-036-apikey-set-permissions-silently-discards-non-dict-.md,planning/done/DM-037-apikey-validate-token-grants-group-context-without.md,planning/done/DM-038-rest-batch-save-ignores-can-update-can-create-flag.md,planning/done/DM-039-get-api-group-pk-member-resolves-touches-any-group.md,planning/done/DM-041-config-driven-item-id-prefixes-dm-canonical-workfl.md,planning/in_progress/DM-042-authenticated-abuse-doom-loop-hardening-default-pe.md,planning/inbox/apikey-group-context-ignores-group-is-active.md,planning/inbox/apikey-identity-gate-hardening.md,planning/inbox/apikey-parent-key-inactive-descendant-one-way-door.md,planning/inbox/apikey-suspension-residual-surfaces.md,planning/inbox/batch-ignores-can-update-can-create-flags.md,planning/inbox/filevault-unfiltered-pk-cross-tenant-access.md,planning/inbox/get-member-for-user-parent-walk-ignores-parent-is-active.md,planning/inbox/group-me-member-endpoint-oracle-touch.md,planning/inbox/member-perms-ignore-group-is-active.md,planning/inbox/phone-verify-dev-bypass-code-db-settable.md,planning/inbox/test-security-full-suite-red.md,planning/inbox/user-is-superuser-unguarded-on-non-user-identity.md,scripts/intake.sh,scripts/ready.sh,testit/client.py,tests/test_account/test_bouncer_limits.py,tests/test_account/test_disable_kill_switch.py,tests/test_account/test_group_me_member_oracle.py,tests/test_email/email_change.py,tests/test_global_perms/apikey_group_inactive.py,tests/test_limits/__init__.py,tests/test_limits/api_throttle.py,tests/test_limits/block_dedup.py,tests/test_limits/traffic_concentration.py,tests/test_models/batch_feature_flags.py,tests/test_realtime/connection_limits.py,tests/test_verification/verification.py,uv.lock
- tests added: tests/test_limits/api_throttle.py (6: over-limit 429 + Retry-After,
  anonymous skip, exempt prefix, accounting-with-enforcement-off, per-key
  ApiKey.limits override, Redis fail-open), tests/test_limits/block_dedup.py
  (2: legacy _block and _throttle_block each emit exactly 1 event per window),
  tests/test_limits/traffic_concentration.py (5: sustained alert + hourly
  dedup, single-bucket spike no-alert, share trigger + MIN_TOTAL floor,
  ip-members never alert, ruleset bootstrap notify-not-block),
  tests/test_account/test_disable_kill_switch.py (4: disable kills live JWT +
  reactivation doesn't resurrect, validate_jwt generic error, user_api_key of
  disabled user rejected, revoke_sessions still rotates),
  tests/test_realtime/connection_limits.py (5: connect-rate block + single
  event, disabled + fail-open, pre-accept 4429 via real ASGI app, auth_required
  advertises 10s, 11th concurrent socket rejected),
  tests/test_account/test_bouncer_limits.py (1: muid limit blocks 31st, fresh
  session same IP passes). Updated to the new kill-switch contract:
  tests/test_email/email_change.py, tests/test_verification/verification.py.
