# Login Events — Django Developer Reference

`UserLoginEvent` records one row per successful login with denormalized geolocation data. Supports map visualizations, first-time-country/region anomaly flags, and per-country metrics.

## Model: `UserLoginEvent`

Located at `mojo.apps.account.models.login_event`.

Inherits `models.Model, MojoModel`. Append-only audit log — no REST writes.

### Fields

| Field | Type | Description |
|---|---|---|
| `user` | FK → `account.User` | The user who logged in |
| `device` | FK → `account.UserDevice` (nullable) | Device used, if known |
| `ip_address` | `GenericIPAddressField` | IP at login time (indexed). Nullable — a login can be recorded with no resolved client IP. |
| `country_code` | `CharField(3)` | ISO 3166 code from GeoLocatedIP (nullable, indexed) |
| `region` | `CharField(100)` | Region/state name (nullable, indexed) |
| `region_code` | `CharField(10)` | ISO 3166-2 subdivision code from GeoLocatedIP, e.g. `US-CA` (nullable, indexed). May be `None` while `region` is set for older/subnet-fallback geo rows |
| `city` | `CharField(100)` | City name (nullable) |
| `latitude` | `FloatField` | Geo centroid latitude (nullable) |
| `longitude` | `FloatField` | Geo centroid longitude (nullable) |
| `source` | `CharField(32)` | Login method: `password`, `magic`, `sms`, `totp`, `oauth`, etc. |
| `user_agent_info` | `JSONField` | Parsed UA data — browser, OS, device family |
| `is_new_country` | `BooleanField` | True if this is the first login from this country for this user (indexed) |
| `is_new_region` | `BooleanField` | True if this is the first login from this country+region for this user (indexed) |
| `created` | `DateTimeField` | Login timestamp (indexed, auto) |
| `modified` | `DateTimeField` | Auto-updated (indexed) |

Composite indexes: `(user, country_code)` and `(user, country_code, region)` for fast per-user geo aggregation.

### RestMeta

| Setting | Value |
|---|---|
| `VIEW_PERMS` | `['manage_users', 'security', 'users']` |
| `SEARCH_FIELDS` | `ip_address`, `country_code`, `region`, `city` |

No `SAVE_PERMS` — the model is append-only. All writes go through `UserLoginEvent.track()`.

### Graphs

| Graph | Fields |
|---|---|
| `list` | `id`, `ip_address`, `country_code`, `region`, `region_code`, `city`, `latitude`, `longitude`, `source`, `is_new_country`, `is_new_region`, `created` + `user` (basic graph) |
| `default` | All list fields + `user_agent_info`, `modified` + `user` (basic) + `device` (basic) |

---

## track() — Recording a Login

```python
UserLoginEvent.track(request, user, device=None, source=None)
```

Call this after a successful authentication. It is already called automatically inside `jwt_login()` for all standard login paths.

**What it does:**

1. Checks `LOGIN_EVENT_TRACKING_ENABLED` — returns `None` immediately if disabled
2. Looks up `GeoLocatedIP` for `request.ip` (reads existing cache, does not trigger a new lookup)
3. Checks `is_new_country` and `is_new_region` by querying prior events for this user
4. Parses `request.user_agent` via `rhelper.parse_user_agent()`
5. Creates the `UserLoginEvent` row
6. If `request.ip` is `None`, files a suppressed incident (see below) — **after**
   the row is created, so a reporter fault can never cost the login record
7. Records metrics (see below)
8. Returns the created event (or `None` if tracking disabled)

### No-client-IP incident

A login recorded with no resolved client IP files
`account:login_no_client_ip` (level 5) via `report_event_suppressed`. This is
almost always a reverse-proxy / ingress that is not forwarding the client
address, which affects **every** request — so the suppression key is deliberately
**global, not per-user** (`account:login_event:no_ip_alerted`). A per-user key
would file one incident per login and flood the plane under exactly the
misconfiguration the incident exists to surface. The body names a `user.id` /
`event.id` only as an **example**, with an explicit note that the condition is not
user-specific.

**Manual call (e.g. custom auth flow):**

```python
from mojo.apps.account.models.login_event import UserLoginEvent

event = UserLoginEvent.track(request, user, device=request.device, source="custom_sso")
```

---

## The user-visible half: `incident.Event` sign-in rows

`UserLoginEvent` is the operator's log — it is gated behind `manage_users` /
`security` / `users` and the end user never sees it. The user's own Security
page (`GET /api/account/security-events`) reads `incident.Event` instead, so
`jwt_login()` and `group_token_login()` write a second, minimal row there
alongside the `UserLoginEvent`.

**Where it is written.** In `jwt_login` (`mojo/apps/account/rest/user.py`),
immediately after the `UserLoginEvent.track()` try/except and before
`fire_user_login`. The position is load-bearing: the post-credential geofence
gate, `requires_password_change`, and a raising `group_token.mint` all return
above it, so a blocked, forced-change or failed-mint login writes nothing.

**The row.** Fixed, non-interpolated text — no username, email, token, path or
query string can reach it:

