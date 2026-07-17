# API Keys — Django Developer Reference

API keys give programmatic clients authenticated access scoped to a group, without requiring a user login. They authenticate via the standard `Authorization` header and plug into the existing permission system with no special cases.

> **API Keys vs User Auth Tokens:** MOJO has two authentication mechanisms for programmatic access. **API Keys** (`ApiKey` model, `Authorization: apikey <token>`) are group-scoped with explicit permissions — use these for external integrations. **User Auth Tokens** (`User.generate_api_token()`, `Authorization: bearer <token>`) are JWT tokens that carry a user's full system-level permissions — use these only when you need to act as a specific user. See [REST API docs](../../web_developer/account/api_keys.md) for the REST-facing comparison.

## How It Works

```
Authorization: apikey <raw_token>
```

`AuthenticationMiddleware` routes this to `ApiKey.validate_token()`, which:

1. SHA-256 hashes the incoming token and looks it up by `token_hash`
2. Checks `is_active` and `expires_at`
3. Sets `request.group = api_key.group` and `request.api_key = api_key`
4. Returns a synthetic user object whose `has_permission` delegates to `api_key.has_permission`

From that point forward the request behaves like a **group-scoped** request against **group-owned** data — `RestMeta` permission checks, `requires_perms`, and `request.group` filtering all confine the key to its own group. This synthetic user is not a request `User`, so any code that touches group membership must be ApiKey-safe: see [`Group.get_member_for_user` / `user_has_permission`](group.md#membership) for the identity guard that makes group permission gates degrade to deny/`None` instead of raising for a non-`User` identity.

**A key is confined to its group — it cannot reach platform-global data.** Two gates enforce this beyond the group filter:

- **`@md.requires_global_perms`** — endpoints with platform-wide effect (job control, AWS infra, geofence config, etc.) reject an ApiKey identity by default, regardless of its permissions dict — [`is_request_user(request)`](../helpers/request.md#is_request_user) is `False` for an `ApiKey`, so it never reaches the permission check. Pass `allow_api_keys=True` only for a federation/machine-ingest surface.
- **Model security on groupless models** — a `uses_model_security` model that has **no `group` foreign key** (e.g. `User`, `GeoLocatedIP`, `Job`, `UserLoginEvent`) is platform-global; there is no group to confine a key to, so the model-security layer **denies ApiKey identities by default**. A model may opt in with `RestMeta.ALLOW_API_KEY_GLOBAL = True` (default `False`) — no model does initially. Without this, a key self-claiming `manage_users` could otherwise read every tenant's rows. `Group` (also groupless) confines a key to its own group + descendants on both list and detail. **`ALLOW_API_KEY_GLOBAL` is honored only on genuinely groupless models — setting it `True` on a model that has a `group` FK is a misconfiguration and is ignored (fail-closed, logged via `logit.error`), since such a key could otherwise reach unscoped rows by arriving with no active group context.**

Machine access to platform-global data should use a dedicated `allow_api_keys` endpoint (like the geoip federation sync) or a **service-account `User`** with a real global grant — not a group ApiKey. See [permissions.md](../core/permissions.md#global-vs-group-scoped-permission-checks).

## Permissions

Permissions are stored as a plain JSON dict on the key:

```python
{"view_data": True, "edit_data": True}
```

**Rules:**
- `sys.*` permissions are **always denied** — API keys have no backing user to escalate to
- `"all"` always returns `True`
- List/set input uses OR logic — any match grants access
- Everything else is a direct dict lookup

**The `sys.` prefix convention:**

In `GroupMember.has_permission`, a permission like `sys.manage_users` strips the prefix and checks `user.has_permission("manage_users")` — escalating to the user's system-level permissions. This is how endpoints enforce "only a real system-level user can do this, even within a group context." API keys have no backing user, so `sys.*` is unconditionally denied.

**Who can assign a key's permissions (`APIKEY_PERMS_PROTECTION`):** a key's `permissions` are gated on write by `ApiKey.can_change_permission` (mirroring `GroupMember`). A global `manage_users`/`manage_groups` holder may assign anything — as of the bare-category expansion (see [Core → Permissions](../core/permissions.md#category-permissions-broad-access)), a holder of the combined `users`/`groups` term qualifies too, since it includes `manage_users`/`manage_groups` by definition; otherwise the requester must be a member of the key's group and hold the perm required by the `APIKEY_PERMS_PROTECTION` setting (a `{perm: required_perm}` dict, default `{}`, read as `kind="dict"`; `sys.`-prefixed requirements escalate to a global grant). This stops a group admin from self-minting a key with permissions they aren't entitled to grant. `ApiKey.create_for_group(...)` sets permissions directly (trusted internal call) and is not gated. The REST setter (`set_permissions`) accepts only a real dict — any other payload shape, including a JSON-encoded string, raises `ValueException` (400) rather than being silently ignored.

**Why regular permissions are still group-scoped:**

`validate_token` sets `request.group` to the key's group when that group is active (else `None` — see Group Scoping below). In `rest_check_permission`, when `request.group` is set the check routes to the api_key branch and returns immediately — the system-level user permission branch is never reached. This means `manage_users` on an API key applies within the key's group only, not system-wide.

## Group Scoping

Every API key belongs to one group. The key can access that group and any of its **active** descendants. If a request passes `group=<id>` in the request data and that group is not the key's group or a descendant, the dispatcher returns 403; an **inactive** group's id never resolves at all (same as a nonexistent id).

**Deactivating a group suspends its keys instantly (DM-037).** The active-state check is enforced at request time, so keys are never mutated — reactivating the group restores them immediately. It holds on every surface a key derives group context from:

- `validate_token` sets `request.group` only when the key's group is active; an inactive group leaves it `None`, so a no-`group=` request fails closed at model security.
- A detail/save/delete op re-binds `request.group` from the target row's group; the model-security api_key branch re-checks `is_active` there too, so it fails closed rather than being revived by the re-bind.
- The RestMeta list fallback derives the key's groups from `ApiKey.get_groups`, which excludes inactive groups — so a deactivated tenant's rows never enumerate.
- `ApiKey.is_group_allowed` requires the target group to be active. This also gates `Group.check_view_permission`/`check_edit_permission` (which run before the model-security branch), so a suspended tenant's key cannot read or write its own `Group` row — in particular it cannot flip `is_active` back on and un-suspend itself.
- The `@md.requires_perms` / `@md.requires_group_perms` decorators trust a non-User identity's permission dict only within an ACTIVE group context — a key with `request.group = None` is denied before its self-claimed perms are consulted. This closes custom (non-RestMeta) endpoints like `sms/send` too.
- **(DM-045)** For a group-scoped model, the model-security layer denies an inactive-group instance to an ApiKey identity *before* `check_view_permission`/`check_edit_permission` run — a future hook that naively grants via bare `request.api_key.has_permission(perms)` cannot reopen a suspended tenant's rows. See [Core → Instance-Level Permission Hooks](../core/permissions.md#instance-level-permission-hooks).

Not a hard token reject: an inactive-group key still **authenticates** (it returns `request.group = None`, not a 401). This preserves the group-independent federation path (`requires_global_perms(..., allow_api_keys=True)`, e.g. the geoip `/sync` receiver), which authorizes on the key's `has_permission` and ignores `request.group`. An **active child under an inactive parent** stays reachable via explicit `group=<child id>` — the filter is per-group, not a cascade.

## Creating Keys

### Programmatically

```python
from mojo.apps.account.models import ApiKey

api_key, raw_token = ApiKey.create_for_group(
    group=my_group,
    name="Mobile App v2",
    permissions={"view_orders": True, "create_orders": True},
)
# raw_token is a 48-char alphanumeric string — store it now, it cannot be recovered
```

### Via REST

```
POST /api/group/apikey
```

```json
{
  "group": 42,
  "name": "Mobile App v2",
  "permissions": {"view_orders": true, "create_orders": true}
}
```

The raw token is included in the creation response under `data.token` and is stored encrypted via `MojoSecrets` so it can be retrieved at any time.

```json
{
  "status": true,
  "data": {
    "id": 7,
    "name": "Mobile App v2",
    "token": "aB3kR9...48chars",
    "is_active": true,
    "permissions": {"view_orders": true, "create_orders": true},
    ...
  }
}
```

## Rate Limit Overrides

The `limits` field stores per-endpoint rate limit overrides used by `@md.rate_limit` and `@md.strict_rate_limit`:

```python
api_key, token = ApiKey.create_for_group(
    group=my_group,
    name="High-volume integration",
    permissions={"view_orders": True},
    limits={"orders": {"limit": 500, "window": 60}},  # window in minutes
)
```

See [Rate Limiting](../core/rate_limiting.md) for full details.

## REST Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/group/apikey` | List keys for a group |
| `POST` | `/api/group/apikey` | Create a key (returns token once) |
| `GET` | `/api/group/apikey/<id>` | Get key details |
| `POST` | `/api/group/apikey/<id>` | Update name, permissions, limits, is_active |
| `DELETE` | `/api/group/apikey/<id>` | Delete key |
| `GET` | `/api/group/apikey/me` | Whoami — the **calling** key's own identity + permissions |
| `POST` | `/api/group/apikey/rotate` | Rotate the **calling** key's secret in place; returns the new token once |

The CRUD endpoints require `manage_group` or `manage_groups` permission. The
`me` and `rotate` endpoints require only that the request is authenticated
**with an API key** (`@md.requires_auth()`) — no management permission (the caller
already holds the secret).

### `GET /api/group/apikey/me` — whoami

A self-introspection endpoint for service principals, analogous to
`GET /api/user/me` for human users. It lets a key holder confirm the token
is valid and inspect what the key is allowed to do, without holding any
management permission.

- Authenticate with `Authorization: apikey <token>`.
- A user/JWT-authenticated request has no API key and gets **401** — those
  callers should use `GET /api/user/me` instead.
- Serialized with the `me` graph: `id`, `created`, `name`, `is_active`,
  `permissions`, `limits`, `last_used`, `expires_at`, plus the nested
  `group` (basic). The graph is forced server-side — a `?graph=` override
  is ignored — so the raw `token` is **never** returned.

```json
{
  "status": true,
  "data": {
    "id": 7,
    "name": "sms-bridge",
    "is_active": true,
    "permissions": {"send_sms": true},
    "group": {"id": 12, "name": "Acme Co"},
    "last_used": "2026-05-20T17:04:00Z",
    "expires_at": null
  }
}
```

This is what `PhoneConfig.test_connection()` calls to validate a `mojo`
SMS-provider configuration without sending a real message.

### `POST /api/group/apikey/rotate` — rotate self

Rotates the **calling** key's secret **in place**: same key id, name,
permissions, and limits — a brand-new token. The previous token is invalidated
immediately (its hash is overwritten), so the new token must be persisted by
the caller; like creation, it is returned **exactly once** and cannot be
recovered afterward.

- Authenticate with `Authorization: apikey <token>` (the key being rotated).
- Self-service: no management permission — the caller already holds the secret
  (same trust model as `me`). A user/JWT request has no API key and gets **401**.
- Returns the `me` graph **plus** the new `token`:

```json
{
  "status": true,
  "data": {
    "id": 7,
    "name": "sms-bridge",
    "is_active": true,
    "permissions": {"send_sms": true},
    "group": {"id": 12, "name": "Acme Co"},
    "token": "<new-48-char-token>"
  }
}
```

Use it for scheduled credential rotation: a service rotates its own key, stores
the returned token, and continues — no second key, no `manage_group` grant, no
gap where the old secret lingers. (`ApiKey.rotate_token()` is the model-level
equivalent.)

## Lifecycle

```python
# Deactivate without deleting
api_key.is_active = False
api_key.save()

# Set expiry
from mojo.helpers import dates
api_key.expires_at = dates.utcnow() + dates.timedelta(days=90)
api_key.save()

# Rotate — create a new key, delete the old one
new_key, new_token = ApiKey.create_for_group(group, name, permissions)
old_key.delete()
```

## Security Notes

- The raw token is stored encrypted via `MojoSecrets` (AES encryption, key derived from the record's pk + created timestamp)
- `sys.*` permissions are unconditionally denied
- Expired or inactive keys return 401
- Group scope is enforced at the dispatcher level — keys cannot access groups outside their hierarchy
- `token_hash` and `mojo_secrets` are never included in API responses
