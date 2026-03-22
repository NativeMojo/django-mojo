# Unified Device Identity & Security Tracking

**Type**: request
**Status**: open
**Date**: 2026-03-22

## Description

Introduce server-controlled device identity (`_muid` cookie), session identity (`_msid`
cookie), and tab identity (`_mtab` sessionStorage) as first-class framework primitives.
Unify pre-auth (BouncerDevice) and post-auth (UserDevice) tracking by linking on the
server-controlled `_muid`. Refactor bouncer to use framework helpers instead of
duplicating them. Add identity correlation signals to the scoring engine.

## Why This Exists

Currently the only device identity is `duid` — a client-generated UUID stored in
localStorage. This is entirely client-controlled: a bot can rotate it per request, clear
it, or spoof it. The server has no independent way to identify a device.

Additionally, `BouncerDevice` (pre-auth) and `UserDevice` (post-auth) are completely
disconnected. After a user logs in, there is no link between their pre-auth bouncer
reputation and their authenticated device record. An admin cannot answer "what was the
bouncer history for this user's device before they logged in?"

## The Four Identity Layers

```
_muid (HttpOnly cookie, 2yr TTL)     — server-controlled device identity
  └─ _msid (HttpOnly cookie, session) — browser session identity (dies on quit)
      └─ _mtab (sessionStorage, JS)    — tab-scoped identity
duid (localStorage, JS)                — client claim (cross-referenced, not trusted)
```

| Name | Storage | Set by | Scope | Lifetime | Forgeable |
|------|---------|--------|-------|----------|-----------|
| `_muid` | HttpOnly cookie | Server (middleware) | All tabs | 2 years | No |
| `_msid` | HttpOnly cookie (no Expires) | Server (middleware) | All tabs | Browser session | No |
| `_mtab` | sessionStorage | JS (mojo-bouncer.js) | Single tab | Tab close | Yes |
| `duid` | localStorage | JS (mojo-auth.js) | All tabs | Permanent | Yes |
| `mbp` | HttpOnly cookie | Server (bouncer assess) | All tabs | 24h | No |

## Architecture

### Middleware (`MojoMiddleware`)

Set `_muid` and `_msid` on **every request**, not just bouncer requests. This makes
`request.muid` and `request.msid` available framework-wide.

```python
# In MojoMiddleware.__call__():
request.muid = request.COOKIES.get('_muid')
request.msid = request.COOKIES.get('_msid')

# Generate if missing — set on response
if not request.muid:
    request.muid = uuid.uuid4().hex
    # Set in process_response: _muid cookie, HttpOnly, Secure, SameSite=Lax, Max-Age=2yr

if not request.msid:
    request.msid = uuid.uuid4().hex
    # Set in process_response: _msid cookie, HttpOnly, Secure, SameSite=Lax, no Max-Age (session)
```

`request.duid` stays as-is (extracted from headers/params by existing middleware).
`request.mtab` read from `request.DATA.get('_mtab', '')` where present.

### Model Changes

#### `BouncerDevice` — add `muid` as primary, keep `duid` as secondary

```python
muid          # unique, indexed — server-controlled primary identity
duid          # indexed — client claim (for correlation)
msid          # nullable — current session ID (updated on each assess)
fingerprint_id
risk_tier     # unknown/low/medium/high/blocked
event_count, block_count
last_seen_ip
linked_muids  # JSONField — other muids sharing same fingerprint (rename from linked_duids)
first_seen, last_seen
```

`get_or_create_for_duid(duid, ip)` becomes `get_or_create_for_muid(muid, duid='', ip='')`.
Reputation keyed on `muid` — bots can't escape by rotating `duid`.

#### `UserDevice` — add `muid` to link pre-auth and post-auth

```python
user          # FK to User
muid          # indexed, nullable — links to BouncerDevice
duid          # kept for backward compat
device_info
user_agent_hash
last_ip, first_seen, last_seen
```

`UserDevice.track()` stores `request.muid` on the device record. Admin can now join
`UserDevice.muid → BouncerDevice.muid` to see pre-auth history for any user's device.

#### `BouncerSignal` — track all four identifiers

```python
muid          # indexed — server device identity
duid          # client claim
msid          # browser session
mtab          # tab session (nullable)
device        # FK to BouncerDevice
# session_id field replaced by msid
stage, ip_address, page_type, raw_signals, server_signals
risk_score, decision, triggered_signals, token_nonce, geo_ip, created
```

### New Scoring Signals

Add to `BOUNCER_SCORE_WEIGHTS` defaults:

