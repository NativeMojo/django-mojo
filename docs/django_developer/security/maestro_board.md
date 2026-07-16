# Maestro Board Link — Pushing Tickets to a Remote Maestro Board

The incident app can push `Ticket` rows into a board on a remote maestro
instance and keep linked tickets in sync in both directions. This is the
**client half** of maestro's board link protocol: a maestro workspace admin
mints a board-scoped link key and hands you a paste URL; registering it here
creates a `MaestroBoard`, and tickets can then be pushed manually, on every
linked-ticket change, or automatically from the rules engine.

Design stance: **fail-closed setup, fail-open sync**. Registering a bad link
fails the save immediately; once linked, no maestro outage or error can ever
block or break a ticket save — remote calls run in jobs with a small retry,
then drop with a local log.

## Models

### `incident.MaestroBoard` (MojoSecrets)

One row per registered board link (`mojo/apps/incident/models/maestro_board.py`).

| Field | Meaning |
|-------|---------|
| `name` | Cached board name (from the register response) |
| `api_url` | Maestro API base, parsed from the paste URL |
| `remote_board_id` | The board's id on the maestro side |
| `schema` | Cached register response: `{"label": ..., "columns": [...]}` |
| `status_map` | Optional ticket-status ↔ board-column mapping (below) |
| `sync_notes` | Mirror ticket notes as board comments (default on) |
| `callback_token` | Unguessable path segment of this project's webhook URL |
| `group` | Optional — when set, only tickets of that group may push to it |
| `is_active` | Kill switch — inactive boards send and accept nothing |

The raw link key is stored encrypted via MojoSecrets (`get_secret("link_key")`)
and never serializes.

### `incident.MaestroBoardLink`

One row per pushed (ticket, board) pair: `ticket` FK (`ticket.board_links`),
`maestro_board` FK, `remote_item_id`, `remote_url`, `last_synced`. Created only
by the sync service — REST exposure is list + delete (= unlink).

## Registration

Registering = pasting the link URL into a board create:

```
POST /api/incident/maestro/board
{"paste_url": "https://maestromojo.com/api/boards/link/<key>"}
```

`set_paste_url()` parses the URL into `api_url` + secret key;
`on_rest_pre_save` then calls maestro's `link/register` synchronously,
submitting this project's callback URL and caching the returned board
name/id/schema. Any failure raises a 400 and nothing persists.

The callback URL is built from settings:

| Setting | Meaning |
|---------|---------|
| `MAESTRO_CALLBACK_BASE` | Preferred public base URL for the webhook receiver |
| `BASE_URL` | Fallback when `MAESTRO_CALLBACK_BASE` is unset |
| `MAESTRO_LINK_TIMEOUT` | Outbound HTTP timeout in seconds (default 10) |
| `MAESTRO_ALLOW_HTTP` | Dev-only: allow http:// pastes and local hosts (default off) |

One of the two base URLs must be configured or registration fails with a
clear 400. The webhook path is
`/api/incident/maestro/webhook/<callback_token>`.

Because the server POSTs the link key to the pasted host, the paste is an
SSRF/key-leak surface: `https` is required and loopback/private/link-local
IP-literal hosts are rejected. Set `MAESTRO_ALLOW_HTTP=true` only for local
development against a dev maestro instance.

## Pushing tickets

Three triggers, one idempotent service call
(`services/maestro_sync.py:push_ticket`) — an existing link updates the remote
item instead of creating a duplicate:

1. **Manual action** — `POST /api/incident/ticket/<pk>` with
   `{"push_to_board": <board id>}`. Validates the board is active and
   group-compatible (fail-closed), then enqueues the push.
2. **Linked-ticket changes** — REST edits to `title`, `description`, or
   `status` on a ticket with links enqueue an update per linked board; new
   REST-created notes enqueue a board comment (unless the board's
   `sync_notes` is off).
3. **Rules engine** — the existing `ticket://` handler accepts an optional
   `board=<id>` param: `ticket://?priority=9&board=3` creates the ticket and
   pushes it.

The first successful push writes a system ticket note containing the remote
item URL.

All pushes run as jobs on the `incident_handlers` channel
(`asyncjobs.maestro_push_ticket` / `maestro_sync_change` / `maestro_push_note`)
with `max_retries=3`, exponential backoff. Retriable failures (timeouts, 5xx)
re-raise so the engine retries; terminal failures (4xx — revoked key,
validation) drop immediately with a log. Nothing propagates to the caller.

## status_map — syncing ticket status

Maestro boards have no fixed status field — "status" is whatever category
column the board defines. Ticket `status` therefore syncs **only** when the
board's `status_map` is configured:

```json
{
  "column": "state",
  "map": {"open": "todo", "closed": "done"}
}
```

Outbound, `map` translates ticket status → column option value; inbound, the
reverse mapping applies a board column change to `ticket.status`
(compare-before-write). A status/option with no mapping is skipped with a log
— never an error.

## Inbound webhooks

`POST /api/incident/maestro/webhook/<callback_token>`
(`rest/maestro_webhook.py`) — public endpoint, fail-closed:

- board looked up by its unguessable `callback_token`, must be `is_active`
  and must hold a link key (a keyless board 401s rather than falling back to
  any other signing secret)
- `X-Mojo-Signature` must verify: HMAC-SHA256 of the canonical JSON payload
  dict keyed by the raw link key (`mojo.helpers.crypto.sign.verify_signature`
  on the **parsed dict**, not raw bytes)
- rejections are 401 — terminal to maestro's retry queue
- IP rate-limited (`rate_limit("maestro_webhook", ip_limit=60)`), matching
  the app's other public receivers
- `note.created` deliveries dedup on the board-side note id — a replayed or
  retried payload never duplicates a ticket note. (The signed payload carries
  no timestamp/nonce yet, so a captured `item.updated` can still be replayed
  to re-apply a stale status until the maestro contract adds one;
  compare-before-write bounds the effect.)

Verified events (`item.updated`, `item.archived`, `item.restored`,
`note.created`) always produce a system ticket note (`user=None`,
`metadata.origin="maestro"`); `item.updated` additionally applies a
`status_map` column change to the ticket status. Webhooks for unlinked items
return 200 `{"ignored": true}`.

## Echo suppression

A synced change must not bounce back:

- **Maestro-side**: writes arriving via the link API never dispatch a webhook
  back to the same link.
- **Client-side**: the webhook handler writes tickets/notes via direct ORM
  saves, which never enter the REST hook pipeline — so they cannot re-trigger
  the outbound sync hooks in `Ticket.on_rest_saved` /
  `TicketNote.on_rest_saved`. Notes carrying `metadata.origin="maestro"` are
  additionally excluded from note mirroring.

## Wire contract

All request/response shapes live in one file —
`mojo/apps/incident/services/maestro_sync.py` — versioned `{"v": 1}` payloads,
`Authorization: linkkey <key>` auth. If maestro's contract drifts, that file
is the only thing to fix. Contract source: maestro repo
`docs/web_developer/boards/linking.md` (plan-of-record:
`planning/confirmed/maestro-connect.md`).

## Permissions

Board CRUD and the `push_to_board` action require `manage_security` (or the
combined `security`) — the board row holds a credential, so there is no
view-only tier. `MaestroBoardLink` rows are readable with `view_security`,
deletable with `manage_security`. The webhook receiver is public by design
and protected by token + signature.
