# Chat Services

Domain logic for the chat app lives in `mojo/apps/chat/services/`.

- `services/messages.py` — `send_message`, the one creation path for a chat
  message, plus the two read bounds `join_bounded_messages` and
  `visible_messages`
- `services/read_state.py` — `resolve_read_target` and `mark_read`, the one
  read-acknowledgement path
- `services/deletion.py` — the deletion notification hook

## `send_message`

```python
from mojo.apps.chat.services.messages import send_message

msg, error = send_message(room, user, body, kind="text", metadata=None, *,
                          client_authored=True, enforce_room_policy=True,
                          broadcast=True, broadcast_extra=None,
                          client_key=None, publisher=None,
                          validators=_UNSET, max_bytes=_UNSET)
```

Returns `(message, None)` on success and `(None, error_dict)` on refusal. The
error dict is already frame-shaped: `{"type": "error", "error": "..."}`, plus
`reasons` on a moderation block.

The WebSocket handler (`_handle_send`) and the three server-authored system
messages in `rest/rooms.py` both go through it.

### Two independent gates

They are deliberately **not** one flag.

| Parameter | Default | What it gates |
|---|---|---|
| `client_authored` | `True` | The **kind whitelist**, and nothing else. |
| `enforce_room_policy` | `True` | The **rate limit**, the **room rules** and **moderation** — all applied to `body`. |

Both default to the safe value, so a caller that forgets one gets the strict
behavior.

A server-authored *file* message carries a user-written caption that must still
be moderated and rate limited; a server-authored *join/leave* message must not
be. Collapsing the two onto a single flag would silently stop moderating
captions.

**Metadata validation and the byte cap run on every send regardless of either
gate** — they are data integrity, not room policy.

### Kind whitelist

```python
CLIENT_KINDS    = {"text", "image"}                          # always client-authorable
VALIDATED_KINDS = {"card"}                                   # client-authorable with a validator
SERVER_KINDS    = {"text", "image", "system", "card", "file"}  # server-authorable
```

- `client_authored=True` admits `CLIENT_KINDS`, plus any kind in
  `VALIDATED_KINDS` that has a validator registered for it.
- `client_authored=False` admits `SERVER_KINDS`.
- Anything else is refused with `"Unsupported message kind"`.

`file` is in `SERVER_KINDS` but in neither client set, so a client can never
author one. `system` likewise.

### Order of operations

1. Kind whitelist (per `client_authored`)
2. Metadata validation and byte cap (**unconditional**)
3. Body rule — `kind == "text"` requires a body; every other kind is satisfied
   by a body **or** non-empty validated metadata
4. Rate limit (`enforce_room_policy`)
5. Room rules — `check_rules(room, body, kind)` (`enforce_room_policy`)
6. Card payload rules — `check_payload_rules(room, metadata)`
   (`client_authored and enforce_room_policy`)
7. Moderation — `check_moderation(body)` (`enforce_room_policy`)
8. Persist
9. Publish the `chat_message` frame when `broadcast`
10. `room.save(update_fields=["modified"])`

Step 6 runs only for client-authored sends: its purpose is stopping a *client*
smuggling a link past a room owner who set `allow_urls=false`. A server-derived
reference is not that. See [Rules & Moderation](rules.md).

`client_key` is written to the row but the idempotency **lookup** stays in
`_handle_send` — it is client-authored replay protection and is meaningless for
a server-authored message. An `IntegrityError` from the partial unique
constraint propagates to the caller, which is how the handler's dedupe recovery
still works. See [WebSocket Handler](handler.md).

### Metadata contract

`metadata` is a JSON object validated on every send:

- Only `str`, `int`, `float`, `bool`, `None`, `list` and `dict` values.
- Floats must be finite — `json.dumps` happily emits `NaN`/`Infinity`, which is
  invalid JSON and breaks every non-Python consumer.
- Dict keys must be strings of at most `MAX_METADATA_KEY_LEN` (64) characters.
- Nesting at most `MAX_METADATA_DEPTH` (5) levels.
- The compact JSON encoding must be at most `CHAT_METADATA_MAX_BYTES` bytes,
  default **4096**.

Payload strings are **not** sanitized and **not** escaped. `safe_text.sanitize_scalar`
would corrupt exactly the data this carries: it rebuilds a bare URL without its
fragment (so a hash-router link like
`https://maestromojo.com/app/#/board/116?item=3357` becomes
`https://maestromojo.com/app/`) and redacts any scalar containing a long
high-entropy run — a base64 external id. HTML safety is a **rendering**
boundary, not a storage one; the web-developer doc tells clients to render
payloads as text.

### Validator hook

A host registers a per-kind validator so the chat app never has to know what a
card means:

```python
CHAT_KIND_VALIDATORS = {
    "card": "myapp.chat_cards.validate_card",
}
```

Contract: `validator(room, user, metadata) -> metadata`. It may normalize; the
returned payload is re-checked against the shape walk and the byte cap. Raising
refuses the send. The service catches `Exception`, logs it, and returns a
**generic** client error — the exception text is never leaked.

**Resolution fails closed, per kind.** A kind whose validator is configured but
unloadable is refused with `"This message kind is not available"`; the path is
cached as failed (keyed by the path string, so fixing a typo'd setting takes
effect without a restart). Only that kind is affected — `text` has no validator
and keeps working. A deployment that misconfigures the `card` validator loses
card sends until it is fixed. That is correct: a payload nobody vetted must not
persist.

The default configuration registers no validators, so **`card` is off until a
host opts in**.

### Compatibility target

A host that composes a file message itself collapses onto:

```python
send_message(room, user, caption, kind="file",
             metadata={"file": {...}}, client_authored=False)
```