```python
# Identity correlation signals
'muid_missing': 10,           # no server cookie — first visit or cookie-blocker
'muid_duid_mismatch': 15,     # muid and duid don't match known pair
'muid_duid_changed': 10,      # same muid but different duid than last assess
'msid_missing': 5,            # no session cookie — fresh browser or blocker
'concurrent_mtabs': 20,       # >N active mtabs per muid in last 5 min
'mtab_missing': 10,           # no tab session — non-JS client
'muid_multi_user': 25,        # muid logged into 3+ accounts in 24h
'muid_ip_drift': 15,          # muid from completely different geo than history
'msid_too_long': 10,          # same msid active > 24h — never-closing bot
```

New analyzer: `IdentityAnalyzer` — cross-references the four identity signals.

### Bouncer Code Audit Fixes (bundled into this work)

While touching all bouncer files, also fix the duplication issues identified in audit:

1. **Incident reporting** — switch from `BouncerDevice.class_report_incident()` with
   flat strings to `incident.report_event()` with structured metadata (duid, muid,
   risk_score, triggered_signals, page_type, decision as kwargs). Enables RuleSet
   matching on structured fields.

2. **Pass cookie HMAC** — replace manual `hmac.new()` in `_set_pass_cookie()` and
   `verify_pass_cookie()` with `mojo.helpers.crypto.sign.generate_signature()` and
   `verify_signature()`.

3. **GeoIP enrichment** — extract the 3x duplicated try/except block into a shared
   helper on BouncerDevice or a module-level function.

4. **Use `request.user_agent`** instead of raw `request.META['HTTP_USER_AGENT']` in
   `EnvironmentService`.

### JS Changes

`mojo-bouncer.js` generates `_mtab` in sessionStorage and sends it in every bouncer
API call:

```javascript
// On load:
let mtab = sessionStorage.getItem('_mtab');
if (!mtab) {
    mtab = crypto.randomUUID();
    sessionStorage.setItem('_mtab', mtab);
}
// Include in assess/event payloads: { duid, _mtab: mtab, ... }
```

`duid` stays in localStorage as-is. No changes to `mojo-auth.js` — it already sends
`duid` on every auth call.

### Admin Visibility (REST API)

The existing bouncer admin endpoints already serve BouncerDevice, BouncerSignal, and
BotSignature via REST CRUD. With `muid` as the linking key, the admin portal can:

- Query `BouncerDevice` by `muid` → full pre-auth history
- Query `UserDevice` by `muid` → which users logged in from this device
- Query `BouncerSignal` by `muid` → timeline of all security events
- Query `BouncerSignal` by `msid` → single browser session timeline
- Cross-reference: "show all muids that authenticated as user X" → account security view

No new endpoints needed — the existing RestMeta CRUD handles this via query params.

### Security Analysis Matrix

| Scenario | Signals | Interpretation |
|----------|---------|----------------|
| `_muid` present, `duid` matches known pair | Clean | Normal returning visitor |
| `_muid` present, `duid` different from last time | `muid_duid_changed` | localStorage cleared or rotated |
| `_muid` missing, `duid` present | `muid_missing` | Cookie-blocking bot or first visit |
| `_muid` present, `_msid` missing | `msid_missing` | Session cookie cleared (unusual) |
| Same `_muid`, 10+ `_mtab` values in 5 min | `concurrent_mtabs` | Multi-tab automation |
| Same `_muid`, 5 different user logins in 1h | `muid_multi_user` | Credential stuffing |
| Same `_msid` active for 48 hours | `msid_too_long` | Bot that never closes browser |
| `_muid` was in London, now Tokyo, 20 min later | `muid_ip_drift` | VPN hop or session theft |

---

## Acceptance Criteria

### Middleware
- [ ] `MojoMiddleware` sets `_muid` HttpOnly cookie (2yr, Secure, SameSite=Lax) on every
      response where the cookie is missing
- [ ] `MojoMiddleware` sets `_msid` HttpOnly session cookie (no Expires, Secure,
      SameSite=Lax) on every response where the cookie is missing
- [ ] `request.muid` and `request.msid` available on every request
- [ ] `request.mtab` read from `request.DATA.get('_mtab', '')` where present

### Models
- [ ] `BouncerDevice` gains `muid` field (unique, indexed) as primary identity
- [ ] `BouncerDevice` keeps `duid` as indexed secondary
- [ ] `BouncerDevice` gains `msid` field for current session tracking
- [ ] `BouncerDevice.linked_duids` renamed to `linked_muids`
- [ ] `BouncerDevice.get_or_create_for_muid(muid, duid, ip)` replaces duid-based lookup
- [ ] `UserDevice` gains `muid` field (indexed, nullable for legacy)
- [ ] `UserDevice.track()` stores `request.muid` on the device record
- [ ] `BouncerSignal` gains `muid`, `msid`, `mtab` fields (indexed where appropriate)
- [ ] `BouncerSignal.session_id` either replaced by `msid` or kept alongside for
      backward compat

