# Chat Support via Realtime System

**Type**: feature
**Status**: Planning
**Priority**: High
**Date**: 2026-03-26

## Goal

Add real-time chat as a new Django app (`mojo.apps.chat`) built on top of the existing `mojo.apps.realtime` WebSocket infrastructure. Rooms, messages, moderation, reactions, editing, read receipts — robust enough for support chats and casual group chats, simple enough for web developers to build on. Fully integrated with the existing User/Group/Member permission system.

## Background

The realtime system already provides everything needed for live message transport:
- Single WebSocket endpoint (`ws/realtime/`) with JWT auth
- Topic-based pub/sub via Redis (`publish_topic`, `send_to_user`, `broadcast`)
- Subscription authorization via `on_realtime_can_subscribe(topic)` on the User model
- Presence tracking (`is_online`, `get_online_users`)
- Custom message routing via `on_realtime_message(data)` or `REALTIME_MESSAGE_HANDLERS` setting

What's missing is the **persistence layer** (models, history) and the **chat-specific logic** (rooms, membership, moderation, reactions, read receipts).

### Architecture: Redis + DB

- **Redis (already built):** Live message delivery via pub/sub, presence, typing indicators (ephemeral)
- **Database (to build):** Message history, room definitions, membership, read state, reactions

A chat room with `id=5` maps directly to realtime topic `chat:5`. When a message is sent, it's persisted to the DB and published to the topic in one step.

## Permission System Integration

Chat integrates with the existing User/Group/Member permission model:

### How it works

- **ChatRoom has both `user` (owner) and `group` (optional) ForeignKeys** — this is the standard pattern for permission-scoped models
- **Group-linked rooms**: When `room.group` is set, permission checks flow through `group.user_has_permission(user, perm)`, which checks both system-level (`user.permissions`) and group-level (`member.permissions`). Parent group membership grants access to child group rooms automatically.
- **Non-group rooms**: Direct messages and standalone rooms use `ChatMembership` for access control (no group required)
- **RestMeta VIEW_PERMS/SAVE_PERMS** on chat models integrate with `rest_check_permission` automatically
- All REST endpoints use `@md.uses_model_security(Model)` — no `@md.requires_auth()` needed

### Chat permissions (group-scoped via `member.permissions`)

| Permission | What it grants |
|-----------|---------------|
| `chat` | Can participate in group chat rooms (send messages, react) |
| `manage_chat` | Can create/delete rooms, manage membership, update room rules |
| `moderate_chat` | Can flag/remove messages, mute/ban users in chat rooms |

These are stored in `member.permissions` at the group level. System-level users with `manage_groups` bypass all checks (superuser pattern).

### Group-linked rooms

A common pattern: a Group (org, team, project) gets a chat room automatically or on demand.

```python
# Create a chat room linked to a group
room = ChatRoom.objects.create(
    name="Acme Corp Chat",
    kind="group",
    user=request.user,      # owner
    group=acme_group,        # links to permission system
)
```

When `room.group` is set:
- Subscription auth checks `group.user_has_permission(user, "chat")` instead of just `ChatMembership`
- `ChatMembership` is still created for tracking role/status/read state within the room
- Members of parent groups inherit access to child group rooms (existing hierarchy behavior)
- `manage_chat` permission at group level lets group admins manage rooms without system-level perms

### Non-group rooms (DMs, standalone)

When `room.group` is `None`:
- Access is purely `ChatMembership`-based
- No group permission checks involved
- DMs are always non-group

## Room Types

One `ChatRoom` model, three `kind` values:

| Kind | Group-linked? | Behavior |
|------|--------------|----------|
| `direct` | Never | 1:1 private chat. Auto-created when user A messages user B. Exactly 2 members, no join/leave. |
| `group` | Optional | Invite-only. Members see history from when they joined. Supports mute/ban. Good for support chats, teams. When linked to a Group, permissions flow through the group. |
| `channel` | Optional | Public, anyone can join/leave (or any group member if group-linked). Full history visible. Announcements, community rooms. |

## Per-Room Rules

Each room has a `rules` JSONField with toggleable content policies. Room admins (or users with `manage_chat` group permission) can change these. The handler enforces them before publishing.

```python
# Default rules
rules = {
    "allow_urls": True,
    "allow_media": True,
    "allow_phone_numbers": True,
    "max_message_length": 4000,
    "disappearing_ttl": 0,  # seconds, 0 = off (e.g. 86400 = 24h). Default is off.
    "rate_limit": 10,  # max messages per user per second
}
```

