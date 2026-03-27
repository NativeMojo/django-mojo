# Chat Models

## ChatRoom

Represents a chat room. Three kinds:

| Kind | Behavior |
|------|----------|
| `direct` | 1:1 DM. Exactly 2 members. Cannot join/leave. |
| `group` | Invite-only. Members see history from join date. |
| `channel` | Public. Anyone can join/leave. Full history visible. |

**Key fields:**
- `name` — display name (blank for DMs)
- `kind` — `"direct"`, `"group"`, or `"channel"`
- `user` — FK to owner (User)
- `group` — FK to Group (optional). Links room to the permission system.
- `rules` — JSONField with per-room content policies
- `created`, `modified` — timestamps

**Properties:**
- `room.topic` — returns `"chat:{room.pk}"` for realtime pub/sub

**Hooks:**
- `on_rest_pre_save` — sets default rules on creation
- `on_rest_created` — auto-creates owner membership

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
- `kind` — `"text"`, `"image"`, or `"system"`
- `moderation_decision` — `"allow"`, `"warn"`, or `"block"`
- `edited_at` — set when message is edited
- `is_flagged` — True if flagged by moderator (hidden from normal history)
- `flagged_by`, `flagged_at` — who flagged and when
- `metadata` — flexible JSONField
- `created` — timestamp

## ChatMembership

Links a user to a room with role and status.

**Key fields:**
- `room` — FK to ChatRoom
- `user` — FK to User
- `role` — `"member"`, `"admin"`, or `"owner"`
- `status` — `"active"`, `"muted"`, or `"banned"`
- `last_read_at` — used for channel unread counts
- `joined_at` — timestamp

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
