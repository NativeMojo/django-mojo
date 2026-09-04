# System Security — Web Developer Guide

How to build a security operations dashboard using django-mojo's APIs. This guide ties together incidents, events, firewall, bouncer, logs, and metrics into a single picture.

## Permissions

Two permissions control all security-related access:

| Permission | Access |
|------------|--------|
| `view_security` | Read-only: incidents, events, history, tickets, rules, firewall status |
| `manage_security` | Full: edit incidents, manage tickets, create rules, block IPs, merge incidents |

Both permissions can be granted **globally** (platform-wide) or as a
**GroupMember-scoped** grant on a single group (assignable by that group's
own admin). Models that carry a `group` field — events, incidents, tickets,
and incident history — automatically confine a group-scoped grant to that
group's rows (the framework's group-scoped list fallback), so a member
`view_security` grant reads only their own group's events/incidents/tickets.
RuleSets, IPSets, and platform-wide summaries (e.g. Health Summary below)
have no group field and require a **global** grant.

**A row with no tenant needs a global grant, and passing `?group=` will not
help.** Whenever you fetch a single record, the server binds the permission
check to *that row's* owning tenant, not to the `?group=` / `?group_uuid=` you
sent. If the row belongs to no tenant — a platform-level record, or one whose
tenant link is null — the check falls through to your **global** permissions
only, and a GroupMember-scoped grant is refused with a `403`. Sending
`?group=<a group you belong to>` has never widened access and now cannot appear
to: it is discarded before the check. List endpoints are unaffected — they have
always filtered to your own groups, and tenant-less rows never appeared in them.
If a detail fetch that used to return `200` now returns `403`, the record has no
tenant and the fix is a global grant, not a different `?group=`.

## The Security Pipeline

```
Detection → Event → Rules → Incident → Handlers → Enforcement
```

1. **Detection** — failed logins, rate limits, OSSEC alerts, bouncer blocks, app errors
2. **Events** — every detection creates an Event record with category, level, and metadata
3. **Rules** — RuleSets match events by category and apply threshold/bundling logic
4. **Incidents** — matched events are grouped into Incidents for investigation
5. **Handlers** — governed rules fire allowlisted actions: block IPs, notify, create tickets, or resolve incidents
6. **Enforcement** — IP blocks propagate fleet-wide via iptables/ipset

## APIs at a Glance

| API | Path | What it provides |
|-----|------|-----------------|
| Incidents | `/api/incident/incident` | Security incidents with status, priority, category |
| Events | `/api/incident/event` | Raw security events that feed into incidents — includes the OAuth redirect-allowlist categories `auth:oauth_redirect_refused`, `auth:redirect_allowlist_unusable_entry`, and `auth:redirect_allowlist_tenant_entry_unusable` (Redis-suppressed, so at most one per host/source/group per hour) |
| History | `/api/incident/incident/history` | Audit trail for each incident |
| Health Summary | `/api/incident/health/summary` | Latest event per `system:health:*` category — one row per subsystem |
| Tickets | `/api/incident/ticket` | Human review items, LLM conversation threads |
| Ticket Notes | `/api/incident/ticket/note` | Ticket conversation (human + LLM) |
| RuleSets | `/api/incident/event/ruleset` | Read-only compatibility view of RuleSet summaries |
| Rules | `/api/incident/event/ruleset/rule` | Read-only compatibility view of Rule conditions |
| GeoIP | `/api/system/geoip` | IP records, block status, threat level, geolocation |
| Logs | `/api/logs` | Audit logs, firewall history |
| Metrics | `/api/metrics/fetch` | Time-series data for dashboards |
| Bouncer (client) | `/api/account/bouncer/assess` | Bot detection (called by bouncer JS, not your app) |
| Bouncer Devices | `/api/account/bouncer/device` | Device reputation, risk tiers, block counts |
| Bouncer Signals | `/api/account/bouncer/signal` | Assessment audit trail with full signal payloads |
| Bot Signatures | `/api/account/bouncer/signature` | Manage bot signatures (auto-learned + manual) |
| IPSet | `/api/incident/ipset` | Bulk CIDR blocking: countries, datacenters, abuse lists |
| Maestro Item Links | `/api/incident/maestro/item-link` | Remote Maestro items linked to local Tickets or Incidents |
| Admin Security | `/api/incident/admin/security` | Versioned, bounded and redacted operational sections plus the typed policy schema |
| Admin Security Actions | `/api/incident/admin/security/action` | Fresh-auth, version-bound RuleSet and recommendation actions |
| IPSet Actions | `/api/incident/ipset/action` | Fresh-auth, revision-bound enable/disable/sync actions with checked fleet results |

See individual API docs for full details:
- [MojoSec Sensor Ingestion](mojosec.md) — per-installation authentication,
  strict batch contract, acknowledgement semantics, and central-policy boundary
- [Rate Limits & Client Backoff](rate_limits.md) — the 429/`Retry-After` contract every client must honor
- [Maestro Reporting](maestro_board.md) — deployment-configured workspace reporting, Ticket/Incident actions, item links and signed callbacks
- [Incidents](../logging/incidents.md)
- [Events & Reporting](../logging/reporting_events.md)
- [Firewall & GeoIP](../account/firewall.md)
- [Logs](../logging/logs.md)
- [Metrics](../metrics/metrics.md)
- [Bouncer](../account/bouncer.md)
- [GeoIP](../account/geoip.md)

### Admin Security client contract

Use the Admin Security endpoints for policy-management UI. They require global
human grants; an API key or group membership is never sufficient. Reads accept
global `view_security`, `manage_security`, or `security`. Writes accept global
`manage_security` or `security` and require authentication within 600 seconds.

#### Read

`GET /api/incident/admin/security` accepts:

| Parameter | Meaning |
|---|---|
| `sections` or `section` | A comma-separated string or array drawn from `overview`, `cases`, `incidents`, `events`, `rules`, `ipsets`, `recommendations`, and `schemas`; omitted means all sections |
| `limit` | Rows per list section; default 50, maximum 100 |
| `window_hours` | Window for time-bound sections; default 24, maximum 2160 (90 days) |
| `recommendation_id` | Adds the bounded target projection to the `recommendations` section for one recommendation |

The standard response envelope contains a versioned map. Every requested
section completes independently:

```json
{
  "status": true,
  "code": 200,
  "data": {
    "schema_version": 1,
    "sections": {
      "rules": {
        "status": "available",
        "observed_at": "2026-09-04T17:18:19.123456+00:00",
        "cutoff": "2026-09-04T17:18:19.123456+00:00",
        "window": {
          "hours": 24,
          "start": "2026-09-03T17:18:19.123456+00:00",
          "end": "2026-09-04T17:18:19.123456+00:00"
        },
        "truncated": false,
        "data": []
      }
    }
  }
}
```

