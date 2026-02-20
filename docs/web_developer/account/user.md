# User API — REST API Reference

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/user` | List users |
| POST | `/api/user` | Create user |
| GET | `/api/user/<id>` | Get user |
| POST/PUT | `/api/user/<id>` | Update user |
| DELETE | `/api/user/<id>` | Delete user |
| GET | `/api/user/me` | Get current user |

## Get Current User

**GET** `/api/user/me`

```bash
curl -H "Authorization: Bearer <token>" https://api.example.com/api/user/me
```

**Response (default graph):**

```json
{
  "status": true,
  "data": {
    "id": 42,
    "display_name": "Alice Smith",
    "username": "alice@example.com",
    "email": "alice@example.com",
    "phone_number": null,
    "permissions": {"manage_reports": true},
    "metadata": {},
    "is_active": true,
    "last_login": "2024-01-15T09:00:00Z",
    "last_activity": "2024-01-15T10:30:00Z",
    "avatar": null,
    "org": {"id": 5, "name": "Acme Corp"}
  }
}
```

## Update Own Profile

**POST** `/api/user/me` or **POST** `/api/user/<id>`

Users can update their own record (owner permission). Admins with `manage_users` can update any user.

```json
{
  "display_name": "Alice J. Smith",
  "phone_number": "+15551234567"
}
```

## List Users

Requires `view_users` or `manage_users` permission.

**GET** `/api/user`

```
GET /api/user?search=alice&is_active=true&sort=-created&start=0&size=20
```

**Response:**

```json
{
  "status": true,
  "count": 3,
  "start": 0,
  "size": 20,
  "data": [
    {
      "id": 42,
      "display_name": "Alice",
      "username": "alice@example.com",
      "is_active": true
    }
  ]
}
```

## Available Graphs

| Graph | Fields |
|---|---|
| `basic` | id, display_name, username, last_activity, is_active, avatar |
| `default` | id, display_name, username, email, phone_number, permissions, metadata, is_active, avatar, org |
| `full` | All fields |

```
GET /api/user/42?graph=basic
GET /api/user?graph=basic&size=50
```

## Update Permissions (Admin Only)

Requires `manage_users` permission.

```json
{
  "permissions": {
    "manage_reports": true,
    "view_analytics": true
  }
}
```

Permissions are merged (not replaced) due to JSONField merge behavior.

## Filtering

```
GET /api/user?is_active=true
GET /api/user?search=alice
GET /api/user?org=5
```
