# Chat Permissions

## Permission Model

Chat integrates with the existing User/Group/Member permission system.

### Chat-specific permissions (group-scoped)

| Permission | What it grants |
|-----------|---------------|
| `chat` | Participate in group chat rooms (send messages, react) |
| `manage_chat` | Create/delete rooms, manage membership, update rules |
| `moderate_chat` | Flag messages, mute/ban users |

These are stored in `member.permissions` at the group level.

### System-level

Users with `manage_chat` in `user.permissions` have global access.

## How Permission Checks Work

### Group-linked rooms (`room.group` is set)

- **Subscription auth**: `group.user_has_permission(user, ["chat", "manage_chat"])` — checks both user-level and member-level permissions
- **Parent group inheritance**: members of a parent group automatically get access to child group rooms
- **ChatMembership** is still created for tracking role/status/read state within the room

### Non-group rooms (`room.group` is None)

- **Subscription auth**: checks `ChatMembership` exists with `status in ("active", "muted")`
- No group permission checks

### Room-level roles

- **owner** — full control (set via RestMeta OWNER_FIELD)
- **admin** — can manage members, update rules, edit/flag messages
- **member** — can send messages, react, read

### REST endpoint security

- Room CRUD: `@md.uses_model_security(ChatRoom)` with RestMeta perms
- Custom endpoints: `@md.requires_auth()` + manual permission checks via `_check_room_admin` / `_check_room_moderator`
- **CREATE_PERMS = ["authenticated"]** — any logged-in user can create a room

### Muted vs Banned

- **Muted**: can subscribe (see messages) but handler rejects sends
- **Banned**: cannot subscribe at all (`on_realtime_can_subscribe` returns False)

`status in ("active", "muted")` is the **read-side predicate**, used by
`GET /api/chat/room/messages`, `GET /api/chat/unread`, `chat_read`,
`POST /api/chat/room/read` and `chat_react`. Active-only (as `chat_typing`
uses) is the wrong bound for any of them: a muted member reads, and one who
could not mark read would show as permanently unread to everyone else in the
room.

| Capability | active | muted | banned |
|---|---|---|---|
| Read history / unread counts | yes | yes | no |
| Mark read (`chat_read`, `POST room/read`) — receipts and `last_read_at` | yes | yes | **no** |
| React (`chat_react`) | yes | yes | **no** |
| Send (`chat_message`) | yes | no | no |
| Subscribe to the room topic | yes | yes | no |

A banned member attempting to mark read or react gets an error and writes
nothing. React's refusal is the **generic** `"Message not found"`, not a
membership error — see [the handler doc](handler.md#chat_react--add-remove-or-toggle-an-emoji-reaction)
for why distinguishing them would be a cross-tenant existence oracle.
