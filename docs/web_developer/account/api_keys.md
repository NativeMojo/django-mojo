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
| **IP restriction** | No | Yes (`allowed_ips` required) |

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
| `permissions` | No | JSON dict of granted permissions (default: empty) |
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

### Managing API Keys

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/group/apikey` | List keys for a group |
| `POST` | `/api/group/apikey` | Create a key |
| `GET` | `/api/group/apikey/<id>` | Get key details |
| `POST` | `/api/group/apikey/<id>` | Update name, permissions, limits, is_active |
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
| `allowed_ips` | Yes | List of allowed IP addresses/CIDR ranges (must have at least one) |
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

The request runs with the user's full permissions. Requests from IPs not in the `allowed_ips` list are rejected.

---

## Security Notes

- Store all tokens securely — treat them like passwords
- **API Keys**: scoped to one group, explicit permissions, `sys.*` always denied
- **User Auth Tokens**: carry full user permissions including `sys.*` — use with caution
- Set short expiry periods for temporary integrations
- All key generation is logged in the audit trail