- **`allow_urls`** — `content_guard` already detects URLs. If `False`, messages with URLs are rejected.
- **`allow_media`** — if `False`, `kind="image"` messages are rejected.
- **`allow_phone_numbers`** — `content_guard` detects phone numbers. If `False`, rejected.
- **`max_message_length`** — enforced before persistence.
- **`disappearing_ttl`** — messages older than TTL are deleted by periodic cleanup task (or lazy on fetch). `0` = keep forever. Default is off.
- **`rate_limit`** — max messages per user per second. Default 10. Prevents spam.

## Models

### ChatRoom

```python
class ChatRoom(models.Model, MojoModel):
    class RestMeta:
        GRAPHS = {
            "default": "all",
            "list": ["id", "name", "kind", "group", "created"],
        }
        VIEW_PERMS = ["chat", "manage_chat", "owner"]
        SAVE_PERMS = ["manage_chat"]
        OWNER_FIELD = "user"

    name = models.CharField(max_length=255, blank=True)
    kind = models.CharField(max_length=20, default="group")  # "direct", "group", "channel"
    user = models.ForeignKey("account.User", null=True, on_delete=models.SET_NULL, related_name="owned_chat_rooms")
    group = models.ForeignKey("account.Group", null=True, blank=True, on_delete=models.CASCADE, related_name="chat_rooms")
    rules = models.JSONField(default=dict)  # per-room content rules
    metadata = models.JSONField(default=dict)
    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)
```

### ChatMessage

```python
class ChatMessage(models.Model, MojoModel):
    class RestMeta:
        GRAPHS = {
            "default": "all",
            "list": ["id", "user", "body", "kind", "created", "edited_at"],
        }

    room = models.ForeignKey(ChatRoom, on_delete=models.CASCADE, related_name="messages")
    user = models.ForeignKey("account.User", on_delete=models.SET_NULL, null=True, related_name="chat_messages")
    body = models.TextField()
    kind = models.CharField(max_length=20, default="text")  # "text", "image", "system"
    moderation_decision = models.CharField(max_length=10, default="allow")  # "allow", "warn", "block"
    edited_at = models.DateTimeField(null=True, blank=True)
    is_flagged = models.BooleanField(default=False)  # admin-flagged, hidden from view
    flagged_by = models.ForeignKey("account.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    flagged_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["room", "created"]),
            models.Index(fields=["is_flagged"]),
        ]
```

### ChatMembership

```python
class ChatMembership(models.Model, MojoModel):
    room = models.ForeignKey(ChatRoom, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey("account.User", on_delete=models.CASCADE, related_name="chat_memberships")
    role = models.CharField(max_length=20, default="member")  # "member", "admin", "owner"
    status = models.CharField(max_length=20, default="active")  # "active", "muted", "banned"
    last_read_at = models.DateTimeField(null=True, blank=True)  # used for channels only
    joined_at = models.DateTimeField(auto_now_add=True)
    metadata = models.JSONField(default=dict)

    class Meta:
        unique_together = ("room", "user")
```

### ChatReaction

```python
class ChatReaction(models.Model, MojoModel):
    message = models.ForeignKey(ChatMessage, on_delete=models.CASCADE, related_name="reactions")
    user = models.ForeignKey("account.User", on_delete=models.CASCADE, related_name="chat_reactions")
    emoji = models.CharField(max_length=8)  # unicode emoji

    class Meta:
        unique_together = ("message", "user", "emoji")
```

### ChatReadReceipt

Per-message read receipts for `direct` and `group` rooms only. One record per user per message, created on first read, never updated.

```python
class ChatReadReceipt(models.Model, MojoModel):
    message = models.ForeignKey(ChatMessage, on_delete=models.CASCADE, related_name="read_receipts")
    user = models.ForeignKey("account.User", on_delete=models.CASCADE, related_name="chat_read_receipts")
    read_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("message", "user")
```

For **channels**, read receipts are not tracked per-message — `last_read_at` on `ChatMembership` is used for unread count badges instead.

## Admin Message Flagging

Admins and users with `moderate_chat` permission can flag messages. Flagging:
- Sets `is_flagged=True`, `flagged_by`, `flagged_at` on the message
- Hides the message from normal history queries (filtered out by default)
- Publishes a `chat_message_flagged` event to the room topic so frontends remove it in real-time
- The message is **not deleted** — it stays in the DB as evidence for review
- A flagged messages endpoint lets moderators view all flagged messages for a room

```python
# History query excludes flagged messages by default
ChatMessage.objects.filter(room=room, is_flagged=False).order_by("-created")

# Moderator view of flagged messages
ChatMessage.objects.filter(room=room, is_flagged=True).order_by("-created")
```

