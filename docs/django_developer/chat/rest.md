# Chat REST Endpoints

All endpoints are under `/api/chat/`. Authentication is required on all endpoints.

## Room Management

### `GET /api/chat/rooms` — List user's rooms

Returns rooms the authenticated user is a member of.

### `POST /api/chat/room` — Create a room

Any authenticated user can create a room.

```json
{"name": "My Room", "kind": "group", "group": 5}
```

Owner membership is auto-created with `role="owner"`.

### `GET /api/chat/room/<pk>` — Get room detail

### `POST /api/chat/room/join` — Join a channel

Only works for `kind="channel"`. For group-linked channels, checks group permission.

```json
{"room_id": 5}
```

### `POST /api/chat/room/leave` — Leave a room

Cannot leave DM rooms.

```json
{"room_id": 5}
```

### `POST /api/chat/room/member/add` — Add member

Requires room admin or `manage_chat` permission.

```json
{"room_id": 5, "user_id": 42}
```

### `POST /api/chat/room/member/remove` — Remove member

Requires room admin or `manage_chat` permission.

```json
{"room_id": 5, "user_id": 42}
```

### `POST /api/chat/room/member/mute` — Mute member

Requires room admin, `moderate_chat`, or `manage_chat` permission. Muted users can read but not send.

```json
{"room_id": 5, "user_id": 42}
```

### `POST /api/chat/room/member/ban` — Ban member

Requires room admin, `moderate_chat`, or `manage_chat` permission. Banned users cannot subscribe.

```json
{"room_id": 5, "user_id": 42}
```

### `POST /api/chat/room/rules` — Update room rules

Requires room admin or `manage_chat` permission. Merges provided keys into existing rules.

```json
{"room_id": 5, "allow_urls": false, "max_message_length": 1000}
```

### `GET /api/chat/room/members?room_id=5` — List room members

Excludes banned members.

### `GET /api/chat/room/online?room_id=5` — Online members

Returns list of currently online members with their role.

## Messages

### `GET /api/chat/room/messages?room_id=5` — Message history

Paginated, newest first. Excludes flagged messages. Supports cursor pagination.

| Param | Description |
|-------|-------------|
| `room_id` | Required |
| `limit` | Max messages per page (default 50, max 200) |
| `before` | Cursor — message ID to fetch before |

Response includes `has_more` and `cursor` for pagination.

**Join-time history cutoff.** For every room kind **except `channel`**, only
messages created at or after the caller's `ChatMembership.joined_at` are
returned. The bound is an exclusion, not an allowlist: `ChatRoom.kind` is a
caller-settable CharField, so a room created with an unrecognised kind is bound
too. `joined_at` is `auto_now_add`, so deleting and recreating a membership row
re-stamps it and the member loses everything sent before the new row — which is
exactly what makes a remove/re-add safe.

Two callers are deliberately not bound: `channel` rooms (public, full history to
every member), and the `manage_chat` moderator read path, which has no
membership row and therefore no `joined_at` to be bound by.

The same three bounds — unflagged, join cutoff, disappearing-message TTL — live
in `mojo.apps.chat.services.messages.visible_messages(room, membership)`. Every
reader narrows through it, `GET /api/chat/unread` included, so a badge can never
count a message this endpoint will not return.

### `GET /api/chat/room/flagged?room_id=5` — Flagged messages

Moderator-only. Returns flagged messages for review.

## Direct Messages

### `POST /api/chat/dm` — Get or create DM

Returns existing DM room if one exists between the two users, or creates a new one.

```json
{"user_id": 42}
```

## Read State

### `POST /api/chat/room/read` — Mark as read

```json
{"room_id": 5, "up_to_message_id": 482}
```

### `GET /api/chat/unread` — Unread counts

Returns unread message counts per room for the authenticated user.

```json
{
    "status": true,
    "data": [
        {"room_id": 5, "room_name": "Team Chat", "room_kind": "group", "unread_count": 3}
    ]
}
```
