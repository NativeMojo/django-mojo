# Incident Management System - REST API Documentation

This comprehensive guide provides developers with everything needed to integrate with the MOJO Incident Management System's REST API. The system processes events through a powerful rule engine, creates actionable incidents, and provides ticketing functionality.

**Base URL:** `/api/`  
**Authentication:** Required for all endpoints  
**Permissions:** 
- `view_incidents` - Required to read incidents, events, and tickets
- `manage_incidents` - Required to create/modify rules, incidents, and tickets

---

## Table of Contents

1. [Core Concepts](#core-concepts)
2. [Event Management](#event-management)
3. [Rule Engine](#rule-engine)
4. [Incident Management](#incident-management)
5. [Ticket System](#ticket-system)
6. [Comment Systems](#comment-systems)
7. [API Reference](#api-reference)
8. [Best Practices](#best-practices)
9. [Future Improvements](#future-improvements)

---

## Core Concepts

### Architecture Overview

The system follows a clear flow: **Events** → **Rules** → **Incidents** → **Tickets**

```
External System → Event → Rule Engine → Incident → Ticket → Resolution
```

### Data Models

#### Event
The fundamental unit representing a discrete occurrence that needs recording.

**Key Fields:**
- `level` (0-15): Severity level (7+ can auto-escalate to incidents)
- `category`: Groups similar events (e.g., "auth", "ossec", "api_error")
- `metadata`: JSON field containing all event details for rule processing
- `source_ip`, `hostname`, `uid`: Common contextual fields

#### RuleSet & Rule
The decision engine that determines when events become incidents.

**RuleSet Logic:**
- `match_by`: `0` (ALL rules must match) or `1` (ANY rule can match)
- `bundle_by`: Controls how events are grouped into single incidents
- `bundle_minutes`: Time window for event bundling
- `handler`: Actions to execute when ruleset triggers (email, tasks, notifications)

#### Incident
High-level records created when events meet significance criteria.

**States:** `open`, `investigating`, `resolved`, `closed`
**Priority:** Integer (higher = more urgent)

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

**Single Alert:**
```http
POST /api/ossec/alert
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

**Batch Alerts:**
```http
POST /api/ossec/alert/batch
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
GET /api/event
GET /api/event?category=ossec&level__gte=7
GET /api/event?source_ip=192.168.1.100&created__gte=2024-01-15T00:00:00Z
GET /api/event/123?graph=incident
```

---

## Rule Engine

### Understanding the Rule Engine

The rule engine processes every event against category-matched RuleSets in priority order. Rules operate on the `Event.metadata` JSON field.

### Creating a RuleSet

```http
POST /api/event/ruleset
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

**Bundle Options:**
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

### Adding Rules to a RuleSet

```http
POST /api/event/ruleset/rule
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

**Supported Comparators:**
- `==`, `eq`: Equal to
- `>`, `>=`, `<`, `<=`: Numeric comparisons
- `contains`: String contains
- `regex`: Regular expression match

### Complete Example: Brute Force Detection

```http
# Step 1: Create RuleSet
POST /api/event/ruleset
{
    "name": "SSH Brute Force Detection",
    "category": "auth",
    "priority": 5,
    "match_by": 0,
    "bundle_by": 4,
    "bundle_minutes": 5,
    "handler": "task://block_ip?duration=3600,email://security@company.com"
}

# Step 2: Add rules
POST /api/event/ruleset/rule
{
    "parent": 124,
    "name": "High severity auth events",
    "field_name": "level",
    "comparator": ">=",
    "value": "7",
    "value_type": "int"
}

POST /api/event/ruleset/rule
{
    "parent": 124,
    "name": "SSH-related events",
    "field_name": "details",
    "comparator": "contains",
    "value": "SSH",
    "value_type": "str"
}
```

---

## Incident Management

### Listing Incidents

```http
GET /api/incident
GET /api/incident?state=open&priority__gte=8
GET /api/incident?category=ossec&created__gte=2024-01-15T00:00:00Z
```

**Response:**
```json
{
    "data": [
        {
            "id": 456,
            "title": "Multiple SSH failures from 192.168.1.100",
            "category": "auth",
            "state": "open",
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
GET /api/incident/456?graph=events,history
```

**Response:**
```json
{
    "data": {
        "id": 456,
        "title": "Multiple SSH failures from 192.168.1.100",
        "category": "auth",
        "state": "open",
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

### Updating Incident State

```http
PATCH /api/incident/456
Content-Type: application/json

{
    "state": "investigating",
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
GET /api/incident/456/history
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
| GET | `/api/event` | List events with filtering |
| GET | `/api/event/{id}` | Get specific event |
| POST | `/api/ossec/alert` | Submit OSSEC alert |
| POST | `/api/ossec/alert/batch` | Submit multiple OSSEC alerts |

### Rule Engine Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/event/ruleset` | List rule sets |
| POST | `/api/event/ruleset` | Create rule set |
| GET | `/api/event/ruleset/{id}` | Get specific rule set |
| PATCH | `/api/event/ruleset/{id}` | Update rule set |
| GET | `/api/event/ruleset/rule` | List rules |
| POST | `/api/event/ruleset/rule` | Create rule |
| PATCH | `/api/event/ruleset/rule/{id}` | Update rule |

### Incident Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/incident` | List incidents |
| GET | `/api/incident/{id}` | Get specific incident |
| PATCH | `/api/incident/{id}` | Update incident |
| GET | `/api/incident/history` | List all incident history |
| POST | `/api/incident/{id}/history` | Add history entry |

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

1. **Use Meaningful Categories**: Group related events with consistent category names
2. **Appropriate Severity Levels**: Reserve high levels (8-15) for truly critical events
3. **Rich Metadata**: Include all relevant context in the metadata field
4. **Consistent Field Names**: Use standard field names across your application

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

1. **Start Simple**: Begin with basic rules and add complexity as needed
2. **Test Thoroughly**: Verify rules with historical data before deployment
3. **Avoid Over-bundling**: Too aggressive bundling can mask individual issues
4. **Use Meaningful Names**: Clear rule and ruleset names aid troubleshooting

### Incident Management

1. **Consistent State Management**: Define clear state transition workflows
2. **Prioritize Appropriately**: Align incident priorities with business impact
3. **Document Resolutions**: Always add history entries when resolving incidents

### Ticket Workflow

1. **Clear Titles**: Use descriptive, actionable ticket titles
2. **Detailed Descriptions**: Include enough context for assignees to act
3. **Regular Updates**: Use notes to track progress and decisions
4. **Link to Incidents**: Always link tickets to related incidents when applicable

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
- **State Transitions**: Automatic status changes based on actions
- **Escalation Rules**: Auto-assignment based on time or priority
- **SLA Tracking**: Due date calculation and breach notifications

#### 3. Enhanced Notifications
```json
{
    "handler": "slack://channel/security,email://team@company.com,webhook://https://api.company.com/alerts"
}
```

#### 4. Metrics and Reporting
- **MTTR Tracking**: Mean Time To Resolution by category
- **Incident Trends**: Pattern recognition across events
- **Performance Dashboards**: Team and individual metrics

#### 5. Integration Improvements
- **Bidirectional Sync**: Two-way integration with external ticketing systems
- **Custom Fields**: Configurable metadata fields per category
- **Bulk Operations**: Mass updates and actions

### Implementation Priorities

1. **Phase 1**: Enhanced ticket states and workflow automation
2. **Phase 2**: Advanced notifications and escalation rules
3. **Phase 3**: Metrics, reporting, and performance tracking
4. **Phase 4**: External integrations and custom fields

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
- Review the [Developer Guide](../incidents.md) for internal system mechanics
- Check the source code in `mojo/apps/incident/` for implementation details
- File issues for bugs or feature requests

**Remember**: This system prioritizes simplicity and reliability. When in doubt, choose the simpler solution that maintains data integrity and system stability.