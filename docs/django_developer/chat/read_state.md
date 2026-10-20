# Cursor Read State

The default `CHAT_READ_STATE_BACKEND = "legacy"` retains per-message receipts
and the existing channel timestamp counter. A host may opt in to DB-only
arrival cursors for every room kind:

```python
# settings.py -- file-only; a database Setting row is never read
CHAT_READ_STATE_BACKEND = "cursor"
CHAT_READ_HANDLER = "myapp.chat_hooks.on_read"  # optional
```

Only `legacy` and `cursor` are accepted; another value fails application
configuration. Cursor mode does not dual-write `ChatReadReceipt` rows and
uses no Redis read-state store. See [activation](#activation-and-rollback)
before enabling it on an existing installation.

## Arrival and membership rules

Each cursor-mode insert locks its room, advances `ChatRoom.message_seq`, saves
the message with that `room_seq`, and advances the author's active/muted seat
through that sequence in one transaction. Sending therefore acknowledges older
arrivals, including sparse unread exceptions through the send. A system author
without a seat has no cursor to advance. Other recipients receive no per-send
database writes. An outer rollback rolls back the head, row and sender state,
and emits no committed message/read frame.

New memberships start at the locked current head with zero unread, including
`objects.create`, `get_or_create`, instance `save` and supported `bulk_create`.
Existing channel history remains visible, but is not newly unread on join.
Muted members can read. Banned members cannot acknowledge and are omitted from
normal snapshots; their stored state survives unban. Deleting a membership
deletes its exceptions; rejoining creates a new head baseline and join bound.

Counts track arrival positions, not the current visible page. Flagging,
expiry and ordinary physical deletion do not decrement/recycle the room head
or discard sparse exception count slots. A count can include an invisible or
deleted tail. Trusted server code can acknowledge that tail with
`mark_current_head`; clients still name an actual message ID. Display history
continues to apply flags, join entitlement and TTL independently.

## Public Python API

All functions below live in `mojo.apps.chat.services.read_state`.

| Function | Result |
|---|---|
| `resolve_read_target(room, membership, up_to_message_id)` | Exact entitled in-room `ChatMessage`, or `None` |
| `mark_through(room, user, membership, up_to_message_id)` | `objict(target, position, changed)` |
| `mark_read(room, user, membership, up_to_message_id)` | Compatibility wrapper returning only the target or `None` |
| `recipient_positions(room, user_ids=None, message_ids=None)` | User-ID-keyed position map |
| `unread_counts(user, room_ids=None)` | Room-ID-keyed position map |
| `unread_queryset(membership, queryset=None)` | Room-scoped queryset filtered to exact unread eligibility |
| `unread_message_ids(membership, message_ids)` | Set of unread IDs from a bounded candidate set |
| `message_is_read(message, position)` | Pure snapshot predicate; no hidden query |
| `mark_current_head(room, user, membership)` | Trusted current-head acknowledgment result |

`mark_through`, `mark_read` and `mark_current_head` accept keyword-only
`handler=` and `publisher=` overrides for integrations/tests. Normal callers
use the configured hook and realtime publisher. Snapshot/count/current-head
helpers require cursor mode. The target and read wrappers also support legacy
mode; legacy `mark_through` returns `position=None`.

### Exact targets and monotonic reads

Cursor reads require the exact positive message PK. Missing, foreign,
flagged, malformed or pre-join IDs make no change and resolve to `None`; they
do not clamp to another row. The target uses `join_bounded_messages`, so an
otherwise entitled message can still be acknowledged after TTL expiry.
Channel targets have no join cutoff. See [read bounds](services.md#read-bounds--join_bounded_messages-vs-visible_messages).

`mark_through` locks the current membership, then reloads the room and target
and rechecks active/muted status and entitlement. Reads advance the sequence
monotonically within a generation and remove that seat's exception rows
through the actual target, even when it is below the encoded prefix. Valid
duplicate/stale reads may return `changed=False`. Invalid targets return the
current position with `changed=False` when the seat still exists; an invalid
seat returns no position. Database failures propagate, and impossible
cursor/cache invariants raise `ReadStateUnavailable`; neither becomes a zero
count or a fallback history scan.

`mark_current_head` obtains the current head itself; it accepts no client
sequence. It clears invisible/deleted tail positions without inventing an
acknowledged message PK or timestamp.

### Position shape

Both batch helpers read membership and room state in one SQL statement;
`recipient_positions(..., message_ids=[...])` may add one bounded exception
query. Freshness-critical reads use the primary database. Positions contain:

| Field | Meaning |
|---|---|
| `room_id`, `user_id`, `room_name`, `room_kind` | Snapshot identity and room labels |
| `generation` | Room's current numbering generation |
| `head_seq`, `read_seq` | Current arrival head and member's encoded prefix |
| `last_read_message_id`, `read_at` | Stored actual acknowledged message identity and its creation time; nullable |
| `unread_exception_count` | Cached number of sparse unread slots below/at the prefix |
| `unread_count` | `head_seq - read_seq + unread_exception_count` |
| `read_frontier_message_id` | `last_read_message_id` when no exceptions remain, otherwise `None` |

Successful acknowledgments add `ack_target_id`, `ack_target_seq`,
`ack_target_created`, `ack_generation` and `reason` (`explicit`, `own_send` or
`current_head`). These describe the actual operation. Current-head clears have
no target ID/time. The standing position can be newer than those facts, and a
maintenance prefix at the head does not prove every earlier message was read.
Do not substitute `head_seq` or `last_read_message_id` for the actual
acknowledgment boundary, or compare sequence values across generations.

### Eligibility before limits

Sparse `ChatUnreadException` rows encode unread message identities after a
merge/resequence; ordinary traffic creates none. A null `room_seq` from old
history is considered read. Maintenance can sequence it while preserving the
current unread identity set.

```python
from mojo.apps.chat.services.messages import visible_messages
from mojo.apps.chat.services.read_state import unread_queryset

# Apply host content-kind/visibility policy, then unread eligibility, then cap.
base = visible_messages(room, membership).filter(kind="text")
messages = unread_queryset(membership, base).order_by("room_seq")[:100]
```

Do not fetch a page and then discard read messages: sparse unread rows may lie
outside that page. `unread_queryset` itself deliberately applies no display
filter so maintenance can retain hidden unread identities, and binds its
deferred query to the primary. Eligibility helpers use the supplied membership
snapshot; refresh/revalidate it under a lock when concurrent membership or
maintenance changes affect correctness.

For in-memory delivery decisions, request bounded detail for the candidates:

```python
positions = recipient_positions(room, user_ids, message_ids=[m.pk for m in messages])
is_read = message_is_read(message, positions[user_id])
```

Those positions add `checked_message_ids` and
`unread_exception_message_ids`. Without sufficient detail for a below-prefix
message, `message_is_read` raises `ReadStateUnavailable` when exceptions
exist. It never assumes that an unknown sparse message is read. Reload
positions after a maintenance generation changes.

## Committed read hook and frames

```python
def on_read(room_id, user_id, position, changed):
    # Reconcile host notifications from actual ack facts and exact eligibility.
    pass
```

The hook runs on commit for successful explicit reads (including valid no-ops),
own-send advancement and trusted current-head clears. Before calling it, the
service fetches a fresh standing position and combines it with the captured
`ack_*` facts. Callbacks fetch current state rather than replaying a captured
count, including when queued callbacks execute in reverse order. A changed
maintenance generation invalidates the old callback entirely.
Callback/publication failures are logged without undoing durable state or
substituting zero unread. A failed fresh snapshot produces no read frame/hook.

The framework owns one `chat_read` room-topic frame per successful operation
for REST, WebSocket and own sends in cursor mode, for all room kinds. It
contains the position fields and actual acknowledgment facts;
`up_to_message_id` is `read_frontier_message_id`, which can be `null` while
sparse exceptions remain. Hosts should not emit a second generic read frame.
Hooks still own host notification/playback policy: use `ack_*` and
exception-aware eligibility when deciding which notifications the operation
covered. The hook position does not include bounded candidate exception IDs
unless the host requests them separately.

## Supported writers

Prefer `send_message` for validation, moderation, room policy and message
publication. Trusted `ChatMessage.objects.create` and new instance `save`
share atomic numbering and own-send advancement, but do not supply the send
service's content policy or generic new-message broadcast. Existing edits
keep their identity and do not allocate another number. WebSocket
`client_key` retries return the existing PK and durable sequence/generation;
they produce no duplicate message or sender advancement. Message frames,
send acks and history rows carry `generation` alongside `room_seq`, so a
sequence remains bound to its numbering version after maintenance.

Cursor mode rejects message `bulk_create` and room/sequence identity updates;
use the maintenance API for imports or relocation. Membership room/user
identity changes and bulk conflict-upserts are also rejected. Ordinary
membership `bulk_create` is supported and initializes all seats under ordered
room locks. Raw SQL and deliberately bypassing these model/queryset bridges
are outside the supported writer contract. Audit every API, worker, script,
file writer and membership bulk door before activation.

## Maintenance and reversible relocation

`mojo.apps.chat.services.maintenance` supplies these rare O(history) operations:

| Function | Contract |
|---|---|
| `snapshot_rooms(room_ids)` | Versioned JSON provenance: room generations, PK-to-room routing and membership context; no stale unread set |
| `relocate_messages(room_ids, message_rooms, *, arrival_message_ids=None)` | Move a `{message_pk: destination_room_pk}` mapping; return the pre-operation snapshot |
| `restore_snapshot(snapshot)` | Restore named message routing using current unread identities; unnamed messages stay where they are |
| `import_snapshot(snapshot)` | Validate v1, or adapt explicit v0 routing context; does not mutate read state |

Create/retain destination seats before relocation, and recreate the desired
source seats before restore, using the host's current role/status policy.
Remove obsolete seats only after the move returns. These APIs do not create
memberships or restore old roles, bans or join policy. A v0 import must supply
`room_ids`, `message_rooms` and `memberships`; missing cursor fields do not
authorize a reset. Incompatible snapshots fail before mutation. Route new
stragglers explicitly with `relocate_messages` if they are absent from the
snapshot.

When a straggler arrived after its source seats disappeared, the host may
explicitly designate it in `arrival_message_ids`. Every designated ID must be
live, mapped and inside the affected rooms. Each entitled destination seat
except the author gains that exact unread identity, unioned with its current
unread set. The author's other unread messages remain unread. No other
absent-source history is inferred unread. This optional arrival policy is
separate from preserving existing unread state.

Maintenance locks all rooms in PK order, then their seats, including banned
seats. It captures current extant unread IDs with join entitlement but without
display/TTL filtering, moves and resequences all affected history in message
PK order, increments each room's generation, and encodes the preserved set as
head prefix plus sparse exceptions. The final destination join bound is
applied before exceptions are created (channels remain unbounded); entitled
flagged/expired rows are retained. Actual acknowledgment identity/time is
retained only in the room that still contains that message. This preserves
merge → read → revert: reads since the snapshot remain read. Restoring old
unread flags/cursors would resurrect them and is unsupported.

Deleted rows have no live unread identity. Maintenance may reconcile their
phantom arrival slots, while preserving every extant hidden/expired/flagged
unread ID. This differs from ordinary deletion, which retains count slots.
After commit each affected room receives
`chat_read_state_invalidated {room_id, generation}`; consumers must discard
old positions and refresh. Publication failures log; the durable generation
remains authoritative.

## Activation and rollback

1. Apply additive chat migration `0005_chatunreadexception_and_more` while
   still using `legacy`. Old rows retain `room_seq=NULL`; do not backfill
   history or infer prior reads.
2. Upgrade/audit every writer and pause/drain old API, ASGI, worker and script
   processes. Mixed legacy/cursor writers are unsupported after activation.
3. Run `maintenance.activate_initial(room_ids=None)` with writers drained.
   This initial Option A reset touches rooms/seats only, marks old unread read
   once and returns the number of rooms activated. A durable generation marker
   makes repeating it harmless; new cursor rooms start at generation 1.
4. Enable the file-only cursor setting consistently across all processes,
   resume writers, and audit for unexpected new null sequences outside
   request paths. Initial activation must finish before cursor writers resume.

Before activation an application rollback can keep the additive schema and
legacy setting. After activation retain the cursor provider during application
rollback: receipts were not dual-written, so switching to legacy receipt counts
can resurrect stale unread. A downgrade requires a separate designed, drained
transition. No framework version or deployment is implied by this procedure.

## Local benchmark

After `bin/create_testproject`, with no concurrent test session in the checkout:

```bash
uv run python scripts/bench_chat_read_state.py --list-cells
uv run python scripts/bench_chat_read_state.py --duration 180 --output testproject/var/chat-matrix.json
uv run python scripts/bench_chat_read_state.py --cells h420000-burst20,site20,barrier32,sparse --duration 180
```

Omitting `--cells` runs the full history/traffic matrix. The script requires
the generated per-checkout local PostgreSQL test database and committed
framework/benchmark source for evidence. It records source hashes, fixture
identity, operation latency, offered/completed throughput, errors, queues,
database plans/waits, storage and invariants in JSON, and removes only its own
fixtures unless `--keep-fixtures` is supplied.

This measures instrumented in-process public-service processing, including
send policy and committed fresh-position fetches. It disables read/message
transport and the host read hook, and is not HTTP/WebSocket delivery or a soak
test. Bulk fixture construction is reported separately from application send
throughput. Use the emitted sample counts and limitations when interpreting
results; these commands alone make no performance claim.
