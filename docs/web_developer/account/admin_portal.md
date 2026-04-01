# Admin Portal API Guide — REST API Reference

This guide is for web developers building internal admin portals on top of django-mojo.

## What "Admin Portal" Means in Mojo

An admin portal is a frontend that calls privileged REST endpoints. Access is controlled by permissions stored on each user. Always design UI and API calls around explicit permission checks. A logged-in user is not automatically an admin.

## Base Pattern

1. Authenticate user (`POST /api/login`).
2. Store JWT securely and send `Authorization: Bearer <token>`.
3. Pass `group=<id>` when operating on group-scoped resources.
4. Handle `403` as "authenticated but missing permission".

## Permissions

### Category Permissions (Use These in Your UI)

Category permissions are the **recommended way** to assign access. Each category grants full read+write access to an entire domain. Display these as toggles in your admin portal's user/member permission editor.

| Permission | Label | What it unlocks |
|---|---|---|
| `users` | User Management | Users, passkeys, MFA, API keys, OAuth, devices, locations |
| `groups` | Group Management | Groups, members, group API keys, settings |
| `security` | Security & Logs | Incidents, events, rules, tickets, IP blocks, bouncer, GeoIP, system logs |
| `comms` | Communications | Email, phone, SMS, push notifications, chat rooms, messages |
| `jobs` | Job System | Jobs, job events, job logs, runners, queue control |
| `metrics` | Metrics | All metrics — recording, fetching, categories, permissions |
| `files` | File Management | File managers, files, renditions, vault files, vault data |

**Superusers** (`is_superuser=true`) bypass all permission checks automatically. No category permissions needed.

### How to Display Permissions

Build your permission editor as a simple list of category toggles:

```
User Management     [on/off]
Group Management    [on/off]
Security & Logs     [on/off]
Communications      [on/off]
Job System          [on/off]
Metrics             [on/off]
File Management     [on/off]
```

This replaces the need to show 30+ individual permission toggles. Seven toggles cover the entire platform.

### Assigning Permissions via API

**Update a user's permissions:**

```
POST /api/user/<id>
```

```json
{
  "permissions": {
    "users": true,
    "groups": true,
    "security": true,
    "comms": true
  }
}
```

Requires `manage_users` or `users` permission.

**Update a group member's permissions:**

```
POST /api/group/member/<id>
```

```json
{
  "permissions": {
    "files": true,
    "comms": true,
    "jobs": true
  }
}
```

Requires `manage_groups`, `manage_group`, or `groups` permission.

**Invite a user with permissions:**

```
POST /api/group/member/invite
```

```json
{
  "email": "alice@example.com",
  "group": 7,
  "permissions": {
    "security": true,
    "files": true,
    "metrics": true
  }
}
```

### Reading Current Permissions

Permissions are returned in the user and group member responses as a JSON object:

```json
{
  "id": 42,
  "username": "alice",
  "permissions": {
    "security": true,
    "files": true,
    "users": true,
    "comms": true,
    "jobs": true
  }
}
```

Use this to set the initial state of your toggle UI. Any key not present or set to `false` means the user does not have that permission.

### Fine-Grained Permissions (Advanced)

Category permissions cover most use cases. Fine-grained permissions exist for when you need read-only access or scoped access within a domain. You do **not** need to show these in a standard admin UI — they are for special cases.

| Fine-Grained | Category | Use case |
|---|---|---|
| `view_security` | `security` | Read-only security dashboard (no edit access) |
| `view_users` | `users` | Read-only user directory |
| `view_groups` | `groups` | Read-only group listing |
| `view_fileman` | `files` | Read-only file browser |
| `view_logs` | `security` | Read-only log viewer (logs are part of `security`) |
| `view_jobs` | `jobs` | Read-only job monitoring |
| `view_metrics` | `metrics` | Read-only metrics dashboard |
| `manage_group` | `groups` | Manage only the user's own group (not all groups) |

If your portal needs a "read-only security viewer" role, assign `view_security` instead of `security`. But for most admin roles, the category permission is all you need.

### Protected Permissions

Some permission changes require the assigning user to have `manage_users` or `users`:

- `manage_users`, `manage_groups`, `view_logs`, `view_admin`, `manage_notifications`, `manage_files`, `manage_aws`
- All category permissions (`users`, `groups`, `security`, `comms`, `jobs`, `metrics`, `files`)

This prevents non-admin users from escalating their own access.

## Common Admin Endpoints

| Area | Endpoint(s) | Category Permission |
|---|---|---|
| User administration | `GET/POST /api/user`, `GET/POST /api/user/<id>` | `users` |
| Group administration | `GET/POST /api/group`, `GET/POST /api/group/<id>` | `groups` |
| Group membership | `POST /api/group/member`, `POST /api/group/member/<id>` | `groups` |
| Secure settings | `GET/POST /api/settings`, `DELETE /api/settings/<id>` | `groups` |
| Security dashboard | `GET /api/incident/incident`, `GET /api/incident/event` | `security` |
| Firewall / IP blocks | `GET/POST /api/incident/ipset` — see [IPSet Bulk Blocking](../security/README.md#ipset-bulk-blocking) | `security` |
| Bouncer devices | `GET /api/account/bouncer/device` | `security` or `users` |
| Bot signatures | `GET/POST /api/account/bouncer/bot_signature` | `security` or `users` |
| System logs | `GET /api/logs` | `security` |
| Email / SES | `GET/POST /api/aws/mailbox`, `GET/POST /api/aws/email_template` | `comms` |
| Phone numbers | `GET/POST /api/phonehub/phone` | `comms` |
| SMS | `GET/POST /api/phonehub/sms` | `comms` |
| Push notifications | `GET/POST /api/account/push/config` | `comms` |
| Chat rooms | `GET/POST /api/chat/room` | `comms` |
| Job system | `GET /api/jobs/status`, `GET /api/jobs/health` | `jobs` |
| Job control | `POST /api/jobs/control/*` | `jobs` |
| Metrics | `GET /api/metrics/fetch`, `POST /api/metrics/record` | `metrics` |
| Metrics permissions | `GET/POST /api/metrics/permissions` | `metrics` |
| File management | `GET/POST /api/fileman/manager`, `GET/POST /api/fileman/file` | `files` |

## Secure Settings API (Admin)

The secure settings API is intended for admin portals and configuration consoles.

### Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/settings` | List settings (requires `groups` or `manage_settings`) |
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

## Example: Permission-Aware Frontend

```js
const headers = { Authorization: `Bearer ${token}` };

// Fetch current user to check their permissions
const me = await fetch('/api/user/me', { headers }).then(r => r.json());
const perms = me.data.permissions || {};

// Show/hide admin sections based on category permissions
if (perms.users)    showSection('user-admin');
if (perms.groups)   showSection('group-admin');
if (perms.security) showSection('security-dashboard');
if (perms.comms)    showSection('communications');
if (perms.jobs)     showSection('job-system');
if (perms.metrics)  showSection('metrics-dashboard');
if (perms.files)    showSection('file-manager');

// API calls — the server enforces permissions regardless of UI state
await fetch('/api/user?size=20&sort=-created', { headers });
await fetch('/api/group?size=50&sort=name', { headers });
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
- [Security Dashboard](../security/README.md)
