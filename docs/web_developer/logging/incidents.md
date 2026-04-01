# Incident API — REST API Reference

## Permissions Required

- `view_security` — read-only access to incidents, events, history, tickets
- `manage_security` — create, edit, delete, merge incidents, manage tickets and rules

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/incident/event` | List security events |
| GET | `/api/incident/event/<id>` | Get event |
| GET | `/api/incident/incident` | List incidents |
| GET | `/api/incident/incident/<id>` | Get incident |
| POST | `/api/incident/incident/<id>` | Update incident |
| GET | `/api/incident/incidenthistory` | List incident history |
| GET | `/api/incident/event/ruleset` | List rule sets |
| GET | `/api/incident/event/ruleset/rule` | List rules |
| GET | `/api/incident/ticket` | List tickets |
| GET | `/api/incident/ticket/<id>` | Get ticket |
| POST | `/api/incident/ticket` | Create ticket |
| GET | `/api/incident/ticketnote` | List ticket notes |
| POST | `/api/incident/ticketnote` | Create ticket note |

## List Incidents

**GET** `/api/incident/incident`

```
GET /api/incident/incident?status=new&sort=-created&size=20
```

**Response:**

```json
{
  "status": true,
  "count": 5,
  "start": 0,
  "size": 20,
  "data": [
    {
      "id": 301,
      "created": "2026-03-27T10:00:00Z",
      "state": 0,
      "status": "new",
      "priority": 5,
      "category": "auth:failed",
      "scope": "account",
      "source_ip": "1.2.3.4",
      "hostname": "web-01",
      "title": "Failed login attempts",
      "metadata": {
        "username": "unknown_user"
      }
    }
  ]
}
```

## Incident Status Lifecycle

Incidents move through these statuses:

| Status | Set by | Meaning |
|--------|--------|---------|
| `pending` | System | Below threshold, accumulating events |
| `new` | System | Unhandled — neither human nor LLM has touched it |
| `investigating` | LLM | LLM agent is actively triaging |
| `resolved` | LLM or Human | Real threat, handled (blocked, notified, etc.) |
| `ignored` | LLM or Human | Noise / false positive |
| `open` | Human | Human has taken ownership |
| `paused` | Human | Human paused investigation |
| `closed` | Human | Final — done, archived |

**For your dashboard:**
- Show `status=new` as the "unhandled" queue
- Show `status=open` as the "human work" queue
- If the LLM agent is configured, most incidents move from `new` to `investigating` to `resolved`/`ignored` automatically

## Update Incident

**POST** `/api/incident/incident/<id>`

```json
{
  "status": "open"
}
```

## Merge Incidents

**POST** `/api/incident/incident/<id>`

```json
{
  "action": "merge",
  "value": [302, 303]
}
```

Merges incidents 302 and 303 into incident `<id>`. Events are re-linked, source incidents are deleted.

## Incident History

**GET** `/api/incident/incidenthistory?parent=301&sort=-created`

Returns the audit trail for an incident — every state change, handler execution, LLM assessment, and admin edit.

```json
{
  "data": [
    {
      "id": 1,
      "created": "2026-03-27T10:00:01Z",
      "kind": "created",
      "note": "Incident created from event (category: auth:failed, level: 5, rule: brute_force)",
      "state": 0,
      "priority": 5,
      "by": null
    },
    {
      "id": 2,
      "created": "2026-03-27T10:00:02Z",
      "kind": "handler:llm",
      "note": "[LLM Agent] Triage complete: noise — single failed login from known IP",
      "state": 0,
      "priority": 5,
      "by": null
    }
  ]
}
```

**History `kind` values:**

| Kind | Meaning |
|------|---------|
| `created` | Incident created from event |
| `priority_escalated` | Priority increased by new event |
| `status_changed` | Status transition |
| `threshold_reached` | Pending → new (trigger_count met) |
| `handler:block` | Block handler fired |
| `handler:email` | Email handler fired |
| `handler:sms` | SMS handler fired |
| `handler:notify` | Notification handler fired |
| `handler:llm` | LLM agent action |
| `merged` | Incidents merged |
| `updated` | Admin edited fields |

## List Events

**GET** `/api/incident/event`

```
GET /api/incident/event?category=auth:failed&sort=-created&size=50
```

Events with `metadata.dedup_count > 1` represent multiple identical events that were deduplicated. Check this field to get true event volume.

## Tickets

Tickets are the bridge between automated systems and human operators.

**GET** `/api/incident/ticket?status=open&sort=-priority`

```json
{
  "data": [
    {
      "id": 10,
      "title": "[Rule Proposal] Block repeated SSH from unknown IPs",
      "status": "open",
      "priority": 5,
      "category": "llm_review",
      "incident": { "id": 301 },
      "metadata": { "llm_linked": true }
    }
  ]
}
```

Tickets with `metadata.llm_linked=true` are managed by the LLM agent. When you post a note to these tickets, the LLM is automatically re-invoked to continue the conversation.

**POST** `/api/incident/ticketnote`

```json
{
  "parent": 10,
  "note": "Approved. Enable the rule."
}
```

## RuleSet Fields

When reading or writing rules via `/api/incident/event/ruleset`, these fields control threshold and retrigger behavior:

| Field | Type | Description |
|---|---|---|
| `trigger_count` | int or null | Fire the handler when the incident reaches this many events. `null` = fire immediately on the first event. |
| `trigger_window` | int or null | Only count events within this many minutes when evaluating `trigger_count`. `null` = count all events on the incident. |
| `retrigger_every` | int or null | Re-fire the handler every N additional events after the initial trigger. `null` = fire once only. |

**Example — block after 10 failed logins in 10 minutes, re-alert every 20 more:**

```
POST /api/incident/event/ruleset
{
  "category": "auth:failed",
  "name": "Brute Force Detection",
  "bundle_by": 4,
  "bundle_minutes": 10,
  "handler": "block://?ttl=3600,notify://perm@manage_security",
  "trigger_count": 10,
  "trigger_window": 10,
  "retrigger_every": 20
}
```

Incidents stay at `pending` until `trigger_count` is reached, then transition to `new` and the handler fires. With `retrigger_every=20`, the handler fires again at 30 events, 50, 70, and so on.

## Filtering

| Filter | Applies to | Description |
|---|---|---|
| `status` | Incidents, Tickets | Status string |
| `priority` | Incidents, Tickets | Priority level (0-15) |
| `category` | Events, Incidents, Tickets | Category string |
| `scope` | Events, Incidents | App scope (e.g., `account`, `system`) |
| `source_ip` | Events, Incidents | Source IP address |
| `hostname` | Events, Incidents | Server hostname |
| `dr_start`, `dr_end` | All | Date range |
| `parent` | History, TicketNotes | Parent incident/ticket ID |
