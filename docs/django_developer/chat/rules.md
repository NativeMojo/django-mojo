# Chat Rules & Moderation

## Per-Room Rules

Each room has a `rules` JSONField. The handler enforces these before persisting messages.

| Rule | Default | Description |
|------|---------|-------------|
| `allow_urls` | `True` | If `False`, messages with URLs are rejected |
| `allow_media` | `True` | If `False`, `kind="image"` messages are rejected |
| `allow_phone_numbers` | `True` | If `False`, messages with phone numbers are rejected |
| `max_message_length` | `4000` | Messages exceeding this are rejected |
| `disappearing_ttl` | `0` | Seconds until messages auto-delete. 0 = off. |
| `rate_limit` | `10` | Max messages per user per second |

## Content Guard Integration

Every message send and edit runs through `content_guard.check_text(body, surface="chat")`:

- **block** — message rejected, not persisted, error returned to sender
- **warn** — message persisted with `moderation_decision="warn"`
- **allow** — message persisted normally

URL and phone number detection reuses content_guard's existing match types (`spam_link`, `url`, `spam_phone`, `phone`).

## Rate Limiting

Uses a Redis sorted set sliding window (1-second window). Each message adds a timestamped entry. If the count exceeds the room's `rate_limit`, the message is rejected.

## Disappearing Messages

When `disappearing_ttl > 0`:
- `mojo.apps.chat.cleanup.run_cleanup()` deletes expired messages
- Flagged messages are exempt (evidence preservation)
- History endpoint also filters out expired messages as a fallback

Call `run_cleanup()` from a periodic task (cron job).

## Flagging

Moderators can flag messages via `chat_flag` WebSocket message or REST endpoint. Flagging:
- Sets `is_flagged=True` on the message
- Records `flagged_by` and `flagged_at`
- Publishes event to room topic (frontends hide the message)
- Message stays in DB as evidence
- Excluded from normal history, visible via moderator endpoint
