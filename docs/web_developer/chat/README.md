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
| PUT | `/api/chat/room/<pk>` | Update a room (owner/admin) `{name?, metadata?, rules?}` |
| DELETE | `/api/chat/room/<pk>` | Delete a room (`manage_chat`) |
| POST | `/api/chat/room/join` | Join a channel `{room_id}` |
| POST | `/api/chat/room/leave` | Leave a room `{room_id}` |
| POST | `/api/chat/room/member/add` | Add member (admin) `{room_id, user_id}` |
| POST | `/api/chat/room/member/remove` | Remove member (admin) `{room_id, user_id}` |
| POST | `/api/chat/room/member/mute` | Mute member (mod) `{room_id, user_id}` |
| POST | `/api/chat/room/member/ban` | Ban member (mod) `{room_id, user_id}` |
| POST | `/api/chat/room/rules` | Update rules (admin) `{room_id, ...rules}` |
| GET | `/api/chat/room/members?room_id=X` | List members |
| GET | `/api/chat/room/online?room_id=X` | Online members (members, or group chat/manage_chat) |

**The room's owner is always you.** A `user` field on create is ignored.

**Who you can add.** You can add any active account, including one outside
the room's group. A project can limit who counts as reachable on the server.
A banned admin can no longer add or manage members.

**One answer for every "can't add this person".** After the room checks (404
room not found, 403 not your room or not an admin, 400 a direct room), every
refusal about the person is 404 `{"error": "User not found"}`: no `user_id`, a
bad one, your own, an account that does not exist, is inactive, or that you
cannot reach. Do not tell these apart in your UI; the server deliberately
doesn't.

**Room rules are checked.** `room/rules` and a `rules` value in PUT
`room/<pk>` refuse a bad value with 400 naming the key, and nothing changes:

| Rule | Accepted |
|---|---|
| `disappearing_ttl` | `0` (off), or 3600 to 31536000 seconds (1 hour to 365 days) |
| `max_message_length` | 1 to 4000 |
| `rate_limit` | 1 to 100 per second, or `0` (off) |
| `allow_urls`, `allow_media`, `allow_phone_numbers` | `true` or `false` |

**Turning on disappearing messages is a platform decision.** By default only
a platform chat admin (the global `manage_chat`) may turn `disappearing_ttl`
on or shorten it; a room admin gets 403. A room
admin may still lengthen the TTL or turn it off. Hide or explain the control
rather than letting the 403 surprise the user.

**Who may manage whom.** An admin cannot remove, mute or ban the room owner
(403). The owner may act on admins; anyone may remove themself. A banned
member can only leave.

A banned owner, or one who left the room, cannot update it through PUT
`room/<pk>`.

A project can change any of these defaults on the server, except the value
checks and the banned-caller refusal.

### Messages

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/chat/room/messages?room_id=X` | Message history (paginated) |
| GET | `/api/chat/room/flagged?room_id=X` | Flagged messages (moderator) |

Pagination: pass `limit` (max 200) and `before` (message ID cursor).

Each history row is `{id, room_seq, generation, user_id, body, kind, edited_at, moderation_decision,
moderation_reasons, moderation_score, created, metadata, client_key}`. `client_key` carries the key on your own messages and is `null`
on everyone else's — but **do not treat that as privacy**: your key is broadcast to every
room subscriber on the live `chat_message` frame by design. A `client_key` must be an
opaque per-send identifier (a fresh UUID or ULID) and must never encode anything you would
not show the room. `metadata` is an object (`{}` when empty) — see
[Message metadata](#message-metadata).

`room_seq` is a server-assigned room-local arrival sequence in cursor mode,
or null on old/legacy messages. `generation` binds it to the room's numbering
version; compare sequences only within one generation. Continue using message
IDs for pagination and read requests; never submit your own sequence.

`/api/chat/room/flagged` now includes `metadata` on each row too, so a moderator
sees the payload a card or file message carried.

**History starts at your join date.** In every room kind except `channel`
(`direct`, `group`, and anything else), history returns only messages sent at or
after you joined the room. If you were removed and later re-added, the room opens
at the moment you rejoined — the messages you missed are not returned, and there
is no cursor that reaches them. Channels are public and always return their full
history. Expect an empty first page for a freshly-joined room; that is the bound,
not an error.

### Direct Messages

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/chat/dm` | Get or create DM `{user_id}` — returns the room, including its `group` |

**Check `group` on the room you get back.** `/api/chat/dm` prefers the pair's
*personal* (groupless) direct room. If the only direct room you two share is a
workspace-managed one, that room is returned as-is with `group` set to the
group id — the endpoint will not quietly open a second, empty room and strand
the conversation you already have. `group: null` means a personal DM. Decide
what to do about a non-null `group` in your UI; there is no flag to force a
new personal room.

