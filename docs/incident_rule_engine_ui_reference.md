# Incident Rule Engine - UI/API Reference

**For building forms and REST interfaces**

---

## Bundle By Field - Dropdown Options

Use `BundleBy.CHOICES` for your dropdown:

```python
from mojo.apps.incident.models import BundleBy

# In your REST serializer or form
bundle_by_options = BundleBy.CHOICES

# Example response for dropdown:
[
    {"value": 0, "label": "Don't bundle - each event creates new incident"},
    {"value": 1, "label": "Bundle by hostname"},
    {"value": 2, "label": "Bundle by model type"},
    {"value": 3, "label": "Bundle by specific model instance"},
    {"value": 4, "label": "Bundle by source IP"},
    {"value": 5, "label": "Bundle by hostname + model type"},
    {"value": 6, "label": "Bundle by hostname + specific model"},
    {"value": 7, "label": "Bundle by IP + model type"},
    {"value": 8, "label": "Bundle by IP + specific model"},
    {"value": 9, "label": "Bundle by IP + hostname"}
]
```

### Most Common Options for UI:
```python
COMMON_BUNDLE_OPTIONS = [
    (BundleBy.NONE, "Don't bundle"),
    (BundleBy.HOSTNAME, "By server/hostname"),
    (BundleBy.SOURCE_IP, "By source IP"),
    (BundleBy.SOURCE_IP_AND_MODEL_NAME_AND_ID, "By IP + resource"),
]
```

---

## Match By Field - Dropdown Options

Use `MatchBy.CHOICES` for your dropdown:

```python
from mojo.apps.incident.models import MatchBy

# Simple dropdown:
[
    {"value": 0, "label": "All rules must match"},
    {"value": 1, "label": "Any rule can match"}
]
```

---

## Bundle Minutes Field - Dropdown Options

Use `BundleMinutes.CHOICES` for your dropdown:

```python
from mojo.apps.incident.models import BundleMinutes

# Full options
bundle_minutes_options = BundleMinutes.CHOICES

# Example response:
[
    {"value": 0, "label": "Disabled - don't bundle by time"},
    {"value": 5, "label": "5 minutes"},
    {"value": 10, "label": "10 minutes"},
    {"value": 15, "label": "15 minutes"},
    {"value": 30, "label": "30 minutes"},
    {"value": 60, "label": "1 hour"},
    {"value": 120, "label": "2 hours"},
    {"value": 360, "label": "6 hours"},
    {"value": 720, "label": "12 hours"},
    {"value": 1440, "label": "1 day"},
    {"value": null, "label": "No limit - bundle forever"}
]
```

### Recommended Default:
```python
default_bundle_minutes = BundleMinutes.TEN_MINUTES  # 10
```

### Most Common Options for UI:
```python
COMMON_TIME_OPTIONS = [
    (BundleMinutes.DISABLED, "Disabled"),
    (BundleMinutes.FIVE_MINUTES, "5 minutes"),
    (BundleMinutes.TEN_MINUTES, "10 minutes"),
    (BundleMinutes.THIRTY_MINUTES, "30 minutes"),
    (BundleMinutes.ONE_HOUR, "1 hour"),
    (BundleMinutes.ONE_DAY, "1 day"),
]
```

---

## Complete Example: REST Serializer

```python
from rest_framework import serializers
from mojo.apps.incident.models import RuleSet, BundleBy, MatchBy, BundleMinutes

class RuleSetSerializer(serializers.ModelSerializer):
    # Include choices for frontend dropdowns
    bundle_by_choices = serializers.SerializerMethodField()
    match_by_choices = serializers.SerializerMethodField()
    bundle_minutes_choices = serializers.SerializerMethodField()
    
    class Meta:
        model = RuleSet
        fields = [
            'id', 'name', 'category', 'priority',
            'bundle_by', 'bundle_by_choices',
            'match_by', 'match_by_choices',
            'bundle_minutes', 'bundle_minutes_choices',
            'handler', 'metadata'
        ]
    
    def get_bundle_by_choices(self, obj):
        return [{"value": v, "label": l} for v, l in BundleBy.CHOICES]
    
    def get_match_by_choices(self, obj):
        return [{"value": v, "label": l} for v, l in MatchBy.CHOICES]
    
    def get_bundle_minutes_choices(self, obj):
        return [{"value": v, "label": l} for v, l in BundleMinutes.CHOICES]
```