`file ∈ SERVER_KINDS` passes the whitelist, no validator is required
(`file ∉ VALIDATED_KINDS`), and `enforce_room_policy` defaults `True` so the
caption still runs the room's rate limit, `check_rules` and `check_moderation`.

One gap is preserved deliberately: `check_rules`'s `allow_media` branch tests
`kind == "image"` only, so a `file` message in an `allow_media=False` room is
not blocked here.

**Accepting `kind="file"` from a server-side caller is not an endorsement of any
host's share rule.** Authorizing who may reference which file stays the host's
responsibility.

### Test seams

`validators=`, `max_bytes=` and `publisher=` are keyword-only seams with
sentinel/`None` defaults; production callers pass none of them. They exist
because `tests/test_chat` is scanned strict by `testit/isolation.py` — the
package declares `default_core` with no cold budget and carries `@th.tier("core")`
tests, so `th.server_settings()` and `mock.patch` both fail the whole package.
The seams are the only legal way to exercise the settings-driven behavior.

## Read bounds — `join_bounded_messages` vs `visible_messages`

```python
from mojo.apps.chat.services.messages import join_bounded_messages, visible_messages
```

Two bounds, one nested inside the other. Picking the wrong one is the single
easiest mistake in this app, so they are named for what they answer:

| Function | Bounds | Answers |
|---|---|---|
| `join_bounded_messages(room, membership)` | unflagged + `created >= joined_at` (every kind except `channel`) | *Which message id may this caller name?* — **entitlement** |
| `visible_messages(room, membership)` | the above **plus** the room's `disappearing_ttl` | *What does this caller see right now?* — **display** |

`visible_messages` is the display bound: `GET /api/chat/room/messages` and
`GET /api/chat/unread` both narrow through it, so a badge can never count a
message opening the room will not show.

`join_bounded_messages` is the entitlement bound, and it is what
`services/read_state.py` resolves read and react targets against. Entitlement
does not expire; visibility does.

`membership` is `None` only on the `manage_chat` moderator read path — a
moderator reviewing a room they never joined has no `joined_at` to be bound by.
The join cutoff is *excluded for `channel`* rather than allowlisted for
`direct`/`group`: `ChatRoom.kind` is a caller-settable CharField, so an
allowlist would fail **open** on any kind nobody reasoned about.

## `read_state` — resolving a read acknowledgement

```python
from mojo.apps.chat.services.read_state import resolve_read_target, mark_read

target = resolve_read_target(room, membership, up_to_message_id)   # ChatMessage or None
target = mark_read(room, user, membership, up_to_message_id)       # writes, then returns it
```

`resolve_read_target` clamps a client-supplied bound to **the newest message at
or below it that the caller was entitled to see**. `None` means "nothing to
acknowledge": the value was not a usable message id (non-numeric, `< 1`), or
the caller is entitled to no message at or below it. `mark_read` writes nothing
and returns `None` in that case, and its callers must not broadcast.

`mark_read` writes the receipt bulk-create for non-`channel` rooms and advances
`membership.last_read_at` to `target.created` — **only forward**, never
backward.

> **Resolution and receipts apply the join bound, NOT the TTL.** TTL expiry is
> monotonic in age and age is monotonic in pk: once message N has aged out,
> every message below it has too, so a TTL-bounded `pk__lte=N` lookup is
> *empty*, not clamped. Resolving through `visible_messages` would discard the
> read entirely — no receipt, no `last_read_at`, no broadcast — in exactly the
> race the clamp exists to survive. `cleanup.run_cleanup` is wired to nothing
> in this repo, so expired rows persist indefinitely and nothing self-heals.

Both `chat_read` and `POST /api/chat/room/read` go through `mark_read`; they
previously carried duplicate copies of the receipt block and could drift.
`chat_react` resolves its target through `join_bounded_messages` directly.

## Deletion hook

A consumer that keeps rows pointing at a chat message needs to know when those
messages disappear.

```python
CHAT_MESSAGE_DELETED_HANDLER = "myapp.chat_hooks.on_messages_deleted"
```

Contract: `handler(room_id, message_ids)`. Ids are delivered in chunks of 1000.
**Exceptions are caught and logged, never propagated** — a broken consumer hook
must not block a deletion.

```python
from mojo.apps.chat.services.deletion import notify_deleted, notify_room_deleted
```

| Function | Fires from |
|---|---|
| `notify_deleted(room_id, message_ids, *, handler=_UNSET)` | `cleanup.run_cleanup()` — the disappearing-messages TTL sweep |
| `notify_room_deleted(room, *, handler=_UNSET)` | `ChatRoom.on_rest_pre_delete()` — REST room deletion |

Room deletion is hooked because `ChatMessage.room` is `on_delete=CASCADE` and
`ChatRoom.RestMeta.CAN_DELETE` is `True`: one room delete destroys every message
in it at once, the worse orphan case. The ids are only readable before the
delete, hence the *pre* hook.

**Coverage gap, stated honestly.** The hook covers TTL cleanup and REST room
deletion. It does **not** fire for a raw ORM cascade: `ChatRoom.group` is
`on_delete=CASCADE` from `account.Group`, so deleting a Group destroys rooms and
their messages with no REST layer involved. Do not treat this as "every
deletion".

`run_cleanup(*, handler=_UNSET)` keeps its signature and return value: the total
number of rows deleted.

## Settings

All optional, all defaulting to today's behavior.

| Setting | Default | Purpose |
|---|---|---|
| `CHAT_KIND_VALIDATORS` | `{}` | Per-kind payload validators, `{kind: "dotted.path"}` |
| `CHAT_METADATA_MAX_BYTES` | `4096` | Byte cap on the compact JSON encoding of `metadata` |
| `CHAT_MESSAGE_DELETED_HANDLER` | `None` | Dotted path to `handler(room_id, message_ids)` |
