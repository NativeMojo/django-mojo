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

`body` is the moderated surface. `check_moderation` and `check_rules` both read
`body` only.

## Card Payloads — `check_payload_rules`

```python
check_payload_rules(room, metadata)  # -> list of error strings
```

A room owner who sets `allow_urls=false` means it. Without this check a card
could carry `{"link": "https://evil.tld/lure"}` and defeat the rule outright,
because `check_rules` never looks at `metadata`.

`check_payload_rules` flattens every string key and string value out of the
already-validated, already-capped payload, joins them, and runs the **same**
`content_guard.check_text(..., surface="chat")` call as `check_rules` — but
applies only the `allow_urls` and `allow_phone_numbers` branches. It
early-returns when both rules are on, which is the default.

**The moderation classifier is deliberately NOT applied to payloads.**
`check_moderation`'s `block` is a heuristic; running it over ids, slugs and
external references produces false positives with no recourse, and the
human-visible moderated surface is `body`.

**It runs only for client-authored sends** (`client_authored and
enforce_room_policy` — see [Services](services.md#two-independent-gates)). Its
purpose is stopping a *client* smuggling a link past the room owner. A
server-derived reference — a file row the host already verified — is not that,
and checking it would break a legitimately-named file in an `allow_urls=false`
room.

## Rate Limiting

Uses a Redis sorted set sliding window (1-second window). Each message adds a timestamped entry. If the count exceeds the room's `rate_limit`, the message is rejected.

## Disappearing Messages

When `disappearing_ttl > 0`:
- `mojo.apps.chat.cleanup.run_cleanup()` deletes expired messages and notifies
  the [deletion hook](services.md#deletion-hook) with the ids it removed
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
