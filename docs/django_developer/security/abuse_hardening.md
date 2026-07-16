# Authenticated-Abuse Hardening (Doom-Loop Defense)

Authenticated abuse is a first-class threat category: a paying customer's AI
agent scraping your portal 24/7 with valid credentials produces the same
infrastructure damage as a deliberate application-layer DoS — and per-IP
throttles, user-agent sniffing, and WAF signatures never see it, because every
request carries a real login and looks like a browser. Worse, naive client
failure handling (instant websocket reconnects, one telemetry write per error)
turns server slowness into *more* load: a self-amplifying doom loop.

django-mojo ships layered, framework-level defenses (DM-042). This page
documents each control, its cost, and the deployment-level hardening the
framework cannot do for you.

**Request-path cost of all of this: one pipelined Redis round-trip (4
commands) per authenticated REST request. Anonymous requests: zero.** Every
control fails open on Redis errors — an outage never locks users out.

---

## 1. Global Per-Identity API Throttle

Every `@md.URL` route is throttled per authenticated identity (User pk or
ApiKey pk) — enforced in the URL dispatcher *before* group resolution and the
view, so a rejected request costs no DB work beyond the auth lookup. Anonymous
traffic is deliberately skipped: it is covered by the per-endpoint
`rate_limit` decorators, and IP-keyed global limits punish CGNAT bystanders.

Over-budget requests get a cheap static `429` with `Retry-After` (seconds
until the window resets):

```json
{"error": "Rate limit exceeded", "code": 429, "status": false}
```

### Settings

| Setting | Default | Meaning |
|---|---|---|
| `API_THROTTLE_ENABLED` | `True` | Enforcement on/off. Accounting (§2) runs regardless. |
| `API_THROTTLE_USER` | `240` | Requests per window per User. `<= 0` = unlimited. |
| `API_THROTTLE_APIKEY` | `600` | Requests per window per ApiKey. `<= 0` = unlimited. |
| `API_THROTTLE_WINDOW` | `60` | Fixed window, seconds. |
| `API_THROTTLE_EXEMPT_PREFIXES` | `[]` | Path carve-outs, same shape as `LOGIT_NO_LOG_PREFIX`: `"/api/foo"` or `"POST:/api/foo"`. |
| `API_THROTTLE_REPORT_FLOOR` | `60` | Identities above this many requests/window enter the accounting zset (§2) — the detection floor. |
| `API_THROTTLE_CONFIG_TTL` | `30` | In-process config cache, seconds. Setting changes land within this + one window. |

All keys are DB-settable (`Setting` rows / `settings.get`) and resolved at
most once per `API_THROTTLE_CONFIG_TTL` per process — never per request. The
defaults only catch machine-rate traffic: 240/min sustained for a full minute
is ~4 req/s from one account; real SPA bursts (a dashboard load firing 30–60
requests in a few seconds) stay far under it.

### Per-key and per-tier overrides

A busy machine integration raises its own budget via the existing
`ApiKey.limits` convention (window in **minutes**):

```python
api_key.limits["api"] = {"limit": 5000, "window": 1}
api_key.save()
```

Fleet-federation endpoints (`requires_global_perms(..., allow_api_keys=True)`
receivers — jobs control/broadcast, geoip sync, aws ingest) authenticate as
ApiKeys and are subject to the `apikey` budget: give fleet keys a `limits["api"]`
override, or exempt the paths via `API_THROTTLE_EXEMPT_PREFIXES`.

### De-amplified 429s

A blocked identity generates **one** metric + incident event per window
(first engagement only, `SET NX`-gated) — never one per rejected request. The
same gate was retrofitted onto the per-endpoint `rate_limit` /
`strict_rate_limit` decorators (one event per key+IP per minute). A failed
request must never cost the server more than a served one; that inversion is
the doom-loop mechanism.

---

## 2. Traffic-Concentration Detection

The postmortem signature this catches: *one account silently becoming 96% of
a service's traffic*. Detection is always on (independent of enforcement):

- The throttle's own counter doubles as accounting. Once per identity per
  window, its exact previous-window count is flushed into a 5-minute
  `traffic:top:{bucket}` zset (identities above `API_THROTTLE_REPORT_FLOOR`
  only), alongside a `traffic:total:{bucket}` counter.
- The `check_traffic_concentration` cron (every 5 minutes,
  `mojo/apps/incident/cronjobs.py`) reads the completed buckets and emits a
  level-6 `traffic:concentration` incident event when an identity is over
  threshold. At most one alert per identity per hour.

| Setting | Default | Meaning |
|---|---|---|
| `TRAFFIC_CONCENTRATION_RPM` | `120` | Sustained requests/minute that trigger an alert. |
| `TRAFFIC_CONCENTRATION_SUSTAIN_WINDOWS` | `2` | Consecutive 5-min buckets required (2 ≈ 10 min sustained). |
| `TRAFFIC_CONCENTRATION_SHARE` | `0.20` | Alert when one identity is this share of a bucket's traffic… |
| `TRAFFIC_CONCENTRATION_MIN_TOTAL` | `1000` | …but only when the bucket has at least this many requests (a dev box where one user IS the traffic must not page). |