### Example API Response:
```json
{
  "id": 1,
  "name": "OSSEC High Severity",
  "category": "ossec",
  "priority": 10,
  "bundle_by": 8,
  "match_by": 0,
  "bundle_minutes": 10,
  "bundle_by_choices": [
    {"value": 0, "label": "Don't bundle - each event creates new incident"},
    {"value": 1, "label": "Bundle by hostname"},
    ...
  ],
  "match_by_choices": [
    {"value": 0, "label": "All rules must match"},
    {"value": 1, "label": "Any rule can match"}
  ],
  "bundle_minutes_choices": [
    {"value": 0, "label": "Disabled - don't bundle by time"},
    {"value": 5, "label": "5 minutes"},
    {"value": 10, "label": "10 minutes"},
    ...
  ]
}
```

---

## Complete Example: React Component

```jsx
import React, { useState } from 'react';

function RuleSetForm({ ruleset, onSave }) {
  const [bundleBy, setBundleBy] = useState(ruleset?.bundle_by || 3);
  const [matchBy, setMatchBy] = useState(ruleset?.match_by || 0);
  const [bundleMinutes, setBundleMinutes] = useState(ruleset?.bundle_minutes || 10);

  const bundleByOptions = [
    { value: 0, label: "Don't bundle" },
    { value: 1, label: "By hostname" },
    { value: 2, label: "By model type" },
    { value: 3, label: "By specific model instance" },
    { value: 4, label: "By source IP" },
    { value: 5, label: "By hostname + model type" },
    { value: 6, label: "By hostname + specific model" },
    { value: 7, label: "By IP + model type" },
    { value: 8, label: "By IP + specific model" },
    { value: 9, label: "By IP + hostname" },
  ];

  const matchByOptions = [
    { value: 0, label: "All rules must match" },
    { value: 1, label: "Any rule can match" },
  ];

  const bundleMinutesOptions = [
    { value: 0, label: "Disabled" },
    { value: 5, label: "5 minutes" },
    { value: 10, label: "10 minutes" },
    { value: 15, label: "15 minutes" },
    { value: 30, label: "30 minutes" },
    { value: 60, label: "1 hour" },
    { value: 120, label: "2 hours" },
    { value: 360, label: "6 hours" },
    { value: 720, label: "12 hours" },
    { value: 1440, label: "1 day" },
    { value: null, label: "No limit - bundle forever" },
  ];

  return (
    <form onSubmit={handleSubmit}>
      <div className="form-group">
        <label>Bundle By</label>
        <select 
          value={bundleBy} 
          onChange={e => setBundleBy(Number(e.target.value))}
        >
          {bundleByOptions.map(opt => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
        <small className="help-text">
          How to group related events into a single incident
        </small>
      </div>

      <div className="form-group">
        <label>Time Window</label>
        <select 
          value={bundleMinutes} 
          onChange={e => setBundleMinutes(e.target.value === 'null' ? null : Number(e.target.value))}
        >
          {bundleMinutesOptions.map(opt => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
        <small className="help-text">
          Only bundle events that occur within this time window
        </small>
      </div>

      <div className="form-group">
        <label>Match Mode</label>
        <select 
          value={matchBy} 
          onChange={e => setMatchBy(Number(e.target.value))}
        >
          {matchByOptions.map(opt => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
        <small className="help-text">
          Whether all rules must match, or any rule can match
        </small>
      </div>

      <button type="submit">Save RuleSet</button>
    </form>
  );
}
```

---

## Usage Examples

### Example 1: Security Events from Same IP
```python
from mojo.apps.incident.models import RuleSet, BundleBy, MatchBy, BundleMinutes

ruleset = RuleSet.objects.create(
    name="Failed Login Attempts",
    category="auth",
    priority=10,
    bundle_by=BundleBy.SOURCE_IP,
    bundle_minutes=BundleMinutes.FIFTEEN_MINUTES,
    match_by=MatchBy.ALL
)
```
**Effect:** Groups all failed login events from the same IP within 15 minutes into one incident.

