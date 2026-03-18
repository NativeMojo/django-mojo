# Admin Portal API Guide — REST API Reference

This guide is for web developers building internal admin portals on top of django-mojo.

## What "Admin Portal" Means in Mojo

An admin portal is a frontend that calls privileged REST endpoints, usually with one or more of:

- `manage_users`
- `manage_groups`
- `manage_settings`
- `view_logs` / `manage_incidents` / other app-specific admin perms

Always design UI and API calls around explicit permission checks. A logged-in user is not automatically an admin.

## Base Pattern

1. Authenticate user (`POST /api/account/login`).
2. Store JWT securely and send `Authorization: Bearer <token>`.
3. Pass `group=<id>` when operating on group-scoped resources.
4. Handle `403` as "authenticated but missing permission".

## Common Admin Endpoints

| Area | Endpoint(s) | Typical Permission |
|---|---|---|
| User administration | `GET/POST /api/user`, `GET/POST /api/user/<id>` | `view_users`, `manage_users` |
| Group administration | `GET/POST /api/group`, `GET/POST /api/group/<id>` | `view_groups`, `manage_groups` |
| Group membership | `POST /api/group/member`, `POST /api/group/member/<id>` | `manage_users`, `manage_members`, `manage_group`, `manage_groups` |
| Secure settings | `GET/POST /api/settings`, `GET/POST /api/settings/<id>`, `DELETE /api/settings/<id>` | `manage_settings` |

## Secure Settings API (Admin)

The secure settings API is intended for admin portals and configuration consoles.

### Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/settings` | List settings (requires `manage_settings`) |
| `POST` | `/api/settings` | Create setting |
| `GET` | `/api/settings/<id>` | Get one setting |
| `POST` | `/api/settings/<id>` | Update setting |
| `DELETE` | `/api/settings/<id>` | Delete setting |

### Create a global setting

```json
POST /api/settings
{
  "key": "WEBHOOK_SECRET",
  "value": "super-secret-value",
  "is_secret": true
}
```

### Create a group-scoped setting

```json
POST /api/settings
{
  "group": 42,
  "key": "WEBAPP_BASE_URL",
  "value": "https://portal.example.com",
  "is_secret": false
}
```

### Response behavior for secrets

When `is_secret=true`, API responses include masked `display_value` (`"******"`). Build UIs to treat secret values as write-only unless the user explicitly replaces them.

### Listing with search

```http
GET /api/settings?search=WEBAPP&sort=key
```

## Example: Permission-Aware Frontend Calls

```js
const headers = { Authorization: `Bearer ${token}` };

// User admin list
await fetch('/api/user?size=20&sort=-created', { headers });

// Group admin list
await fetch('/api/group?size=50&sort=name', { headers });

// Settings list (admin only)
await fetch('/api/settings?size=100&sort=key', { headers });
```

## Error Handling Contract

Use `status`, `code`, and `error` from the response envelope:

- `401`: not authenticated (login required / expired token)
- `403`: authenticated but missing required permission
- `404`: resource not found

Do not infer permission from UI state alone. Always trust API responses.

## Related References

- [Core Authentication](../core/authentication.md)
- [User API](user.md)
- [Group API](group.md)
- [Account Overview](README.md)