### Scoring
- [ ] New `IdentityAnalyzer` registered in scoring pipeline
- [ ] Signals: `muid_missing`, `muid_duid_mismatch`, `muid_duid_changed`, `msid_missing`,
      `concurrent_mtabs`, `mtab_missing`, `muid_multi_user`, `muid_ip_drift`,
      `msid_too_long`
- [ ] Default weights added to `BOUNCER_SCORE_WEIGHTS`

### Bouncer Audit Fixes
- [ ] Incident reporting uses `incident.report_event()` with structured metadata kwargs
- [ ] Pass cookie uses `crypto.sign.generate_signature()` / `verify_signature()`
- [ ] GeoIP enrichment extracted into shared helper (not copy-pasted 3x)
- [ ] `EnvironmentService` uses `request.user_agent` not raw META

### JS
- [ ] `mojo-bouncer.js` generates `_mtab` in sessionStorage, sends in every API call

### Tests
- [ ] `_muid` cookie set on first request, persisted on subsequent requests
- [ ] `_msid` cookie set on first request, persisted within session
- [ ] `request.muid` available in endpoint handlers
- [ ] `BouncerDevice` created/looked up by `muid`
- [ ] `UserDevice.track()` stores `muid` on the device record
- [ ] `muid_missing` signal fires when `_muid` cookie absent
- [ ] `muid_duid_mismatch` signal fires when duid doesn't match known pair for muid
- [ ] `concurrent_mtabs` signal fires when >3 mtab values per muid in 5 min
- [ ] Bouncer assess, event, views all use `muid` as primary identity
- [ ] Pass cookie uses `crypto.sign` (not manual HMAC)
- [ ] Incident reporting passes structured metadata (duid, muid, risk_score, etc.)
- [ ] All existing bouncer tests still pass

### Admin Visibility (UserDevice `sessions` graph)
- [ ] `UserDevice.RestMeta.GRAPHS` includes a `sessions` graph that nests bouncer
      reputation, active sessions, and location history into the standard CRUD response
- [ ] `GET /api/user/device/<id>?graph=sessions` returns:
      - Device fields: `muid`, `duid`, `last_ip`, `device_info`, `first_seen`, `last_seen`
      - `bouncer_device`: nested dict from `BouncerDevice` joined on `muid` — `risk_tier`,
        `event_count`, `block_count`, `fingerprint_id`, `first_seen`, `last_seen`
      - `active_sessions`: list of recent sessions grouped by `msid` from `BouncerSignal`,
        each with: `msid`, earliest/latest `created`, `ip_address`, geo context, signal
        count, and nested `tabs` list grouped by `mtab` (each with `mtab`, earliest/latest
        `created`, signal count)
      - `recent_locations`: from `UserDeviceLocation` — `ip_address`, geo context,
        `first_seen`, `last_seen`
- [ ] Session/tab data assembled via computed properties on `UserDevice` (query
      `BouncerSignal` by `muid` with time window, group by `msid`/`mtab`)
- [ ] No custom endpoints — standard RestMeta CRUD with graph selection
- [ ] `manage_users` or `admin_security` permission required (existing `UserDevice`
      VIEW_PERMS)

### Docs
- [ ] `docs/django_developer/account/bouncer.md` updated with identity model
- [ ] `docs/web_developer/account/bouncer.md` updated with `_mtab` field in API calls
- [ ] `docs/django_developer/core/middleware.md` updated with `request.muid`, `request.msid`

## Constraints

- Must not break existing login flow for projects not using bouncer
- `duid` in localStorage and all JS that sends it stays as-is (backward compat)
- `_muid` and `_msid` cookies are set on ALL requests (not bouncer-only) — they are
  framework primitives
- No Python type hints
- No migration files committed (generated by projects at deploy time)

## Notes

- `_muid` uses `SameSite=Lax` (not Strict) so it's sent on top-level navigations from
  external links. `Strict` would cause the cookie to be missing on the first click from
  Google, email links, etc. — creating false `muid_missing` signals.
- `_msid` as a session cookie has a quirk: Chrome's "continue where you left off"
  setting restores session cookies across browser restarts. This means `msid_too_long`
  is the more reliable staleness signal than assuming `_msid` resets on browser close.
- `_mtab` is the only JS-controlled identifier in the new stack. It's inherently
  forgeable, but its absence (`mtab_missing`) is the stronger signal — a client that
  doesn't send it is likely not running JS at all.
- The `muid → UserDevice` link means the admin portal can answer "which users have
  ever logged in from this device?" and "what was this user's pre-auth bouncer history?"
  without any new endpoints — just REST queries with `muid` filter.