**You can start a DM with any active account**, unless the app's server
limits it. A DM you already have always comes back. Every
refusal — no `user_id`, a bad one, yourself, an account that does not exist,
is inactive or that you cannot reach — is the same 404
`{"error": "User not found"}`. A DM to yourself used to be 400.

### Read State

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/chat/room/read` | Mark read `{room_id, up_to_message_id}` |
| GET | `/api/chat/unread` | Unread counts per room |

The host chooses the read-state backend. The default is legacy receipts;
deployments may opt in to DB-only cursor state. Both retain this request shape
and require an active or muted membership. Banned members cannot mark read.

**Read the result from `data`, not the top level.** The REST response is
wrapped with `status` at both levels. It now includes `position` and `changed`:

```json
{
  "status": true,
  "code": 200,
  "data": {"status": true, "up_to_message_id": 480,
           "position": null, "changed": true}
}
```

`up_to_message_id` is the server-resolved actual target. Malformed values
return 200 with a null target and no mutation. A message that has expired from
display can still be acknowledged when you were entitled to it: TTL does not
invalidate the read target. `position` is null in legacy mode and an object in
cursor mode.

#### Legacy backend (default)

The supplied ID clamps downward to the newest entitled message at or below
it. A foreign, pre-join, flagged or not-yet-existing ID may therefore resolve
to a different row. Trust the echoed ID. `changed` means a target resolved.
`/api/chat/unread` counts visible unread messages using the same join-date,
flag and disappearing-message bounds as history, excluding your own sends.

#### Cursor backend

Reads require the exact entitled in-room message ID. A foreign, missing,
flagged or pre-join ID returns a null target, the current position and
`changed: false`; it does not clamp or emit a read event. A valid duplicate
can also have `changed: false`, but may still emit the committed read event.
Reads move forward within the current generation and clear sparse exceptions
through the actual target. No per-message receipt rows are written.

Positions in read acknowledgments and `/api/chat/unread` rows contain:

| Field | Meaning |
|---|---|
| `room_id`, `user_id`, `room_name`, `room_kind` | Identity and room labels |
| `generation` | Version of this room's sequence numbering |
| `head_seq`, `read_seq` | Current arrival head and encoded member read prefix |
| `last_read_message_id`, `read_at` | Stored actual acknowledged message ID/time, nullable |
| `unread_exception_count` | Sparse unread slots below/at the encoded prefix |
| `unread_count` | `head_seq - read_seq + unread_exception_count` |
| `read_frontier_message_id` | Stored actual read ID when exceptions are zero, otherwise null |

Successful read results also carry `ack_target_id`, `ack_target_seq`,
`ack_target_created`, `ack_generation` and `reason` (`explicit`, `own_send`,
`current_head` or `join_boundary`). A server ownership/privacy boundary change
can reconcile unread state with `reason: "join_boundary"` and null target
ID/sequence/time. It preserves later eligible unread arrivals without making
older private history newly unread. These are the actual operation facts. The standing
position can already reflect a later read: do not rewind your UI to a stale
ack target, or treat a newer standing head as part of that old acknowledgment.
A genuine message from before cursor activation can have a null
`ack_target_seq` while retaining its real target ID/time. Acknowledging it does
not advance the numbered prefix; a trusted hidden-tail clear can retain that
real visible legacy target as the stored actual acknowledgment.

Counts describe arrivals, so flags, expiry and ordinary deletion do not reduce
them. They can exceed the visible page length or retain an invisible/deleted
tail. The host can explicitly clear the current head on the server; clients
cannot submit a raw sequence. Sending a message acknowledges all older
arrivals through your own send. New memberships start with zero unread at the
current head, including channels with visible older history. Ban/unban retains
the seat's state; removal/rejoin starts a new baseline.

Merge/revert maintenance can encode the prefix at the head while retaining
earlier unread messages as sparse exceptions. When exceptions are nonzero,
do not mark every message below `read_seq` or `last_read_message_id` as read.
The host must use exception-aware message eligibility; count snapshots alone
do not list which messages are exceptions. Only compare sequence values in
the same generation.

The framework publishes committed `chat_read` events for explicit reads,
own sends and trusted head clears, for all room kinds. Its event
`up_to_message_id` is the current `read_frontier_message_id`, so it can be
null while exceptions remain. The event includes fresh standing state plus
the actual `ack_*` facts; it can be newer than the direct ack. Callback errors
do not undo a durable read. Reconcile after reconnect using server state.

On `chat_read_state_invalidated {room_id, generation}`, discard cached
positions and refresh: maintenance has resequenced the room. It preserves
current unread message identities, including reads made between merge and
revert. Old-generation delayed callbacks are discarded.

Backend integration, supported writers and drained activation/rollback are
documented in [Cursor Read State](../../django_developer/chat/read_state.md).

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
 "room_seq": 100, "generation": 1,
 "kind": "text", "metadata": {}, "created": "...", "client_key": "01J8Z0K3Q0X",
 "moderation_decision": "allow", "moderation_reasons": [], "moderation_score": 0}
```

