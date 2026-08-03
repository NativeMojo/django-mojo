# Maestro Reporting — REST and Rule API

A django-mojo deployment has one reporting connection to one Maestro workspace.
The secret lives in server `django.conf`; browser clients never create, select
or read a connection credential.

Maestro owns the integration's default board. Wherever this API accepts
`board`, it is a remote Maestro board id inside the configured workspace.

## Ticket action

Users with Ticket save permission (`manage_security` or `security`) can push an
existing Ticket:

```http
POST /api/incident/ticket/42
Content-Type: application/json

{"push_to_maestro": true}
```

`true` uses Maestro's default board. To select a remote board:

```json
{"push_to_maestro": 3}
```

The response is the ordinary Ticket response. Reporting happens in a job and
is idempotent: a Ticket already linked to Maestro updates the same item rather
than creating another. A missing deployment key or malformed selector returns
400 before enqueueing.

## Rules handlers

Report an Incident directly, without a Ticket:

```text
maestro://
maestro://?board=3
```

Create/reuse a local Ticket and also report it:

```text
ticket://?priority=8&maestro=1
ticket://?priority=8&board=3
```

Plain `ticket://?priority=8` stays local-only. `maestro=1` selects the server
default; presence of `board` opts into Maestro and selects that remote board.

## Item links

Links are read-only except for explicit unlink:

```http
GET    /api/incident/maestro/item-link
GET    /api/incident/maestro/item-link/17
DELETE /api/incident/maestro/item-link/17
```

Reading requires `view_security` or `security`; unlinking requires
`manage_security`. Each row exposes its Ticket-or-Incident source, stable
integration id, remote item/board ids, URL and last-sync timestamp. Deleting it
stops future sync and leaves the remote Maestro item intact.

The old `/api/incident/maestro/board` and `/api/incident/maestro/link` surfaces
are legacy read-only records. New setup is deployment configuration, not REST.

## Ongoing updates

After linking:

- Ticket title, description, status and priority changes sync to Maestro.
- New Ticket notes sync as comments.
- Incident title, details, category, status and priority changes sync.
- Incident history entries sync as comments.
- Maestro comments return as Ticket notes or Incident history.
- Maestro lifecycle `active`, `done`, and `parked` maps to local `open`,
  `resolved`, and `paused`.
- A same-workspace Maestro board move updates the cached board and sync
  continues.

Outbound failures never fail a local save. Timeouts/5xx retry; terminal 4xx
responses are dropped with an operator log.

## Callback receiver

```http
POST /api/incident/maestro/webhook
X-Mojo-Signature: <HMAC-SHA256 signature>
```

This endpoint is for Maestro, not browser callers. It is public but
rate-limited and verifies the bounded JSON payload using the deployment
`MAESTRO_API_KEY`. The signed payload must include `integration_id` and
`item.id`; lookup uses both. Invalid/missing signatures return 401 and unknown
links return 200 with `ignored=true`.

Remote note ids are deduplicated, and Maestro-origin changes are excluded from
outbound hooks to prevent loops.
