# Incident System — Django Developer Reference

## Purpose and Intent

The incident system is the framework's central intelligence layer for security and system health. Its job is not simply to log things — it is to **aggregate raw signals, suppress noise, identify patterns, and surface only what matters**.

Every part of the framework should treat the incident system as its primary channel for reporting anything security-relevant or operationally significant. This includes authentication failures, permission denials, suspicious IPs, payment errors, rate limit hits, data integrity anomalies — anything that, at sufficient volume or severity, indicates a real problem.

The core insight is that **individual events are rarely meaningful on their own**. A single failed login is noise. Fifty failed logins from the same IP in five minutes is an attack. The incident system exists to bridge that gap automatically, without requiring developers to think about thresholds, deduplication, or alerting in their application code.

As the framework grows, the incident system will expand into a full analysis engine. Every component that participates today will automatically benefit from improvements to rule evaluation, anomaly detection, and automated response — with no changes to calling code.

---

## Architecture Overview

```
Event (raw signal)
  → RuleSet.check_by_category()   (rule matching by scope, then category)
    → Rule.check_rule()            (field-level conditions on event.metadata)
  → threshold/bundling logic       (pending → new transition)
  → Incident (correlated group)
    → handler chain               (job, email, notify, ticket)
    → Ticket (actionable work)
```

Events are the input. Incidents are the output. Rules, RuleSets, and handlers are the processing pipeline in between.

---

## Core Models

| Model | Purpose |
|---|---|
| `Event` | A single raw signal — one occurrence of something noteworthy |
| `RuleSet` | A named policy: which events match, how to bundle them, what to do |
| `Rule` | A single field-level condition within a RuleSet |
| `Incident` | A correlated group of related events requiring attention |
| `IncidentHistory` | Audit trail of incident state changes |
| `Ticket` | An actionable work item linked to an incident |
| `TicketNote` | A note or status change attached to a ticket |

---

## Reporting Events

### From any Python code

```python
from mojo.apps import incident

incident.report_event(
    "User exceeded rate limit",
    title="Rate Limit Hit",
    category="rate_limit",
    scope="api",
    level=4,
    request=request,
    uid=request.user.id,
)
```

`report_event` is the primary API. It:
1. Creates an `Event` record with all fields and metadata populated
2. Calls `event.sync_metadata()` to enrich with geo-IP data
3. Calls `event.publish()` to run rule matching and incident creation

### From a MojoModel instance

```python
# Report an event tied to a specific model instance (auto-fills model_name, model_id)
self.report_incident(
    "Suspicious edit attempt",
    event_type="security_alert",
    level=5
)

# Report at the class level with request context
MyModel.class_report_incident(
    "Unauthorized list attempt",
    event_type="permission_denied",
    request=request
)
```

### Automatically — permission denials

Every permission denial issued by `MojoModel`'s permission system is automatically reported as an event. Categories include:

- `unauthenticated`
- `view_permission_denied`
- `edit_permission_denied`
- `group_member_permission_denied`
- `user_permission_denied`

No extra code required.

---

## Event Fields

| Field | Description |
|---|---|
| `level` | Severity 0–15. 0–3 informational, 4–7 warning, 8–15 critical |
| `scope` | Logical domain: `"global"`, `"account"`, `"payment"`, etc. |
| `category` | Dot-namespaced event type: `"auth:failed"`, `"permission_denied"` |
| `source_ip` | IP address of the originating request |
| `hostname` | Server hostname (auto-populated from `socket.gethostname()`) |
| `uid` | User ID associated with this event |
| `country_code` | ISO 3166-1 alpha-2 country code (auto-populated via GeoIP) |
| `title` | Short human-readable summary |
| `details` | Full description, stack trace, or structured message |
| `model_name` | Related model (e.g., `"account.User"`) |
| `model_id` | Related model instance PK |
| `metadata` | JSON bag of arbitrary context — also used by Rules for field matching |

### Custom metadata — pass anything as keyword arguments

Any extra keyword argument passed to `report_event` is automatically stored in `metadata` and becomes available for rule matching. There is no need to build a dict manually:

```python
incident.report_event(
    "Failed login attempt",
    category="auth:failed",
    level=5,
    request=request,
    username=attempted_username,   # custom
    attempt_count=7,               # custom
    auth_method="password",        # custom
)
```

All three custom fields end up in `event.metadata` alongside the standard ones. A Rule with `field_name="attempt_count"` and `comparator=">="` and `value="5"` would match this event.

