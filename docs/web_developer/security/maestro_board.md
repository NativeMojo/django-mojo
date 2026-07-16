# Maestro Boards — REST API

Push incident tickets into a remote maestro board and keep them in sync.
A maestro workspace admin mints a board link and gives you a paste URL
(`https://<maestro-host>/api/boards/link/<key>`); registering it here creates
a MaestroBoard, after which tickets can be pushed to the board and board-side
changes flow back onto the ticket as notes.

## Permissions

| Permission | Access |
|------------|--------|
| `manage_security` (or `security`) | Board CRUD, `refresh_schema`, `push_to_board`, link delete |
| `view_security` (or `security`) | List/read links |

There is no view-only tier for boards — the row holds a credential.

## Boards

### Register (create) a board

```
POST /api/incident/maestro/board
{"paste_url": "https://maestromojo.com/api/boards/link/<key>"}
```

The save validates the link against maestro synchronously — a bad or
unreachable link returns 400 and nothing is created. The paste must be
`https` with a public hostname (local/private hosts are rejected unless the
server sets the dev-only `MAESTRO_ALLOW_HTTP`). On success the response
carries the cached board `name`, `remote_board_id`, and `schema`
(`{"label": ..., "columns": [...]}`). The link key itself is stored encrypted
and never appears in any response.

Optional writable fields:

| Field | Meaning |
|-------|---------|
| `status_map` | `{"column": "<slug>", "map": {"<ticket status>": "<option value>"}}` — enables status sync both directions; without it, ticket status never changes from board activity |
| `sync_notes` | Mirror ticket notes as board comments (default `true`) |
| `group` | Restrict pushes to tickets of this group |
| `is_active` | Kill switch |

### Standard CRUD

```
GET  /api/incident/maestro/board            # list
GET  /api/incident/maestro/board/<id>
POST /api/incident/maestro/board/<id>       # update (re-paste a new link via paste_url)
DELETE /api/incident/maestro/board/<id>
```

### Refresh the cached schema

```
POST /api/incident/maestro/board/<id>
{"refresh_schema": 1}
```

Re-registers against maestro and returns the refreshed
`{"name", "remote_board_id", "schema"}`.

## Pushing a ticket

```
POST /api/incident/ticket/<id>
{"push_to_board": <board id>}
```

Queues an async push (the response is the normal ticket payload). The push is
idempotent — re-pushing updates the existing board item instead of creating a
duplicate. On first push a system note with the remote item URL is added to
the ticket. Errors: 400 unknown/inactive board, 403 when the board is
group-scoped and the ticket belongs to a different group.

After a ticket is linked:

- Edits to `title` / `description` / `status` sync to the board automatically
  (status only when `status_map` is configured).
- New ticket notes are mirrored as board comments (unless `sync_notes` is
  off). Notes originating from the board itself are never mirrored back.
- Board-side changes and comments arrive as system ticket notes
  (`user=null`, `metadata.origin="maestro"`).

Rule-driven auto-push: the rules engine's `ticket://` handler accepts
`board=<id>` — see the [security guide](README.md).

## Links

```
GET    /api/incident/maestro/link           # list (filter: ticket, maestro_board)
GET    /api/incident/maestro/link/<id>
DELETE /api/incident/maestro/link/<id>      # unlink — stops syncing, remote item stays
```

Link rows are read-only (`remote_item_id`, `remote_url`, `last_synced`);
they are created by pushing, never via REST.

## Webhook receiver (maestro → this project)

```
POST /api/incident/maestro/webhook/<callback_token>
```

Not for browser clients — this is the endpoint maestro calls. No session
auth; it is protected by the unguessable per-board token plus an
`X-Mojo-Signature` header (HMAC-SHA256 of the canonical JSON payload, keyed
by the raw link key). Invalid token/signature → 401. Events for items that
are no longer linked return `200 {"ignored": true}`.
