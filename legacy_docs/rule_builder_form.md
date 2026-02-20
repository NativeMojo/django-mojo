# Rule Builder Form Guide

This guide documents the fields and design considerations for building a user-friendly rule creation form for the MOJO incident management system.

## Overview

The rule system consists of two main entities:
- **RuleSet**: A collection of rules that define when and how to create incidents
- **Rule**: Individual conditions that must be met for a RuleSet to match an event

## RuleSet Form Fields

### Basic Information

| Field | Type | Required | Description | Example |
|-------|------|----------|-------------|---------|
| `name` | text | Yes | Human-readable name for the ruleset | "OSSEC 31104 - Web Attack Detection" |
| `category` | select/text | Yes | Event category this ruleset applies to | "ossec", "auth", "api_error" |
| `priority` | number | Yes | Lower numbers = higher priority (checked first) | 10 (specific rules), 100 (catch-all) |

**UI Tips:**
- Category should be a combo box (type or select) showing existing categories
- Priority helper text: "Lower numbers are checked first. Use 1-50 for specific rules, 100+ for catch-all rules"

### Rule Matching

| Field | Type | Required | Description | Options |
|-------|------|----------|-------------|---------|
| `match_by` | select | Yes | How to combine multiple rules | `0` = All rules must match<br>`1` = Any rule can match |

**UI Tips:**
- Display as radio buttons: "All rules must match" / "Any rule can match"
- Show icon: AND (∧) for all, OR (∨) for any

### Bundling Configuration

| Field | Type | Required | Description | Options |
|-------|------|----------|-------------|---------|
| `bundle_by` | select | Yes | How to group events into incidents | See Bundle Options table below |
| `bundle_minutes` | number | No | Time window for **finding existing incidents** to bundle into | `0` = disabled (always create new incident)<br>`60` = look back 1 hour<br>`null` = no limit (bundle forever) |

**Bundle Options:**

| Value | Display Name | Description | Use Case |
|-------|--------------|-------------|----------|
| `0` | Don't bundle | Each event creates new incident | Single critical events |
| `1` | Bundle by hostname | Group by source hostname | Server-specific issues |
| `2` | Bundle by model type | Group by event type | Application errors |
| `3` | Bundle by model instance | Group by specific object | User-specific issues |
| `4` | Bundle by IP | Group by source IP | Network attacks, scanning |
| `5` | Hostname + Model | Combine hostname and type | Server app errors |
| `6` | Hostname + Instance | Combine hostname and object | Server user issues |
| `7` | IP + Model | Combine IP and type | Attack patterns |
| `8` | IP + Instance | Combine IP and object | Targeted attacks |
| `9` | IP + Hostname | Combine IP and hostname | Network correlation |

**Important: `bundle_minutes` controls INCIDENT LOOKUP**
- When a new event arrives, system searches for existing incidents to add it to
- `bundle_minutes = 60` → "Find incidents created in last 60 minutes with matching criteria"
- `bundle_minutes = 0` → "Don't look for existing incidents, always create new one"
- `bundle_minutes = null` → "Find any incident ever created with matching criteria"

**UI Tips:**
- Show common presets: "Don't bundle", "By IP (1 hour)", "By Hostname (1 hour)"
- Bundle minutes helper: "0 = disabled, blank = no time limit"
- Warning: Setting to `0` with bundling criteria will still match by IP/hostname/etc, but creates a new incident each time

### Threshold Logic (Optional)

| Field | Type | Required | Description | Example |
|-------|------|----------|-------------|---------|
| `metadata.min_count` | number | No | Events needed before transitioning to "new" status | `5` = wait for 5 events |
| `metadata.window_minutes` | number | No | Time window for **counting events** toward threshold | `60` = count events from last 1 hour<br>(defaults to `bundle_minutes` if not set) |
| `metadata.pending_status` | text | No | Status while waiting for threshold | `"pending"` (default) |

**Important: `metadata.window_minutes` controls THRESHOLD COUNTING**
- Counts how many events occurred in the time window to determine if threshold is met
- Independent from `bundle_minutes` but defaults to it if not specified
- Used ONLY when `min_count` is set

**Example Scenarios:**

**Scenario 1: Same time window for bundling and threshold**
```json
{
  "bundle_by": 4,           // Bundle by IP
  "bundle_minutes": 60,     // Look back 1 hour for existing incidents
  "metadata": {
    "min_count": 5,         // Need 5 events
    "window_minutes": null  // Uses bundle_minutes (60) by default
  }
}
```
Result: Bundles events from same IP in last 60 minutes, needs 5 events in that same 60-minute window.