When a `request` is passed, metadata is also automatically enriched with:

```
request_ip, http_path, http_protocol, http_method,
http_query_string, http_user_agent, http_host,
user_name, user_email (if authenticated)
```

---

## Level Guide

| Range | Meaning | Typical Use |
|---|---|---|
| 0–3 | Informational | Routine activity worth recording |
| 4–6 | Warning | Anomaly, policy violation, soft error |
| 7–9 | Elevated | Attack pattern, repeated failures, suspicious behavior |
| 10–14 | High severity | Confirmed attack, data integrity issue |
| 15 | Critical | System compromise, emergency |

Events with `level >= INCIDENT_LEVEL_THRESHOLD` (default: `7`) automatically create or escalate an incident even without a matching RuleSet.

---

## Rule Engine

The rule engine evaluates each event against configured RuleSets. It is the mechanism that separates signal from noise.

### RuleSet

A RuleSet defines:

- **`category`** — Which event category it applies to (matched by `scope` first, then `category`)
- **`priority`** — Evaluation order (lower = higher priority). First matching RuleSet wins.
- **`match_by`** — `ALL` (all rules must match) or `ANY` (any rule can match)
- **`bundle_by`** — How to group events into one incident (see bundling below)
- **`bundle_minutes`** — Time window for bundling. `0` = disabled, `None` = unlimited, `>0` = window in minutes
- **`handler`** — What to do when a RuleSet triggers (see handlers below)
- **`metadata`** — Optional threshold configuration (`min_count`, `window_minutes`, `pending_status`)

### Rule

Each Rule checks one field in `event.metadata` against a target value:

| Field | Description |
|---|---|
| `field_name` | The metadata key to inspect |
| `comparator` | `==`, `>`, `>=`, `<`, `<=`, `contains`, `regex` |
| `value` | The target value |
| `value_type` | `str`, `int`, `float`, `bool` |
| `index` | Evaluation order within the RuleSet |

Rules operate on `event.metadata`. Since `report_event` syncs all standard fields (level, category, source_ip, etc.) into metadata automatically, they are all available for rule matching alongside any custom fields you pass in.

### Bundling

Bundling controls how related events are collapsed into a single incident rather than creating a new one for each event.

| `bundle_by` value | Groups events by |
|---|---|
| `NONE` | Each event creates its own incident |
| `HOSTNAME` | Same server |
| `MODEL_NAME` | Same model type |
| `MODEL_NAME_AND_ID` | Same model instance |
| `SOURCE_IP` | Same source IP |
| `SOURCE_IP_AND_HOSTNAME` | Same IP + server |
| `SOURCE_IP_AND_MODEL_NAME` | Same IP + model type |
| `SOURCE_IP_AND_MODEL_NAME_AND_ID` | Same IP + model instance |
| `HOSTNAME_AND_MODEL_NAME` | Same server + model type |
| `HOSTNAME_AND_MODEL_NAME_AND_ID` | Same server + model instance |

### Thresholds (pending → new)

A RuleSet can hold incidents in `pending` status until a minimum event count is reached within a time window. This eliminates alerting on isolated events:

```python
RuleSet.objects.create(
    category="auth:failed",
    name="Brute Force Detection",
    bundle_by=BundleBy.SOURCE_IP,
    bundle_minutes=10,
    handler="ticket://?status=new&priority=8&category=security",
    metadata={
        "min_count": 10,           # Wait for 10 matching events
        "window_minutes": 10,      # Within 10 minutes
        "pending_status": "pending"
    }
)
```

Until 10 events accumulate, the incident sits at `pending`. Once the threshold is crossed, it transitions to `new` and the handler fires.

---

## Handlers

Handlers execute when a RuleSet triggers. Multiple handlers can be chained with commas.

### Syntax

```
job://app.module.function
email://admin@example.com
notify://user_id_or_channel
ticket://?status=open&priority=8&category=security&title=Investigate
```

Chained example:

```
ticket://?status=new&priority=9&category=security,email://security@example.com
```

### Handler Types

| Handler | Action |
|---|---|
| `job://` | Queues an async job (function path in netloc) |
| `email://` | Sends a notification email to the recipient |
| `notify://` | Sends a push/in-app notification to a user or channel |
| `ticket://` | Creates a Ticket linked to the incident |

### Ticket Handler Parameters

| Param | Description |
|---|---|
| `title` | Ticket title (defaults to event title) |
| `description` | Ticket body (defaults to event details) |
| `status` | Initial status (`open`, `new`, etc.) |
| `priority` | Integer priority (defaults to `event.level`) |
| `category` | Ticket category (default: `"incident"`) |
| `assignee` | User ID to assign the ticket to |