The prebuilt ruleset (`RuleSet.ensure_traffic_rules()`, bootstrapped on the
detector's first run and included in `ensure_default_rules()`) bundles events
per identity for an hour and fires `notify://perm@manage_security`. It
deliberately does **not** `block://` — the abuser holds valid credentials and
can rotate IPs; IP blocks hit CGNAT bystanders. Use the kill switch (§3).

Event metadata carries the identity, measured rpm, traffic share, bucket
total, and the bucket's top source IPs.

---

## 3. The Account Kill Switch

Disabling a user is now a **single admin action that revokes access within
one request**:

```
POST /api/user/<pk>  {"disable": {"reason": "abuse", "note": "..."}}
```

`disable_entity` flips `is_active=False` **and rotates `auth_key` in the same
atomic UPDATE**, then force-disconnects the user's live websockets
(best-effort, cross-process). Consequences:

- Every outstanding JWT dies on its next request (`validate_jwt` checks both
  the signature against the rotated key *and* `is_active`; `user_api_key`
  tokens of a disabled user are rejected too).
- The rejection error is generic (`Invalid token user`) — no account-state
  oracle for whoever holds the token.
- **Reactivating does not resurrect old tokens** — the key was rotated; the
  user must re-authenticate. This is deliberate for the abuse case.
- `revoke_sessions` (rotate without disabling) also drops live websockets.

ApiKey and group suspension were already instant (`ApiKey.is_active`,
`Group.is_active` — validated per-request; see the DM-037 gates).

**Blocking playbook, in order:** ① account level (disable — precise, no
bystanders, survives IP rotation), ② edge deny by session/IP, ③ host-firewall
L3 drop for active bleeding (`GeoLocatedIP.block(reason, ttl=...)`, always
with a TTL, after checking CGNAT collateral across all properties).

---

## 4. WebSocket Connection Limits

| Setting | Default | Meaning |
|---|---|---|
| `WS_CONNECT_RATE_LIMIT` | `30` | Connects per minute per IP, checked **before** `websocket.accept`. `<= 0` disables. |
| `WS_MAX_CONNECTIONS` | `10` | Concurrent sockets per authenticated identity, checked at auth. `<= 0` disables. |
| `WS_UNAUTH_TIMEOUT` | `10` | Seconds an unauthenticated socket may live (advertised in `auth_required`). |

A reconnect storm now costs one Redis `INCR` and a refused handshake (close
code **4429** — clients must treat it as "deliberate rejection, back off"),
instead of a dedicated Redis pub/sub connection + three asyncio tasks + 30
seconds of state per attempt. The pub/sub connection is only created **after**
successful authentication. Storm engagement emits one deduped incident event
per IP per window (`traffic:ws_connect`) / per identity per minute
(`traffic:ws_maxconn`).

---

## 5. Telemetry Ingestion Bounds

Endpoints where a client POST becomes a DB write are bounded per **session**
(muid — the server-set HttpOnly cookie), not just per IP:

- `account/bouncer/event`, `account/bouncer/assess`: `ip_limit=60, muid_limit=30`/min.
- `incident/event`: `ip_limit=240, muid_limit=120`/min (generous — the same
  route serves security-dashboard reads; ingest volume is also bounded by §1).

**Never rate-limit `incident/ossec/alert/batch`.** The mojo-ossec sender is
already structurally capped (max 10 events/POST, one flush per 5s ≈ 12
POSTs/min/host) and treats a 429 like any error: after 3 retries it parks the
batch in a backup directory nothing re-reads. A 429 there means silently lost
security alerts during exactly the incidents you want them for.

---

## 6. Deployment Hardening (outside the framework)

The postmortem lessons that belong to your infrastructure, not this codebase:

- **Edge rate limits** (nginx `limit_req` or gateway equivalent) on the three
  places retry storms concentrate: websocket upgrades, telemetry/error
  endpoints, and auth/token refresh. Reject with static 429s at the edge —
  over-limit traffic should never reach the application, session store, or DB.
- **Burstable instances**: a burstable cloud instance in standard credit mode
  on a critical path loses ~80% of its capacity precisely during its worst
  hour. Use fixed-performance instances or unlimited mode with a billing
  alarm — and **alarm on the credit balance itself** (e.g. <20% for 10 min).
  It is the best early-warning metric this incident class has.
- **Alarm on inbound volume** vs baseline: a sustained >10× jump is nearly
  free to detect and nearly false-positive-proof.
- **Graceful web-server reloads do not apply new deny rules to established
  HTTP/2 and websocket connections** — old workers keep serving them under
  the old config. Sever at the host firewall (or kill draining workers) when
  it matters.
- **Fleet-wide reloads drop every long-lived websocket at once**, creating a
  reconnect stampede sized to your fleet. Stagger reloads; drain connections.
- **Before any IP block, check CGNAT collateral** across all your properties
  (a mobile-carrier IP is shared with unknown innocents — possibly including
  your own devices), and give every IP block an expiry.
- **Separate blast domains**: don't colocate the async job/task master with a
  public-facing service — a portal flood must not degrade payment posting.
- **Channel the demand**: a customer scraping your portal wants data access
  you don't sell. Detect sustained automation and answer with a sanctioned
  path (read-only API key with quotas, scheduled exports), not only a block.

---

## Test-Suite Notes

- The test profile sets `API_THROTTLE_ENABLED = False` and
  `WS_CONNECT_RATE_LIMIT = 0` — every module shares one identity budget and
  one IP, so suite-wide enforcement would flake.
- Throttle tests opt in per-request with the `X-Mojo-Test-Api-Throttle`
  header (JSON overrides: `enabled`, `user_limit`, `apikey_limit`, `window`,
  `report_floor`, `exempt_prefixes`), gated by the standard
  `mojo.helpers.test_mode.is_test_request` defenses.
- `clear_rate_limits(user_id=..., apikey_id=...)` clears an identity's
  throttle counters; `testit`'s `client.login()` does this automatically.

## Related

- [Security System Overview](README.md) — events, rules, handlers, incidents
- [Bouncer Architecture](../account/bouncer.md) — pre-auth bot detection
- [GeoIP System](../account/geoip.md) — IP blocking, threat escalation
- [Web Developer: Rate Limits & Client Backoff](../../web_developer/security/rate_limits.md) — the client-side contract
