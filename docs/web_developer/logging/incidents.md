# Incident API — REST API Reference

## Permissions Required

- `view_incidents` or `manage_users`

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/incident/event` | List security events |
| GET | `/api/incident/event/<id>` | Get event |
| GET | `/api/incident/incident` | List incidents |
| GET | `/api/incident/incident/<id>` | Get incident |
| POST | `/api/incident/incident/<id>` | Update incident state |
| GET | `/api/incident/rule` | List rules |
| GET | `/api/incident/ticket` | List tickets |

## List Incidents

**GET** `/api/incident/incident`

```
GET /api/incident/incident?state=open&sort=-created&size=20
```

**Response:**

```json
{
  "status": true,
  "count": 5,
  "start": 0,
  "size": 20,
  "data": [
    {
      "id": 301,
      "created": "2024-01-15T10:00:00Z",
      "state": "open",
      "status": "new",
      "priority": 5,
      "category": "auth:failed",
      "scope": "account",
      "metadata": {
        "source_ip": "1.2.3.4",
        "username": "unknown_user"
      }
    }
  ]
}
```

## Incident States

- `open` — Active, requires attention
- `investigating` — Being worked on
- `resolved` — Root cause addressed
- `closed` — No further action needed

## Update Incident State

**POST** `/api/incident/incident/<id>`

```json
{
  "state": "investigating"
}
```

## Merge Incidents

**POST** `/api/incident/incident/<id>`

```json
{
  "action": "merge",
  "incident_id": 302
}
```

Merges incident 302 into incident `<id>`.

## List Events

**GET** `/api/incident/event`

```
GET /api/incident/event?category=permission_denied&sort=-created&size=50
```

## Filtering

| Filter | Description |
|---|---|
| `state` | Incident state |
| `priority` | Priority level (1-10) |
| `category` | Event/incident category |
| `scope` | App scope (e.g., `account`) |
| `dr_start`, `dr_end` | Date range |
