# Maestro Workspace Reporting

The incident app can report either an `Incident` or a local `Ticket` to one
central Maestro workspace. This is a many-projects-to-one-workspace design:
each django-mojo deployment has one reporting ApiKey, while Maestro owns the
workspace, default board, board validation and cross-board triage.

The deployment connection is static configuration, not a database model. Only
the per-source remote item association is persisted locally.

## Configuration

Set the reporting key in `var/django.conf`:

```python
MAESTRO_API_KEY = "<workspace integration reporting key>"

# Optional; this is the default.
MAESTRO_API_URL = "https://maestromojo.com"
```

The client reads both through `settings.get_static()`, so a database `Setting`
row cannot replace the deployment credential. The key is sent only as:

```text
Authorization: apikey <token>
```

`MAESTRO_API_URL` must be an HTTPS public origin. `MAESTRO_ALLOW_HTTP=True`
relaxes that rule only for local development. Existing `BASE_URL` (or
`MAESTRO_CALLBACK_BASE`) supplies this deployment's public callback origin.

After installation or an in-place key rotation, validate and register the
callback:

```bash
python manage.py register_maestro
```

The command prints the safe integration/workspace/default-board identity and
never the key. It refuses a key belonging to a different stable integration
when local item links already exist. No network call occurs at Django import or
ordinary process startup.

`django.conf` is loaded once per process. Rotation therefore requires a
coordinated pause/drain, config update on every node, worker/web restart,
registration, token activation and resume. Do not roll a new key through only
some processes: old workers will fail new callback signatures and new workers
will receive terminal authorization failures for old work.

## Routing

`board` always means a remote Maestro board id in the configured ApiKey's
workspace. It is never a local model primary key.

- Omit `board` to use the integration's server-side default board.
- Supply `board=3` to request Maestro board 3.
- Maestro rejects missing/inactive defaults and inactive or cross-workspace
  overrides. django-mojo never guesses a replacement.
- After creation, updates and comments address the remote item id, so a human
  can move the item between boards inside the workspace without severing sync.

## Reporting modes

### Incident only

Use the rules handler when Maestro should be the workflow record and no local
Ticket is needed:

```text
maestro://
maestro://?board=3
```

The handler reports its associated Incident idempotently. A directly linked
Incident is retained through resolution cleanup and age pruning, cannot be
deleted until explicitly unlinked, and transfers its link through an
unambiguous Incident merge.

### Local Ticket plus Maestro

Plain `ticket://` remains local-only. Opt into Maestro explicitly:

```text
ticket://?priority=8&maestro=1
ticket://?priority=8&board=3
```

`maestro=1` uses the default; `board=3` both opts in and selects the remote
board. Rule-created Tickets inherit the Incident group. Existing unresolved
Ticket dedupe is group-scoped; a recurring Incident reuses and pushes the
eligible Ticket while recording the occurrence without reparenting it.

An existing Ticket can be pushed later through its RestMeta action:

```json
{"push_to_maestro": true}
{"push_to_maestro": 3}
```

JSON `true` means the default board. A strict positive integer means that
remote board. Booleans are handled before integers so `true` can never become
board id 1.

## Local model

`incident.MaestroItemLink` stores one association for exactly one Ticket or
Incident:

| Field | Meaning |
|---|---|
| `ticket` / `incident` | Exactly one local source (`PROTECT` on deletion) |
| `remote_integration_id` | Stable, non-secret Maestro integration identity |
| `remote_item_id` | Item id inside that integration |
| `remote_board_id` | Current/resolved Maestro board |
| `remote_url` | Current item URL |
| `last_synced` | Last successful outbound/location sync |

Constraints enforce one link per Ticket, one per Incident, and unique
`(remote_integration_id, remote_item_id)`. Links are created internally and are
REST read/delete only; deleting a link stops sync but leaves the Maestro item.

The older `MaestroBoard` and `MaestroBoardLink` tables remain readable legacy
records. Their board-scoped credentials are not assumed to be workspace keys
and their setup endpoints are read-only. Adoption requires an explicit
Maestro-confirmed ownership/rekey operation; schema migration never guesses or
deletes encrypted credentials.

## Synchronization

`services/maestro_sync.py` is the wire-contract boundary. Source creates carry
project, kind (`ticket` or `incident`), local id, backlink, title/details,
priority and canonical lifecycle. An optional board is sent only on first
create; Maestro returns the stable integration id and resolved board.

Linked changes and notes/history publish jobs on `incident_handlers`:

- `asyncjobs.maestro_push_source`
- `asyncjobs.maestro_sync_change`
- `asyncjobs.maestro_push_note`

Timeouts and 5xx responses retry up to the configured job bound. 4xx responses
are terminal and logged without breaking a local save. Remote bodies are
not logged or returned to local users.

Lifecycle is board-independent. django-mojo sends `done` for local
resolved/closed, `parked` for paused/ignored, and `active` otherwise. Maestro
translates its board's workflow roles back to those values; callbacks map them
to local resolved, paused and open states.

## Callbacks

Maestro calls the fixed public endpoint:

```text
POST /api/incident/maestro/webhook
```

The endpoint is rate-limited, caps JSON size and requires
`X-Mojo-Signature`, verified over the parsed payload with
`MAESTRO_API_KEY`. The signed body carries the stable integration id; lookup is
by `(integration_id, remote_item_id)`, preventing item-id collisions across
integrations.

Remote comments become `TicketNote` or `IncidentHistory` entries. Their
metadata records `origin="maestro"` and the remote note id for replay dedupe.
Callback-applied lifecycle saves use direct ORM writes, and Maestro-origin
notes/history never enqueue outbound work, preventing echoes.

## Settings

| Setting | Meaning |
|---|---|
| `MAESTRO_API_KEY` | Required deployment reporting ApiKey; secret/static |
| `MAESTRO_API_URL` | Maestro origin; default `https://maestromojo.com` |
| `MAESTRO_CALLBACK_BASE` | Optional callback origin; falls back to `BASE_URL` |
| `MAESTRO_LINK_TIMEOUT` | Outbound timeout in seconds; default 10 |
| `MAESTRO_ALLOW_HTTP` | Development-only HTTP/private-host relaxation |

The server half of this contract is Maestro item 32. Do not enable one side
against an unpinned incompatible version.

## CloudWatch alarm routing

CloudWatch SNS ingestion never calls Maestro directly. Configure an explicit
`aws:cloudwatch` RuleSet with `ticket://?board=<id>` to create a local Ticket;
the normal Ticket -> `MaestroItemLink` job path then creates or updates the
remote item. Alarm recovery resolves the machine Incident and synchronizes a
recovery note, but leaves Ticket/board closure to the human workflow. See
[CloudWatch SNS alarm ingestion](../aws/cloudwatch.md#sns-alarm-ingestion).