---

## Incident Lifecycle

```
pending  →  new  →  open  →  investigating  →  resolved  →  closed
```

| Status | Meaning |
|---|---|
| `pending` | Below threshold — waiting for more events |
| `new` | Threshold met or level-based trigger — needs triage |
| `open` | Acknowledged, active |
| `investigating` | Actively being worked |
| `resolved` | Root cause addressed |
| `closed` | No further action needed |

Incidents can be merged when duplicates are identified:

```python
primary_incident.on_action_merge([incident_id_1, incident_id_2])
```

This moves all events from the listed incidents into the primary and deletes the originals.

---

## Integration Patterns

### Every component should report events

The incident system only works if data flows into it. When writing a new service, model, or REST handler, ask:

- Could this action fail in a way that indicates abuse or misconfiguration?
- Could repeated failures from one source indicate an attack?
- Is this an action with security or compliance significance?

If yes, report an event.

### GeoLocatedIP — IP trust scoring

`GeoLocatedIP` classifies IPs as Tor, VPN, proxy, cloud, known attacker, etc. and maintains a `risk_score`. This data should feed the incident system so rules can act on IP reputation:

```python
from mojo.apps.account.models import GeoLocatedIP
from mojo.apps import incident

geo = GeoLocatedIP.geolocate(request.ip)

if geo.is_threat:
    incident.report_event(
        f"Request from known threat IP {request.ip}",
        category="ip:known_threat",
        scope="account",
        level=10,
        request=request,
        source_ip=request.ip,
        is_tor=geo.is_tor,
        is_vpn=geo.is_vpn,
        threat_level=geo.threat_level,
        risk_score=geo.risk_score,
    )
elif geo.is_suspicious:
    incident.report_event(
        f"Request from suspicious IP {request.ip}",
        category="ip:suspicious",
        scope="account",
        level=6,
        request=request,
        source_ip=request.ip,
        risk_score=geo.risk_score,
    )
```

A RuleSet on `category="ip:known_threat"` can auto-create a ticket, notify security staff, or trigger a blocking job — without any of that logic living in the calling code. As the analysis engine matures, cross-referencing `risk_score` trends with auth failure events can enable automatic trust level adjustments per IP.

### Pattern: rate limiting

```python
incident.report_event(
    f"User {user.id} exceeded API rate limit",
    category="rate_limit:api",
    scope="api",
    level=4,
    request=request,
    uid=user.id,
    endpoint=request.path,
)
```

A RuleSet bundling by `SOURCE_IP` with `min_count=20` and `window_minutes=1` fires only when a real abuse pattern emerges — not on a single slow request.

### Pattern: authentication failures

```python
incident.report_event(
    f"Failed login for {username}",
    category="auth:failed",
    scope="account",
    level=5,
    request=request,
    username=username,
)
```

### Pattern: payment anomalies

```python
incident.report_event(
    "Multiple card declines for user",
    category="payment:declined",
    scope="billing",
    level=5,
    uid=user.id,
    model_name="billing.Order",
    model_id=order.id,
)
```

### Pattern: data integrity

```python
incident.report_event(
    f"Unexpected deletion of {obj.__class__.__name__} {obj.pk}",
    category="data:unexpected_delete",
    scope="admin",
    level=7,
    uid=request.user.id,
    model_name=obj.__class__.__name__,
    model_id=obj.pk,
)
```

---

## Settings

| Setting | Default | Description |
|---|---|---|
| `INCIDENT_LEVEL_THRESHOLD` | `7` | Minimum level to auto-create an incident without a matching RuleSet |
| `INCIDENT_EVENT_METRICS` | — | Enable metrics recording for events and incidents |
| `INCIDENT_METRICS_MIN_GRANULARITY` | `"hours"` | Granularity for incident metrics |

---

## Why Consistency Matters

The incident system gets more valuable as more components use it. A RuleSet configured to detect brute force across `auth:failed` events only works if every authentication path reports `auth:failed` consistently.

Establish naming conventions per domain and stick to them:

```
auth:failed
auth:locked
auth:mfa_bypass_attempt
ip:suspicious
ip:known_threat
permission:denied
payment:declined
data:unexpected_delete
rate_limit:api
```

The rule engine matches on these strings — consistent category naming means rule coverage automatically extends to every code path that reports under the same category, without touching the RuleSet.
