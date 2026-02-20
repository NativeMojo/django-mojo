# Group API — REST API Reference

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/group` | List groups |
| POST | `/api/group` | Create group |
| GET | `/api/group/<id>` | Get group |
| POST/PUT | `/api/group/<id>` | Update group |
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

This scopes all requests to that group.

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
    "avatar": null
  }
}
```

## List Groups

Requires `view_groups` or `manage_groups` permission.

```
GET /api/group?kind=organization&sort=name
```

## Available Graphs

| Graph | Fields |
|---|---|
| `basic` | id, name, created, modified, last_activity, is_active, kind, avatar |
| `default` | basic fields + parent, metadata |
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

Returns `{"id": -1, "permissions": []}` if not a member.

### Invite a User

**POST** `/api/group/member/invite`

Requires `manage_users`, `manage_members`, `manage_group`, or `manage_groups` permission within the group.

```json
{
  "email": "bob@example.com",
  "group": 7,
  "permissions": {"manage_content": true}
}
```

### Update Member Permissions

**POST** `/api/group/member/<id>`

```json
{
  "permissions": {
    "manage_content": true,
    "view_reports": true
  }
}
```

## Hierarchical Groups

Groups can have a parent group. Child groups inherit parent-level permissions for members. Use `parent=<id>` to filter by parent:

```
GET /api/group?parent=7
```