Render each section's own `status`, `observed_at`, `cutoff`, `window`, and
`truncated` fields. A failed collector returns `status: "unavailable"`,
`reason: "collector_unavailable"`, and empty `data`; do not turn that into a
zero. Only metrics explicitly labelled exact are suitable for exact totals;
case and learning projections identify themselves as sampled.

The `rules` section is a summary list: it includes the aggregate revision,
configuration, validation status, `rule_count`, and—for a valid policy—the
safe typed handlers under `validation.handlers`; it does not inline child
rules. Successful `ruleset.create` and `ruleset.replace` action responses
include their validated typed `rules`, top-level `handlers`, and
`delete_on_resolution`. Do not treat the generic RuleSet read as an editable
aggregate: its raw handler field is deliberately omitted.

To review a recommendation, request
`?sections=recommendations&recommendation_id=<id>`. That bounded detail is the
only Admin Security read that returns target IPs; it intentionally omits
execution errors and prior block reasons. Bind the returned `modified` revision
and exact target set into the operator's confirmation. The detail list is
bounded to 1024 targets and reports `targets_truncated`; never offer an action
when it is true.

`sections=schemas` returns the server-owned `rule_policy.aggregate` object
contract, condition fields/types/operators, bundling choices, typed handler
arguments, caps, and the action roster. Build editors from that response;
never send raw handler URLs.

#### Write

For a write, first read the row, retain its `modified` value, then post the
selected action with `expected_modified` and the exact confirmation string. A
409 means the object changed and the UI must reload/review rather than retry
blindly. Rule edits are complete inactive replacements; activation is another
action, with a second confirmation for catch-all policies. Do not send raw
handler URLs. The action-specific fields and confirmation strings are:

| Action | Required request fields | `confirm` value |
|---|---|---|
| `ruleset.create` | `ruleset` | `CREATE RULESET` |
| `ruleset.replace` | `ruleset_id`, `expected_modified`, complete inactive `ruleset` | `REPLACE RULESET <id>` |
| `ruleset.activate` | `ruleset_id`, `expected_modified`; `confirm_catch_all` when applicable | `ACTIVATE RULESET <id>` |
| `ruleset.deactivate` | `ruleset_id`, `expected_modified` | `DEACTIVATE RULESET <id>` |
| `ruleset.delete` | `ruleset_id`, `expected_modified` | `DELETE RULESET <id>` |
| `recommendation.approve`, `.reject`, `.cancel`, `.reverse` | `recommendation_id`, `expected_modified`, optional `note` (maximum 256 characters) | `<VERB> RECOMMENDATION <id>` |
| `ipset.enable`, `.disable`, `.sync` | `ipset_id`, `expected_modified` | `<VERB> IPSET <id>` |

A catch-all activation additionally requires
`confirm_catch_all: "ACTIVATE CATCH-ALL RULESET <id>"`. Unknown fields are
rejected. Create always stores the policy inactive; replace requires
`ruleset.is_active: false`.

```json
{
  "action": "ruleset.create",
  "confirm": "CREATE RULESET",
  "ruleset": {
    "name": "High-severity authentication failures",
    "category": "auth:failed",
    "priority": 20,
    "bundle_minutes": 30,
    "bundle_by": 4,
    "bundle_by_rule_set": true,
    "match_by": 0,
    "trigger_count": 10,
    "trigger_window": 30,
    "retrigger_every": null,
    "handlers": [
      {"type": "notify", "permission": "manage_security"}
    ],
    "rules": [
      {"name": "High severity", "field": "level", "operator": ">=", "value": 8}
    ],
    "delete_on_resolution": false,
    "is_active": false
  }
}
```

Success returns the action and its safe object projection:

```json
{
  "status": true,
  "code": 200,
  "data": {
    "schema_version": 1,
    "action": "ruleset.create",
    "data": {
      "id": 42,
      "modified": "2026-09-04T17:18:19.123456+00:00",
      "is_active": false,
      "handlers": [
        {"type": "notify", "permission": "manage_security"}
      ],
      "rules": [
        {"name": "High severity", "field": "level", "operator": ">=", "value": "8", "value_type": "int", "is_required": false}
      ]
    }
  }
}
```

| HTTP/body `code` | Meaning |
|---|---|
| 400 | Unknown action/field, invalid typed policy, bad ID/note, or missing typed confirmation |
| 403 | The caller lacks a qualifying global human grant or is key-backed |
| 404 | The named RuleSet, recommendation, or IPSet does not exist |
| 409 | Stale revision, invalid recommendation state/scope, or a legacy RuleSet that must be replaced before activation |
| 440 | Reauthentication is required; refreshing the token does not update its authentication time |

The older RuleSet/Rule URLs are read-only compatibility surfaces. IPSet
metadata/CIDR writes remain on the generic model URL; lifecycle changes and
deletion are closed there.

## Building a Security Dashboard

### 1. Incident Queue

The main view. Show incidents that need attention:

```
GET /api/incident/incident?status=new&sort=-priority,-created&size=50
```

This returns incidents that haven't been handled by a human or the LLM agent.

If the LLM agent is configured, most incidents flow through automatically:
- `new` → LLM picks it up → `investigating` → `resolved` or `ignored`
- Humans only see `status=open` (things the LLM escalated or humans claimed)

**Recommended tabs:**

| Tab | Filter | Purpose |
|-----|--------|---------|
| Unhandled | `status=new` | Needs attention (human or LLM) |
| My Work | `status=open` | Human-owned incidents |
| LLM Active | `status=investigating` | LLM is working on these |
| Resolved | `status=resolved` | Recently resolved |
| Ignored | `status=ignored` | Noise (review periodically) |

### 2. Incident Detail

For a single incident, fetch the incident + its history + its events:

```
GET /api/incident/incident/301
GET /api/incident/incident/history?parent=301&sort=created
GET /api/incident/event?incident=301&sort=-created
```

The history shows the full timeline: creation, handler execution, LLM assessments, admin edits, merges.

### 3. Health Summary Strip

A single endpoint returns the most recent event for each `system:health:*` category. Use it to render a row of per-subsystem health indicators without making N separate queries.

**Permission:** `view_security` / `security` — held as a **global** grant;
gated with `@md.requires_global_perms`, so a group/member-scoped grant does
not authorize this endpoint.

```
GET /api/incident/health/summary
```

**Response:**

```json
{
  "status": true,
  "data": [
    {
      "category": "system:health:cpu",
      "level": 8,
      "last_seen": "2026-04-26T14:55:00",
      "title": "CPU threshold exceeded",
      "details": "CPU at 94% on web-03",
      "hostname": "web-03",
      "source_ip": null,
      "incident_id": 142
    },
    {
      "category": "system:health:runner",
      "level": 10,
      "last_seen": "2026-04-26T14:40:00",
      "title": "Dead job runner detected",
      "details": "Runner on worker-02 not responding",
      "hostname": "worker-02",
      "source_ip": null,
      "incident_id": 139
    }
  ]
}
```

