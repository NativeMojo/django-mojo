# Incident Management System - REST API Documentation

This comprehensive guide provides developers with everything needed to integrate with the MOJO Incident Management System's REST API. The system processes events through a powerful rule engine, creates actionable incidents, and provides ticketing functionality.

Base URL: `/api/`
Authentication: Required for all endpoints
Permissions:
- `view_incidents` - Required to read incidents, events, and tickets
- `manage_incidents` - Required to create/modify rules, incidents, and tickets

---

## Table of Contents

1. [Core Concepts](#core-concepts)
2. [Event Management](#event-management)
3. [Rule Engine](#rule-engine)
   - [Rule Building Guide](#rule-building-guide)
   - [Bundling Strategies](#bundling-strategies)
   - [Rule Recipes](#rule-recipes)
   - [Default Rules Quick Start](#default-rules-quick-start)
4. [Incident Management](#incident-management)
5. [Ticket System](#ticket-system)
6. [Comment Systems](#comment-systems)
7. [API Reference](#api-reference)
8. [Best Practices](#best-practices)
9. [Future Improvements](#future-improvements)

---

## Core Concepts

### Architecture Overview

The system follows a clear flow: Events → Rules → Incidents → Tickets

```
External System → Event → Rule Engine → Incident → Ticket → Resolution
```

### Data Models

#### Event
The fundamental unit representing a discrete occurrence that needs recording.

Key Fields:
- `level` (0-15): Severity level (7+ can auto-escalate to incidents)
- `category`: Groups similar events (e.g., "auth", "ossec", "api_error")
- `metadata`: JSON field containing all event details for rule processing
- `source_ip`, `hostname`, `uid`: Common contextual fields

Note: Core fields are mirrored into `metadata` on save, so rules can reference either the original or the mirrored metadata key (e.g., `level` is available as `metadata.level`).

#### RuleSet & Rule
The decision engine that determines when events become incidents.

RuleSet Logic:
- `match_by`: `0` (ALL rules must match) or `1` (ANY rule can match)
- `bundle_by`: Controls how events are grouped into single incidents
- `bundle_minutes`: Time window for event bundling
- `handler`: Actions to execute when ruleset triggers (email, tasks, notifications)

#### Incident
High-level records created when events meet significance criteria.

Status: `new`, `open`, `investigating`, `resolved`, `closed`
Priority: Integer (higher = more urgent)

#### Ticket
Actionable tasks that can be assigned to users for resolution.

---

## Event Management

### Creating Events Programmatically

Events are typically created internally, but you can submit them via the reporter:

```python
from mojo.apps.incident import report_event

# Basic event
report_event(
    details="User failed login attempt",
    title="Failed Login",
    category="auth",
    level=5,
    source_ip="192.168.1.100",
    username="admin"
)

# Event with request context
report_event(
    details="API endpoint returned 500 error",
    category="api_error",
    level=8,
    request=request,  # Automatically extracts IP, user agent, etc.
    error_code="INTERNAL_SERVER_ERROR"
)
```

### OSSEC Integration

For external SIEM systems like OSSEC:

Single Alert:
```http
POST /api/incident/ossec/alert
Content-Type: application/json

{
    "timestamp": "2024-01-15T10:30:00Z",
    "rule_id": 2501,
    "level": 8,
    "text": "SSH authentication failed for user admin from 192.168.1.100",
    "source_ip": "192.168.1.100",
    "hostname": "web-server-01"
}
```

Batch Alerts:
```http
POST /api/incident/ossec/alert/batch
Content-Type: application/json

{
    "batch": [
        {
            "rule_id": 2501,
            "level": 8,
            "text": "SSH authentication failed for user admin from 192.168.1.100"
        },
        {
            "rule_id": 31104,
            "level": 10,
            "text": "Web attack detected on /admin from 203.0.113.5"
        }
    ]
}
```

### Querying Events

```http
GET /api/incident/event
GET /api/incident/event?category=ossec&level__gte=7
GET /api/incident/event?source_ip=192.168.1.100&created__gte=2024-01-15T00:00:00Z
GET /api/incident/event/123?graph=incident
```

---

## Rule Engine

### Understanding the Rule Engine

The rule engine processes every event against category-matched RuleSets in priority order. Rules operate on the `Event.metadata` JSON field. If a rule set matches, an incident is created or the event is bundled into an existing incident per the ruleset’s bundling policy.

### Creating a RuleSet

```http
POST /api/incident/event/ruleset
Content-Type: application/json

{
    "name": "Failed SSH Login Detection",
    "category": "auth",
    "priority": 10,
    "match_by": 0,
    "bundle_by": 1,
    "bundle_minutes": 10,
    "handler": "email://security@company.com"
}
```

Bundle Options:
- `0`: No bundling
- `1`: Bundle by hostname
- `2`: Bundle by model_name
- `3`: Bundle by model_name + model_id
- `4`: Bundle by source_ip
- `5`: Bundle by hostname + model_name
- `6`: Bundle by hostname + model_name + model_id
- `7`: Bundle by source_ip + model_name
- `8`: Bundle by source_ip + model_name + model_id
- `9`: Bundle by source_ip + hostname

Tip: If you want to bundle by “event type”, consider populating `model_name` and `model_id` on events to represent a stable type key (e.g., `model_name="ossec_rule"` and `model_id=<rule_id>`) and then use bundle_by `8` to scope by `source_ip + type`.

### Adding Rules to a RuleSet

```http
POST /api/incident/event/ruleset/rule
Content-Type: application/json

{
    "parent": 123,
    "name": "Check SSH authentication failures",
    "field_name": "level",
    "comparator": ">=",
    "value": "8",
    "value_type": "int"
}
```

Supported Comparators:
- `==`, `eq`: Equal to
- `>`, `>=`, `<`, `<=`: Numeric comparisons
- `contains`: String contains
- `regex`: Regular expression match

Field Values:
- `field_name` reads from `Event.metadata`. Core fields such as `level`, `category`, `source_ip`, `hostname`, `model_name`, and `model_id` are mirrored into `metadata` automatically.

Handlers:
- `task://name?param1=value1`
- `email://recipient@example.com`
- `notify://channel-or-user`
- `ticket://?status=open&priority=8&title=Investigate` (creates a ticket; optional params: `title`, `description`, `status`, `priority`, `category`, `assignee`)

RuleSet metadata (optional):
- `min_count` (int): Minimum number of bundled events within the window to consider the incident “open”. If not met, incident status is set to `pending_status`.
- `window_minutes` (int): Time window in minutes used to count bundled events. Defaults to `bundle_minutes` if unset.
- `pending_status` (str): Incident status to use when below `min_count` (default: `pending`).
- `action` (str): When set to `ignore`, matching events are ignored (no incident or handler execution).

Tip on bundling by ip + rule_id:
- Set events’ `model_name="ossec_rule"` and `model_id=<rule_id>`, then use `bundle_by=8` to bundle by `source_ip + model_name + model_id`.

### Rule Building Guide

- Start with category-specific rule sets (e.g., `ossec`, `auth`, `api_error`).
- Use `match_by=0` to require all conditions, or `match_by=1` for any condition.
- Keep handlers simple: `email://user@company.com`, `notify://team-security`, `task://your_task?param=value`, or `ticket://?status=open&priority=8`.
- Use bundling to prevent duplicate incidents:
  - Bundle by `source_ip` for network-driven alerts.
  - Add `model_name/model_id` to separate different event types for the same IP.
- Keep severity levels consistent; high severities should only trigger when truly necessary.

### Bundling Strategies

- Minimal bundling: `bundle_by=0`
- By asset: `bundle_by=1` (hostname)
- By event type: populate `model_name/model_id` and use `3` or `8`
- By network source: `bundle_by=4` (source_ip)
- By mixed context: `bundle_by=9` (source_ip + hostname)

Time window: Use `bundle_minutes` to cap how long events will be grouped into one incident.

### Rule Recipes

1) OSSEC: Bundle events to a single incident by source_ip and event type (rule_id-like)
```http
# Create a ruleset for a specific OSSEC rule class
POST /api/incident/event/ruleset
{
  "name": "OSSEC 2501 Bundled by IP",
  "category": "ossec",
  "priority": 10,
  "match_by": 0,
  "bundle_by": 4,
  "bundle_minutes": 15,
  "handler": "email://security@company.com"
}

# Only match OSSEC events with level >= 7
POST /api/incident/event/ruleset/rule
{
  "parent": <ruleset_id>,
  "name": "Severity >= 7",
  "field_name": "level",
  "comparator": ">=",
  "value": "7",
  "value_type": "int"
}

# Constrain to a specific rule type using OSSEC metadata
POST /api/incident/event/ruleset/rule
{
  "parent": <ruleset_id>,
  "name": "OSSEC rule 2501",
  "field_name": "rule_id",
  "comparator": "==",
  "value": "2501",
  "value_type": "int"
}
```
Note: For strict per-type bundling (e.g., source_ip + rule_id), set events’ `model_name="ossec_rule"` and `model_id=<rule_id>`, then use `bundle_by=8` to guarantee separation across rule_ids.

2) Batch events as “pending” when below a trigger threshold
- Pattern: Use a “collector” ruleset to bundle by IP/type and create an incident immediately; keep its `status` as “pending” until a count threshold is met within a time window, then programmatically set it to “open” and run notifications.
- Implementation approach:
  - Choose a reasonable `bundle_minutes` to define the evaluation window.
  - Periodically count matching events for the incident context and, if the threshold is reached, update the incident via:
    ```http
    PATCH /api/incident/{id}
    {
      "status": "open",
      "note": "Threshold met (N events in M minutes)"
    }
    ```
  - Use a task/cron in your environment to enforce the transition and trigger downstream actions.

3) Completely ignore some events
- At ingestion: For OSSEC, leverage parser-side ignore logic (e.g., filter known noisy `rule_id` or text signatures before they become events).
- Via rules and severity: Ensure noisy categories remain below the incident threshold and have no matching rulesets. They will be recorded as events but will not produce incidents.
- For targeted suppression, create a high-priority ruleset that matches the noise pattern and do not attach handlers; design your severity and thresholds so no incident gets created for these matches.

4) Escalate major events
- Use a dedicated high-severity ruleset (e.g., `level >= 10`) with stronger handlers (paging, Slack, email).
- If an ongoing incident receives higher-severity events, update the incident priority via API:
```http
PATCH /api/incident/{id}
{
  "priority": 10,
  "note": "Escalated due to higher-severity event"
}
```

5) Turn an event/incident into a ticket
- From an incident:
```http
POST /api/incident/ticket
Content-Type: application/json

{
  "title": "Investigate suspicious activity",
  "description": "Bundled incident by source IP. Review logs and block if necessary.",
  "priority": 8,
  "status": "open",
  "incident": 456,
  "assignee": 123
}
```

### Default Rules Quick Start

These examples create common “core” rules via the REST API.

A) OSSEC: Bundle all high-severity OSSEC events by source IP (15 min window)
```http
POST /api/incident/event/ruleset
{
  "name": "OSSEC - Bundle by IP (high severity)",
  "category": "ossec",
  "priority": 10,
  "match_by": 0,
  "bundle_by": 4,
  "bundle_minutes": 15,
  "handler": "email://security@company.com"
}

POST /api/event/ruleset/rule
{
  "parent": <ruleset_id>,
  "name": "OSSEC severity >= 7",
  "field_name": "level",
  "comparator": ">=",
  "value": "7",
  "value_type": "int"
}
```

B) OSSEC: Per-rule bundling (example for `rule_id=31104`)
```http
POST /api/incident/event/ruleset
{
  "name": "OSSEC 31104 by IP",
  "category": "ossec",
  "priority": 9,
  "match_by": 0,
  "bundle_by": 4,
  "bundle_minutes": 20,
  "handler": "notify://sec-oncall"
}

POST /api/event/ruleset/rule
{
  "parent": <ruleset_id>,
  "name": "Severity >= 8",
  "field_name": "level",
  "comparator": ">=",
  "value": "8",
  "value_type": "int"
}

POST /api/incident/event/ruleset/rule
{
  "parent": <ruleset_id>,
  "name": "Rule 31104",
  "field_name": "rule_id",
  "comparator": "==",
  "value": "31104",
  "value_type": "int"
}
```

C) Ignore noisy low-severity OSSEC events (example for `rule_id=510`)
- Prefer ingestion-time ignore for OSSEC. If you must handle via rules, ensure severity stays below your incident threshold and omit handlers to avoid alert fatigue:
```http
POST /api/incident/event/ruleset
{
  "name": "Ignore OSSEC 510",
  "category": "ossec",
  "priority": 1,  // high priority so it matches first
  "match_by": 0,
  "bundle_by": 0
}

POST /api/event/ruleset/rule
{
  "parent": <ruleset_id>,
  "name": "Rule 510",
  "field_name": "rule_id",
  "comparator": "==",
  "value": "510",
  "value_type": "int"
}

POST /api/incident/event/ruleset/rule
{
  "parent": <ruleset_id>,
  "name": "Low severity",
  "field_name": "level",
  "comparator": "<",
  "value": "7",
  "value_type": "int"
}
```

D) Escalation for critical OSSEC events
```http
POST /api/incident/event/ruleset
{
  "name": "OSSEC Critical Escalation",
  "category": "ossec",
  "priority": 5,
  "match_by": 1,
  "bundle_by": 4,
  "bundle_minutes": 30,
  "handler": "email://security@company.com,notify://sec-ops"
}

POST /api/event/ruleset/rule
{
  "parent": <ruleset_id>,
  "name": "OSSEC severity >= 10",
  "field_name": "level",
  "comparator": ">=",
  "value": "10",
  "value_type": "int"
}
```

---

## Incident Management

### Listing Incidents

```http
GET /api/incident
GET /api/incident?status=open&priority__gte=8
GET /api/incident?category=ossec&created__gte=2024-01-15T00:00:00Z
```

Response:
```json
{
  "data": [
    {
      "id": 456,
      "title": "Multiple SSH failures from 192.168.1.100",
      "category": "auth",
      "status": "open",
      "priority": 8,
      "created": "2024-01-15T10:30:00Z",
      "source_ip": "192.168.1.100",
      "hostname": "web-server-01"
    }
  ],
  "meta": {
    "total": 25,
    "page": 1,
    "per_page": 20
  }
}
```

### Getting Incident Details with Events

```http
GET /api/incident/incident/456?graph=events,history
```

Response:
```json
{
  "data": {
    "id": 456,
    "title": "Multiple SSH failures from 192.168.1.100",
    "category": "auth",
    "status": "open",
    "priority": 8,
    "events": [
      {
        "id": 789,
        "title": "SSH authentication failed for user admin",
        "level": 8,
        "created": "2024-01-15T10:30:00Z",
        "metadata": {
          "username": "admin",
          "source_ip": "192.168.1.100"
        }
      }
    ],
    "history": [
      {
        "id": 101,
        "kind": "created",
        "created": "2024-01-15T10:30:00Z",
        "note": "Incident automatically created by rule: SSH Brute Force Detection"
      }
    ]
  }
}
```

### Updating Incident State/Status

```http
PATCH /api/incident/456
Content-Type: application/json

{
  "status": "investigating",
  "note": "Security team investigating the source IP"
}
```

---

## Ticket System

### Creating Tickets

```http
POST /api/incident/ticket
Content-Type: application/json

{
  "title": "Investigate suspicious SSH activity",
  "description": "Multiple failed SSH attempts from 192.168.1.100 detected",
  "priority": 8,
  "status": "open",
  "assignee": 123,
  "incident": 456
}
```

### Listing Tickets

```http
GET /api/incident/ticket
GET /api/incident/ticket?status=open&assignee=123
GET /api/incident/ticket?incident=456
```

### Updating Ticket Status

```http
PATCH /api/incident/ticket/789
Content-Type: application/json

{
  "status": "in_progress",
  "assignee": 456
}
```

---

## Comment Systems

Both Incidents and Tickets support comment-like interfaces for collaboration and audit trails.

### Incident History (Comments)

Add notes to incidents:
```http
POST /api/incident/456/history
Content-Type: application/json

{
  "kind": "comment",
  "note": "Blocked the source IP at firewall level. Monitoring for additional attempts.",
  "by": 123
}
```

Get incident history:
```http
GET /api/incident/incident/456/history
```

### Ticket Notes (Comments)

Add notes to tickets:
```http
POST /api/incident/ticket/789/note
Content-Type: application/json

{
  "note": "Contacted the user - confirmed they were attempting to login from home. IP has been whitelisted.",
  "author": 123
}
```

With file attachments:
```http
POST /api/incident/ticket/789/note
Content-Type: application/json

{
  "note": "Attached network trace showing the attack pattern",
  "author": 123,
  "media": 456
}
```

Get ticket notes:
```http
GET /api/incident/ticket/789/note
```

---

## API Reference

### Event Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/incident/event` | List events with filtering |
| GET | `/api/incident/event/{id}` | Get specific event |
| POST | `/api/incident/ossec/alert` | Submit OSSEC alert |
| POST | `/api/incident/ossec/alert/batch` | Submit multiple OSSEC alerts |

### Rule Engine Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/incident/event/ruleset` | List rule sets |
| POST | `/api/incident/event/ruleset` | Create rule set |
| GET | `/api/incident/event/ruleset/{id}` | Get specific rule set |
| PATCH | `/api/incident/event/ruleset/{id}` | Update rule set |
| GET | `/api/incident/event/ruleset/rule` | List rules |
| POST | `/api/incident/event/ruleset/rule` | Create rule |
| PATCH | `/api/incident/event/ruleset/rule/{id}` | Update rule |
| POST | `/api/incident/event/ruleset/defaults` | Create default rule sets |

### Incident Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/incident/incident` | List incidents |
| GET | `/api/incident/incident/{id}` | Get specific incident |
| PATCH | `/api/incident/incident/{id}` | Update incident |
| GET | `/api/incident/incident/history` | List all incident history |
| POST | `/api/incident/incident/{id}/history` | Add history entry |

### Ticket Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/incident/ticket` | List tickets |
| POST | `/api/incident/ticket` | Create ticket |
| GET | `/api/incident/ticket/{id}` | Get specific ticket |
| PATCH | `/api/incident/ticket/{id}` | Update ticket |
| GET | `/api/incident/ticket/note` | List all ticket notes |
| POST | `/api/incident/ticket/{id}/note` | Add note to ticket |

---

## Best Practices

### Event Creation

- Use meaningful categories (e.g., `ossec`, `auth`, `database`)
- Keep severity consistent; 8–15 should be reserved for truly critical signals
- Include rich metadata; prefer stable keys that support bundling (e.g., type identifiers)
- Mirror type identity into `model_name/model_id` when you need per-type bundling

```python
# Good
report_event(
    details="Database connection failed",
    category="database",
    level=9,
    database_name="primary_db",
    error_code="CONNECTION_TIMEOUT",
    retry_count=3
)

# Not recommended
report_event("db error", level=15)  # Too vague, inappropriate severity
```

### Rule Design

- Start simple; add complexity as needed
- Test with historical data before deployment
- Avoid over-bundling; too much bundling can hide distinct problems
- Use meaningful names for rulesets and rules

### Incident Management

- Define clear, consistent state/status transitions
- Prioritize by business impact, not just raw event severity
- Document investigation notes and resolution steps in history

### Ticket Workflow

- Create tickets for actionable work, with clear titles and context
- Update status frequently; use notes for collaboration and audit
- Link tickets to incidents to keep evidence and context together

Additional baseline rules you may want to add:
- Geo-risk filters (e.g., high priority for certain country codes)
- Asset sensitivity (hosts containing “prod” get higher priority)
- Auth anomalies (multiple failed logins, new device logins)
- API errors (5xx spikes bundled by route or service)

---

## Future Improvements

To evolve this into a full-featured ticket system while maintaining the KISS principle:

### Planned Enhancements

#### 1. Advanced Ticket Features
```json
{
  "due_date": "2024-01-20T17:00:00Z",
  "estimated_hours": 4.0,
  "actual_hours": 2.5,
  "labels": ["security", "urgent", "network"],
  "watchers": [123, 456, 789],
  "parent_ticket": 456,
  "blocked_by": [123, 124]
}
```

#### 2. Workflow Automation
- State transitions and auto-assignment
- Time-based escalations
- SLA tracking and breach notifications

#### 3. Enhanced Notifications
```json
{
  "handler": "slack://channel/security,email://team@company.com,webhook://https://api.company.com/alerts"
}
```

#### 4. Metrics and Reporting
- MTTR, incident trends, performance dashboards

#### 5. Integration Improvements
- Two-way sync with external ticketing systems
- Custom fields per category
- Bulk operations

### API Evolution

Future API additions will maintain backward compatibility:

```http
# Enhanced ticket creation
POST /api/incident/ticket/v2
{
  "title": "Investigate security incident",
  "template": "security_incident",
  "auto_assign": true,
  "sla_hours": 24,
  "custom_fields": {
    "affected_systems": ["web-01", "db-02"],
    "security_classification": "confidential"
  }
}

# Workflow automation
POST /api/incident/ticket/789/transition
{
  "action": "start_work",
  "note": "Beginning investigation"
}
```

These improvements will be implemented incrementally, ensuring the system remains simple and maintainable while providing enterprise-level functionality.

---

## Support

For additional help:
- Review the Developer Guide for internal system mechanics
- Check the source code in `mojo/apps/incident/` for implementation details
- File issues for bugs or feature requests

Remember: This system prioritizes simplicity and reliability. When in doubt, choose the simpler solution that maintains data integrity and system stability.