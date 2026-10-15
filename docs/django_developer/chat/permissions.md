# Chat Permissions

## Permission Model

Chat integrates with the existing User/Group/Member permission system.

### Chat-specific permissions (group-scoped)

| Permission | What it grants |
|-----------|---------------|
| `chat` | Read a group-linked room's history and members, join its channels, create a room in the group |
| `manage_chat` | Delete rooms, manage membership, update rules |
| `moderate_chat` | Flag messages, mute/ban users |

These are stored in `member.permissions` at the group level.

No permission sends a message on its own: sending and editing need an active
`ChatMembership` row in the room, and reacting needs an active or muted one
(see the table at the end).

### System-level

Users with `manage_chat` in `user.permissions` have global access. That
grant also reaches any active user through the reachability rule below, but
never through a session that assumes a member (an override ApiKey or a group
token).

## Who a caller may put in a chat (reachability)

`POST /api/chat/dm` (a new room) and `POST /api/chat/room/member/add` ask one
rule, in `mojo/apps/chat/services/reachability.py`: may this caller put this
person in a chat with them? Server code that creates rooms or memberships
through the ORM is never checked.

- **Default:** caller and target share an active group — both hold an active
  direct `GroupMember` row in the same effectively active group. Inherited
  (parent-chain) membership does not count. A global `manage_chat` holder
  reaches any active user.
- **Group rooms (fixed):** a room with a `group` only takes members of that
  group, and its admin must be one too unless they hold the global
  `manage_chat`. No hook can loosen this.
- **Hook:** `CHAT_REACHABILITY_HANDLER` in the Django settings file (never a
  database Setting) names `handler(user, target, room)`; `room` is None for a
  DM. Only a return of `True` reaches. A load error, a non-callable or an
  exception refuses. The module ships `default_can_reach` (the default rule,
  to compose with), `allow_all` (the pre-#8345 reach; group rooms still
  apply) and `deny_all` (no client DMs or adds).
- **One refusal:** every target-side refusal — no id, a bad id, the caller's
  own, a missing, inactive or unreachable user, a hook error — is the same
  404 `"User not found"`, with nothing written, so the answer reveals no user
  ids. A custom hook runs after the target is loaded, so its timing is the
  consumer's to keep even.

A pair that already shares a direct room gets it back from `dm` without the
rule: both are members already.

## How Permission Checks Work

### Group-linked rooms (`room.group` is set)

- **Subscription auth**: `group.user_has_permission(user, ["chat", "manage_chat"])` — checks both user-level and member-level permissions
- **Parent group inheritance**: members of a parent group automatically get access to child group rooms
- **ChatMembership** is still created for tracking role/status/read state within the room

### Non-group rooms (`room.group` is None)

- **Subscription auth**: checks `ChatMembership` exists with `status in ("active", "muted")`
- No group permission checks

### Room-level roles

- **owner** — full control (set via RestMeta OWNER_FIELD; always the creator)
- **admin** — can manage members, update rules, edit/flag messages
- **member** — can send messages, react, read

A **banned** owner or admin has no room authority. A **muted** one keeps it
(member management, rules) but, like any muted member, cannot send or edit.

### REST endpoint security

- Room CRUD: `@md.uses_model_security(ChatRoom)` with RestMeta perms
- Custom endpoints: `@md.requires_auth()` + manual permission checks via `_check_room_admin` / `_check_room_moderator`
- **CREATE_PERMS = ["authenticated"]** — any logged-in user can create a
  groupless room. A room in a group needs the creator to hold `chat` or
  `manage_chat` there (`ChatRoom.on_rest_pre_save`), else 403 and no row.
  A body `user` is ignored (`NO_SAVE_FIELDS`): the owner is the creator.

### Muted vs Banned

- **Muted**: can subscribe (see messages) but handler rejects sends
- **Banned**: cannot subscribe at all (`on_realtime_can_subscribe` returns False)

### Open sockets

A socket checks chat access when it subscribes to `chat:<room_id>` and
remembers the answer, so delivering a frame costs no SQL. Loss of access still
reaches an open socket:

- **Within one frame** for the writes that publish an access-change frame on
  the room topic after they commit: `room/leave` (`chat_member_left`),
  `room/member/remove` (`chat_member_removed`), `room/member/ban`
  (`chat_member_banned`) and REST room delete (`chat_room_deleted`). The
  affected socket re-checks that frame, drops it, and unsubscribes.
- **At the next re-check** (`WS_SUBSCRIPTION_RECHECK_SECONDS`, default 300)
  for everything else: a `chat` / `manage_chat` permission removed from the
  user or the group member, a membership row changed outside these endpoints,
  a group deleted with its rooms.

Code that removes access some other way and needs it to take effect at once
should call `publish_access_change` (see [Services](services.md#access-change-frames)).
See [realtime architecture](../realtime/architecture.md#chat-topic-delivery-chatroom_id)
for the mechanism.

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
| Edit (`chat_edit`) — own message, or any as a room admin | yes | no | no |
| Subscribe to the room topic | yes | yes | no |

A banned member attempting to mark read or react gets an error and writes
nothing. React's refusal is the **generic** `"Message not found"`, not a
membership error — see [the handler doc](handler.md#chat_react--add-remove-or-toggle-an-emoji-reaction)
for why distinguishing them would be a cross-tenant existence oracle.
