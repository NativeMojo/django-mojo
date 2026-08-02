# API Keys & Auth Tokens — REST API Reference

## Overview

MOJO provides two ways to authenticate programmatic access:

| | **API Keys** (recommended) | **User Auth Tokens** |
|---|---|---|
| **Endpoint** | `POST /api/group/apikey` | `POST /api/auth/generate_api_key` |
| **Scope** | Group-scoped with explicit permissions | User's full system-level permissions |
| **Header** | `Authorization: apikey <token>` | `Authorization: bearer <token>` |
| **Use case** | External services, bots, integrations | Server acting as a specific user |
| **Permissions** | Only what you grant in `permissions` dict | Everything the user can do |
| **IP restriction** | No | Optional (`allowed_ips`) |

**Use API Keys** for external integrations and services. They follow least-privilege — only the permissions you explicitly grant are allowed, and access is confined to a single group.

**Use Auth Tokens** only when you need to act as a specific user with their full permissions (e.g., a backend service performing user-level operations).

---

## API Keys (Group-Scoped)

### Create an API Key

**POST** `/api/group/apikey`

Requires `manage_group`, `manage_groups`, or the combined `groups` permission (global or member-level).

```json
{
  "group": 42,
  "name": "Mobile App v2",
  "permissions": {"view_orders": true, "create_orders": true}
}
```

| Field | Required | Description |
|---|---|---|
| `group` | Yes | Group ID the key is scoped to |
| `name` | Yes | Descriptive name for the key |
| `permissions` | No | JSON **object** of granted permissions (default: empty). Must be a real object — any other shape, including a JSON-encoded string, is rejected with `400` |
| `limits` | No | Per-endpoint rate limit overrides |

**Response:**

```json
{
  "status": true,
  "data": {
    "id": 7,
    "name": "Mobile App v2",
    "token": "aB3kR9...48chars",
    "is_active": true,
    "permissions": {"view_orders": true, "create_orders": true}
  }
}
```

> **The token is not "shown once" — but you have to ask for it.** It is stored encrypted server-side and stays retrievable through the `token` graph:
>
> ```
> GET /api/group/apikey/<id>?graph=token
> ```
>
> ```json
> {
>   "status": true,
>   "graph": "token",
>   "data": { "id": 7, "name": "Mobile App v2", "token": "aB3kR9...48chars", "is_active": true }
> }
> ```
>
> Ordinary reads never carry the secret: `GET /api/group/apikey` (list), `GET /api/group/apikey/<id>` (detail) and `GET /api/group/apikey/me` all omit `token` unless you pass `?graph=token`.
>
> **The list endpoint honors it too.** `GET /api/group/apikey?graph=token` returns a live token for every key in the group in one response — so this is a bulk credential read, not a per-key one. The opt-in changes *where the secret travels*, not *who may ask for it*: it is open to the same `manage_group` / `manage_groups` / `groups` holders as any other read, and every returned token is audited server-side. Read access to a group's API keys remains equivalent to holding those keys — grant it accordingly.
>
> **Watch the spelling.** An unrecognized graph name silently falls back to the default graph, so `?graph=tokens` returns `200` with no `token` field rather than an error.

### Acting as a Member — `user` and `override_user`

A key can name the member it acts as. Both fields are writable on
`POST /api/group/apikey` and `POST /api/group/apikey/<id>`, and both appear in
the `default` and `me` graphs (`user` is nested using the User `basic` graph —
id, display name, username, activity flags and avatar; no email, phone,
permissions, or superuser flag).

| Field | Type | Meaning |
|---|---|---|
| `user` | user id, or `null` to clear | The member this key acts as |
| `override_user` | bool, default `false` | `false`: the link is a **reference** — it changes who your writes are attributed to, nothing else. `true`: the key **assumes** that member, and permissions come from their group membership. |

```http
POST /api/group/apikey/42
Content-Type: application/json

{"user": 137, "override_user": true}
```

**Constraints** (403 if violated):

- The target must be an **active member of the key's own group** — not a parent
  group, not another tenant.
- The target must **not** be a superuser.
- You need key-management permissions (`manage_group` / `manage_members` /
  `manage_users` / `manage_groups`, or the global equivalents).
- `override_user: true` requires a linked `user`. Clearing `user` also clears
  `override_user`.

**What an override key can and cannot do.** It can do the member's ordinary
work, bounded by the key's group. It **cannot**:

- reach any group the key itself doesn't belong to, even if the member does;
- use `sys.*` permissions;
- change the member's credentials — registering a passkey, generating a user
  auth token, setting up or disabling TOTP, changing the email, phone,
  username, or revoking sessions all return **403** for a key-authenticated
  request. This is deliberate: it keeps *deleting the key* sufficient to end
  the access.
- edit `User` records via `/api/user/<id>`.

If the linked member is deactivated, the key stops working and returns the same
`"API key is inactive"` error as a disabled key.

### Using an API Key

```
Authorization: apikey <token>
```

The key's group is automatically set on the request. Only permissions in the key's `permissions` dict are allowed. System-level permissions (`sys.*`) are always denied.

**Deactivating the key's group instantly suspends the key** — every group-scoped request (list, detail, save, delete, custom endpoints, with or without a `group=` param) is denied while the group is inactive, including reads/writes of the group record itself. The key is never modified, so reactivating the group restores it immediately; you do not need to (and should not have to) deactivate the key itself. The key still *authenticates* (so the group-independent federation-sync path keeps working) — it simply has no group context. **Deactivating a parent group also suspends every descendant's keys** — a child group is only reachable via `group=<child id>` while it *and every ancestor* are active; an active child under a deactivated parent is treated as inactive too, with no flag written to the child. Reactivating the parent restores the whole subtree instantly.

