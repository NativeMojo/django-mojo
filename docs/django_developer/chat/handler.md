# Chat WebSocket Handler

Chat messages are sent and received over the existing realtime WebSocket connection (`ws/realtime/`). The User model's `on_realtime_message` hook routes `chat_*` message types to the chat handler.

## Message Types

### chat_message — Send a message

```json
{"type": "chat_message", "room_id": 5, "body": "hello", "kind": "text"}
```

Flow:
1. Validate membership and `status == "active"`
2. Check rate limit
3. Enforce room rules (max length, URL/phone/media restrictions)
4. Run `content_guard.check_text()` — block or warn
5. Persist `ChatMessage` to DB
6. Publish to `chat:5` topic
7. Return ack with `message_id` and `created`

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
