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

Each history row is `{id, user_id, body, kind, edited_at, moderation_decision, created,
metadata, client_key}`. `client_key` is **author-scoped**: it carries the key on your
own messages and is `null` on everyone else's. `metadata` is an object (`{}` when
empty) — see [Message metadata](#message-metadata).

`/api/chat/room/flagged` now includes `metadata` on each row too, so a moderator
sees the payload a card or file message carried.

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
{"type": "chat_message", "room_id": 5, "body": "hello", "kind": "text",
 "metadata": {}, "client_key": "01J8Z0K3Q0X"}
```
Response:
```json
{"type": "chat_message_ack", "message_id": 123, "room_id": 5,
 "kind": "text", "metadata": {}, "created": "...", "client_key": "01J8Z0K3Q0X"}
```

`kind` and `metadata` are optional — see [Message kinds](#message-kinds) and
[Message metadata](#message-metadata). `client_key` is optional too — see
[Idempotent sends](#idempotent-sends).

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

## Message kinds

`kind` defaults to `"text"`. What you may send:

| Kind | You can send it | Meaning |
|------|-----------------|---------|
| `text` | yes | Ordinary message. `body` is **required**. |
| `image` | yes | Image message. `body` (the caption) is optional when `metadata` is non-empty. |
| `card` | only if this deployment registered a card validator | A structured payload the host app defines — a board item, a plan, a link preview. |
| `system` | **no** | Server-generated notices ("Alice joined"). |
| `file` | **no** | Server-generated file messages. |

Sending a kind you are not allowed to author — or a kind that does not exist —
returns:

```json
{"type": "error", "error": "Unsupported message kind"}
```

`card` is **off by default**. Until the deployment registers a validator for it,
a `card` send gets that same `"Unsupported message kind"` error. If a validator
is configured but cannot be loaded, you get
`{"type": "error", "error": "This message kind is not available"}` — `text` and
`image` keep working either way.

**A body is not always required.** `text` still needs one. For every other kind,
a body **or** non-empty `metadata` satisfies the requirement — an uncaptioned
image is normal. A message with neither returns
`{"type": "error", "error": "body or metadata is required"}`.

## Message metadata

Any message may carry a `metadata` object. It rides along on the ack, on the
`chat_message` broadcast and in history rows, so you render a message without a
second fetch.

**Limits — all enforced on every send:**

- JSON objects, arrays, strings, numbers, booleans and `null` only.
- Numbers must be finite. `NaN` and `Infinity` are refused.
- Object keys must be strings of at most 64 characters.
- At most 5 levels of nesting.
- **At most 4096 bytes** for the whole object, measured as compact JSON
  (`JSON.stringify` with no whitespace, UTF-8). Over the cap returns
  `{"type": "error", "error": "metadata exceeds the 4096 byte limit"}`.

Keep payloads small: a reference plus a label, not an embedded document.

**Card payloads are checked against the room's rules.** If the room has
`allow_urls: false` or `allow_phone_numbers: false`, the strings inside your
payload are checked too — you cannot slip a link past the setting by moving it
from `body` into `metadata`. You get the same
`"URLs are not allowed in this room"` error you would get for the body.

> **Render payloads as text, never as HTML.** Card metadata is untrusted data
> written by another consumer of the API. The server stores it verbatim — it is
> deliberately not sanitized or escaped, because escaping would corrupt links
> and ids that must round-trip. Put payload strings on the page with
> `textContent` (or your framework's default text binding). **Never**
> `innerHTML`, never `dangerouslySetInnerHTML`, and never build a URL from a
> payload value without validating its scheme.

## Idempotent sends

A WebSocket send can be lost after the server stored it but before the ack reached you.
Retrying blindly posts the message twice. To make a retry safe, put a `client_key` on
the send frame.

**How to use it**

1. Generate a **fresh UUID or ULID per logical send** — one new key each time the user
   presses send, not per connection and not per room.
2. If the ack does not arrive, **resend the identical frame** — same `client_key`, same
   `body`, same `kind`.
3. Match the ack (and the `chat_message` event on the room topic) back to your pending
   send by `client_key`; `message_id` is only known after the first ack.

**Rules**

- 1–64 characters from `A-Z a-z 0-9 . _ : -`, sent exactly as-is. No spaces, no
  newlines, no surrounding whitespace — those are rejected with an error frame. Omit
  the field (or send an empty string) to opt out entirely.
- A retry returns the **original** `message_id`. No duplicate message is stored and no
  duplicate `chat_message` event is broadcast.
- **Reusing a key with different content is an error**, not a silent no-op: you get
  `{"type": "error", "error": "client_key is already bound to a different message",
  "client_key": "..."}` and nothing is stored. Never recycle a key for a new message —
  generate a new one.
- Keys are scoped per room and per sender. The same key in a different room is a
  different message.
- Every error frame from a send echoes your `client_key` back (except the frame
  rejecting the key itself, which never echoes the bad value).

## Incoming Events (subscribe to `chat:{room_id}`)

| Event type | Description |
|-----------|-------------|
| `chat_message` | New message `{message_id, room_id, user_id, body, kind, metadata, created, client_key?}` |
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
- `metadata` is an object on every message (`{}` when empty) — see
  [Message metadata](#message-metadata)
- `edited_at` is non-null if the message was edited — show "(edited)" indicator

## Room Types

| Kind | Join | History | Use case |
|------|------|---------|----------|
| `direct` | Auto (DM endpoint) | Full | 1:1 private messages |
| `group` | Invite only | From join date | Teams, support |
| `channel` | Self-join | Full | Announcements, community |
