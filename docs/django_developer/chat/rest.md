# Chat REST Endpoints

All endpoints are under `/api/chat/`. Authentication is required on all endpoints.

## Room Management

### `GET /api/chat/rooms` — List user's rooms

Returns rooms the authenticated user is a member of.

### `POST /api/chat/room` — Create a room

Any authenticated user can create a room, groupless or in a group, as in
1.35.3. The owner is always the creator; a body `user` is ignored.

```json
{"name": "My Room", "kind": "group", "group": 5}
```

Owner membership is auto-created with `role="owner"`. The owner is always the
creator: a `user` field in the body is ignored.

### `GET /api/chat/room/<pk>` — Get room detail

### `PUT /api/chat/room/<pk>` — Update a room

Passes the [room-authority rule](permissions.md#who-may-run-a-room-admin-operation-room-authority)
first, then RestMeta `SAVE_PERMS`. An active or muted owner or admin, or a
`manage_chat` / `comms` holder, may change `name`, `metadata` and safe
`rules`; `kind` and `group` are left to `SAVE_PERMS`. A banned
owner, or one who left the room, gets 403. A `rules` value is validated like
`room/rules` (400 naming the key); one that is not an object is 400.

### `DELETE /api/chat/room/<pk>` — Delete a room

Requires `manage_chat` (globally or in the room's group). A banned caller is
refused, and a configured authority hook may refuse too. Notifies `CHAT_MESSAGE_DELETED_HANDLER` before the
cascade, and publishes `chat_room_deleted` `{room_id}` on the room topic after
the delete commits, so open sockets drop the topic at once.

### `POST /api/chat/room/join` — Join a channel

Only works for `kind="channel"`. For group-linked channels, checks group
permission. A channel with no group is open to any signed-in user unless the
project's authority hook refuses the join (403 and no row). A banned member
gets 403.

```json
{"room_id": 5}
```

### `POST /api/chat/room/leave` — Leave a room

Cannot leave DM rooms. Publishes `chat_member_left` `{room_id, user_id}` after the write commits.

```json
{"room_id": 5}
```

### `POST /api/chat/room/member/add` — Add member

Requires room admin (not banned) or `manage_chat` permission, then the
[reachability rule](permissions.md#who-a-caller-may-put-in-a-chat-reachability):
with no hook, any active user, including one outside the room's `group`.
A project's hook may narrow it.

```json
{"room_id": 5, "user_id": 42}
```

Room-side answers come first and are unchanged: 404 `"Room not found"`, 403
for another tenant's room, 400 for a direct room, 403 for a non-admin. After
that, every target-side refusal — no `user_id`, a bad one, your own, a
missing, inactive or refused user — is 404 `"User not found"`, with no
membership and no system message written. A banned member is reactivated only
when an active admin adds them and they are still reachable.

### `POST /api/chat/room/member/remove` — Remove member

Requires room admin or `manage_chat` permission, and role order: an admin
cannot act on the owner (403). A banned caller gets 403. Publishes `chat_member_removed` `{room_id, user_id}` after the write commits, so the removed user's open sockets stop receiving the room at once.

```json
{"room_id": 5, "user_id": 42}
```

### `POST /api/chat/room/member/mute` — Mute member

Requires room admin, `moderate_chat`, or `manage_chat` permission, and role
order (an admin cannot mute the owner). Muted users can read but not send.

```json
{"room_id": 5, "user_id": 42}
```

### `POST /api/chat/room/member/ban` — Ban member

Requires room admin, `moderate_chat`, or `manage_chat` permission, and role
order (an admin cannot ban the owner). Banned users cannot subscribe. Publishes `chat_member_banned` `{room_id, user_id}` after the write commits, so the banned user's open sockets stop receiving the room at once.

```json
{"room_id": 5, "user_id": 42}
```

### `POST /api/chat/room/rules` — Update room rules

Requires room admin or `manage_chat` permission. Merges provided keys into
existing rules. Every value is checked first: a bad one is 400 naming the key
and its range, with nothing written (ranges in
[Rules](rules.md#who-may-change-a-rule)). Turning `disappearing_ttl` on or
shortening it needs the global `manage_chat` (403 otherwise).

```json
{"room_id": 5, "allow_urls": false, "max_message_length": 1000}
```

### `GET /api/chat/room/members?room_id=5` — List room members

Excludes banned members.

### `GET /api/chat/room/online?room_id=5` — Online members

Returns list of currently online members with their role. Requires an
active/muted membership, or `chat`/`manage_chat` on the room's group; anyone
else is a 403 — the same gate as message history (a banned member is denied,
and a groupless room admits members only).

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
            "id": 482, "room_seq": 100, "user_id": 7, "body": "hello", "kind": "text",
            "edited_at": null, "moderation_decision": "allow",
            "moderation_reasons": [], "moderation_score": 0,
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

`room_seq` is the cursor arrival sequence, or null for old/legacy rows. It is
server-owned; clients continue to use message IDs for pagination and reads.

All rows carry persisted `moderation_decision`, `moderation_reasons` and
`moderation_score`. Numeric 0 is clean; null means legacy/unscored. Existing
history is not rescored. All severity levels retain their real body in
responses and count normally as unread. Consumers choose what to hide
(Maestro uses score >=35), permit Show, and suppress hidden notification
previews; see [Rules](rules.md#consumer-display-and-notification-contract).
Both ChatMessage REST graphs carry the same moderation fields, including the
moderator list. Clients cannot overwrite them through generic REST saves.

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

**Channel history is unbounded, and a groupless channel is open.** Any
signed-in user can join a channel with no `group` (`POST room/join`) and then
read its whole history. Put anything not meant for every account in a group
room, a group-linked channel, or a direct room.

The same three display bounds — unflagged, join cutoff, disappearing-message
TTL — live in `visible_messages(room, membership)`. Legacy unread counts use
them too. Cursor unread counts instead track arrival positions and may include
hidden/deleted slots; see [Unread counts](#get-apichatunread--unread-counts).

### `GET /api/chat/room/flagged?room_id=5` — Flagged messages

Moderator-only. Returns flagged messages for review.

## Direct Messages

### `POST /api/chat/dm` — Get or create DM

Returns the pair's existing direct room if one exists, otherwise creates one
when the [reachability rule](permissions.md#who-a-caller-may-put-in-a-chat-reachability)
allows it: by default any active user, as in 1.35.3.

```json
{"user_id": 42}
```

Every refusal about the target — no `user_id`, a bad one, your own, a missing,
inactive or refused user, a hook error — is the same 404
`{"error": "User not found"}`, with no room written. (A DM to yourself used to
be 400 `"Cannot DM yourself"`.) The existing-room lookups run before the rule,
so a pair already sharing a direct room always gets it back.

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

`up_to_message_id` is resolved through `services.read_state.mark_through`,
shared with the `chat_read` WebSocket handler. Both backends apply join
entitlement and exclude flagged targets, but deliberately do not apply TTL.
An entitled message can still be acknowledged after it expires from display.

The response carries the resolved id **additively**:

```json
{"status": true, "code": 200,
 "data": {"status": true, "up_to_message_id": 480, "position": null, "changed": true}}
```

The endpoint returns a plain dict with no `data` key, so the response decorator
wraps it — `data.status` is unchanged and `data.up_to_message_id` is the new
field. It is `null` when nothing resolved.

A **non-numeric** `up_to_message_id` now resolves to `null` and returns 200.
It previously ran a bare `int()` and raised `ValueError` out of the view as a
500.

**Default legacy backend.** The target clamps downward to the newest entitled
message at or below the supplied ID. The service writes non-channel receipts
and advances `last_read_at` only forward. `position` is null;
`changed` means a target resolved.

**Opt-in cursor backend.** The exact entitled in-room PK must exist; foreign,
missing, flagged and pre-join IDs do not clamp. `position` is the effective
durable [position plus actual acknowledgment facts](read_state.md#position-shape).
`changed` reports a state change; a valid duplicate can be false. An invalid
target returns null target, current position and false without a frame/hook.
Reads advance monotonically within a generation and clear sparse exceptions
through the target, including below the encoded prefix. No receipts are
written. The framework publishes the committed cursor read frame and calls
`CHAT_READ_HANDLER`; adapters must not publish a duplicate frame.

### `GET /api/chat/unread` — Unread counts

Returns unread message counts per room for the authenticated user, over
memberships with `status in ("active", "muted")`. Rooms with a zero count are
omitted.

**Default legacy backend — same bound as history.** Each room's count narrows through
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

**Cursor backend — arrival counts.** Each row is a current
[position](read_state.md#position-shape), including the same room labels plus
`user_id`, `generation`, `head_seq`, `read_seq`, `last_read_message_id`,
`read_at`, `unread_exception_count`, `read_frontier_message_id` and
`unread_count = head_seq - read_seq + unread_exception_count`. Tenant policy
still filters rooms before snapshots are returned. The count query joins
memberships to rooms; it does not scan message history or read receipts.

Flagging, TTL expiry and ordinary deletion do not reduce cursor arrival
counts. Sending advances the author's own seat through that send, and new
seats start at the current head. Counts can include an invisible/deleted tail;
trusted server `mark_current_head` can clear it. Clients cannot supply a raw
sequence or set the model read fields. For displayable unread candidates,
hosts use exception-aware `unread_queryset` before content limits rather than
assuming a count equals the visible page length.