## Message Format

- **Markdown only** — body is stored as raw markdown. Frontend renders it. No raw HTML stored or rendered — avoids injection.
- **Emojis** — native unicode, no special handling. Just part of the markdown text.
- **System messages** — `kind="system"` for join/leave/rename events. `body` is a human-readable string, `metadata` has structured data for the frontend to render however it wants.

## Message Flow

### Sending a message (WebSocket)

1. Client sends:
   ```json
   {"message_type": "chat_message", "room_id": 5, "body": "hello"}
   ```
2. Handler validates auth:
   - If `room.group` is set: check `group.user_has_permission(user, "chat")`
   - Else: check `ChatMembership` exists with `status == "active"`
3. Check rate limit (default 10/sec per user per room)
4. Check room `rules` — enforce `max_message_length`, `allow_urls`, `allow_phone_numbers`
5. `content_guard.check_text(body, surface="chat")` for moderation
6. If `decision == "block"` — return error to sender, do not persist or publish
7. If `decision == "allow"` or `"warn"` — persist `ChatMessage` to DB
8. `realtime.publish_topic("chat:5", {message data})` for live delivery
9. Return ack to sender with message id + created timestamp

### Editing a message (WebSocket)

1. Client sends:
   ```json
   {"message_type": "chat_edit", "message_id": 42, "body": "updated text"}
   ```
2. Handler validates: user is the message author, or has `moderate_chat` permission
3. Re-run room rules + `content_guard` on new body
4. Update `body`, set `edited_at = now()`
5. Publish edit event to room topic so all clients update in place

### Flagging a message (WebSocket or REST)

1. Admin/moderator sends:
   ```json
   {"message_type": "chat_flag", "message_id": 42}
   ```
2. Handler validates: user has `moderate_chat` permission (group-level) or is room admin
3. Set `is_flagged=True`, `flagged_by=user`, `flagged_at=now()`
4. Publish `chat_message_flagged` event to room topic — frontends hide the message
5. Message stays in DB for evidence/review

### Reactions (WebSocket)

1. Client sends:
   ```json
   {"message_type": "chat_react", "message_id": 42, "emoji": "👍"}
   ```
2. Toggle: if reaction exists, remove it. If not, create it.
3. Publish reaction event to room topic

### Typing indicators (WebSocket, ephemeral)

1. Client sends: `{"message_type": "chat_typing", "room_id": 5}`
2. Handler publishes to `chat:5` topic: `{"type": "typing", "user_id": 123}`
3. No persistence — purely ephemeral via Redis pub/sub

### Read receipts (REST or WebSocket)

1. Client sends: `{"message_type": "chat_read", "room_id": 5, "up_to_message_id": 482}`
2. For `direct`/`group` rooms: bulk-create `ChatReadReceipt` for all unread messages up to that ID (skip any that already exist)
3. For `channel` rooms: update `last_read_at` on `ChatMembership`
4. Publish read event to room topic (so sender sees "read" indicator in direct/group)

### Room presence

- Use existing `realtime.get_online_users()` cross-referenced with `ChatMembership` to show who's online in a room
- Publish join/leave system messages to room topic when membership changes

## Subscription Authorization

Extend `on_realtime_can_subscribe` on the User model:

```python
if topic.startswith("chat:"):
    room_id = int(topic.split(":")[1])
    room = ChatRoom.objects.filter(id=room_id).first()
    if not room:
        return False

    # Banned users cannot subscribe
    membership = ChatMembership.objects.filter(room=room, user=self).first()
    if membership and membership.status == "banned":
        return False

    # Group-linked room: check group permission
    if room.group:
        return room.group.user_has_permission(self, "chat")

    # Non-group room: check membership exists
    return membership is not None and membership.status in ("active", "muted")
```

Muted users can still subscribe (see messages) but the handler rejects their sends.

## Content Guard Integration

Every message (and edit) runs through `content_guard.check_text()` plus room rules:

```python
from mojo.helpers.content_guard import check_text

# Room rules check
if not room.rules.get("allow_urls", True):
    # content_guard detects URLs — reject if found
if not room.rules.get("allow_phone_numbers", True):
    # content_guard detects phone numbers — reject if found

# General moderation
result = check_text(body, surface="chat")
if result.decision == "block":
    return {"error": "message_blocked", "reasons": result.reasons}
```

Deterministic, no external API calls — important for chat latency.

## Disappearing Messages

