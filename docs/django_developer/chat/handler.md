# Chat WebSocket Handler

Chat messages are sent and received over the existing realtime WebSocket connection (`ws/realtime/`). The User model's `on_realtime_message` hook routes `chat_*` message types to the chat handler.

## Message Types

### chat_message — Send a message

```json
{"type": "chat_message", "room_id": 5, "body": "hello", "kind": "text",
 "metadata": {}, "client_key": "01J8Z0K3Q0X"}
```

Flow:
1. Validate `client_key` if one was supplied (see below)
2. Validate membership
3. **Dedupe on `client_key`** — return the existing ack if this key already sent
4. Check `can_send` (`status == "active"`)
5. Hand off to [`send_message`](services.md) with `client_authored=True`, which
   whitelists the kind, validates `metadata`, applies the rate limit, the room
   rules, the card payload rules and moderation, persists the row and publishes
   to `chat:5`
6. Return ack with `message_id`, `room_id`, `kind`, `metadata`, `created`

`_handle_send` owns the frame shape, the membership checks and the `client_key`
idempotency contract; everything from validation onward lives in the send
service, which is also what the server-authored system messages in
`rest/rooms.py` use. The `IntegrityError` recovery below still sits in the
handler — the service lets the exception propagate for exactly that reason.

#### kind and metadata

