# Chat Rules & Moderation

## Per-Room Rules

Each room has a `rules` JSONField. [`send_message`](services.md) enforces these
before persisting, gated by `enforce_room_policy` — the WebSocket handler no
longer carries its own copy. `chat_edit` still enforces `check_rules` and
`check_moderation_scored` directly in the handler.

| Rule | Default | Description |
|------|---------|-------------|
| `allow_urls` | `True` | If `False`, messages with URLs are rejected |
| `allow_media` | `True` | If `False`, `kind="image"` messages are rejected |
| `allow_phone_numbers` | `True` | If `False`, messages with phone numbers are rejected |
| `max_message_length` | `4000` | Messages exceeding this are rejected |
| `disappearing_ttl` | `0` | Seconds until messages auto-delete. 0 = off. |
| `rate_limit` | `10` | Max messages per user per second |

## Content Guard Integration

Every classified send and edit calls the chat adapter:

```python
from mojo.apps.chat.rules import check_moderation, check_moderation_scored

decision, reasons, score = check_moderation_scored(body)
decision, reasons = check_moderation(body)  # compatible two-tuple
```

### Application moderation switch

`CHAT_MODERATION_ENABLED` defaults to `True`. The chat adapter reads it using
`settings.get("CHAT_MODERATION_ENABLED", True, kind="bool")`: a global runtime
DB `Setting` overrides the file setting, which overrides the framework default.
Boolean coercion accepts text values such as `"false"`; deleting the DB override
restores the file/default behavior. Unrecognized text values use the default `True`.

When disabled, `check_moderation_scored` returns `("allow", [], None)` without
calling the language classifier; the legacy wrapper returns `("allow", [])`.
New sends persist that unscored state. Accepted edits replace all three fields,
clearing any previous score/reasons on the edited message. Re-enabling restores
advisory classification for subsequent sends and edits.

The switch does not bypass authorization, rate limits, length/media constraints,
or explicit room URL/phone rules. Those room rules may still call content_guard
for URL/phone detection. Do not use `enforce_room_policy=False` as this switch:
it also bypasses unrelated room policies.

Changing the setting does not rewrite history or reclassify idempotent retries.
Consumers separately decide how to display historical scores. Host applications
and file-caption adapters must use a framework release containing this switch;
older-version fallbacks must explicitly honor the host's disabled setting.

When enabled, the scored helper calls `content_guard.check_text(body, surface="chat")` once
and returns `(decision, list(result.reasons), result.score)`. Only classifier
`block` changes to `masked`; `allow` and `warn` are unchanged. The generic
classifier's normalization, wordlists, scores and thresholds are unchanged.

**Language moderation is advisory at every severity.** Even `high_severity`
stores and acknowledges normally. A single high-severity hit scores 50 and
stays `warn`; two distinct ordinary deny terms score 75 and become `masked`.
There is no special severity refusal. Room rules, permissions and rate limits
still refuse independently. `enforce_room_policy=False` is the trusted-server
bypass: it skips classification and stores `allow`, `[]`, `null`.

All classified messages persist `moderation_decision`, `moderation_reasons`
and `moderation_score`, including low-score `allow` messages with reasons.
Score is an integer **0–100**; **0** means classified clean, while **null**
means legacy/unscored. Existing history is never re-scored or backfilled.
Reasons are category codes: `high_severity`, `deny_hit`, `repeated_profanity`,
`spam_link`, `spam_phone`, `excessive_repetition`, `repeated_words`,
`excessive_caps`. They contain neither matched phrases nor normalized text.

`body` is the moderated surface, preserved subject to existing whitespace
trimming. Authorized history and events retain the real body. Moderation does
not change message visibility, unread counts, join bounds, flags or TTL bounds.
URL/phone room rules reuse existing match types (`spam_link`, `url`,
`spam_phone`, `phone`). Opaque kind metadata cannot set top-level moderation.

### Consumer display and notification contract

The consumer owns its display threshold. Maestro initially hides valid numeric
scores **>=35**, the current warning threshold, and offers each viewer a local
Show action. Numeric score is authoritative, including 0; absent, null or
invalid scores fall back to legacy `warn`/`masked`/`block` decisions. `masked`
records a classifier block; it does not redact stored content or impose the UI
threshold. Successful edits replace all three fields together; clean edits with moderation enabled
return `allow`/`[]`/`0` so clients clear prior hidden state.

Consumers must substitute **`Hidden by moderation`** for hidden message
notification previews, including file captions, while preserving the real body
in authorized message responses. Ship compatible display/preview handling
before enabling advisory posting: old pages may render bodies automatically.
The framework supplies no REST send endpoint or notification preview producer;
those adapters belong to the host application.

### Writer rollout and rollback

Mixed-version chat writers are not supported. Drain all old writers before
enabling new scored sends or edits: an old edit updates body/decision without
replacing score/reasons, leaving a stale numeric score authoritative in clients.

Before an application-only rollback, stop all chat writers and invalidate
existing moderation scores/reasons while retaining bodies and decisions:

```python
from mojo.apps.chat.models import ChatMessage

ChatMessage.objects.all().update(moderation_score=None, moderation_reasons=[])
```

Keep the additive schema in place, then start the old application. Old edits
now retain null scores and use decision fallback. After re-upgrade, these rows
remain unscored until edited by a new writer; history is not rescored. Before
enabling new writers again, drain the old writers as for the initial rollout.

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
The classifier's score is a heuristic; running it over ids, slugs and
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
- [`visible_messages`](services.md#read-bounds--join_bounded_messages-vs-visible_messages)
  filters expired rows out of `GET /api/chat/room/messages` and
  `GET /api/chat/unread` as a fallback, so a badge and the history agree even
  before the sweep runs

The TTL is **not** applied when resolving a read or react target — see
[`read_state`](services.md#read_state--resolving-a-read-acknowledgement) for why
applying it there would discard the read instead of clamping it.

Call `run_cleanup()` from a periodic task (cron job). Nothing in this repo is
wired to call it, so a deployment that does not schedule it keeps expired rows
indefinitely — the read bound is what hides them.

## Flagging

Moderators can flag messages via `chat_flag` WebSocket message or REST endpoint. Flagging:
- Sets `is_flagged=True` on the message
- Records `flagged_by` and `flagged_at`
- Publishes event to room topic (frontends hide the message)
- Message stays in DB as evidence
- Excluded from normal history, visible via moderator endpoint
