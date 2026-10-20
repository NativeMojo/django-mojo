# Chat Permissions

## Permission Model

Chat integrates with the existing User/Group/Member permission system.

### Chat-specific permissions (group-scoped)

| Permission | What it grants |
|-----------|---------------|
| `chat` | Read a group-linked room's history and members, join its channels |
| `manage_chat` | Delete rooms, manage membership, update rules |
| `moderate_chat` | Flag messages, mute/ban users |

These are stored in `member.permissions` at the group level.

No permission sends a message on its own: sending and editing need an active
`ChatMembership` row in the room, and reacting needs an active or muted one
(see the table at the end).

### System-level

Users with `manage_chat` in `user.permissions` have global access. Under the
opt-in `require_shared_group` reachability rule below, that grant also reaches
any active user, but never through a session that assumes a member (an
override ApiKey or a group token).

## Who a caller may put in a chat (reachability)

`POST /api/chat/dm` (a new room) and `POST /api/chat/room/member/add` ask one
rule, in `mojo/apps/chat/services/reachability.py`: may this caller put this
person in a chat with them? Server code that creates rooms or memberships
through the ORM is never checked.

- **Default:** any active user, as in 1.35.3. A room with a `group` takes
  members from outside that group, and an admin outside it may add them.
- **Opt-in shared-group rule:** set `CHAT_REACHABILITY_HANDLER =
  "mojo.apps.chat.services.reachability.require_shared_group"`. Caller and
  target must then share an active group — both hold an active direct
  `GroupMember` row in the same effectively active group; inherited
  (parent-chain) membership does not count. A global `manage_chat` holder
  reaches any active user.
- **Hook:** `CHAT_REACHABILITY_HANDLER` in the Django settings file (never a
  database Setting) names `handler(user, target, room)`; `room` is None for a
  DM. Only a return of `True` reaches. A load error, a non-callable or an
  exception refuses. The module ships `require_shared_group`, `allow_all`
  (the same as no hook) and `deny_all` (no client DMs or adds). A project
  that wants a group room to take only its group's members checks it in its
  hook, e.g. `if room is not None and room.group and
  room.group.get_member_for_user(target, check_parents=True) is None:
  return False`.
- **One refusal:** every target-side refusal — no id, a bad id, the caller's
  own, a missing or inactive user, a hook refusal or error — is the same 404
  `"User not found"`, with nothing written. With no hook a DM that opens
  tells the caller the id is an active account, so active ids can be found
  one DM at a time; a project that minds sets `require_shared_group` or
  `deny_all`. A custom hook runs after the target is loaded, so its timing is
  the consumer's to keep even.

A pair that already shares a direct room gets it back from `dm` without the
rule: both are members already. A project's `REALTIME_SUBSCRIBE_HANDLER`
(below) can still refuse that room back to the caller.

## A project's own rule for who takes part (`REALTIME_SUBSCRIBE_HANDLER`)

A project that restricts some of its own users — a player limited to support
chat, say — names one function in its Django settings file:

```python
REALTIME_SUBSCRIBE_HANDLER = "myapp.realtime.can_subscribe"

def can_subscribe(user, topic, default):
    # default is django-mojo's own verdict, and is always truthy here.
    if not topic.startswith("chat:"):
        return True
    if not is_restricted(user.pk):          # read fresh from the database
        return True
    return is_support_room(int(topic.split(":", 1)[1]))
```

It answers one question: may this user take part in `topic`? The module is
`mojo/apps/realtime/policy.py`.

- **It can only narrow.** django-mojo's own checks run first — membership,
  bans, the group `chat` permission, the confined-credential and protected-group
  checks. The hook is asked only when they allow, and nothing it returns can
  grant access they refused.
- **Unset changes nothing.** With the setting absent or `None` every answer is
  exactly as before. Any other value that is not a callable or a dotted path —
  `False`, `0`, `""`, `[]`, `{}` — is a broken setting, not "unset".
- **It fails closed.** Only a return of `True` allows, and the hook is asked
  only when django-mojo's own answer is `True`. A return of `False` refuses
  without logging. An import error, a non-callable, an exception or any other
  return (`None`, `1`, `"True"`) refuses and is logged at error level to
  `realtime.log`, naming the topic and the return's type but never its value;
  the client sees only the refusal. A broken hook therefore takes chat down for everyone — the
  user's own `user:<id>` channel and server writes keep working.
- **Settings file only.** It is read with `settings.get_static`, never from a
  database Setting row, so nobody holding `manage_settings` can point it at
  importable code.
- **Shipped:** `mojo.apps.realtime.policy.allow_all` (the same as unset) and
  `.deny_all` (refuse every topic it is asked about).

Where it is asked, for the room's `chat:<id>` topic:

| Door | Refusal |
|---|---|
| Client `subscribe` (any topic) and the chat re-check on an open socket | `Access denied to topic: ...`; the open socket is unsubscribed |
| `chat_message` (send), after the `client_key` dedupe | `Not allowed in this room` |
| `chat_edit` | `Cannot edit this message` |
| `chat_react` | `Message not found`, the same answer as every other refusal |
| `chat_typing` | Nothing is broadcast |
| `chat_read` | `Not allowed in this room` |
| `chat_flag` | `Permission denied` |
| `GET room/messages`, `GET room/online`, `GET room/members`, `POST room/read` | 403 |
| `POST dm` returning a DM the pair already shares | The uniform 404 `"User not found"`; nothing is created |

Left open, and why:

- `POST room` (create) stays open. A refused user's own room is no use to them:
  they cannot subscribe to it, send into it or read it, and others reach it
  only through `room/join` or `room/member/add`, which have their own rules.