`kind` defaults to `"text"`. The **kind whitelist** is what makes accepting it
from a client safe: a WebSocket client may author `text` and `image`, plus
`card` when the host has registered a validator for it. `system` and `file` are
**server-authored only** — a client frame carrying either is refused with
`{"type": "error", "error": "Unsupported message kind"}` and nothing is stored.
Any unrecognized kind string is refused the same way. See
[Services](services.md#kind-whitelist).

`metadata` is read from the frame (previously ignored) and validated on every
send regardless of kind: JSON scalars/lists/dicts only, finite numbers, string
keys, depth-bounded, and capped at `CHAT_METADATA_MAX_BYTES` (default 4096).
See [the metadata contract](services.md#metadata-contract).

**Body rule.** `kind="text"` still requires a body, exactly as before. Every
other kind is satisfied by a body **or** non-empty validated metadata — an
uncaptioned image or a bodyless card is normal. A message with neither is
refused with `"body or metadata is required"`.

Note the body check now runs *after* membership resolution rather than before
it, because it lives in the send service. A non-member sending an empty body
gets `"Not a member of this room"` instead of `"body is required"`.

#### client_key — idempotent sends

`client_key` is **optional**. When present it makes the send idempotent for the
`(room, user)` pair: a client that loses the ack can resend the identical frame and
gets the original message back instead of creating a duplicate.

**Bounds and charset.** 1–64 characters from `[A-Za-z0-9._:-]` — validated with
`re.fullmatch` against the value **exactly as sent**. A whitespace-only (or absent)
value means "no key" and the send behaves as it always did; anything else that fails
the pattern — too long, not a string, an interior space, a trailing newline, leading
or trailing padding — is rejected with an error frame. The value is never normalized:
`" abc"` and `"abc"` must not silently collapse into one idempotency key. The error
frame deliberately **does not echo the offending value**.

**Contract.**

- Same key + same `body` and `kind` → the ack for the message already stored
  (same `message_id`), no second row and **no second broadcast**.
- Same key + *different* `body` or `kind` → an error frame,
  `"client_key is already bound to a different message"`. The stored message is
  untouched. Refusing is deliberate: acking a rebind would report success while
  silently discarding the new message — worse than the duplicate this guards against.
- The key is scoped to `(room, user)`. The same key in another room, or from another
  user, is a different message.

**Where dedupe sits.** After membership resolution, **before** `can_send` and before
the rate limiter. A retry is not a new message, so it must not consume rate-limit
budget or be refused because the sender was muted after the original send landed.

**Enforcement.** A partial unique constraint on `(room, user, client_key)` where
`client_key IS NOT NULL` (`chat_message_client_key_uniq`) backs the check, so two
concurrent sends of the same key cannot both persist. The create is wrapped in
`transaction.atomic()`; on `IntegrityError` the handler re-reads the row and returns
that ack (or the rebind error) **directly** — it never falls through to the broadcast,
so the race loser does not publish a second frame for the same `message_id`. If the
re-read finds nothing (e.g. the room was deleted mid-send and the `IntegrityError`
came from the foreign key, not the unique index) it returns
`"Could not persist the message"` rather than crashing. A send with no `client_key`
re-raises the `IntegrityError` unchanged.

**Echo sites.** The key is echoed on:

- the `chat_message_ack` frame (all ack paths, including the deduped one),
- every error frame from a send **except** the `client_key`-validation error itself,
- the `chat_message` broadcast payload on the room topic,
- the generic crash frame from `handle_chat_message`, best-effort,
- `GET /api/chat/room/messages` rows — **author-scoped**: `client_key` is returned
  only on the requesting user's own messages and is `null` on everyone else's, since
  keys are client-chosen and may encode device identity or content equality.

`ChatMessage.RestMeta.NO_SAVE_FIELDS` also lists `client_key` so no future model-security
route can let a client write it directly. (`ChatMessage` has no
`uses_model_security` route today — this is forward defense, not a live control.)

**Ack and broadcast shape.** Both the `chat_message_ack` frame and the
`chat_message` broadcast carry `kind` and `metadata`, so a client renders the
message without a second fetch. The `client_key` echo described above is
unchanged and rides on both.

**Test seam.** `_handle_send(user, data, *, publisher=None)` takes an optional
`publisher` callable with the same signature as `publish_topic(topic, payload)`.
Tests pass a capture function to assert on the broadcast without patching anything;
production callers omit it and the realtime publisher is used.

### chat_edit — Edit a message

```json
{"type": "chat_edit", "message_id": 42, "body": "updated text"}
```

- Author or room admin can edit
- Re-runs room rules and content_guard on new body
- Sets `edited_at` timestamp
- Publishes `chat_message_edited` event to room topic

### chat_flag — Flag a message (moderator)

```json
{"type": "chat_flag", "message_id": 42}
```

- Requires room admin role, `moderate_chat`, or `manage_chat` permission
- Sets `is_flagged=True`, records `flagged_by` and `flagged_at`
- Publishes `chat_message_flagged` event (frontends hide the message)
- Message stays in DB as evidence

### chat_react — Add, remove or toggle an emoji reaction

```json
{"type": "chat_react", "message_id": 42, "emoji": "\ud83d\udc4d", "action": "add"}
```

`action` is **optional**:

| `action` | Behaviour |
|---|---|
| absent | Legacy toggle — add if absent, remove if present. Unchanged. |
| `"add"` | `get_or_create`. Acks `added` whether or not a row was written. |
| `"remove"` | `filter(...).delete()`. Acks `removed` whether or not a row existed. |
| anything else | `{"type": "error", "error": "Invalid action"}` |

Explicit actions are **idempotent**, so a retried frame cannot invert the
reaction the way a retried toggle did.

> **The request verbs are `add`/`remove`; the ack and the `chat_reaction` event
> report `added`/`removed`.** That mismatch is a shipped contract, not an
> oversight. Do not rename either side.

**A no-op does not broadcast.** `chat_reaction` is published only on a real
state change — `get_or_create`'s `created` flag and `delete()`'s row count are
the signal, free. `chat_react` is *not* rate limited (`check_rate_limit` runs
inside `_handle_send` only), so publishing on every call would let one member
loop `add` and fan one inbound frame out to every subscriber in the room. The
idempotent **ack** is the useful half of the contract; the broadcast is not.

**Target resolution.** The message must be one the caller was entitled to see —
unflagged and at or after their `joined_at`, via
`services.messages.join_bounded_messages`. A nonexistent message, a flagged
one, one from before the caller joined, **and one in a room the caller is not
in** all return the same `{"type": "error", "error": "Message not found"}`. The
frame carries no `room_id`, so a distinguishable "Not a member of this room"
would be a *global* cross-tenant message-existence oracle for any authenticated
user. Keep those returns identical.

**Test seam.** `_handle_react(user, data, *, publisher=None)` takes an optional
publisher with the same signature as `publish_topic(topic, payload)`, so a test
can assert on the broadcast — or its absence — without patching anything.

### chat_typing — Typing indicator (ephemeral)

```json
{"type": "chat_typing", "room_id": 5}
```

- No persistence, purely ephemeral via Redis pub/sub
- Publishes `chat_typing` event to room topic
- No ack returned

### chat_read — Mark messages as read

```json
{"type": "chat_read", "room_id": 5, "up_to_message_id": 482}
```

- Requires a membership with `status in ("active", "muted")` — the same
  predicate history and `/unread` use. A **banned** member is refused with
  `"Not a member of this room"` and writes nothing; a **muted** member still
  marks read, because mute stops sending, not reading (a muted member who
  could not mark read would show as permanently unread to everyone else).
- `up_to_message_id` is **resolved, not trusted** — see below
- For `direct`/`group` rooms: bulk-creates `ChatReadReceipt` for unread messages up to the RESOLVED id
- For `channel` rooms: updates `last_read_at` on ChatMembership
- Publishes `chat_read` for direct/group rooms, carrying the **resolved** id,
  and only when something resolved
- Acks `{"type": "chat_read_ack", "room_id": 5, "up_to_message_id": <resolved or null>}`

#### Target resolution and last_read_at

Both this handler and `POST /api/chat/room/read` go through
`services.read_state.mark_read`; the two used to carry duplicate copies of the
receipt block and could drift.

`resolve_read_target(room, membership, up_to_message_id)` returns **the newest
message at or below the supplied id that the caller was entitled to see**, or
`None`. A non-integer or `< 1` value resolves to `None`; so does an id below
everything the caller may read. `None` writes nothing, broadcasts nothing, and
acks `up_to_message_id: null`.

`last_read_at` comes from `target.created`, not `utcnow()`, and **only moves
forward**. There is no mark-unread feature, and the channel branch of
`GET /api/chat/unread` counts `created__gt=last_read_at`, so rewinding would
resurrect already-read messages as unread. This is a real behaviour change for
channels, which previously stamped the wall clock.

> **Resolution applies the join bound, NOT the disappearing-message TTL.**
> This is deliberate and easy to get wrong. TTL expiry is monotonic in age and
> age is monotonic in pk, so once message N has aged out, every message below
> it has too — a TTL-bounded `pk__lte=N` lookup is *empty*, not "the newest
> message still on screen". Resolving through `visible_messages` would
> therefore **discard the read entirely** in exactly the race the clamp exists
> to survive, and the sender's read indicator would never update for a message
> the other party genuinely read. Visibility belongs on *which id you may
> name* (`join_bounded_messages` — entitlement, which does not expire), not on
> *which messages the acknowledgement covers*. Receipts are sourced from the
> same join bound, for the same reason.

Receipts are bounded by `pk`, `last_read_at` by `created`. `created` is
`auto_now_add` set in Python while `pk` comes from the database sequence, so
under concurrent inserts the two orderings can disagree; the pairing matches
how `GET /api/chat/unread` reads each. It is not a claim that they agree.

**Test seam.** `_handle_read(user, data, *, publisher=None)`, same contract as
`_handle_send`'s.

## Integration

The handler is wired via `User.on_realtime_message`:

```python
# In User model
if mtype and mtype.startswith("chat_"):
    from mojo.apps.chat.handler import handle_chat_message
    result = handle_chat_message(self, data)
```

No settings configuration needed.
