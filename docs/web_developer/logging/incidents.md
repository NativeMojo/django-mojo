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
| GET | `/api/incident/incident/history` | List incident history |
| GET | `/api/incident/event/ruleset` | List rule sets |
| GET | `/api/incident/event/ruleset/rule` | List rules |
| GET | `/api/incident/ticket` | List tickets |
| GET | `/api/incident/ticket/<id>` | Get ticket |
| POST | `/api/incident/ticket` | Create ticket |
| GET | `/api/incident/ticket/note` | List ticket notes |
| POST | `/api/incident/ticket/note` | Create ticket note |

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
      "group_id": 7,
      "metadata": {
        "username": "unknown_user",
        "group_id": 7,
        "group_name": "Acme Corp"
      }
    }
  ]
}
```

## Group Context on Events and Incidents

Both `Event` and `Incident` responses include a scalar `group_id` field (the originating group's primary key, or `null` when no group was associated). The full group object is intentionally **not** nested into the response — the security console must look up the group separately, gated by the requester's group permissions, to avoid cross-tenant leakage.

```json
"group_id": 7
```

The event's `metadata` carries a stable snapshot of the group at event-creation time, captured under the requester's permission context:

| Key | Description |
|---|---|
| `metadata.group_id` | Group PK at the time the event was created |
| `metadata.group_name` | Group name at the time the event was created |

These values persist even if the group is later renamed or deleted (the `group` FK becomes `null` on deletion, but `metadata` is preserved).

> **UI note**: `metadata.group_name` may contain user-controlled text. Always HTML-escape it when rendering in a console.

When an incident has aggregated events from more than one group, `metadata.group_mismatch` will be `true` on the incident and `incident.group_id` will be `null`. This flag is set once and never cleared — it is an audit marker, not a transient state.

---

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

### Protecting an incident from deletion

Some incidents are auto-deleted when resolved (controlled by the RuleSet) or pruned after 90 days by the `prune_incidents` job. To prevent either from happening, set `metadata.do_not_delete` on save:

```json
{
  "metadata": {"do_not_delete": true}
}
```

Use this for confirmed serious incidents — real intrusions, active data exfiltration, anything that needs long-term retention. When `do_not_delete` is `true`, the incident is never touched by automatic deletion regardless of its RuleSet configuration.

### Incident deleted on resolution

If an incident belongs to a RuleSet with `delete_on_resolution` enabled, the incident is automatically deleted when its status becomes `resolved` or `closed`. The POST response will still return the incident data as it existed at save time, but a subsequent GET on that incident ID will return 404. This is expected — the record was cleaned up. Do not treat a 404 after resolving as an error.

## Merge Incidents

**POST** `/api/incident/incident/<id>`

```json
{
  "merge": [302, 303]
}
```

Merges incidents 302 and 303 into incident `<id>`. The value is passed to `on_action_merge`. Events are re-linked, source incidents are deleted.

## Request LLM Analysis

**POST** `/api/incident/incident/<id>`

**Permission required:** `manage_security`

```json
{
  "analyze": 1
}
```

The value is passed to `on_action_analyze` — any truthy value works.

Triggers deep LLM analysis on the incident. The agent runs asynchronously — this call returns immediately.

**Successful response:**

```json
{"status": true}
```

**Error responses:**

| Condition | Response |
|-----------|----------|
| `LLM_HANDLER_API_KEY` not configured | `{"status": false, "error": "LLM_HANDLER_API_KEY not configured"}` |
| Analysis already running | `{"status": false, "error": "Analysis already in progress"}` |

**What the agent does:**
1. Sets incident to `investigating`
2. Reviews all events on the incident and related open incidents in the same category
3. Merges incidents that clearly represent the same underlying pattern
4. Proposes a new (disabled) RuleSet to auto-handle this pattern in the future
5. Resolves the merged incident with a summary note

**How to check progress:**

Poll `metadata.analysis_in_progress`:

```
GET /api/incident/incident/<id>
```

When `metadata.analysis_in_progress` is `false` and `metadata.llm_analysis` is present, the analysis is complete.

**Reading the result:**

The agent's summary is stored in `metadata.llm_analysis.summary` (up to 3000 characters). The full action trail is in `IncidentHistory` with `kind=handler:llm`.

**Example GET response after analysis completes:**

```
GET /api/incident/incident/<id>
```

```json
{
  "id": 301,
  "status": "resolved",
  "priority": 8,
  "category": "ossec",
  "title": "SSH brute force from 10.0.0.77",
  "source_ip": "10.0.0.77",
  "metadata": {
    "analysis_in_progress": false,
    "llm_analysis": {
      "summary": "Analysis complete. Merged 3 related incidents (#302, #303, #304) — all SSH brute force from different IPs. Proposed rule: 'Auto-block SSH brute force' (disabled, pending approval). The rule bundles by source_ip with a 30-minute window and blocks for 1 hour. Ticket #45 created for human review."
    },
    "llm_assessment": {
      "status": "resolved",
      "note": "Merged 3 incidents, proposed auto-block rule."
    }
  }
}
```

**Key metadata fields for the UI:**

| Field | Type | Description |
|-------|------|-------------|
| `metadata.analysis_in_progress` | `bool` | `true` while the agent is running. Poll until `false`. |
| `metadata.llm_analysis.summary` | `string` | The agent's final summary (up to 3000 chars). Present only after analysis completes. |
| `metadata.llm_assessment.status` | `string` | Final incident status set by the agent (`resolved`, `ignored`, `investigating`). |
| `metadata.llm_assessment.note` | `string` | Agent's reasoning for the status change. |

**History trail:**

```
GET /api/incident/incident/history?parent=<id>&sort=created
```

## Incident History

**GET** `/api/incident/incident/history?parent=301&sort=-created`

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

**POST** `/api/incident/ticket/note`

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
| `metadata.delete_on_resolution` | bool | When `true`, incidents created by this RuleSet are auto-deleted the moment they transition to `resolved` or `closed`. Intended for noise patterns (bot scanners, brute-force probes) where the incident has no long-term value. Overridden per-incident by `metadata.do_not_delete`. |

### `bundle_by` values

| Value | ID | Groups events by |
|---|---|---|
| `NONE` | 0 | Each event creates its own incident |
| `HOSTNAME` | 1 | Same server |
| `MODEL_NAME` | 2 | Same model type |
| `MODEL_NAME_AND_ID` | 3 | Same model instance |
| `SOURCE_IP` | 4 | Same source IP |
| `SOURCE_IP_AND_HOSTNAME` | 5 | Same IP + server |
| `SOURCE_IP_AND_MODEL_NAME` | 6 | Same IP + model type |
| `SOURCE_IP_AND_MODEL_NAME_AND_ID` | 7 | Same IP + model instance |
| `HOSTNAME_AND_MODEL_NAME` | 8 | Same server + model type |
| `HOSTNAME_AND_MODEL_NAME_AND_ID` | 9 | Same server + model instance |
| `GROUP_ID` | 10 | Same group |
| `GROUP_AND_MODEL_NAME` | 11 | Same group + model type |
| `GROUP_AND_MODEL_NAME_AND_ID` | 12 | Same group + model instance |
| `GROUP_AND_SOURCE_IP` | 13 | Same group + source IP |

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
| `group` | Events, Incidents | Group ID |
| `dr_start`, `dr_end` | All | Date range |
| `parent` | History, TicketNotes | Parent incident/ticket ID |