- `room/leave` always works.
- `GET rooms` and `GET unread` list room names and counts, not content.
- The own-topic subscribe a socket makes at connect, and topics a server hook
  subscribes a socket to, are server-chosen and never ask the hook.
- Server writes are never gated: `send_message(client_authored=False)` and
  rooms or memberships created through the ORM. Server-created support rooms
  keep working, as long as the hook allows their topics.

The `user` argument is the caller as django-mojo knows it: on a socket the
`User` loaded at connect (a fresh row on the chat re-check), on REST
`request.user`, which for an API key that is not linked to a user is the key
itself. Read restriction state from the database or your own store, never from
the passed object's attributes, and return `True` for callers your rule does
not cover.

For open sockets, see [Open sockets](#open-sockets).

## Who may run a room-admin operation (room authority)

`POST room/rules`, `room/join`, `room/leave`, `room/member/remove`,
`room/member/mute`, `room/member/ban`, and PUT/POST/PATCH/DELETE on
`room/<pk>` ask one rule, in `mojo/apps/chat/services/room_authority.py`. It
runs only in those REST views: server code that writes rooms, rules or
memberships through the ORM is never checked, and its values are never
validated.

**Fixed (no hook changes these):** tenant confinement, the per-kind answers
(join only a channel, leave no direct room), value validation of every rules
write (see [Rules](rules.md#who-may-change-a-rule)), and **a banned caller is
refused every operation except `leave`**, including PUT by a banned owner.

**Default rule.** A "global grant" is the user's own `manage_chat`, never
through a session that assumes a member (an override ApiKey or a group token).

| Operation | Default |
|---|---|
| `room/rules` | Room admin (not banned) or `manage_chat` in the group or globally. Turning disappearing messages on or shortening them needs the **global** `manage_chat`. |
| PUT `room/<pk>` | An active or muted owner/admin membership, or `manage_chat` / `comms` in the room's group or globally. Rules changes follow the `rules` row. `name`, `metadata`, `kind` and `group` stay as before. RestMeta `SAVE_PERMS` still runs afterwards. |
| DELETE `room/<pk>` | Unchanged: RestMeta `DELETE_PERMS` (`manage_chat` globally or in the room's group). |
| `room/join` | Unchanged: a channel with no group is open to any signed-in user. A group channel still needs `chat` or `manage_chat` in its group, and no hook loosens that. |
| `room/leave` | Unchanged. |
| `room/member/remove` | Room admin or `manage_chat`, and role order. |
| `room/member/mute`, `ban` | Room admin, `moderate_chat` or `manage_chat`, and role order. |

**Role order:** owner outranks admin, which outranks member. A caller acting
only through a room role may act on themself or on a target of strictly
lower role, so an admin cannot remove, mute or ban the owner. A grant holder
(`manage_chat` or `moderate_chat`, in the room's group or globally) acts on
anyone.

**Hook:** `CHAT_ROOM_AUTHORITY_HANDLER` in the Django settings file (never a
database Setting) names `handler(user, room, action, context) -> bool`.
`action` is one of `rules`, `update`, `delete`, `join`, `leave`,
`member_remove`, `member_mute`, `member_ban`. `context` holds `target` (the
target's `ChatMembership`, or None, for `member_*`), `changes`
(`{key: (old, new)}`), `assumed_member` and `default` (the built-in verdict),
so a project narrows with `ctx["default"] and mine` or widens with
`ctx["default"] or mine`. Only `True` allows; a load error, a non-callable,
an exception or any other return refuses and logs to `chat.log`. On the seven
POST endpoints the hook's verdict is final. On `room/<pk>` RestMeta perms run
afterwards, so there it can only narrow. The module ships
`default_room_authority` and `deny_all`. See
[Services](services.md#room-authority--who-may-run-a-room-admin-operation).

There is no batch path: `ChatRoom` does not set `CAN_BATCH`. A consumer that
sets it gets batch writes on `room` (no pk), which this rule never sees; only
RestMeta perms gate those rows.

## How Permission Checks Work

### Group-linked rooms (`room.group` is set)

- **Subscription auth**: `group.user_has_permission(user, ["chat", "manage_chat"])` — checks both user-level and member-level permissions
- **Parent group inheritance**: members of a parent group automatically get access to child group rooms
- **ChatMembership** is still created for tracking role/status/read state within the room

### Non-group rooms (`room.group` is None)

- **Subscription auth**: checks `ChatMembership` exists with `status in ("active", "muted")`
- No group permission checks

### Room-level roles

- **owner** — full control (set via RestMeta OWNER_FIELD; always the creator),
  except deleting the room (`DELETE_PERMS`)
- **admin** — can manage members below the owner, update rules, edit/flag messages
- **member** — can send messages, react, read

A **banned** owner or admin has no room authority. A **muted** one keeps it
(member management, rules) but, like any muted member, cannot send or edit.

### REST endpoint security

- Room CRUD: `@md.uses_model_security(ChatRoom)` with RestMeta perms
- Custom endpoints: `@md.requires_auth()` + the room-authority rule above, whose
  default calls `_check_room_admin` / `_check_room_moderator`
- **CREATE_PERMS = ["authenticated"]** — any logged-in user can create a
  room, with or without a group; no `chat` or `manage_chat` is needed in the
  group (`ChatRoom.on_rest_pre_save` only sets the default rules).
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

A project's `REALTIME_SUBSCRIBE_HANDLER` reaches open sockets the same way: at
the next re-check, or at once when the project calls
`realtime.disconnect_user` after the restriction commits, since every later
subscribe asks the hook again. The disconnect is best effort. Topics other than
chat are checked only at subscribe, so they last until the socket drops.
Rotating the account's `auth_key` does not close open sockets.
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
