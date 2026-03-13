# Notifications API

The notifications API provides a per-user inbox of unread notifications. Notifications are created server-side and also delivered via WebSocket and device push. Clients poll or subscribe to stay current.

## Endpoints

| Method | URL | Description |
|---|---|---|
| `GET` | `/api/account/notification` | List notifications (unread by default) |
| `GET` | `/api/account/notification/<id>` | Get a single notification |
| `POST` | `/api/account/notification/<id>` | Mark read (or other actions) |

All endpoints require authentication. Users can only access their own notifications.

## List notifications

```
GET /api/account/notification
```

Returns unread notifications by default.

**Query parameters**

| Parameter | Description |
|---|---|
| `is_unread` | `true` (default) or `false` to list read notifications |
| `kind` | Filter by kind, e.g. `kind=message` |
| `limit` | Page size |
| `offset` | Pagination offset |

**Response**

```json
{
    "status": true,
    "count": 2,
    "data": [
        {
            "id": 101,
            "created": "2026-03-11T10:00:00Z",
            "title": "Your order shipped",
            "body": "Order #123 is on its way.",
            "kind": "general",
            "data": {},
            "action_url": "/orders/123",
            "is_unread": true,
            "expires_at": "2026-03-11T11:00:00Z"
        }
    ]
}
```

## Mark as read

```
POST /api/account/notification/<id>
```

```json
{ "mark_read": true }
```

**Response**

```json
{ "status": true }
```

## Get all notifications (including read)

```
GET /api/account/notification?is_unread=false
```

## WebSocket delivery

When a notification is created server-side, it is also delivered over WebSocket if the user is connected. The message shape mirrors the REST response:

```json
{
    "type": "notification",
    "title": "Your order shipped",
    "body": "Order #123 is on its way.",
    "kind": "general",
    "action_url": "/orders/123",
    "data": {}
}
```

Listen for `type === "notification"` in your WebSocket message handler to show real-time alerts without polling.

## The `kind` field

`kind` is a free-form string your server sets to help the client route or display notifications differently. Common values:

| kind | Suggested use |
|---|---|
| `general` | Default catch-all |
| `message` | New chat/inbox message |
| `alert` | Important system alert |
| `reminder` | Scheduled reminder |

Filter by kind: `GET /api/account/notification?kind=message`

## Notification lifecycle

1. Server calls `user.notify(...)` — creates DB row, sends WebSocket, sends device push
2. Client receives via WebSocket (if connected) or device push (if offline)
3. Client fetches inbox: `GET /api/account/notification`
4. Client marks read: `POST /api/account/notification/<id>` with `{"mark_read": true}`
5. Expired notifications are pruned automatically (default 1 hour TTL unless server sets `expires_in=None`)
