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
| `ip_address` | `GenericIPAddressField` | IP at login time (indexed) |
| `country_code` | `CharField(3)` | ISO 3166 code from GeoLocatedIP (nullable, indexed) |
| `region` | `CharField(100)` | Region/state name (nullable, indexed) |
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
| `list` | `id`, `ip_address`, `country_code`, `region`, `city`, `latitude`, `longitude`, `source`, `is_new_country`, `is_new_region`, `created` + `user` (basic graph) |
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
6. Records metrics (see below)
7. Returns the created event (or `None` if tracking disabled)

**Manual call (e.g. custom auth flow):**

```python
from mojo.apps.account.models.login_event import UserLoginEvent

event = UserLoginEvent.track(request, user, device=request.device, source="custom_sso")
```

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

All endpoints require `manage_users` + `security` + `users` permissions.