**Scenario 2: Different windows for bundling vs threshold**
```json
{
  "bundle_by": 4,               // Bundle by IP
  "bundle_minutes": 120,        // Look back 2 hours for existing incidents
  "metadata": {
    "min_count": 5,             // Need 5 events
    "window_minutes": 15        // Count events from last 15 minutes only
  }
}
```
Result: Bundles into incidents created in last 2 hours, but only counts events from last 15 minutes toward threshold.

**Scenario 3: Threshold only (no time limit on bundling)**
```json
{
  "bundle_by": 4,               // Bundle by IP
  "bundle_minutes": null,       // Bundle into ANY matching incident ever created
  "metadata": {
    "min_count": 10,            // Need 10 events
    "window_minutes": 60        // Count events from last 1 hour
  }
}
```
Result: All events from same IP go into one incident (forever), but threshold counts only last 60 minutes.

**UI Tips:**
- Show as expandable section: "Advanced: Threshold Settings"
- Helper text: "Leave empty for immediate incident creation"
- Show example: "Wait for 5 events within 60 minutes before alerting"
- Display both time windows clearly with labels:
  - "Incident bundling window" (`bundle_minutes`)
  - "Threshold counting window" (`metadata.window_minutes`)

### Handler Configuration

| Field | Type | Required | Description | Format |
|-------|------|----------|-------------|--------|
| `handler` | text | No | Actions to perform when triggered | URL-like format (see below) |

**Handler Formats:**

```
job://handler_name?param1=value1&param2=value2
email://recipient@example.com
notify://user-or-channel
ticket://?status=new&priority=8&category=security
ignore
```

**Multiple Handlers (chained with comma):**
```
email://security@company.com,notify://ops-team,ticket://?status=new&priority=8
```

**UI Tips:**
- Provide handler builder with dropdown:
  - "No handler"
  - "Ignore (don't create incident)"
  - "Create ticket"
  - "Send email"
  - "Send notification"
  - "Run job"
  - "Multiple actions"
- For ticket handler, show form fields:
  - Status: new, open, paused
  - Priority: 1-10 slider
  - Category: text/select
  - Title: optional text
  - Description: optional textarea

---

## Rule Form Fields (Multiple Rules per RuleSet)

Each RuleSet can have multiple rules. Present these as a dynamic list of rule conditions.

### Required Fields

| Field | Type | Required | Description | Example |
|-------|------|----------|-------------|---------|
| `name` | text | Yes | Description of what this rule checks | "Category is ossec" |
| `field_name` | text/select | Yes | Field to check (supports dot notation) | `category`, `level`, `metadata.rule_id` |
| `comparator` | select | Yes | Comparison operation | See Comparators table below |
| `value` | text | Yes | Value to compare against | `"ossec"`, `"5"`, `"31104"` |
| `value_type` | select | Yes | Data type for comparison | `str`, `int`, `float` |
| `index` | number | No | Display order (auto-assigned) | `0`, `1`, `2` |

### Comparator Options

| Value | Display Name | Description | Works With |
|-------|--------------|-------------|------------|
| `==` or `eq` | Equals | Exact match | All types |
| `>` | Greater than | Numeric comparison | int, float |
| `>=` | Greater than or equal | Numeric comparison | int, float |
| `<` | Less than | Numeric comparison | int, float |
| `<=` | Less than or equal | Numeric comparison | int, float |
| `contains` | Contains | Substring match | str |
| `regex` | Matches regex | Pattern matching | str |

**UI Tips:**
- Show comparator options based on selected value_type
- int/float: show `==`, `>`, `>=`, `<`, `<=`
- str: show `==`, `contains`, `regex`

---

## Available Fields for Rules

### Event Model Fields (Direct Access)

These are standard fields on the Event model:

| Field Name | Type | Description | Example Values |
|------------|------|-------------|----------------|
| `category` | str | Event category | `"ossec"`, `"auth"`, `"api_error"` |
| `level` | int | Severity level (0-15) | `5`, `8`, `12` |
| `source_ip` | str | Source IP address | `"192.168.1.100"` |
| `hostname` | str | Source hostname | `"web-server-01"` |
| `uid` | int | User ID | `12345` |
| `country_code` | str | 2-letter country code | `"US"`, `"CN"` |
| `model_name` | str | Related model type | `"ossec_rule"`, `"user"` |
| `model_id` | int | Related model ID | `31104`, `456` |
| `title` | str | Event title | `"Failed login"` |
| `details` | str | Event details | `"Authentication failure from..."` |

**Important:** These fields are automatically synced to metadata via `sync_metadata()`, so they can also be accessed as `metadata.category`, etc.

### Metadata Fields (Custom Data)

The `metadata` field is a JSON object that can contain any custom data. Access using dot notation:

| Field Pattern | Description | Example |
|---------------|-------------|---------|
| `metadata.rule_id` | OSSEC rule ID | `31104`, `5704` |
| `metadata.alert_id` | Alert identifier | `"1714459899.116740"` |
| `metadata.http_status` | HTTP response code | `404`, `500` |
| `metadata.http_method` | HTTP method | `"GET"`, `"POST"` |
| `metadata.http_url` | Requested URL | `"http://example.com/.env"` |
| `metadata.user_agent` | Browser user agent | `"Mozilla/5.0..."` |
| `metadata.username` | Username from log | `"admin"`, `"root"` |
| `metadata.*` | Any custom field | Limited only by your data |

**UI Tips:**
- Provide autocomplete for common fields
- Show field browser/picker grouped by category:
  - Event Fields (category, level, source_ip, etc.)
  - Network Fields (metadata.source_ip, metadata.ext_ip)
  - HTTP Fields (metadata.http_status, metadata.http_method, metadata.http_url)
  - OSSEC Fields (metadata.rule_id, metadata.alert_id)
  - Custom Fields (metadata.*)

---

## Form Design Recommendations

### 1. Progressive Disclosure

Start with essential fields, hide advanced options:

```
[Basic Settings - Always Visible]
- Name
- Category
- Priority
- Match Mode (All/Any)

[Bundling - Always Visible]
- Bundle By (dropdown)
- Bundle Window (minutes)

[Advanced Settings - Collapsible]
- Threshold Settings
  - Min Count
  - Window Minutes
  - Pending Status
- Handler Configuration

[Rules - Dynamic List]
- Add Rule button
- Rule 1: [field] [comparator] [value]
- Rule 2: [field] [comparator] [value]
- ...
```

### 2. Smart Defaults

```javascript
{
  priority: 100,  // Safe default for new rules
  match_by: 0,    // All rules must match (safer)
  bundle_by: 4,   // Bundle by IP (common use case)
  bundle_minutes: 60,  // 1 hour
  value_type: "str"    // String is most common
}
```

### 3. Field Helpers

Show contextual help for each field:

- **Priority**: "Lower = higher priority. Use 1-50 for specific rules, 100+ for catch-all"
- **Bundle By**: "How to group related events into a single incident"
- **Min Count**: "Number of events required before creating incident (leave empty for immediate)"
- **Field Name**: "Use 'category', 'level', 'source_ip' or 'metadata.custom_field'"

### 4. Visual Validation

```
✓ Category: ossec
✓ Priority: 10
⚠ Handler: Invalid format (should be scheme://...)
✗ Rule 1: Field 'invalid_field' doesn't exist

Rule Preview:
┌─────────────────────────────────────────┐
│ IF category == "ossec"                  │
│ AND metadata.rule_id == 31104           │
│ THEN bundle by source_ip for 60 minutes│
│ WHEN 5+ events → Create ticket          │
└─────────────────────────────────────────┘
```

### 5. Templates/Presets

Offer common rule templates:

- **OSSEC Web Attack Detection** (our 31104 rule)
- **Failed Login Attempts** (SSH, auth failures)
- **API Error Rate** (500 errors from same IP)
- **High Severity Alert** (level >= 10)
- **Catch-All** (category only)

### 6. Testing Interface

Provide a "Test Rule" feature:

```
Test Event Input:
{
  "category": "ossec",
  "level": 6,
  "source_ip": "192.168.1.100",
  "metadata": {
    "rule_id": 31104
  }
}

[Test Button]

Result:
✓ Rule 1 matched: category == "ossec"
✓ Rule 2 matched: metadata.rule_id == 31104
✓ RuleSet matched: Would create incident
  - Bundle: By source_ip (192.168.1.100)
  - Status: pending (0/5 events)
```

---

## Example Form Layouts

### Simple Rule Form (Minimal)

```
┌─────────────────────────────────────┐
│ RuleSet Name: [Web Attack Detection]│
│                                      │
│ Category: [ossec ▼]                  │
│                                      │
│ Match: ○ All rules ● Any rule       │
│                                      │
│ Bundle by: [IP Address ▼] for [60]  │
│            minutes                   │
│                                      │
│ Rules:                              │
│ 1. [category ▼] [equals ▼] [ossec] │
│    [x]                              │
│ 2. [metadata.rule_id ▼] [equals ▼] │
│    [31104] [x]                      │
│                                      │
│ [+ Add Rule]                        │
│                                      │
│ [Cancel] [Save RuleSet]             │
└─────────────────────────────────────┘
```

### Advanced Rule Form (Full)

