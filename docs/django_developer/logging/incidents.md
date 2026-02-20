# Incident System — Django Developer Reference

## Overview

The incident system captures security events, applies rule-based correlation, and creates actionable incidents. It is the framework's central security intelligence layer.

## Core Models

| Model | Purpose |
|---|---|
| `Event` | Raw security event (one per occurrence) |
| `RuleSet` | Group of rules evaluated against events |
| `Rule` | A single matching condition |
| `Incident` | Correlated group of related events |
| `IncidentHistory` | Audit trail of incident state changes |
| `Ticket` | Actionable work item linked to an incident |

## Reporting Events

### From MojoModel

```python
# Instance-level (auto-includes model_name, model_id)
self.report_incident(
    "Suspicious edit attempt",
    event_type="security_alert",
    level=2
)

# Class-level
Book.class_report_incident(
    "Unauthorized list attempt",
    event_type="permission_denied",
    request=request
)

# For current user
Book.class_report_incident_for_user(
    "User triggered rate limit",
    event_type="rate_limit",
    request=request
)
```

### Directly via incident module

```python
from mojo.apps import incident

incident.report_event(
    "Failed login from unusual location",
    title="Suspicious Login",
    category="auth:failed",
    scope="account",
    level=5,
    request=request,
    source_ip=request.ip,
    username=attempted_username
)
```

## Event Fields

| Field | Description |
|---|---|
| `level` | Severity 1–10 (10 = critical) |
| `scope` | App label (e.g., `"account"`, `"myapp"`) |
| `category` | Dot-notation event type (e.g., `"auth:failed"`, `"permission_denied"`) |
| `source_ip` | Source IP address |
| `metadata` | Dict of arbitrary context |

## Automatic Permission Incident Reporting

Every permission denial via `rest_check_permission` automatically reports an incident with:
- `event_type`: one of `unauthenticated`, `view_permission_denied`, `edit_permission_denied`, `group_member_permission_denied`, `user_permission_denied`
- Full context: model name, permission keys, request path, instance repr

No extra code needed — this is built into `MojoModel`.

## Rule Engine

Rules evaluate incoming events and trigger actions when conditions match.

### RuleSet

Groups rules. Configures:
- `match_threshold`: How many rules must match to trigger
- `bundle_window`: Time window for grouping related events into one incident
- `handler`: Action to execute (see below)

### Rule Conditions

Rules support these comparators:

| Comparator | Behavior |
|---|---|
| `==` | Exact match |
| `>`, `>=`, `<`, `<=` | Numeric comparison |
| `contains` | Substring match |
| `regex` | Regular expression match |

### Rule Handlers

When a RuleSet triggers, it executes a handler:

```
job://app.module.function     # Execute async job
email://admin@example.com     # Send notification email
notify://user_id              # Send push notification
ticket://queue_name           # Create a support ticket
```

## Incident States

Incidents progress through states:

| State | Meaning |
|---|---|
| `open` | Active, requires attention |
| `investigating` | Being worked on |
| `resolved` | Root cause addressed |
| `closed` | No further action needed |

## Incident Merging

Related incidents can be merged:

```python
primary_incident.on_action_merge({"incident_id": related_id})
```

## Settings

| Setting | Default | Description |
|---|---|---|
| `INCIDENT_DEFAULT_LEVEL` | `1` | Default event severity |
| `INCIDENT_BUNDLE_WINDOW` | `300` | Default bundle window (seconds) |
