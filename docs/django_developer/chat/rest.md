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

The view builds its rows by hand rather than through a graph, so the shape is
fixed here, not in `ChatMessage.RestMeta.GRAPHS`:

```json
{
    "status": true,
    "data": [
        {
            "id": 482, "user_id": 7, "body": "hello", "kind": "text",
            "edited_at": null, "moderation_decision": "allow",
            "created": "2026-08-30T12:00:00+00:00",
            "metadata": {}, "client_key": "01J8Z0K3Q0X"
        }
    ],
    "has_more": true,
    "cursor": 482
}
```

`client_key` carries the stored key only when `msg.user_id == request.user.pk` and is
`null` on every other row. **This is not a confidentiality boundary** — the live
`chat_message` broadcast carries the sender's key to every room subscriber by design, and
`GRAPHS["default"]` includes it, so `/api/chat/room/flagged?graph=default` returns it on
every flagged row. Treat `client_key` as room-visible. `metadata` is always an object
(`{}` when empty).

`has_more` is `len(messages) == limit`, and `cursor` is the last row's `pk`
only when `has_more` — otherwise `null`.

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

Returns the pair's existing direct room if one exists, otherwise creates one.

```json
{"user_id": 42}
```

**Reuse is groupless-first.** A personal DM is groupless by construction — the
create path here has always built one that way, and `_deny_cross_tenant_room(request, None)`
runs first, so no confined credential (ApiKey / GroupScopedToken) ever reaches
this endpoint. The reuse lookup used to filter only on `kind="direct"` plus
shared membership, so it could hand a **tenant-managed** room back from the
global endpoint as though it were personal. It is now two separate, explicit
lookups:

1. A shared direct room with `group IS NULL` — the personal DM. Returned.
2. Otherwise, a shared direct room with a `group`. **Also returned**, as-is.

Case 2 does *not* fall through to creating a second, groupless room. Minting
one would strand the conversation: the history stays in the old room,
`GET /api/chat/rooms` lists both, and neither party learns why the thread went
blank. The response carries `group` (it is in both `ChatRoom` graphs), which is
how a client tells "this is your tenant-managed room" from "this is your
personal DM" and decides what to do about it.

## Read State

### `POST /api/chat/room/read` — Mark as read

```json
{"room_id": 5, "up_to_message_id": 482}
```

Requires a membership with `status in ("active", "muted")` — the same predicate
history and `/unread` use. A **banned** member gets 404 `"Not a member"` and
writes nothing; a **muted** member still marks read.

`up_to_message_id` is resolved through `services.read_state.mark_read`, shared
with the `chat_read` WebSocket handler — see
[Target resolution and last_read_at](handler.md#target-resolution-and-last_read_at)
for the clamp, the monotonic `last_read_at`, and why the disappearing-message
TTL is deliberately excluded from resolution.

The response carries the resolved id **additively**:

```json
{"status": true, "code": 200, "data": {"status": true, "up_to_message_id": 480}}
```

The endpoint returns a plain dict with no `data` key, so the response decorator
wraps it — `data.status` is unchanged and `data.up_to_message_id` is the new
field. It is `null` when nothing resolved.

A **non-numeric** `up_to_message_id` now resolves to `null` and returns 200.
It previously ran a bare `int()` and raised `ValueError` out of the view as a
500.

### `GET /api/chat/unread` — Unread counts

Returns unread message counts per room for the authenticated user, over
memberships with `status in ("active", "muted")`. Rooms with a zero count are
omitted.

**Same bound as history.** Each room's count narrows through
`visible_messages(room, membership)` — unflagged, the join-time cutoff for every
non-`channel` kind, and the disappearing-message TTL — then excludes the
caller's own messages. A badge can therefore never exceed what
`GET /api/chat/room/messages` returns for the same room. `channel` rooms count
`created__gt=last_read_at` (everything in bound when `last_read_at` is unset);
every other kind counts messages carrying no `ChatReadReceipt` for the caller.

```json
{
    "status": true,
    "data": [
        {"room_id": 5, "room_name": "Team Chat", "room_kind": "group", "unread_count": 3}
    ]
}
```