```
┌──────────────────────────────────────────┐
│ RuleSet Configuration                     │
├──────────────────────────────────────────┤
│ Name: [OSSEC 31104 - Web Attack]         │
│ Category: [ossec ▼] Priority: [10]      │
│                                          │
│ ┌─ Matching ──────────────────┐         │
│ │ ● All rules must match       │         │
│ │ ○ Any rule can match         │         │
│ └────────────────────────────────┘       │
│                                          │
│ ┌─ Bundling ─────────────────────────┐  │
│ │ Bundle by: [Source IP ▼]          │  │
│ │ Time window: [60] minutes         │  │
│ │ ○ Disabled ● Time limit           │  │
│ └───────────────────────────────────┘  │
│                                          │
│ ▼ Advanced: Threshold Settings           │
│ ┌────────────────────────────────────┐  │
│ │ Min events: [5]                    │  │
│ │ Within: [60] minutes               │  │
│ │ Pending status: [pending]          │  │
│ │                                    │  │
│ │ ℹ️ Incident stays "pending" until   │  │
│ │   5 events occur within 60 minutes │  │
│ └────────────────────────────────────┘  │
│                                          │
│ ▼ Handler: Actions to Perform            │
│ ┌────────────────────────────────────┐  │
│ │ Action: [Create Ticket ▼]          │  │
│ │   Status: [new ▼]                  │  │
│ │   Priority: [7] ━━●━━━━━━━━ (1-10) │  │
│ │   Category: [security]             │  │
│ │                                    │  │
│ │ [+ Add Another Action]             │  │
│ └────────────────────────────────────┘  │
│                                          │
│ ┌─ Rules ────────────────────────────┐  │
│ │                                    │  │
│ │ Rule 1                             │  │
│ │ Name: [Category is ossec]          │  │
│ │ Field: [category ▼]                │  │
│ │ Operator: [equals ▼]               │  │
│ │ Value: [ossec]                     │  │
│ │ Type: [string ▼]                   │  │
│ │ [Remove]                           │  │
│ │                                    │  │
│ │ Rule 2                             │  │
│ │ Name: [Rule ID is 31104]           │  │
│ │ Field: [metadata.rule_id ▼]        │  │
│ │ Operator: [equals ▼]               │  │
│ │ Value: [31104]                     │  │
│ │ Type: [integer ▼]                  │  │
│ │ [Remove]                           │  │
│ │                                    │  │
│ │ [+ Add Rule]                       │  │
│ └────────────────────────────────────┘  │
│                                          │
│ [Test Rule] [Cancel] [Save RuleSet]     │
└──────────────────────────────────────────┘
```

---

## JSON Structure Reference

For API integration or JSON-based configuration:

```json
{
  "name": "OSSEC 31104 - Web Attack Detection",
  "category": "ossec",
  "priority": 10,
  "match_by": 0,
  "bundle_by": 4,
  "bundle_minutes": 60,
  "handler": "ticket://?status=new&priority=7&category=security",
  "metadata": {
    "min_count": 5,
    "window_minutes": 60,
    "pending_status": "pending"
  },
  "rules": [
    {
      "name": "Category is ossec",
      "field_name": "category",
      "comparator": "==",
      "value": "ossec",
      "value_type": "str",
      "index": 0
    },
    {
      "name": "Rule ID is 31104",
      "field_name": "metadata.rule_id",
      "comparator": "==",
      "value": "31104",
      "value_type": "int",
      "index": 1
    }
  ]
}
```

---

## Common Pitfalls to Avoid

### ❌ Don't Do This

```javascript
// Using model fields without sync_metadata()
field_name: "level"  // Might not be in metadata yet

// Mixing metadata prefix inconsistently
field_name: "metadata.category"  // Wrong - category is synced automatically
field_name: "category"            // Correct - will be in metadata after sync

// Wrong value type
field_name: "level"
value: "high"  // Should be integer like "5"
value_type: "str"  // Should be "int"
```

### ✅ Do This Instead

```javascript
// Use standard fields directly
field_name: "category"
value: "ossec"
value_type: "str"

// Use metadata prefix for custom fields only
field_name: "metadata.rule_id"
value: "31104"
value_type: "int"

// Match value_type to the actual data
field_name: "level"
value: "5"
value_type: "int"
```

---

## Implementation Checklist

- [ ] Basic RuleSet form with name, category, priority
- [ ] Match mode selector (All/Any)
- [ ] Bundle configuration (by what, time window)
- [ ] Dynamic rule list (add/remove rules)
- [ ] Rule field picker with autocomplete
- [ ] Comparator selector (context-aware based on type)
- [ ] Value type selector
- [ ] Advanced threshold settings (collapsible)
- [ ] Handler builder (UI-friendly)
- [ ] Form validation with helpful error messages
- [ ] Rule preview/explanation
- [ ] Test rule functionality
- [ ] Template/preset library
- [ ] Import/export JSON
- [ ] Field documentation/help text

---

## Additional Resources

- Event Model: `mojo/apps/incident/models/event.py`
- Rule Model: `mojo/apps/incident/models/rule.py`
- REST API: `docs/rest_api/incidents.md`
- Rule Engine Analysis: `docs/incident_rule_engine_analysis.md`
