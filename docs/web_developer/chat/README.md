# Chat — Web Developer Reference

Real-time chat over WebSocket + REST API for history and management.

## Quick Start

1. Connect to WebSocket at `ws/realtime/` and authenticate with JWT
2. Subscribe to room topic `chat:{room_id}`
3. Send messages via WebSocket: `{"type": "chat_message", "room_id": 5, "body": "hello"}`
4. Receive messages from the topic subscription
5. Fetch history via `GET /api/chat/room/messages?room_id=5`

## REST Endpoints

All require JWT authentication via `Authorization: Bearer <token>`.

### Rooms

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/chat/rooms` | List rooms you're a member of |
| POST | `/api/chat/room` | Create a room `{name, kind, group?}` |
| GET | `/api/chat/room/<pk>` | Get room detail |
| POST | `/api/chat/room/join` | Join a channel `{room_id}` |
| POST | `/api/chat/room/leave` | Leave a room `{room_id}` |
| POST | `/api/chat/room/member/add` | Add member (admin) `{room_id, user_id}` |
| POST | `/api/chat/room/member/remove` | Remove member (admin) `{room_id, user_id}` |
| POST | `/api/chat/room/member/mute` | Mute member (mod) `{room_id, user_id}` |
| POST | `/api/chat/room/member/ban` | Ban member (mod) `{room_id, user_id}` |
| POST | `/api/chat/room/rules` | Update rules (admin) `{room_id, ...rules}` |
| GET | `/api/chat/room/members?room_id=X` | List members |
| GET | `/api/chat/room/online?room_id=X` | Online members |

### Messages

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/chat/room/messages?room_id=X` | Message history (paginated) |
| GET | `/api/chat/room/flagged?room_id=X` | Flagged messages (moderator) |

Pagination: pass `limit` (max 200) and `before` (message ID cursor).

### Direct Messages

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/chat/dm` | Get or create DM `{user_id}` |

### Read State

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/chat/room/read` | Mark read `{room_id, up_to_message_id}` |
| GET | `/api/chat/unread` | Unread counts per room |

## WebSocket Messages

Send via the existing realtime WebSocket connection. All require authentication.

### Send message
```json
{"type": "chat_message", "room_id": 5, "body": "hello", "kind": "text"}
```
Response: `{"type": "chat_message_ack", "message_id": 123, "created": "..."}`

### Edit message
```json
{"type": "chat_edit", "message_id": 42, "body": "updated text"}
```

### React to message (toggle)
```json
{"type": "chat_react", "message_id": 42, "emoji": "\ud83d\udc4d"}
```

### Typing indicator
```json
{"type": "chat_typing", "room_id": 5}
```

### Mark as read
```json
{"type": "chat_read", "room_id": 5, "up_to_message_id": 482}
```

### Flag message (moderator)
```json
{"type": "chat_flag", "message_id": 42}
```

## Incoming Events (subscribe to `chat:{room_id}`)

| Event type | Description |
|-----------|-------------|
| `chat_message` | New message `{message_id, room_id, user_id, body, kind, created}` |
| `chat_message_edited` | Message edited `{message_id, body, edited_at}` |
| `chat_message_flagged` | Message flagged (hide it) `{message_id}` |
| `chat_reaction` | Reaction toggled `{message_id, user_id, emoji, action}` |
| `chat_typing` | User is typing `{room_id, user_id}` |
| `chat_read` | Messages read `{room_id, user_id, up_to_message_id}` |
| `chat_member_joined` | Member joined `{room_id, user_id}` |
| `chat_member_left` | Member left `{room_id, user_id}` |

## Message Format

- Bodies are **markdown** — render with any lightweight markdown library
- Emojis are native unicode
- System messages have `kind: "system"` — render differently (e.g. "Alice joined")
- `edited_at` is non-null if the message was edited — show "(edited)" indicator

## Room Types

| Kind | Join | History | Use case |
|------|------|---------|----------|
| `direct` | Auto (DM endpoint) | Full | 1:1 private messages |
| `group` | Invite only | From join date | Teams, support |
| `channel` | Self-join | Full | Announcements, community |
