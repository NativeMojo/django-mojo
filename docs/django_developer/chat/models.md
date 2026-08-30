# Chat Models

## ChatRoom

Represents a chat room. Three kinds:

| Kind | Behavior |
|------|----------|
| `direct` | 1:1 DM. Exactly 2 members. Cannot join/leave. Members see history from join date. |
| `group` | Invite-only. Members see history from join date. |
| `channel` | Public. Anyone can join/leave. Full history visible. |

**Key fields:**
- `name` — display name (blank for DMs)
- `kind` — `"direct"`, `"group"`, or `"channel"`. The choices tuple is
  descriptive: nothing validates it, so a caller may store any value. Read
  visibility treats every kind except `channel` as invite-based.
- `user` — FK to owner (User)
- `group` — FK to Group (optional). Links room to the permission system.
- `rules` — JSONField with per-room content policies
- `created`, `modified` — timestamps

**Properties:**
- `room.topic` — returns `"chat:{room.pk}"` for realtime pub/sub

**Hooks:**
- `on_rest_pre_save` — sets default rules on creation
- `on_rest_created` — auto-creates owner membership
- `on_rest_pre_delete` — notifies the [deletion hook](services.md#deletion-hook)
  with every message id in the room before the cascade destroys them

**Default rules:**
```python
{
    "allow_urls": True,
    "allow_media": True,
    "allow_phone_numbers": True,
    "max_message_length": 4000,
    "disappearing_ttl": 0,  # seconds, 0 = off
    "rate_limit": 10,        # messages per user per second
}
```

## ChatMessage

A single message in a room.

**Key fields:**
- `room` — FK to ChatRoom
- `user` — FK to User (sender)
- `body` — message text (markdown)
- `kind` — one of:

  | Kind | Authorable by | Notes |
  |------|---------------|-------|
  | `text` | client, server | A body is required. |
  | `image` | client, server | Body optional when `metadata` is non-empty. |
  | `system` | **server only** | Join/leave/member-add notices. |
  | `card` | server; client **only** with a registered validator | Opaque, consumer-defined typed payload in `metadata`. |
  | `file` | **server only** | Listed because production rows already carry it. A client-authored `file` frame is refused. |

  The choices tuple is descriptive, not restrictive — nothing in this codebase
  runs `full_clean` on the save path, so `ChatMessage.objects.create` accepts
  any string. Enforcement lives in
  [`send_message`](services.md#kind-whitelist), which is the one creation path.
- `moderation_decision` — `"allow"`, `"warn"`, or `"block"`
- `edited_at` — set when message is edited
- `is_flagged` — True if flagged by moderator (hidden from normal history)
- `flagged_by`, `flagged_at` — who flagged and when
- `metadata` — JSONField carrying the message payload: the card body for
  `kind="card"`, the file reference for `kind="file"`, render hints for an
  image. Validated on every send by
  [`send_message`](services.md#metadata-contract): JSON scalars/lists/dicts
  only, finite numbers, string keys of at most 64 characters, at most 5 levels
  deep, and a byte cap on the compact JSON encoding
  (`CHAT_METADATA_MAX_BYTES`, default **4096**). Listed in `NO_SAVE_FIELDS`, so
  no future model-security route can let a client write it directly. Included
  in both the `list` and `default` graphs, so a client renders a message
  without a second fetch — note the `list` graph is live, it drives
  `GET /api/chat/room/flagged`.
- `client_key` — optional client-supplied idempotency key for the send
  (`CharField(max_length=64, null=True, blank=True)`). Set by the WebSocket
  `chat_message` handler; never writable over REST (listed in `NO_SAVE_FIELDS`).
- `created` — timestamp

**Unique constraint:** `chat_message_client_key_uniq` — a *partial* unique constraint
on `(room, user, client_key)` with condition `client_key IS NOT NULL`. Messages
without a key (the common case) are unaffected. There is deliberately no separate
`db_index=True` on `client_key`: the constraint's index leads with `room, user` and
already serves the dedupe lookup, and a second index would be dead weight on a
high-write table. See `docs/django_developer/chat/handler.md` for the idempotency
contract this enforces.

## ChatMembership

Links a user to a room with role and status.

**Key fields:**
- `room` — FK to ChatRoom
- `user` — FK to User
- `role` — `"member"`, `"admin"`, or `"owner"`
- `status` — `"active"`, `"muted"`, or `"banned"`
- `last_read_at` — used for channel unread counts
- `joined_at` — timestamp, `auto_now_add`. **This is the read bound for every
  non-`channel` room kind**: message history and unread counts return only
  messages created at or after it (see
  [`GET /api/chat/room/messages`](rest.md#messages)).

> **Deleting and recreating a membership resets `joined_at`.** That is the
> intended behaviour for a remove/re-add — the re-added member cannot read what
> they missed — but it makes a *wholesale membership resync* destructive: code
> that deletes every membership in a room and recreates them blanks the room's
> history for everyone. Update the rows you mean to change; do not delete and
> reinsert the set.
>
> **Contract: attach every founding membership before the first message.** A
> participant whose membership is created after messages already exist starts
> with an empty room. `on_chat_dm` and `POST room/member/add` both already do
> this; consumer code driving the ORM directly must too.

**Properties:**
- `is_active` — status == "active"
- `can_send` — status == "active" (muted/banned cannot send)
- `is_admin` — role in ("admin", "owner")

**Unique constraint:** (room, user) — one membership per user per room.

## ChatReaction

Emoji reaction on a message. Toggle-based (add/remove).

**Key fields:**
- `message` — FK to ChatMessage
- `user` — FK to User
- `emoji` — unicode emoji (max 8 chars)

**Unique constraint:** (message, user, emoji) — one reaction per emoji per user.

## ChatReadReceipt

Per-message read receipt for `direct` and `group` rooms only. Created on first read, never updated.

**Key fields:**
- `message` — FK to ChatMessage
- `user` — FK to User
- `read_at` — auto-set on creation

**Unique constraint:** (message, user) — one receipt per user per message.

For **channels**, `last_read_at` on ChatMembership is used instead (no per-message receipts).
