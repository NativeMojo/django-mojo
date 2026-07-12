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

Requires `manage_group` or `manage_groups` permission.

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

### Using an API Key

```
Authorization: apikey <token>
```

The key's group is automatically set on the request. Only permissions in the key's `permissions` dict are allowed. System-level permissions (`sys.*`) are always denied.

**A key cannot reach platform-global data, even with a matching permission.**
Some models have no per-group ownership at all — `User`, `GeoLocatedIP`,
jobs (`Job`/`JobEvent`/`JobLog`/`ScheduledTask`), login events, bouncer
devices/signals/bot-signatures, and file renditions, among others. Because
there is no group to confine the key's access to, these reject an API key by
default regardless of what's in its `permissions` dict — e.g. a key with
`{"manage_users": true}` still gets `403` from `GET /api/user`. Use a
service-account `User` with a real permission grant for that kind of machine
access instead. A handful of endpoints are purpose-built to accept a key for
shared/global data (like the GeoIP federation-sync receiver — see
[GeoIP](geoip.md)) and say so explicitly in their own docs.

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

Rotates the **calling** API key's secret **in place** — same key, same permissions, a new token. Authenticate with the key being rotated; the previous token stops working immediately, so save the new one (it is returned only once, like creation). No management permission needed (you already hold the secret); a user/JWT session gets `401`.

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
| `GET` | `/api/group/apikey` | List keys for a group |
| `POST` | `/api/group/apikey` | Create a key |
| `GET` | `/api/group/apikey/<id>` | Get key details |
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
- **API Keys**: scoped to one group, explicit permissions, `sys.*` always denied
- **User Auth Tokens**: carry full user permissions including `sys.*` — use with caution
- Set short expiry periods for temporary integrations
- All key generation is logged in the audit trail
