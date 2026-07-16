# Group API — REST API Reference

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/group` | List groups |
| POST | `/api/group` | Create group |
| GET | `/api/group/<id>` | Get group |
| POST/PUT | `/api/group/<id>` | Update group |
| POST | `/api/group/<id>` body `{"disable": {...}}` | Disable (archive/block) group — see [Disable Lifecycle](#disable-lifecycle) |
| POST | `/api/group/<id>` body `{"reactivate": {...}}` | Reactivate a disabled group |
| GET | `/api/group/member` | List group members |
| POST | `/api/group/member` | Add member |
| GET | `/api/group/member/<id>` | Get membership |
| POST | `/api/group/member/invite` | Invite user by email |
| GET | `/api/group/<id>/member` | Get current user's membership in group |

## Group Context

When working with group-scoped resources, pass the group ID as a parameter:

```
?group=<id>
```

This scopes all requests to that group. Only **active** groups resolve — a
deactivated group's id behaves exactly like an unknown one (no group context;
member-scoped requests are denied).

## Get Group

**GET** `/api/group/7`

```json
{
  "status": true,
  "data": {
    "id": 7,
    "name": "Acme Corp",
    "kind": "organization",
    "is_active": true,
    "parent": null,
    "metadata": {"timezone": "America/New_York"},
    "created": "2024-01-01T00:00:00Z",
    "modified": "2024-01-15T10:00:00Z",
    "avatar": null,
    "member_count": 12
  }
}
```

`member_count` is the count of active direct members of the group (does not include descendant groups). Available on the `default` graph only — `basic` stays minimal. List endpoints use `default` by default, so the field appears without needing `?graph=`.

> **Reads are downgraded for plain members.** A member without
> `view_group`/`manage_group` still gets a `200` on `GET /api/group/<id>`, but
> the response is the minimal `basic` graph (no `metadata`, `auth_domain`, or
> `parent`). Grant `view_group` (or higher) to see the full `default` graph.

## Update Group

**POST/PUT** `/api/group/<id>`

Updates top-level Group fields (`name`, `kind`, `auth_domain`, `metadata`, …).
**Requires a write grant** — `manage_group` (member-level), or a global
`manage_groups`/`groups`. Being a plain member is **not** sufficient: a member
without one of these perms receives a `403` (they can still read the group, see
above). An API key must be scoped to the group **and** hold the perm.

```json
POST /api/group/7
{ "name": "Acme Holdings", "metadata": {"timezone": "America/New_York"} }
```

Note: `metadata.geofence_strict` (compliance posture) is additionally gated by
the **global** `manage_geofence`/`security` perm — a tenant admin with
`manage_group` cannot flip it. The member-reachable `realtime_message`
POST_SAVE_ACTION also requires `manage_group`.

Note: `metadata.protected.*` requires `admin_compliance` or `admin_verify` — a
write grant alone is not enough. Any update that would touch it (a merge
carrying `"protected"`, a `"__replace": true` payload, or a non-dict `metadata`
value that would overwrite the existing `protected` subtree) returns a `403`.

## List Groups

Requires `view_groups` or `manage_groups` permission.

```
GET /api/group?kind=organization&sort=name
```

## Available Graphs

| Graph | Fields |
|---|---|
| `basic` | id, name, created, modified, last_activity, is_active, kind, avatar |
| `default` | basic fields + parent, auth_domain, metadata, member_count |
| `simple` | id, uuid, name, created, modified, is_active, parent, kind |

## Group Membership

### Get My Membership

**GET** `/api/group/<id>/member`

Returns the authenticated user's membership record in the specified group.

```json
{
  "status": true,
  "data": {
    "id": 15,
    "permissions": {"manage_content": true},
    "is_active": true
  }
}
```

Succeeds only when the caller is an active member of an **active** group. Every
other outcome — the caller is not a member, the group is inactive, or the id
does not exist — returns the standard `403` permission-denied response
(`{"error": "GET permission denied: GroupMember", "code": 403, "status": false}`),
indistinguishable by design (no existence oracle: probing an id reveals nothing
about whether a group exists). Treat a `403` from this endpoint as "no
membership here."

> **Changed:** this endpoint previously returned `200` with
> `{"id": -1, "permissions": []}` for non-members. Clients branching on
> `id === -1` must instead handle the `403`.

### Invite a User

**POST** `/api/group/member/invite`

Requires `manage_users`, `manage_members`, `manage_group`, `manage_groups`, or the combined `users`/`groups` term within the group (each combined term satisfies its `manage_` form by definition).

Requires authentication: an unauthenticated request receives a clean `403` (`{"error": "Permission Denied", "code": 403, "status": false}`), never a `500`. A `group` that does not resolve to an active group is likewise rejected with a generic `403` (not distinguished from a permission denial).

```json
{
  "email": "bob@example.com",
  "group": 7,
  "permissions": {"files": true, "comms": true}
}
```

Use [category permissions](admin_portal.md#category-permissions-use-these-in-your-ui) (`users`, `groups`, `security`, `comms`, `jobs`, `metrics`, `files`) for simplicity. Fine-grained permissions are also supported.

### Update Member Permissions

**POST** `/api/group/member/<id>`

```json
{
  "permissions": {
    "files": true,
    "security": true,
    "comms": true
  }
}
```

## Hierarchical Groups

Groups can have a parent group. Child groups inherit parent-level permissions for members. Use `parent=<id>` to filter by parent:

```
GET /api/group?parent=7
```

---

## Disable Lifecycle

Admins manage group `is_active` state through two named POST_SAVE_ACTIONS. Writing the bare `is_active` field directly still works but does not populate the disable namespace or emit audit events. The actions below are the recommended path for any new code. Requires a global `manage_groups` grant (the combined `groups` term includes it) — stricter than the rest of the Group `SAVE_PERMS` since disable is destructive: a member-level grant is not sufficient.

### Disable a group

**POST** `/api/group/<id>`

```json
{"disable": {"reason": "archived", "note": "Project sunset 2026 Q2"}}
```

`reason` must be one of: `admin`, `abuse`, `archived`. Server-set reasons (`inactive`) are rejected from REST.

Effect: `is_active=False`, populates `metadata.protected.disable` with `{reason, at, by_user_id, by_username, note}`. Members are unaffected — `GroupMember.is_active` does not cascade.

### Reactivate a group

**POST** `/api/group/<id>`

```json
{"reactivate": {"note": "Project resumed"}}
```

Effect: `is_active=True`. Appends a history entry to `disable.history` (FIFO cap 20) with the prior disable context and `reactivated_*` fields.

### Read disable state

`metadata` is in the default graph, so `data.metadata.protected.disable` is on every group response. `disable.reason` distinguishes admin-disabled, archived, abuse-disabled, and auto-disabled (inactivity sweep) cases.