`kind` and `metadata` are optional — see [Message kinds](#message-kinds) and
[Message metadata](#message-metadata). `client_key` is optional too — see
[Idempotent sends](#idempotent-sends).

### Edit message
```json
{"type": "chat_edit", "message_id": 42, "body": "updated text"}
```
Response:
```json
{"type": "chat_edit_ack", "message_id": 42, "edited_at": "...",
 "moderation_decision": "allow", "moderation_reasons": [], "moderation_score": 0}
```

You can edit your own messages, or any message as a room admin, only while you
can send in the room. Once removed, banned or muted you get
`{"type": "error", "error": "Cannot edit this message"}`.

Replace all three local moderation fields on an accepted edit; a clean score
clears prior hidden state. Permission and explicit room-rule refusals preserve
the existing message and publish no edit event.

### React to message
```json
{"type": "chat_react", "message_id": 42, "emoji": "\ud83d\udc4d", "action": "add"}
```

`action` is optional and controls whether the call is a toggle or idempotent:

| `action` | What happens |
|---|---|
| omitted | Toggle — adds if you have not reacted, removes if you have. The original behaviour. |
| `"add"` | Ensures the reaction exists. Sending it twice leaves one reaction. |
| `"remove"` | Ensures it does not. Sending it twice leaves none. |
| anything else | `{"type": "error", "error": "Invalid action"}` |

**Prefer `add`/`remove` over the toggle.** A retried toggle inverts the
reaction; a retried `add` does not. Both explicit forms always ack the state
they guarantee, whether or not anything changed.

> **Mind the verbs.** You *send* `add` / `remove`. The ack and the
> `chat_reaction` event report `added` / `removed`. The two vocabularies do not
> match, and that is the shipped contract — do not send `added`.

```json
{"type": "chat_react_ack", "message_id": 42, "emoji": "\ud83d\udc4d", "action": "added"}
```

**A no-op does not broadcast.** You still get your ack, but other subscribers
see no `chat_reaction` event when nothing actually changed. Drive your own UI
from the ack; drive everyone else's from the event.

A message you cannot react to — nonexistent, flagged, sent before you joined,
in a room you are not in, or in a room you are banned from — returns the same
`{"type": "error", "error": "Message not found"}` in every case. The error does
not tell you which. Muted members can still react; only banned members cannot.

### Typing indicator
```json
{"type": "chat_typing", "room_id": 5}
```

### Mark as read
```json
{"type": "chat_read", "room_id": 5, "up_to_message_id": 482}
```

Acks `{"type": "chat_read_ack", "room_id": 5, "up_to_message_id": 480,
"position": null, "changed": true}`. The target is server-resolved; `position`
is null in legacy mode and a cursor position in cursor mode (see
[Read State](#read-state)). An invalid target echoes null, writes nothing and
emits no read event.

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
| `chat_message` | New message `{message_id, room_id, room_seq, generation, user_id, body, kind, metadata, created, client_key?, moderation_decision, moderation_reasons, moderation_score}` — `client_key` is present only when the sender supplied one |
| `chat_message_edited` | Message edited `{message_id, room_id, user_id, body, edited_at, moderation_decision, moderation_reasons, moderation_score}` |
| `chat_message_flagged` | Message flagged (hide it) `{message_id}` |
| `chat_reaction` | Reaction changed `{message_id, user_id, emoji, action}` — `action` is `added`/`removed`; not sent for a no-op |
| `chat_typing` | User is typing `{room_id, user_id}` |
| `chat_read` | Legacy: resolved `{room_id, user_id, up_to_message_id}` for direct/group rooms. Cursor: committed position plus actual `ack_*` facts for all room kinds; `up_to_message_id` is a nullable effective frontier; see [Read State](#read-state) |
| `chat_read_state_invalidated` | Cursor maintenance resequenced the room `{room_id, generation}` — discard old positions and refresh |
| `chat_member_joined` | Member joined `{room_id, user_id}` |
| `chat_member_left` | Member left `{room_id, user_id}` |
| `chat_member_removed` | An admin removed a member `{room_id, user_id}` |
| `chat_member_banned` | A moderator banned a member `{room_id, user_id}` |

The member named in `chat_member_left`, `chat_member_removed` or
`chat_member_banned` does not receive that event: the server re-checks their
access on it, drops it, and unsubscribes their socket from the room. A
`chat_room_deleted` `{room_id}` frame is published when a room is deleted
through REST, but no client receives it, because nobody has access to a
deleted room; every socket on the topic is unsubscribed instead.

Losing access is silent on the socket: no error frame, no `unsubscribed` frame,
and the connection and its other topics stay open. Access removed by these
events stops delivery immediately; access removed any other way (a group or
platform `chat` / `manage_chat` permission revoked, for example) stops it within
the server's re-check window, 300 seconds by default. Subscribe again once
access is restored; dropped events are not replayed.

## Advisory moderation and display

With moderation enabled, language at every severity is saved and scored. `high_severity` is a reason
code, never a special refusal. Normal sends, retries and race acknowledgements,
new-message events, edit events/acks, history and both moderator graphs always
carry these server-owned fields:

| Field | Meaning |
|---|---|
| `moderation_score` | Integer 0–100 when classified. **0** means clean; **null** means legacy/unscored. |
| `moderation_reasons` | List of category codes, empty when none; never matched phrases. |
| `moderation_decision` | `allow`, `warn`, `masked`, or legacy `block`. `masked` means the score reached the room's live hide level (default 70), or a slur matched. |

For example, a single strong swear word scores 50 and stays `warn` at the
default level; a three-link message scores 75 and stores as `masked`. Low-score
allows may still have reasons. Reasons are `high_severity` (a slur: always
`masked`, whatever the level), `strong_profanity` (other strong swearing, same
score, follows the level), `deny_hit`, `repeated_profanity`, `spam_link`,
`spam_phone`, `excessive_repetition`, `repeated_words`, `excessive_caps`.
Profanity matches whole words only, so work words such as "passed", "mass" and
commit hashes score nothing. Links to a room's allowed domains score nothing.

The hide level (`CHAT_MODERATION_HIDE_LEVEL`, 1–101, default 70) and allowed
domains (`CHAT_MODERATION_ALLOWED_DOMAINS`, a list of hostnames) are settings
an administrator can change globally or per group through the settings API,
without a release; a group member with `manage_settings` for that group may
set that group's values. They apply to new sends and edits; stored messages
keep their decision. Backend integrations use
`check_moderation_scored(body, group=room.group)` returning
`(decision, reasons, score)`; `check_moderation(body, group=...)` remains a
two-tuple, and both still work without a group (global values).

The application can disable classification with `CHAT_MODERATION_ENABLED=False`.
Its framework default is `True`; a global runtime DB Setting overrides the file
setting and is read with boolean coercion (including text `"false"`). Disabled
new sends and accepted edits return `allow` / `[]` / `null`. Replace all three
local fields on edits so previous masking metadata clears. Re-enabling affects
subsequent classifications; the setting alone does not alter historical rows or
idempotent retry responses. Historical presentation is a separate application
choice. Authorization and explicit room rules still apply, including URL/phone
rules. This requires a framework release containing the switch on every writer;
older application/file-caption fallbacks must also honor the disabled setting.

**Hide on the stored decision.** Hide a message when
`moderation_decision == "masked"` and give each viewer a local **Show** action;
the decision already follows the live hide level and the always-hidden slur
list, and the framework does not expose the level for clients to compare
scores against. Only for rows with no decision, fall back to the score. Do not
coerce null to 0. Existing rows are not rescored; old rows get null/[] and
trusted-server bypass messages get allow/[]/null. A client that hides by its
own score threshold (Maestro hid at **>=35**) sees no change from the hide
level until it hides on `masked`.

Authorized responses and events contain the **real body**. Hiding is viewer
presentation, not access control. Preserve ordinary history/unread behavior;
flags, membership, join bounds and disappearing-message expiry still apply.
The three moderation fields cannot be supplied by clients or overridden by
kind metadata. Accepted edits replace all three; clean edits with moderation enabled return
allow/[]/0 so a previous hidden state clears.

**Notifications must use `Hidden by moderation` for hidden previews**, including
file captions; preserve real bodies in authorized message responses. Upgrade
the consumer display/notification handling before enabling advisory posting,
because old pages may render those bodies automatically. The framework provides
no REST send or notification adapter; those are host application integrations.

Mixed-version chat writers are unsupported. Backend rollout must drain old
writers before enabling scoring. Application rollback requires stopping writers
and clearing every message's score/reasons to null/[] before old writers resume;
otherwise old edits can leave a stale numeric score. Clients use decision
fallback for these rows, including after re-upgrade until a newly scored edit.
See the [backend rollout procedure](../../django_developer/chat/rules.md#writer-rollout-and-rollback).

Permission checks, rate limits, kind/metadata validation and explicit room
URL/phone/media/length rules still refuse normally. For older installations,
continue handling their existing `{"type":"error","error":"Message blocked
by moderation","reasons":[...]}` frame and optional `client_key`; new advisory
language sends do not reach that path. No new error-code scheme is introduced.

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
| `direct` | Auto (DM endpoint) | From join date | 1:1 private messages |
| `group` | Invite only | From join date | Teams, support |
| `channel` | Self-join | Full | Announcements, community |
