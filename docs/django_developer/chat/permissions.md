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