- Controlled by `disappearing_ttl` in room rules (seconds, `0` = off, **default is off**)
- Periodic cleanup task deletes messages where `created < now - ttl`
- Lazy cleanup on history fetch as a fallback (don't return expired messages)
- When a message disappears, publish a `chat_message_deleted` event to the room topic so frontends remove it
- Flagged messages are exempt from disappearing cleanup (evidence preservation)

## REST Endpoints

All endpoints use `@md.uses_model_security(ChatRoom)` or appropriate model. No `@md.requires_auth()` — RestMeta VIEW_PERMS handles auth.

### Room management
- `GET /api/chat/rooms` — list rooms the user is a member of (or group rooms the user has access to)
- `POST /api/chat/rooms` — create a room (auto-creates membership with `role="owner"`)
- `GET /api/chat/room/{room_id}` — room detail + online members
- `POST /api/chat/room/{room_id}/members` — add member (`manage_chat` or room admin for group, anyone for channel)
- `DELETE /api/chat/room/{room_id}/members/{user_id}` — remove member / leave room
- `POST /api/chat/room/{room_id}/rules` — update room rules (`manage_chat` or room admin)
- `POST /api/chat/room/{room_id}/mute/{user_id}` — mute a member (`moderate_chat` or room admin)
- `POST /api/chat/room/{room_id}/ban/{user_id}` — ban a member (`moderate_chat` or room admin)

### Messages
- `GET /api/chat/room/{room_id}/messages` — paginated history (cursor-based, newest first, excludes flagged)
- `GET /api/chat/room/{room_id}/flagged` — flagged messages for moderator review (`moderate_chat` required)

### Direct messages
- `POST /api/chat/dm/{user_id}` — get or create a DM room with the given user. Returns existing room if one exists.

### Read state
- `POST /api/chat/room/{room_id}/read` — mark as read up to a message ID
- `GET /api/chat/unread` — unread counts per room

## Files to Create

| File | Purpose |
|------|---------|
| `mojo/apps/chat/__init__.py` | App init |
| `mojo/apps/chat/models.py` | ChatRoom, ChatMessage, ChatMembership, ChatReaction, ChatReadReceipt |
| `mojo/apps/chat/rest/rooms.py` | Room CRUD, membership, mute/ban, rules endpoints |
| `mojo/apps/chat/rest/messages.py` | Message history, flagged messages, DM creation |
| `mojo/apps/chat/handler.py` | WebSocket handler: chat_message, chat_edit, chat_flag, chat_react, chat_typing, chat_read |
| `mojo/apps/chat/rules.py` | Room rules validation + enforcement helpers |
| `mojo/apps/chat/cleanup.py` | Disappearing messages cleanup task |
| `tests/test_chat/__init__.py` | Test package init |
| `tests/test_chat/rooms.py` | Room creation, membership, join/leave, authorization |
| `tests/test_chat/messages.py` | Send, edit, flag, history, pagination, moderation |
| `tests/test_chat/reactions.py` | Add/remove reactions |
| `tests/test_chat/read_receipts.py` | Read receipts for direct/group, unread counts for channels |
| `tests/test_chat/rules.py` | Per-room rules enforcement (URLs, phone numbers, media, length, rate limit) |
| `tests/test_chat/permissions.py` | Group permission integration, moderate_chat, manage_chat |

## Files to Modify

| File | Change |
|------|--------|
| `mojo/apps/account/models/user.py` | Extend `on_realtime_can_subscribe` for `chat:` topics |
| App settings | Register chat handler in `REALTIME_MESSAGE_HANDLERS` |

## Tests

### Rooms
- `test_create_group_room` — create room, verify owner membership auto-created
- `test_create_group_room_linked_to_group` — create room with `group` FK, verify group members get access
- `test_create_channel_room` — create public channel, verify anyone can join
- `test_create_dm_room` — POST to `/dm/{user_id}`, verify room created with both members, `kind="direct"`
- `test_dm_room_reuse` — second POST to same user returns existing room, not a duplicate
- `test_join_channel` — join public channel, verify can subscribe to topic
- `test_leave_room` — leave room, verify membership removed, system message published
- `test_add_member_requires_admin` — non-admin cannot add members to group room
- `test_muted_user_can_read` — muted user can subscribe but handler rejects sends
- `test_banned_user_cannot_subscribe` — banned user rejected by `on_realtime_can_subscribe`

### Messages
- `test_send_message` — send via WebSocket, verify persisted + published to topic
- `test_message_history` — create messages, fetch paginated, verify order and cursor pagination
- `test_message_history_excludes_flagged` — flagged messages hidden from normal history
- `test_edit_message` — edit own message, verify `edited_at` set, edit event published
- `test_edit_message_not_author` — cannot edit someone else's message (unless moderator)
- `test_edit_reruns_moderation` — edited text goes through content_guard again
- `test_rate_limit` — exceed rate limit, verify messages rejected

### Flagging
- `test_flag_message` — moderator flags message, verify `is_flagged=True`, event published
- `test_flag_requires_moderate_permission` — non-moderator cannot flag
- `test_flagged_messages_endpoint` — moderator can view flagged messages
- `test_flagged_messages_preserved` — flagged messages not deleted by disappearing cleanup

### Moderation
- `test_content_guard_blocks` — blocked content rejected, not persisted
- `test_content_guard_warns` — borderline content persisted with `moderation_decision="warn"`
- `test_non_member_cannot_send` — handler rejects messages from non-members

### Rules
- `test_rule_block_urls` — room with `allow_urls=False` rejects messages containing URLs
- `test_rule_block_phone_numbers` — room with `allow_phone_numbers=False` rejects phone numbers
- `test_rule_max_message_length` — message exceeding limit rejected
- `test_rule_block_media` — room with `allow_media=False` rejects image messages
- `test_rules_update_requires_admin` — non-admin cannot change room rules

### Reactions
- `test_add_reaction` — add emoji reaction, verify persisted + published
- `test_remove_reaction` — toggle same reaction off
- `test_multiple_reactions` — multiple users react with different emojis

### Read Receipts
- `test_read_receipt_direct` — mark read in DM, verify `ChatReadReceipt` created, read event published
- `test_read_receipt_group` — mark read in group, verify receipts created
- `test_read_receipt_idempotent` — marking same messages read twice doesn't create duplicates
- `test_no_read_receipts_for_channels` — channels use `last_read_at` on membership, no `ChatReadReceipt`
- `test_unread_counts` — send messages, verify count, mark read, verify count drops
- `test_read_receipt_first_read_only` — receipt `read_at` never updated on subsequent reads

### Permissions
- `test_group_member_can_access_group_room` — user with `chat` permission can access group-linked room
- `test_group_member_without_chat_perm_rejected` — group member without `chat` permission cannot subscribe
- `test_parent_group_member_inherits_access` — parent group membership grants child group room access
- `test_manage_chat_can_create_rooms` — user with `manage_chat` can create/configure rooms
- `test_moderate_chat_can_flag_and_mute` — user with `moderate_chat` can flag messages and mute users
- `test_system_admin_bypasses_group_perms` — user with `manage_groups` system perm has full access

### Presence
- `test_online_members_in_room` — verify room detail shows which members are online
- `test_typing_indicator` — send typing event, verify published to topic

### Disappearing Messages
- `test_disappearing_cleanup` — set TTL, verify old messages deleted by cleanup
- `test_disappearing_not_returned` — expired messages excluded from history fetch
- `test_disappearing_default_off` — rooms default to no disappearing messages

## Decisions Made

- **Markdown only** — no HTML. Frontend renders. Avoids injection.
- **Emojis** — native unicode, no special handling
- **Message editing** — author or moderator can edit. Sets `edited_at`. Re-runs moderation. No edit history v1.
- **Message flagging** — admins/moderators flag messages (hidden from view, preserved in DB as evidence). Not deleted.
- **Reactions** — toggle-based. One reaction per emoji per user per message. In v1.
- **Read receipts** — `direct` and `group` only (per-message `ChatReadReceipt`). Channels use `last_read_at` on membership. First-read only, never updated.
- **Muted users** — can see messages but cannot send. Banned users cannot subscribe at all.
- **System messages** — `kind="system"` for join/leave/rename. Structured metadata for frontend flexibility.
- **DM rooms** — auto-created on first message. Deduplicated (one room per user pair).
- **Rate limiting** — 10 messages/sec/user default, configurable per room.
- **Disappearing messages** — off by default, configurable per room via `disappearing_ttl`.
- **Permission model** — `chat`, `manage_chat`, `moderate_chat` permissions at group level via `member.permissions`. Group-linked rooms use group permission checks. Non-group rooms use `ChatMembership` only.

## Out of Scope (v1)

- Threads / replies
- Voice / video
- End-to-end encryption
- Push notifications for offline users (separate feature)
- Chat bot integrations
- Edit history (just overwrite + `edited_at` timestamp)
- Room size limits (revisit if performance is an issue)

## Open Questions

1. **Room size limits?** — Max members per group room? Could matter for `publish_topic` fan-out and read receipt volume. Suggest 500 for group, unlimited for channel. Revisit based on load testing.