Rows are sorted by `category`. Only subsystems that have ever fired a health event appear — the list is self-discovering. An empty `data` array means no health events have been recorded yet.

One extra category can appear here on AWS deployments that opt in:
`system:health:aws_versions`, filed at most once a day when a managed RDS,
Aurora or ElastiCache service has a major version upgrade available. Its
`level` is 4 (the inventory was incomplete because an AWS API was denied), 5
(upgrade available, no near deadline), 8 (AWS standard support ends soon) or
10 (support has already ended). The finding rows themselves are in the
event's metadata (`findings`, each with `resource_id`, `engine`,
`current_version`, `available_major`, `deadline`, `extended_deadline` and
`days_remaining`), so a UI can render the per-resource detail without a
second call.

A second opt-in category can appear here on AWS deployments:
`system:health:infra_drift`, filed at most once a day when what is actually
serving traffic differs from the fleet recorded in `EDGE_EXPECTED_TOPOLOGY`.
Its `level` is 4 (an AWS read did not answer, so the comparison is incomplete)
or 5 (drift found — a serving node that is not recorded, or a recorded node
that is serving nothing). It never goes higher, and a matching fleet files no
event at all.

`details` on this row is **operator-facing prose written for a human** — each
finding names what it is, who it affects, and what a person should do, and says
explicitly that nothing was changed in AWS. **Render it as-is** (preserve the
line breaks); do not re-summarize it. The structured rows are in the event's
metadata (`findings`, each with `instance_id`, `name`, `private_hostname`,
`instance_state`, `target_groups`, `added_by_capacity`, `reason`,
`suggested_node_id`, `note` and `remediation`) for a UI that wants to build its
own table.

`reason` is one of `unrecorded_node`, `capacity_added_not_recorded` (the portal
added the node and the topology record was not updated) or `node_unserving` (the
reverse direction — a recorded node behind no target group). `suggested_node_id`
is a hint, not an authority: prefer `private_hostname` when it is set.

**No endpoint or response shape changes for this feature.** It is a new category
on the existing health-summary payload and nothing else — no new URL, no new
field, no permission change.

**Optional `?prefix=` param** — defaults to `system:health:`. Pass a different prefix to query other namespaced category roots:

```
GET /api/incident/health/summary?prefix=custom:health:
```

### 4. Firewall Status

Show currently blocked IPs and recent firewall activity:

```
GET /api/system/geoip?is_blocked=true&sort=-blocked_at
GET /api/logs?kind=firewall:block&sort=-created&size=20
```

**Firewall log `kind` values:**

| Kind | Meaning |
|------|---------|
| `firewall:block` | IP blocked (manual or rule) |
| `firewall:unblock` | IP unblocked |
| `firewall:whitelist` | IP whitelisted |
| `firewall:unwhitelist` | Whitelist removed |
| `firewall:auto_block` | Auto-blocked by a governed `block` rule handler |

All firewall logs include structured `payload` JSON with `ip`, `reason`, `trigger`, and action-specific fields. Parse `payload` for dashboard cards.

### 5. Bouncer Status

Show bot detection activity and device reputation:

```
GET /api/account/bouncer/signal?decision=block&sort=-created&graph=list&size=20
GET /api/account/bouncer/device?risk_tier=blocked&sort=-block_count&size=20
GET /api/account/bouncer/signature?is_active=true&sort=-hit_count&size=20
```

Bouncer events also create incidents — query them alongside other security incidents:

```
GET /api/incident/incident?category__startswith=security:bouncer&sort=-created
```

