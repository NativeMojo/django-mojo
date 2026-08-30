# Chat WebSocket Handler

Chat messages are sent and received over the existing realtime WebSocket connection (`ws/realtime/`). The User model's `on_realtime_message` hook routes `chat_*` message types to the chat handler.

## Message Types

### chat_message — Send a message

```json
{"type": "chat_message", "room_id": 5, "body": "hello", "kind": "text",
 "client_key": "01J8Z0K3Q0X"}
```

Flow:
1. Validate `client_key` if one was supplied (see below)
2. Validate membership
3. **Dedupe on `client_key`** — return the existing ack if this key already sent
4. Check `can_send` (`status == "active"`)
5. Check rate limit
6. Enforce room rules (max length, URL/phone/media restrictions)
7. Run `content_guard.check_text()` — block or warn
8. Persist `ChatMessage` to DB
9. Publish to `chat:5` topic
10. Return ack with `message_id`, `room_id`, `created`

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

### chat_react — Toggle emoji reaction

```json
{"type": "chat_react", "message_id": 42, "emoji": "\ud83d\udc4d"}
```

- Toggle: add if not exists, remove if exists
- Publishes `chat_reaction` event with `action: "added"` or `"removed"`

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

- For `direct`/`group` rooms: bulk-creates `ChatReadReceipt` for unread messages up to that ID
- For `channel` rooms: updates `last_read_at` on ChatMembership
- Publishes `chat_read` event for direct/group rooms (sender sees read indicator)

## Integration

The handler is wired via `User.on_realtime_message`:

```python
# In User model
if mtype and mtype.startswith("chat_"):
    from mojo.apps.chat.handler import handle_chat_message
    result = handle_chat_message(self, data)
```

No settings configuration needed.