| | |
|---|---|
| `category` | `login` (`SIGNIN_EVENT_CATEGORY`) |
| `title` | `Successful login` |
| `details` | `A sign-in completed for this account.` |
| `level` | 1 |
| `scope` | `account` |
| `source_ip` | `request.ip` (may be `None`) |

`sessions:logout` (`Browser sign-out requested`) is the same shape, written only
by `POST /api/account/security-events/logout` — an audit-only endpoint that
records history and has no session side effects at all.

### Exempt sources

```python
LOGIN_EVENT_EXEMPT_JWT_SOURCES = ("sessions_revoke", "email_change")
```

An authed re-issue of an existing session is not a new sign-in, and telling the
user "you signed in" because they revoked their sessions or confirmed an email
change would be a lie in their own audit trail.

This is a **separate tuple** from `GEOFENCE_EXEMPT_JWT_SOURCES`, even though the
members match today. That one decides whether a geofence runs and is fail-closed
for every future source; this one decides whether history records a sign-in.
Aliasing them would let an addition here silently skip the geofence.

`refresh` needs no entry: `on_refresh_token` mints directly and never calls
`jwt_login`, so a silent refresh is excluded **structurally**.

### Request-less, with an explicit uid

The rows go through `_record_account_activity(..., provenance="brand")`, which
calls `record_event(request=None)`. That is deliberate:
`mojo/apps/incident/reporter.py` overwrites a caller-supplied `uid` with
`request.user.id` whenever the passed request is authenticated — and on a login
POST that identity can be a **stale foreign bearer** the client left attached
while posting somebody else's credentials. Passing no request keeps the
credential-verified subject on the row. It also keeps `bearer`, `user_email`,
`http_query_string` and `http_user_agent` out of it, which the fixed-text
contract requires at the source.

### Record, do not publish

`record_event` writes the row; `report_event` additionally runs
`Event.publish()` → RuleSet matching, including the `"*"` catch-all. Publishing
would mean an incident on every successful login for any deployment with a
catch-all rule, plus two RuleSet queries on the hot path. Level 1 is far below
`INCIDENT_LEVEL_THRESHOLD` anyway.

### Group attribution

`_attributable_login_group(request, user)` — the sibling of
`_attributable_group`, keyed on the **verified user** rather than
`request.user`. It returns `request.group` only when that group is an
`account.Group` and `get_member_for_user(user, check_parents=False,
is_active=True)` finds a row: an effectively-active group and an active
**direct** membership, the same bar `group_token.can_mint` uses. Anything else
is `None` — the row is written unattributed, and the login itself is never
affected. `group_token_login` skips the check and passes its trusted handoff
group directly, because `mint()` already proved that membership.

Attributed rows appear under `?group=<brand>` on the Security feed;
unattributed rows appear in the owner-wide view only.

### Best effort, always

`_record_account_activity` never raises — it logs through
`logit.error("account_activity", …)` and returns `False`. An audit write that
fails must not fail a committed sign-in. (It returns `True`/`False` for the one
caller that reports the outcome to a client; every other call site ignores it.)

---

## Metrics

Four metrics are recorded per login (when geo data is available):

| Slug | Category | Condition |
|---|---|---|
| `login:country:{CC}` | `logins` | Always when `country_code` is known |
| `login:region:{CC}:{region}` | `logins` | When both `country_code` and `region` are known |
| `login:new_country` | `logins` | When `is_new_country=True` |
| `login:new_region` | `logins` | When `is_new_region=True` |

---

## Settings

All three settings use `get_static` and are read at startup. Changes require a server restart.

| Setting | Default | Description |
|---|---|---|
| `LOGIN_EVENT_TRACKING_ENABLED` | `True` | Master toggle. Set `False` to disable all event creation |
| `LOGIN_EVENT_FLAG_NEW_COUNTRY` | `True` | Enable first-time-country detection per user |
| `LOGIN_EVENT_FLAG_NEW_REGION` | `True` | Enable first-time-region detection per user |

---

## REST Endpoints

See [Login Events REST Reference](../../web_developer/account/login_events.md) for full endpoint documentation.

| Endpoint | Description |
|---|---|
| `GET /api/account/logins` | Paginated list with filtering |
| `GET /api/account/logins/<pk>` | Single event detail |
| `GET /api/account/logins/summary` | System-wide country/region aggregation |
| `GET /api/account/logins/user` | Per-user country/region aggregation |

All endpoints require one of `manage_users`, `security`, or `users`.
`summary` and `user` are gated with `@md.requires_global_perms` (global grant
only, no group/member fallback); `logins`/`logins/<pk>` are RestMeta-driven
and follow normal permission rules.

The Admin People inspector uses the existing list endpoint only. It reads the
stored `ip_address`, country/region/city, `user_agent_info`, `device`, source,
and new-country/new-region flags; it never invokes geolocation during a read.
Links into Activity use the frozen bounded query vocabulary documented in the
People feature and rely on each Activity lane's own permission gate.