**A key cannot reach platform-global data, even with a matching permission.**
Some models have no per-group ownership at all — `User`, `GeoLocatedIP`,
jobs (`Job`/`JobEvent`/`JobLog`/`ScheduledTask`), login events, bouncer
devices/signals/bot-signatures, and file renditions, among others. Because
there is no group to confine the key's access to, these reject an API key by
default regardless of what's in its `permissions` dict — e.g. a key with
`{"manage_users": true}` still gets `403` from `GET /api/user`. Use a
service-account `User` with a real permission grant for that kind of machine
access instead. A handful of endpoints are purpose-built to accept a key for
shared/global data and say so explicitly in their own docs — for `GeoLocatedIP`
that's **both** `GET /api/system/geoip/lookup` (authentication only — no
permissions needed at all) **and** `POST /api/system/geoip/sync` (requires
`geoip_sync`); see [GeoIP](geoip.md). The plain CRUD endpoints
(`GET/POST /api/system/geoip`, `GET/PUT/DELETE /api/system/geoip/<pk>`) still
reject a key by default like every other groupless model.

Endpoints that resolve or inspect a group or its membership — `GET
/api/group/uuid/<uuid>`, `GET /api/group/<pk>`, `GET /api/group/<pk>/member` —
and any group-scoped permission check now work correctly under
`Authorization: apikey <token>`: they cleanly grant or deny access instead of
returning HTTP 400 `Must be "User" instance.`.

### Check a Key — `GET /api/group/apikey/me`

Whoami for the **calling** API key. Authenticate with `Authorization: apikey <token>` and it returns that key's own identity and granted permissions — useful for confirming a token works and seeing what it can do. Requires no management permission. A normal user/JWT session has no API key and gets `401` (use `GET /api/user/me` instead). The raw token is never returned.

```json
GET /api/group/apikey/me
Authorization: apikey <token>

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

### Rotate a Key — `POST /api/group/apikey/rotate`

Rotates the **calling** API key's secret **in place** — same key, same permissions, a new token. Authenticate with the key being rotated; the previous token stops working immediately and cannot be recovered. Save the new one — though note it is **not** write-once: a caller with `manage_group` / `manage_groups` / `groups` can read it back from `GET /api/group/apikey/<id>?graph=token` (see Security Notes). No management permission needed to rotate (you already hold the secret); a user/JWT session gets `401`.

```json
POST /api/group/apikey/rotate
Authorization: apikey <current-token>

{
  "status": true,
  "data": {
    "id": 7,
    "name": "sms-bridge",
    "permissions": {"send_sms": true},
    "group": {"id": 12, "name": "Acme Co"},
    "token": "<new-token-returned-once>"
  }
}
```

### Managing API Keys

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/group/apikey` | List keys for a group (no tokens) |
| `GET` | `/api/group/apikey?graph=token` | List keys **with their live raw tokens** (bulk read, audited) |
| `POST` | `/api/group/apikey` | Create a key |
| `GET` | `/api/group/apikey/<id>` | Get key details (no token) |
| `GET` | `/api/group/apikey/<id>?graph=token` | Get key details **plus the live raw token** (audited) |
| `POST` | `/api/group/apikey/<id>` | Update name, permissions, limits, is_active |
| `POST` | `/api/group/apikey/rotate` | Rotate the calling key's secret (returns new token once) |
| `DELETE` | `/api/group/apikey/<id>` | Delete key |

### Deactivate a Key

```json
POST /api/group/apikey/7
{"is_active": false}
```

---

## User Auth Tokens (JWT)

These generate a long-lived JWT that carries the user's full permissions. **Use API Keys instead** unless you specifically need to act as a user.

### Generate a Token (Own Account)

**POST** `/api/auth/generate_api_key`

Requires authentication.

```json
{
  "allowed_ips": ["192.168.1.1", "10.0.0.0/24"],
  "expire_days": 90
}
```

| Field | Required | Description |
|---|---|---|
| `allowed_ips` | No | List of allowed IP addresses/CIDR ranges (default: unrestricted) |
| `expire_days` | No | Expiry in days (default 360, max 360) |

**Response:**

```json
{
  "status": true,
  "data": {
    "token": "eyJhbGci...",
    "jti": "abc123",
    "expires": 1736899200
  }
}
```

### Generate a Token for Another User (Admin)

**POST** `/api/auth/manage/generate_api_key`

Requires `manage_users` permission.

```json
{
  "uid": 42,
  "allowed_ips": ["10.0.0.1"],
  "expire_days": 30
}
```

### Using a User Auth Token

```
Authorization: bearer <jwt_token>
```

The request runs with the user's full permissions. If `allowed_ips` was set, requests from IPs not in that list are rejected.

---

## Security Notes

- Store all tokens securely — treat them like passwords
- **API key tokens are recoverable, not write-once — but read-back is opt-in.** The raw token is stored encrypted on the record and returned only by `GET /api/group/apikey/<id>?graph=token`; plain list and detail reads omit it. Every opt-in read is audited server-side. Read access to a group's API keys is still equivalent to holding those keys — the opt-in narrows *where the secret travels*, not *who may ask for it*. Rotation does invalidate the *previous* token permanently.
- **API Keys**: scoped to one group, explicit permissions, `sys.*` always denied
- **User Auth Tokens**: carry full user permissions including `sys.*` — use with caution
- Set short expiry periods for temporary integrations
- All key generation is logged in the audit trail