See [Bouncer Admin APIs](../account/bouncer.md#admin-visibility-apis) for full endpoint reference, signal payloads, and dashboard patterns.

### 6. Metrics Dashboards

Fetch time-series data for charts:

**Firewall metrics:**

```
GET /api/metrics/fetch?slug=firewall:blocks&granularity=hours&dr_start=2026-03-20
GET /api/metrics/fetch?slug=firewall:auto_blocks&granularity=hours&dr_start=2026-03-20
GET /api/metrics/fetch?category=firewall&granularity=days
```

**Bouncer metrics:**

```
GET /api/metrics/fetch?slug=bouncer:blocks&granularity=hours&dr_start=2026-03-20
GET /api/metrics/fetch?slug=bouncer:pre_screen_blocks&granularity=hours&dr_start=2026-03-20
GET /api/metrics/fetch?category=bouncer&granularity=days
```

**Incident metrics:**

```
GET /api/metrics/fetch?slug=incidents&account=incident&granularity=hours&dr_start=2026-03-20
GET /api/metrics/fetch?slug=incidents:escalated&account=incident&granularity=hours
GET /api/metrics/fetch?slug=incidents:resolved&account=incident&granularity=hours
GET /api/metrics/fetch?slug=incidents:threshold_reached&account=incident&granularity=hours
```

**Event volume:**

```
GET /api/metrics/fetch?slug=incident_events&account=incident&granularity=hours
GET /api/metrics/fetch?category=incident_events_by_country&account=incident&granularity=days
```

**Auth failures (failed logins, MFA failures, passkey failures):**

```
GET /api/metrics/fetch?slug=auth:failures&account=incident&granularity=hours
```

Use `with_delta=true` on `/api/metrics/series` to get the current value plus a comparison to the previous bucket — useful for KPI tiles showing "+X% vs last hour":

```
GET /api/metrics/series?slugs=auth:failures&account=incident&granularity=hours&with_delta=true
```

**Available metric slugs:**

| Slug | Category | What it tracks |
|------|----------|---------------|
| `firewall:blocks` | firewall | Manual + rule-based IP blocks |
| `firewall:auto_blocks` | firewall | Auto-blocks triggered by rule handlers |
| `firewall:blocks:country:{CC}` | firewall | Blocks by country code |
| `firewall:broadcasts` | firewall | Fleet-wide block broadcasts |
| `incidents` | — | Incidents created |
| `incidents:escalated` | — | Priority escalations |
| `incidents:resolved` | — | Incidents resolved |
| `incidents:threshold_reached` | — | Pending → new transitions |
| `bouncer:assessments` | bouncer | Total bouncer scoring runs |
| `bouncer:blocks` | bouncer | Bouncer blocks (full scoring) |
| `bouncer:blocks:country:{CC}` | bouncer | Bouncer blocks by country |
| `bouncer:monitors` | bouncer | Suspicious but allowed |
| `bouncer:pre_screen_blocks` | bouncer | Signature cache hits (served decoy) |
| `bouncer:honeypot_catches` | bouncer | Credential attempts on decoy pages |
| `bouncer:signatures_learned` | bouncer | Auto-created bot signatures |
| `bouncer:campaigns` | bouncer | Coordinated bot campaign detections |
| `incident_events` | — | Total events |
| `incident_events:country:{CC}` | incident_events_by_country | Events by country |
| `auth:failures` | auth | Aggregate auth failure counter (invalid password, unknown login, TOTP and passkey failures) |

### 7. Ticket Management

Tickets are how the LLM agent communicates with humans:

```
GET /api/incident/ticket?status=open&sort=-priority
```

Tickets with `metadata.llm_enabled=true` (legacy `llm_linked` also honored) are part of an LLM conversation. When you post a note, the LLM reads it and responds:

```
POST /api/incident/ticket/note
{
  "parent": 10,
  "note": "What would this rule match besides the scanner traffic?"
}
```

The LLM will post a follow-up note automatically. Check `ticketnote?parent=10&sort=created` to see the conversation.

Toggle the LLM per ticket with the `enable_llm` / `disable_llm` actions —
`enable_llm` immediately invokes the agent on the full thread:

```
POST /api/incident/ticket/10
{"enable_llm": 1}
```

#### Action notes (structured approvals)

Some notes carry a `metadata.action` block — a structured proposal ("Approve
rule proposal?", "Block 10.0.0.1?") that should render as **Approve/Deny
buttons**, not free text. Tickets with a pending action are flagged
`metadata.requires_approval=true` for queue filtering.

```json
{
  "action": {
    "type": "approval",
    "handler": "incident.rule_approval",
    "label": "Approve rule proposal?",
    "schema": "incident.ticket_approval",
    "schema_version": 1,
    "proposal_note_id": 73,
    "proposal_digest": "28e3fb760c71d0d2f4247688c8bc16d354699fccfdcec1f1ba9bd51bed0b959b",
    "state": "pending",
    "resolved": false,
    "context": {
      "target": {"model": "incident.RuleSet", "pk": 42},
      "ruleset": {
        "name": "SSH brute force blocker",
        "category": "auth:failed",
        "priority": 50,
        "bundle_minutes": 30,
        "bundle_by": 4,
        "bundle_by_rule_set": true,
        "match_by": 0,
        "trigger_count": 10,
        "trigger_window": 5,
        "retrigger_every": null,
        "handlers": [
          {"type": "block", "ttl_seconds": 3600, "fleet_wide": true}
        ],
        "delete_on_resolution": false,
        "is_active": false,
        "rules": [
          {"name": "High severity", "field": "level", "operator": ">=", "value": "8", "value_type": "int", "is_required": false}
        ]
      },
      "expected_modified": "2026-09-04T17:18:19.123456+00:00",
      "confirm": "ACTIVATE RULESET 42",
      "confirm_catch_all": "ACTIVATE CATCH-ALL RULESET 42",
      "deny_confirm": "DELETE RULESET 42"
    },
    "review": {
      "proposal": {
        "target": {"model": "incident.RuleSet", "pk": 42},
        "ruleset": {
          "name": "SSH brute force blocker",
          "category": "auth:failed",
          "priority": 50,
          "bundle_minutes": 30,
          "bundle_by": 4,
          "bundle_by_rule_set": true,
          "match_by": 0,
          "trigger_count": 10,
          "trigger_window": 5,
          "retrigger_every": null,
          "handlers": [
            {"type": "block", "ttl_seconds": 3600, "fleet_wide": true}
          ],
          "delete_on_resolution": false,
          "is_active": false,
          "rules": [
            {"name": "High severity", "field": "level", "operator": ">=", "value": "8", "value_type": "int", "is_required": false}
          ]
        },
        "expected_modified": "2026-09-04T17:18:19.123456+00:00",
        "confirm": "ACTIVATE RULESET 42",
        "confirm_catch_all": "ACTIVATE CATCH-ALL RULESET 42",
        "deny_confirm": "DELETE RULESET 42"
      },
      "target": {"model": "incident.RuleSet", "pk": 42},
      "revision": "2026-09-04T17:18:19.123456+00:00",
      "confirmation": {
        "approve": "ACTIVATE RULESET 42",
        "approve_catch_all": "ACTIVATE CATCH-ALL RULESET 42",
        "deny": "DELETE RULESET 42"
      }
    }
  }
}
```

Render `label` as the question and render the complete bounded
`review.proposal` as the action parameters the human is approving. Preserve
its JSON types and show the complete RuleSet aggregate, including handlers and
child rules; RuleSet uses `name` (there is no persisted `description`) and
canonical `match_by` (not `match_type`). `review.target`, `review.revision`, and
`review.confirmation` are convenience projections for generic cards. Resolve
`review.target` to a link/card generically: `model` `"incident.RuleSet"` + `pk` 42 →
`/api/incident/event/ruleset/42`.
Render all proposal strings as text, never HTML. The server caps canonical
review JSON at 64 KiB, nesting at eight levels, each array/object at 64 entries,
strings at 4096 characters, and keys at 80 characters.
Enable the buttons only while `action.state` is `"pending"`. Disable them for
`claimed`, `unknown`, and `resolved`, even when `action.resolved` is still
false; `unknown` requires operator reconciliation, not another click.

To answer, create a new note whose `metadata.action_response` has exactly four
keys: copy the pending `proposal_note_id`, `proposal_digest`, and `handler`, then
set `action` to `"approve"` or `"deny"`. Do not recompute the digest and do not
send context or extra keys. The digest binds the response to the complete
displayed review; the backend still executes only the context stored on that
exact server-authored proposal note.

```
POST /api/incident/ticket/note
{
  "parent": 10,
  "note": "Approved",
  "metadata": {
    "action_response": {
      "proposal_note_id": 73,
      "proposal_digest": "28e3fb760c71d0d2f4247688c8bc16d354699fccfdcec1f1ba9bd51bed0b959b",
      "handler": "incident.rule_approval",
      "action": "approve"
    }
  }
}
```

Dispatch requires a global `manage_security`/`security` grant, a non-key-backed
session, and authentication within 600 seconds. Approving a rule proposal
activates exactly the revision that was reviewed and resolves the ticket;
denying deletes that same revision and closes it. A stale revision fails closed.
A structured response never triggers an LLM reply; plain notes on an
LLM-enabled ticket do. The outcome is posted back to the thread as an
`[LLM Agent]` system note. After submitting a response, reload the proposal and
follow its server state. The backend first commits a durable claim, then runs
the handler outside that transaction. Its stable dispatch identity is derived
from the action schema/version, proposal-note ID, proposal digest, and
approve/deny choice.
A same-choice retry converges only after a known resolution; a conflicting
choice cannot replace it. A handler refusal/exception is `state: "unknown"`,
and a crash around execution/finalization may remain `state: "claimed"`.
Because either path may have crossed a side-effect boundary, neither state is
automatically replayable.

### 8. Event Reporting (Client-Side)

Report security events from your frontend:

```js
fetch('/api/incident/event', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    category: 'xss:attempt',
    level: 8,
    details: 'Script tag detected in comment field',
    metadata: {
      field: 'comment',
      input: '<script>...',
      page: '/posts/new'
    }
  })
});
```

Events with `level >= 7` automatically create incidents. See [Reporting Events](../logging/reporting_events.md) for full field reference.

## Event Sources

These are the built-in detection sources. Your app can add custom events via the reporting API.

| Source | Category | Level | What it detects |
|--------|----------|-------|-----------------|
| Failed login (unknown user) | `login:unknown` | 8 | Credential stuffing |
| Failed login (wrong password) | `invalid_password` | 1 | Brute force |
| Invalid/expired token | `invalid_token`, `expired_token` | 8 | Token abuse |
| Rate limit hit | `rate_limit:{endpoint}` | 5 | API abuse |
| Global API throttle engaged | `rate_limit:api` | 5 | One identity over its per-minute request budget |
| Traffic concentration | `traffic:concentration` | 6 | One identity dominating overall API traffic |
| WebSocket connect storm | `traffic:ws_connect` | 6 | One IP connecting too fast |
| WebSocket connection cap | `traffic:ws_maxconn` | 6 | One identity over its concurrent-socket limit |
| Bouncer block | `security:bouncer:block` | 8 | Bot detected |
| MFA failures | `totp:login_failed` | 1 | MFA bypass attempt |
| OSSEC alerts | `ossec` | varies | OS-level threats |
| System health | `system:health:{type}` | 5-10 | Infrastructure issues |
| AWS version drift | `system:health:aws_versions` | 4-10 | Managed service on a major version losing support |
| Fleet drift | `system:health:infra_drift` | 4-5 | A serving node is not in the recorded topology, or a recorded node is serving nothing |

### Legacy OSSEC receiver authentication

`POST /api/incident/ossec/alert` and
`POST /api/incident/ossec/alert/batch` are legacy machine receivers, not
browser/admin APIs. They are enabled only when the deployment sets a non-empty
`OSSEC_SECRET`; every request must send that exact value in
`X-OSSEC-SECRET`. An unset secret, missing header, or mismatch returns
`403 {"error": "unauthorized"}`. The comparison is constant-time.

## Configuring RuleSets

RuleSets are the core of the rule engine. Each RuleSet watches a specific event
category, groups related events into incidents, and fires a handler when enough
events accumulate. Human clients mutate the complete RuleSet and its child
rules through the governed Admin Security action endpoint.

### Endpoints

| Method | Path | Description | Permission |
|--------|------|-------------|------------|
| `GET` | `/api/incident/admin/security?sections=rules,schemas` | Bounded RuleSet summaries plus the server-owned input schema | global `view_security`, `manage_security`, or `security`; human only |
| `POST` | `/api/incident/admin/security/action` | Create/replace/activate/deactivate/delete a complete RuleSet | global `manage_security` or `security`; fresh human session |
| `GET` | `/api/incident/event/ruleset[/<id>]` | Read-only compatibility RuleSet projection | `view_security` |
| `GET` | `/api/incident/event/ruleset/rule[/<id>]` | Read-only compatibility child-rule projection | `view_security` |

POST and DELETE requests to the generic RuleSet and Rule URLs are disabled.

### RuleSet Fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Human-readable label |
| `category` | string | Event category to match. Use `*` as a catch-all fallback. |
| `priority` | int | Evaluation order — lower number = higher priority. First match wins. |
| `match_by` | int | `0` = ALL rules must match, `1` = ANY rule can match |
| `bundle_by` | int | How to group events into one incident (see below) |
| `bundle_minutes` | int or null | Time window for bundling. `0` = each event gets its own incident, `null` = bundle forever, `>0` = bundle within N minutes |
| `bundle_by_rule_set` | bool | Include the RuleSet in the bundle identity |
| `handlers` | array | Typed, ordered handler objects; raw URL strings are rejected |
| `rules` | array | Complete ordered child-rule array; maximum 32 |
| `trigger_count` | int or null | Hold incident at `pending` until this many events accumulate. `null` = fire on first event. |
| `trigger_window` | int or null | Only count events within this many minutes when evaluating `trigger_count`. `null` = count all events on the incident. |
| `retrigger_every` | int or null | Re-fire the handler every N additional events while the incident stays active. `null` = fire once only. |
| `delete_on_resolution` | bool | Delete incidents created by this RuleSet when they resolve or close, unless the incident is protected |
| `is_active` | bool | Governed create/replace requires inactive policy; activation is a separate action |

RuleSet has no separately persisted `description`; use `name`. The canonical
condition-combiner field is `match_by`, not `match_type`. The server rejects
both unknown names rather than silently translating them.

### bundle_by Values

| Value | Name | Group events by | When to use |
|-------|------|-----------------|-------------|
| `0` | NONE | — (no grouping) | Each event creates its own incident. Use for one-shot alerts like health checks. |
| `1` | HOSTNAME | Same server | Server-level problems (disk full, CPU spike) that should be tracked per machine. |
| `2` | MODEL_NAME | Same model type | Permission denials across all instances of a model (e.g. all `Order` edits). |
| `3` | MODEL_NAME_AND_ID | Same model instance | Activity on a specific record (e.g. repeated edits to one user account). |
| `4` | SOURCE_IP | Same source IP | Attack patterns from one IP — brute force, scanning, credential stuffing. |
| `5` | HOSTNAME_AND_MODEL_NAME | Same server + model | Server-specific model errors. |
| `6` | HOSTNAME_AND_MODEL_NAME_AND_ID | Same server + model instance | Very specific server-scoped activity. |
| `7` | SOURCE_IP_AND_MODEL_NAME | Same IP + model | IP attacking a specific model type. |
| `8` | SOURCE_IP_AND_MODEL_NAME_AND_ID | Same IP + model instance | IP targeting a specific record. |
| `9` | SOURCE_IP_AND_HOSTNAME | Same IP + server | IP causing problems on a specific instance (distributed attack targeting one node). |
| `10` | GROUP_ID | Same tenant group | Multi-tenant noise isolation — one tenant's flood does not drown out another's signal. |
| `11` | GROUP_AND_MODEL_NAME | Same tenant + model | Per-tenant model activity (e.g. all `Order` denials inside tenant X). |
| `12` | GROUP_AND_MODEL_NAME_AND_ID | Same tenant + model instance | Per-tenant activity on a specific record. |
| `13` | GROUP_AND_SOURCE_IP | Same tenant + source IP | Per-tenant attack patterns — recommended default for multi-tenant deployments where one IP could be hammering one tenant while others operate normally. |

For single-tenant deployments, `bundle_by=4` (SOURCE_IP) is the right default. For multi-tenant deployments, prefer `bundle_by=13` (GROUP_AND_SOURCE_IP) so per-tenant attack signal stays separated. GROUP_* modes require an `Event.group` to bundle on; events without a group fall together into a single "no group" bucket per category.

### trigger_count + trigger_window: Suppress Until Threshold

Without `trigger_count`, the handler fires on the very first event. That's right for critical one-off alerts, but creates noise for gradual attacks. Use `trigger_count` to suppress until you're sure something is real:

**How it works:**
1. Events arrive and get bundled into one incident (which sits at `pending`)
2. Once the incident accumulates `trigger_count` events, it transitions to `new` and the handler fires
3. The `pending` incident is invisible in the main admin queue — it only surfaces when it becomes `new`

`trigger_window` scopes the count to recent events only. Events on the incident older than `trigger_window` minutes don't count toward the threshold.

**Example — block after 10 failed logins in 5 minutes:**

```
POST /api/incident/admin/security/action
{
  "action": "ruleset.create",
  "confirm": "CREATE RULESET",
  "ruleset": {
    "name": "Brute Force Detection",
    "category": "auth:failed",
    "priority": 5,
    "match_by": 0,
    "bundle_by": 4,
    "bundle_minutes": 30,
    "trigger_count": 10,
    "trigger_window": 5,
    "handlers": [
      {"type": "block", "ttl_seconds": 3600, "fleet_wide": true}
    ],
    "rules": [
      {"name": "High severity", "field": "level", "operator": ">=", "value": 8}
    ],
    "is_active": false
  }
}
```

After a separate activation, events 1–9 from the same IP sit quietly at
`pending`. Event 10 trips the threshold → incident goes `new` → IP gets
blocked fleet-wide.

### retrigger_every: Keep Alerting as Things Escalate

Sometimes you want the handler to fire again if the attack keeps going. `retrigger_every` re-fires the handler every N additional events after the initial trigger, as long as the incident is still active (`new`, `open`, or `investigating`).

**Example — ticket at 5 payment failures, then escalate every 10 more:**

```
POST /api/incident/admin/security/action
{
  "action": "ruleset.create",
  "confirm": "CREATE RULESET",
  "ruleset": {
    "name": "Payment Failure Escalation",
    "category": "payment:declined",
    "priority": 10,
    "match_by": 0,
    "bundle_by": 4,
    "bundle_minutes": 60,
    "trigger_count": 5,
    "retrigger_every": 10,
    "handlers": [
      {"type": "ticket", "priority": 7, "status": "open", "category": "incident"},
      {"type": "email", "permission": "manage_security"}
    ],
    "rules": [
      {"name": "Declined", "field": "category", "operator": "==", "value": "payment:declined"}
    ],
    "is_active": false
  }
}
```

- 5 failures → ticket created + email sent (initial trigger)
- 15 failures → ticket + email again
- 25 failures → ticket + email again
- etc.

Re-triggers add a `handler_retriggered` history entry on the incident so you can see the escalation trail.

### Handler Chains

Send handlers as an ordered array of typed objects. The server validates and
compiles that array into its private storage format:

```json
[
  {"type": "block", "ttl_seconds": 3600, "fleet_wide": true},
  {"type": "ticket", "priority": 9, "status": "open", "category": "security"},
  {"type": "email", "permission": "manage_security"}
]
```

**Block handler parameters:**

| Param | Default | Description |
|-------|---------|-------------|
| `ttl_seconds` | required | Seconds until auto-unblock; 300–604800 |
| `fleet_wide` | `true` | Optional explicit scope declaration; if supplied it must be literal `true` because RuleSet blocks are always broadcast fleet-wide |

**Ticket handler parameters:**

| Param | Description |
|-------|-------------|
| `priority` | Ticket priority (1–10; default 5) |
| `status` | Initial status (`open` or `new`; default `open`) |
| `category` | Ticket category (default: `incident`) |
| `maestro` | Boolean: report through the configured default Maestro board |
| `board_id` | Positive remote board id; mutually exclusive with `maestro: true` |

**Notification targets** (for `email`, `sms`, and `notify`):

| `permission` | Who gets notified |
|---|---|
| `manage_security` | Users with the `manage_security` permission |
| `security` | Users with the `security` permission |

The handler array has a maximum of eight entries. `ignore` must be the only
entry and takes no arguments. Arbitrary recipients, handler URLs, jobs, Python
paths, and LLM handlers are not part of the governed schema.

### Common Patterns

| Scenario | bundle_by | trigger_count | trigger_window | retrigger_every | typed handler |
|----------|-----------|---------------|----------------|-----------------|---------|
| Block after 10 SSH failures in 5 min | SOURCE_IP (4) | 10 | 5 | — | `block` (3600 seconds) |
| Block on first credential-stuffing attempt | SOURCE_IP (4) | — | — | — | `block` (1800 seconds) |
| Ticket after 3 payment declines | SOURCE_IP (4) | 3 | 60 | — | `ticket` (priority 7) |
| Notify on first health alert | HOSTNAME (1) | — | — | — | `notify` (`manage_security`) |
| Email at 5 auth failures, re-alert every 10 | SOURCE_IP (4) | 5 | 30 | 10 | `email` (`manage_security`) |
| Block bot + create ticket for review | SOURCE_IP (4) | — | — | — | `block`, then `ticket` |
| Silent audit (no handler) | MODEL_NAME_AND_ID (3) | — | — | — | — |

### Rule Conditions

Each RuleSet action carries the complete ordered `rules` array. Conditions are
not written independently:

```json
"rules": [{
  "name": "Level >= 7",
  "field": "level",
  "operator": ">=",
  "value": 7
}]
```

The `schemas.rule_policy.fields` response is the authority for allowed fields,
their value types, and operators. `field_name`/`comparator` are accepted aliases
for `field`/`operator`, but the two forms cannot disagree. Regex patterns are
bounded to a deliberately small safe subset. Simple literal, character-class,
anchor, and safe-repetition patterns such as `^node-[A-Z0-9]+$` remain valid.
Use `\\|` or `[|]` for a literal pipe; every unescaped `|` is rejected as
alternation.

The validator also rejects capturing and flag-scoped groups, assertions,
backreferences, nested/group repetition, unknown parser operations, and
broader ambiguous repetition. Repeated atoms with overlapping or unprovable
case-insensitive character domains are rejected even when literals separate
them; Unicode `IGNORECASE` equivalences are included in that check. This atomic
boundary applies to every governed/user write.

The only runtime compatibility exception is the five exact audited regex
values installed by `RuleSet.ensure_ossec_rules()`—three Bot/Scanner
conditions, Login Session Noise, and Generic Web Errors. Existing server
defaults continue to match, but clients cannot submit those non-atomic patterns
through a governed action. Compatibility is keyed only to the immutable exact
pattern value, never the RuleSet name, category, or metadata; changing a single
character removes it. Other stored legacy regexes fail closed until replaced
with the strict subset. A RuleSet with no rules is a catch-all and needs the
additional catch-all confirmation before activation.

## Incident Handlers

Governed RuleSets can fire these typed handlers when incidents are created:

| Type | What it does |
|---|---|
| `block` | Places a bounded temporary IP block |
| `email` | Emails users holding an allowed security permission |
| `sms` | Texts users holding an allowed security permission |
| `notify` | Sends in-app/push notification to users holding an allowed security permission |
| `ticket` | Creates a local ticket, optionally reported to Maestro |
| `resolve` | Resolves or closes the incident with an optional bounded note |
| `ignore` | Explicitly performs no action; must be the only handler |

`schemas.rule_policy.handlers` supplies the handler roster, arguments, and
bounds. Stored legacy chains outside the allowlist remain visible for
replacement, deactivation, or deletion, but no longer dispatch.

## LLM Agent

The LLM agent acts as an automated first responder only when a credential, a
valid `LLM_SAFETY_POLICY`, and the protected autonomous-triage switch all
permit it. Credential presence alone never enables catch-all work.

That switch gates only catch-all event pickup and scheduled sweeps. Explicit
admin analysis and LLM-linked ticket replies remain opt-in independently, but
still pass the emergency stop and the complete safety guard.

1. Triages every `status=new` incident
2. Queries context (events, IP history, related incidents, metrics)
3. Takes action: ignore noise, resolve real threats, block IPs, create tickets for humans
4. Learns over time by creating new rules and storing pattern knowledge
5. Communicates with humans through ticket notes

High-level events that do not match a rule are eligible only after the owner's
activation watermark. The sweep runs at 09:00 and 18:00, oldest-first and
bounded; duplicate scheduler delivery converges on one attempt/job.

The LLM creates rules in a **disabled** state and opens a ticket for human approval. Respond to the ticket to approve, modify, or reject the proposed rule.

For new thresholded proposals, the approval note shows the operative event
count, counting window, and bundle window. The disabled RuleSet already stores
that policy in `trigger_count` and `trigger_window`; approval only activates
the persisted values. Reviewers should confirm that the bundle window is long
enough to retain the events needed by the threshold before approving.

The proposal tool accepts only positive integer `min_count` and
`window_minutes` values, and `window_minutes` requires `min_count`. When
`min_count > 1`, `bundle_by` must be a supported non-zero integer choice (not a
boolean), `bundle_minutes` must be positive, and the bundle window must be at
least as long as `window_minutes`. Invalid or unreachable thresholds create
neither a partial RuleSet nor an approval ticket. Existing proposals absorb a
repeat only when their persisted threshold and bundle policy matches; a
different policy remains a separate approval item for reviewers.

Older proposals may instead carry `metadata.min_count` or
`metadata.window_minutes` with null `trigger_count` / `trigger_window`. Treat
those as legacy audit findings, not as operative thresholds. Review the
proposal history and current policy intent, then explicitly save the desired
canonical fields before activation or remediation. Do not blindly copy the
metadata onto an active rule: a null threshold can be intentional, and adding
a count can re-arm an existing incident and cause a handler to execute again.

### On-Demand Deep Analysis

Admins can request a deeper analysis of any incident at any time. This is separate from the automatic triage — it runs a more thorough investigation designed to clean up related open incidents and propose rules.

```
POST /api/incident/incident/<id>
{"analyze": 1}
```

**Requires:** `manage_security`

The call returns immediately. The agent runs in the background and:
- Merges related open incidents in the same category
- Proposes a new disabled RuleSet to cover the pattern
- Stores its summary in `incident.metadata.llm_analysis.summary`

Check progress by polling `metadata.analysis_in_progress` on the incident. When
it becomes `false`, the attempt either completed or reached a terminal safe
failure. Inspect `metadata.llm_analysis`, the incident history, and the job
result before presenting an analysis as successful.

See [Incident API: Request LLM Analysis](../logging/incidents.md#request-llm-analysis) for full request/response reference.

## Dashboard Chart Ideas

**Overview cards:**
- Total incidents today (use `incidents` metric)
- Unhandled count (`GET /api/incident/incident?status=new` → `count`)
- Active blocks (`GET /api/system/geoip?is_blocked=true` → `count`)
- Events/hour trend (use `incident_events` metric)

**Time-series charts:**
- Incident volume over time (`incidents` metric, hourly granularity)
- Block rate (`firewall:blocks` metric)
- Events by country (use `incident_events_by_country` category)
- Resolution rate (`incidents:resolved` vs `incidents` metrics)

**Tables:**
- Top source IPs by event count
- Recent firewall actions (logit with `kind=firewall:*`)
- Open tickets awaiting human response
- LLM-proposed rules pending approval

## Settings Reference

| Setting | Default | Description |
|---------|---------|-------------|
| `INCIDENT_LEVEL_THRESHOLD` | `7` | Min event level to auto-create incidents |
| `INCIDENT_EVENT_METRICS` | `True` | Enable incident/event metrics recording |
| `HEALTH_MONITORING_ENABLED` | `False` | Enable system health monitoring cron |
| `HEALTH_TCP_MAX` | `2000` | TCP connection threshold per node |
| `HEALTH_CPU_CRIT` | `90` | CPU % threshold |
| `HEALTH_MEM_CRIT` | `90` | Memory % threshold |
| `HEALTH_DISK_CRIT` | `85` | Disk % threshold |
| `OSSEC_SECRET` | `None` | Legacy OSSEC shared secret; unset/empty disables the endpoints |
| `MOJOSEC_RECEIPT_RETENTION_DAYS` | `45` | Published MojoSec receipt retention; minimum 7 days |
| `MOJOSEC_HANDLER_MAX_ATTEMPTS` | `100` | Handler dispatch attempts before a MojoSec receipt is dead-lettered |
| `MOJOSEC_HANDLER_QUEUED_STALE_SECONDS` | `1800` | Age after which a queued MojoSec receipt's vanished dispatch job is recovered |
| `MOJOSEC_LEARNING_EVALUATION_RETENTION_DAYS` | `90` | Offline replay/shadow summary retention; clamped to 30–3,650 days |
| `LLM_HANDLER_API_KEY` | `None` | Platform credential selectable by an exact policy route; it never substitutes for a missing `credential: "admin"` route |
| `LLM_HANDLER_MODEL` | (legacy picker pin) | Does not select a guarded model; every guarded request uses its exact policy-route model |
| `LLM_SAFETY_POLICY` | required | File-owned provider routes, budgets, and breaker thresholds; absence denies calls |
| `LLM_SAFETY_POLICY_EXPECTED_HASH` | required DB agreement | Owner-activated hash of the policy deployed identically on every node |
| `LLM_EMERGENCY_STOP` | `False` | Deployment OR protected database stop; database uncertainty denies |
| `LLM_AUTONOMOUS_INCIDENT_TRIAGE_ENABLED` | `False` | Owner-only catch-all switch; enabling stamps a no-history watermark |
| `ASSISTANT_MCP_ENABLED` | `False` | Remote agent access (MCP door); switched from the Admin, never from `/api/settings` |
| `INCIDENT_EMAIL_FROM` | `None` | SES mailbox for incident emails |
| `ADMIN_PORTAL_URL` | `None` | URL for deep links in notifications |

The built-in Admin's Assistant setup owns the MCP switch, Assistant and
platform credentials, the database emergency stop, autonomous-triage switch,
policy activation, breaker reset, and bounded historical triage. See
[the Assistant setup API](../account/admin_portal/assistant.md). Remote clients
authenticate with OAuth grants rather than API keys, and any of them can be
disconnected from that same view; [Connecting an AI client over MCP](../assistant/mcp.md)
is the operator's runbook.

A connection is one of two kinds, shown in the **Access** column: tool-door access (`mcp`), where every change still waits for an approval in the Admin, or **full API access** (`api`), which equals that person's own session token in reach — the same permissions, nothing more — with no approval step on direct API calls. Both are revoked from the same place; a credential a full-API connection mints in turn (an API key, for instance) has its own lifetime and is revoked separately, exactly as one minted from a browser session would be.

## IPSet Bulk Blocking

An IPSet is durable desired state for one Linux `hash:net` set. Creation and
metadata/CIDR editing use the model endpoint; enable, disable, and sync use a
fresh-auth governed action. A lifecycle action verifies one compatible runner
per hostname and returns success only after every host reports the exact set
type, IPv4 membership digest, and INPUT/FORWARD rule counts.

### Endpoints and permissions

| Method | Path | Permission | Description |
|---|---|---|---|
| `GET` | `/api/incident/ipset` | `view_security` or `security` | List IPSets |
| `GET` | `/api/incident/ipset/<id>` | `view_security` or `security` | Get one IPSet |
| `POST` | `/api/incident/ipset` | `manage_security` or `security` | Create a disabled IPSet |
| `POST` | `/api/incident/ipset/<id>` | `manage_security` or `security` | Update writable metadata or CIDRs |
| `POST` | `/api/incident/ipset/action` | global `manage_security` or `security`; human JWT authenticated within 600 seconds | Enable, disable, or re-check desired state |

API keys are refused by the action endpoint. `DELETE` is unsupported: disable
is the durable absence tombstone that lets later reconciliation remove drift.

### Field reference

| Field | Type | Writable | Description |
|---|---|---|---|
| `id` | int | No | Primary key |
| `name` | string | Create only | Unique 1–31 character kernel set name using letters, digits, `_`, or `-`; names ending `_tmp` and framework-reserved names are refused, and enabled names must leave room for the atomic `_tmp` suffix |
| `kind` | string | Yes | `country`, `datacenter`, `abuse`, or `custom` |
| `description` | string | Yes | Human-readable label |
| `source` | string | Yes | `ipdeny`, `abuseipdb`, `tor`, `blocklist_de`, or `manual` |
| `source_url` | string | Yes | Source URL used by scheduled refresh |
| `source_key` | string | Yes, write-only | Source credential/identifier; excluded from every response graph |
| `data` | array of strings on write; newline text on read | Yes | Complete CIDR replacement. Input is validated all-or-nothing, canonicalized, sorted, deduplicated, IPv4-only, and capped at 250,000 networks. |
| `is_enabled` | bool | No | Desired presence, changed only by governed lifecycle actions; it is not observed kernel proof |
| `cidr_count` | int | No | Number of canonical stored networks |
| `last_synced` | datetime | No | Latest checked dispatch or exact aggregated observation; not success proof by itself |
| `sync_error` | string or null | No | Bounded pending/quarantine/failure detail. One host cannot clear it; shared success requires matching fenced observations from the exact current compatible-host roster. |
| `created`, `modified` | datetime | No | Creation timestamp and optimistic-concurrency revision |

The default graph excludes `data` and `source_key`. `?graph=detailed` includes
the stored newline-form CIDR data; `source_key` always remains excluded.
The Admin Security `ipsets` section recomputes `enforcement_status` from the
exact current compatible-host roster and fresh fenced observations on every
read; it does not infer verification from `last_synced` or an empty error.

System-managed `tor_exits` and `blocklist_de` rows are cache-only and always
desired-disabled. Enabling them returns 400; reconciliation treats them as
absence tombstones so threat-cache data never reaches the kernel firewall.
The configured permanent-aggregate set name and framework namespaces are also
reserved, preventing an operator IPSet from overwriting permanent blocks.
The privileged broker derives the permanent identity from root-owned
configuration (default `mojo_blocked`) and treats the application value only as
a required equality assertion; configuration drift therefore fails closed.

### Governed lifecycle request and response

Reload the row immediately before the action and send its exact `modified`
revision plus the typed confirmation:

```http
POST /api/incident/ipset/action
```

```json
{
  "action": "ipset.enable",
  "ipset_id": 3,
  "expected_modified": "2026-09-04T18:15:03.220000+00:00",
  "confirm": "ENABLE IPSET 3"
}
```

Valid action/confirmation pairs are:

| Action | Confirmation | Effect |
|---|---|---|
| `ipset.enable` | `ENABLE IPSET <id>` | Persist desired presence, then reconcile it |
| `ipset.disable` | `DISABLE IPSET <id>` | Persist desired absence, then remove set/rule drift |
| `ipset.sync` | `SYNC IPSET <id>` | Re-check the current desired state without changing it |

The response data includes the row plus `enforcement_status`,
`enforcement_ok`, and, on failure, `error_code`:

```json
{
  "status": true,
  "code": 200,
  "data": {
    "schema_version": 1,
    "action": "ipset.enable",
    "data": {
      "id": 3,
      "is_enabled": true,
      "last_synced": "2026-09-04T18:15:04.020000+00:00",
      "sync_error": "",
      "enforcement_status": "verified",
      "enforcement_ok": true
    }
  }
}
```

`verified` plus `enforcement_ok=true` in this action response is the success
contract. `partial` means at least one checked-command dispatch was confirmed
but complete host proof was not; it does not prove that a broker mutation ran.
`unknown` means dispatch/observation was not confirmed. Neither
proves that no host changed after an ambiguous transport failure. Desired state
remains durable for repair, and a stale `expected_modified` fails with 409.

### Manual CIDR workflow

Create the set (it starts disabled regardless of any submitted
`is_enabled` value):

```http
POST /api/incident/ipset
```

```json
{
  "name": "custom_block",
  "kind": "custom",
  "description": "Blocked datacenter ranges",
  "source": "manual",
  "data": ["192.0.2.0/24", "198.51.100.7", "192.0.2.10/24"]
}
```

The stored networks become `192.0.2.0/24` and `198.51.100.7/32`. Reload the
created row, then call `ipset.enable` with its current revision. Later CIDR
edits replace the complete stored list and mark an enabled row pending; call
`ipset.sync` and keep the UI pending unless its checked response verifies.

Configured non-manual sources are refreshed by the weekly `refresh_ipsets`
job. Source-fetch or checked-reconciliation failure is retained in
`sync_error`; cache-only threat sources use their separate six-hour refresh.

### Listing and filtering

```http
GET /api/incident/ipset?kind=country
GET /api/incident/ipset?is_enabled=true
GET /api/incident/ipset?search=abuse&sort=-cidr_count&size=20
```