### Example 2: Server Issues (Any Time)
```python
ruleset = RuleSet.objects.create(
    name="Server Errors",
    category="errors",
    priority=20,
    bundle_by=BundleBy.HOSTNAME,
    bundle_minutes=None,  # No time limit
    match_by=MatchBy.ALL
)
```
**Effect:** Groups all errors from the same server forever (no time limit).

### Example 3: Critical Alerts (No Bundling)
```python
ruleset = RuleSet.objects.create(
    name="Critical Database Alerts",
    category="database",
    priority=1,
    bundle_by=BundleBy.NONE,  # Each event separate
    bundle_minutes=BundleMinutes.DISABLED,  # Ignored when bundle_by=NONE
    match_by=MatchBy.ALL
)
```
**Effect:** Every critical database alert creates its own incident immediately.

### Example 4: Resource-Specific Issues
```python
ruleset = RuleSet.objects.create(
    name="API Abuse Detection",
    category="api",
    priority=15,
    bundle_by=BundleBy.SOURCE_IP_AND_MODEL_NAME_AND_ID,
    bundle_minutes=BundleMinutes.ONE_HOUR,
    match_by=MatchBy.ANY
)
```
**Effect:** Groups API abuse from same IP targeting same resource within 1 hour.

---

## Field Validation Rules

### bundle_by
- **Type:** Integer
- **Range:** 0-9
- **Default:** 3 (Bundle by specific model instance)
- **Required:** Yes

### match_by
- **Type:** Integer
- **Range:** 0-1
- **Default:** 0 (All rules must match)
- **Required:** Yes

### bundle_minutes
- **Type:** Integer or NULL
- **Range:** 0 or positive integers
- **Default:** 0 (Disabled)
- **Required:** No (can be NULL)
- **Special Values:**
  - `0` = Disabled (each event creates new incident)
  - `NULL` = No time limit (bundle forever)
  - Any positive integer = Time window in minutes

---

## Behavior Matrix

| bundle_by | bundle_minutes | Result |
|-----------|----------------|--------|
| NONE (0) | Any value | Each event creates separate incident |
| HOSTNAME (1) | 0 (disabled) | Each event creates separate incident |
| HOSTNAME (1) | 10 | Bundle events on same hostname within 10 min |
| HOSTNAME (1) | NULL | Bundle events on same hostname forever |
| SOURCE_IP (4) | 30 | Bundle events from same IP within 30 min |

---

## Tips for UI/UX

1. **Show helpful descriptions:** Use the labels from CHOICES, not just the values
2. **Recommended defaults:**
   - `bundle_by`: `BundleBy.SOURCE_IP` (4) for security events
   - `bundle_minutes`: `BundleMinutes.TEN_MINUTES` (10)
   - `match_by`: `MatchBy.ALL` (0)

3. **Conditional display:** 
   - Hide `bundle_minutes` when `bundle_by=NONE`
   - Show warning if `bundle_minutes=NULL` (bundles forever)

4. **Validation messages:**
   - "Bundle minutes must be 0 or positive" 
   - "Cannot use negative values"

5. **Help text examples:**
   - Bundle by: "Group related events into a single incident"
   - Time window: "Only group events within this time period"
   - Match mode: "Require all rules to match, or allow any rule"

---

## Common Pitfalls

❌ **Don't:** Set `bundle_minutes=0` expecting it to bundle forever  
✅ **Do:** Set `bundle_minutes=None` to bundle forever

❌ **Don't:** Use string values like "disabled" or "none"  
✅ **Do:** Use integer values from the constants

❌ **Don't:** Forget to handle NULL value in forms  
✅ **Do:** Include NULL as an option for "no limit"

❌ **Don't:** Show all 10 bundle_by options if users only need 3-4  
✅ **Do:** Create a simplified list of most common options

---

## Questions?

These constants make your forms type-safe and consistent with the backend. Users see friendly labels, but the API receives the correct integer values.
