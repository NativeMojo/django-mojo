# Incident Rule Engine Analysis & Recommendations

**Date:** 2025-10-17  
**Status:** Critical Issues Found

## Executive Summary

A deep analysis of the incident rule engine has revealed **4 critical bugs** that break core functionality, along with **15+ areas for improvement** to make the system more manageable and reliable.

### Critical Finding
**The default OSSEC rules do not work.** Rules checking `category`, `level`, and `model_name` never match because `Rule.check_rule()` only looks in `event.metadata`, not model attributes.

---

## 🔴 Critical Bugs

### Bug #1: Rule Field Matching is Broken
**File:** `mojo/apps/incident/models/rule.py:350-361`  
**Severity:** CRITICAL  
**Impact:** Default rules don't work; rules checking model fields fail

**Problem:**
```python
def check_rule(self, event):
    field_value = event.metadata.get(self.field_name, None)  # Only checks metadata!
```

Rules that check `category`, `level`, `hostname`, `model_name`, `model_id`, `source_ip`, `uid`, or `country_code` will **always fail** because these are model fields, not metadata fields.

**Evidence:**
- Default OSSEC rules created by `ensure_default_rules()` check these fields
- They will never match any events
- The entire default rule system is non-functional

**Why It Works Sometimes:**
The `sync_metadata()` method copies model fields INTO metadata before `publish()` is called, but:
1. This is undocumented and confusing
2. It's fragile - requires perfect timing
3. Rules can't check fields before sync
4. Users don't know to put fields in metadata

**Recommended Fix:**
```python
def check_rule(self, event):
    """Check if event matches this rule."""
    # Try model field first, then metadata
    if hasattr(event, self.field_name):
        field_value = getattr(event, self.field_name)
    else:
        field_value = event.metadata.get(self.field_name, None)
    
    if field_value is None:
        return False
    
    # Rest of logic unchanged...
```

---

### Bug #2: Bundle Time Window Broken When bundle_minutes=0
**File:** `mojo/apps/incident/models/event.py:239-257`  
**Severity:** HIGH  
**Impact:** Events bundle forever instead of not bundling

**Problem:**
```python
if rule_set.bundle_minutes:  # If 0, this is False
    bundle_criteria['created__gte'] = dates.subtract(minutes=rule_set.bundle_minutes)
```

When `bundle_minutes=0` (the default, meaning "don't bundle by time"), no time filter is added. This causes incidents to bundle together across **all time** instead of not bundling.

**Recommended Fix:**
```python
# Use None to mean "no time limit" and 0 to mean "don't bundle at all"
if rule_set.bundle_minutes is not None and rule_set.bundle_minutes > 0:
    bundle_criteria['created__gte'] = dates.subtract(minutes=rule_set.bundle_minutes)
elif rule_set.bundle_minutes == 0:
    # Don't bundle - each event creates new incident
    # Add timestamp to make criteria unique
    bundle_criteria['created__exact'] = timezone.now()
```

---

### Bug #3: Handler Transition Detection Broken
**File:** `mojo/apps/incident/models/event.py:147-153`  
**Severity:** MEDIUM  
**Impact:** Handlers don't execute on pending→new transition

**Problem:**
```python
prev_status = getattr(incident, "status", None)  # Gets CURRENT status
if rule_set and (min_count or window_minutes):
    # ... changes incident.status ...
    incident.save(update_fields=["status"])

# prev_status == incident.status, so this never detects transition
transitioned_to_new = (prev_status == pending_status and 
                        getattr(incident, "status", None) == "open")
```

`prev_status` is captured AFTER the status may have already been modified, so transition detection doesn't work.

**Recommended Fix:**
```python
# Capture BEFORE any modifications
prev_status = incident.status if incident.pk else None

# Now do threshold logic...
if rule_set and (min_count or window_minutes):
    desired_status = "new" if meets_threshold else pending_status
    if incident.status != desired_status:
        incident.status = desired_status
        incident.save(update_fields=["status"])

# Now comparison works correctly
transitioned_to_new = (prev_status == pending_status and incident.status == "new")
```

---

### Bug #4: Threshold Settings Hidden in Unstructured Metadata
**File:** `mojo/apps/incident/models/event.py:120-130`  
**Severity:** MEDIUM  
**Impact:** Hard to discover, error-prone, no validation

**Problem:**
```python
min_count = int(rule_set.metadata.get("min_count")) if rule_set.metadata.get("min_count") is not None else None
window_minutes = int(rule_set.metadata.get("window_minutes")) if rule_set.metadata.get("window_minutes") is not None else None
pending_status = rule_set.metadata.get("pending_status", "pending")
```

Critical settings are buried in JSON metadata instead of being first-class fields:
- No schema validation
- No admin interface support
- Easy to mistype keys
- No documentation
- Can't query efficiently

**Recommended Fix:**
Add proper model fields to RuleSet:
```python
class RuleSet(models.Model):
    # ... existing fields ...
    min_count = models.IntegerField(default=None, null=True, blank=True,
        help_text="Minimum events required before incident becomes 'open'")
    window_minutes = models.IntegerField(default=None, null=True, blank=True,
        help_text="Time window for counting events toward threshold")
    pending_status = models.CharField(max_length=50, default='pending',
        help_text="Status to use while below threshold")
```

---

## 🟡 Major Functional Gaps

### Gap #1: No Rule Testing Interface
**Impact:** Can't validate rules before deploying

**Missing Capabilities:**
- Test if a rule matches sample data
- Preview which rules would trigger
- Debug why a rule isn't matching
- See what field values are being compared

**Recommendation:**
```python
class Rule(models.Model):
    def test(self, sample_event_data):
        """Test if rule would match sample data. Returns (matched, debug_info)"""
        event = self._create_mock_event(sample_event_data)
        matched = self.check_rule(event)
        
        return matched, {
            "field_name": self.field_name,
            "field_value": self._get_field_value(event),
            "comparator": self.comparator,
            "expected_value": self.value,
            "result": matched,
            "error": None
        }
```

### Gap #2: No Rule Audit Trail
**Impact:** Can't track effectiveness or debug issues

**Missing Data:**
- When was rule last triggered
- How many times has it matched
- What events matched
- Rule change history

**Recommendation:**
Add tracking fields and execution log:
```python
class RuleSet(models.Model):
    # ... existing fields ...
    enabled = models.BooleanField(default=True)
    last_triggered = models.DateTimeField(null=True, blank=True)
    trigger_count = models.IntegerField(default=0)
    created_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)

class RuleExecutionLog(models.Model):
    rule_set = models.ForeignKey(RuleSet, on_delete=models.CASCADE)
    event = models.ForeignKey(Event, on_delete=models.CASCADE)
    matched = models.BooleanField()
    executed_at = models.DateTimeField(auto_now_add=True)
    debug_data = models.JSONField(default=dict)
```

### Gap #3: Bundle Configuration is Cryptic
**Impact:** Hard to understand and maintain

**Current System:**
```python
bundle_by = models.IntegerField(default=3)
# 0=none, 1=hostname, 2=model_name, 3=model_name+model_id, 
# 4=source_ip, 5=hostname+model_name, etc...
```

10 hard-coded combinations that are:
- Not self-documenting
- Limited in flexibility
- Require consulting documentation

**Recommendation:**
Use descriptive JSONField:
```python
bundle_config = models.JSONField(default=dict, blank=True,
    help_text='Bundle criteria, e.g. {"fields": ["hostname", "source_ip"], "time_window": 10}')

# Migration: Convert existing bundle_by values
BUNDLE_BY_MAPPINGS = {
    0: {"fields": []},
    1: {"fields": ["hostname"]},
    2: {"fields": ["model_name"]},
    3: {"fields": ["model_name", "model_id"]},
    4: {"fields": ["source_ip"]},
    # ... etc
}
```

### Gap #4: Handler Configuration is Fragile
**Impact:** Errors silently fail, hard to debug

**Current Issues:**
- Custom URL-like syntax with no validation
- Regex splitting can break with special characters
- Errors are caught and swallowed
- No way to test handlers
- No handler execution history

**Recommendation:**
1. **Add validation on save:**
```python
def clean(self):
    if self.handler and self.handler != "ignore":
        try:
            self._parse_and_validate_handlers()
        except Exception as e:
            raise ValidationError(f"Invalid handler syntax: {e}")
```

2. **Add handler execution log:**
```python
class HandlerExecutionLog(models.Model):
    rule_set = models.ForeignKey(RuleSet, on_delete=models.CASCADE)
    event = models.ForeignKey(Event, on_delete=models.CASCADE)
    handler_spec = models.TextField()
    executed_at = models.DateTimeField(auto_now_add=True)
    success = models.BooleanField()
    error_message = models.TextField(null=True, blank=True)
```

3. **Consider structured handler config:**
```python
handlers = models.JSONField(default=list, blank=True)
# Example: [
#   {"type": "task", "name": "process_incident", "params": {"severity": "high"}},
#   {"type": "email", "recipients": ["admin@example.com"]},
#   {"type": "ticket", "status": "open", "priority": 8}
# ]
```

### Gap #5: No Dry-Run or Simulation Mode
**Impact:** Can't safely test rule changes

**Recommendation:**
```python
class RuleSet(models.Model):
    # ... existing fields ...
    dry_run = models.BooleanField(default=False,
        help_text="Log matches but don't create incidents or run handlers")
    
def publish(self):
    # ... existing logic ...
    if rule_set and rule_set.dry_run:
        logger.info(f"DRY RUN: Would create incident for event {self.id}")
        return
    # ... rest of logic ...
```

---

## 🟢 Usability Improvements

### 1. Rule Templates
Make common patterns easy to deploy:
```python
RULE_TEMPLATES = {
    "high_severity": {
        "name": "High Severity Events",
        "description": "Match events with level >= 8",
        "rules": [
            {"field_name": "level", "comparator": ">=", "value": "8", "value_type": "int"}
        ]
    },
    "auth_failures": {
        "name": "Authentication Failures",
        "description": "Match failed authentication attempts",
        "match_by": 0,
        "rules": [
            {"field_name": "category", "comparator": "==", "value": "auth", "value_type": "str"},
            {"field_name": "details", "comparator": "contains", "value": "failed", "value_type": "str"}
        ]
    },
    # ... more templates
}
```

### 2. Rule Import/Export
Enable backup and sharing:
```python
def export_ruleset(self):
    """Export as JSON for backup/sharing"""
    return {
        "ruleset": {
            "name": self.name,
            "category": self.category,
            "priority": self.priority,
            # ... all fields
        },
        "rules": [
            {
                "name": r.name,
                "field_name": r.field_name,
                # ... all fields
            } for r in self.rules.all()
        ]
    }

@classmethod
def import_ruleset(cls, data):
    """Import from JSON"""
    pass
```

### 3. Rule Suggestions
Help users create rules after incidents:
```python
@classmethod
def suggest_rules_for_events(cls, events):
    """
    Analyze events and suggest rules that would catch them.
    Useful for "I want to catch events like this" use case.
    """
    # Analyze common fields, patterns, thresholds
    # Return suggested rule configurations
    pass
```

### 4. Better Error Messages
Current: Silent failures  
Improved: Detailed logging and error visibility

```python
def run_handler(self, event, incident=None):
    # ... existing code ...
    except Exception as e:
        error_msg = f"Error in handler {handler_type}://{handler_url.netloc}: {str(e)}"
        logger.error(error_msg)
        
        # Store in database for visibility
        HandlerExecutionLog.objects.create(
            rule_set=self,
            event=event,
            handler_spec=spec,
            success=False,
            error_message=error_msg
        )
        return False
```

### 5. Time-Based Rules
Enable business hours filtering:
```python
class RuleSet(models.Model):
    # ... existing fields ...
    active_schedule = models.JSONField(default=dict, blank=True,
        help_text='When rule is active, e.g. {"weekdays": [1,2,3,4,5], "hours": [9,17]}')
    
def is_active(self):
    """Check if rule is currently active based on schedule"""
    if not self.active_schedule:
        return True
    
    now = timezone.now()
    if "weekdays" in self.active_schedule:
        if now.weekday() not in self.active_schedule["weekdays"]:
            return False
    
    if "hours" in self.active_schedule:
        if now.hour < self.active_schedule["hours"][0] or now.hour > self.active_schedule["hours"][1]:
            return False
    
    return True
```

### 6. Rule Dependencies
Enable conditional rules:
```python
class Rule(models.Model):
    # ... existing fields ...
    depends_on = models.ForeignKey('self', null=True, blank=True, 
        help_text="Only evaluate this rule if parent rule matches")
```

### 7. Dashboard & Statistics
Track rule effectiveness:
- Trigger frequency
- Average time to resolution
- False positive rate
- Most common patterns
- Rule performance metrics

---

## 📋 Implementation Priority

### Phase 1: Critical Bug Fixes (Required)
1. ✅ Fix `Rule.check_rule()` to check model fields first
2. ✅ Fix bundle time window logic
3. ✅ Fix handler transition detection
4. ✅ Move threshold settings to model fields

### Phase 2: Essential Features (High Priority)
5. Add rule testing interface
6. Add basic audit logging (last_triggered, trigger_count)
7. Add handler validation on save
8. Add dry-run mode
9. Improve error messages and logging

### Phase 3: Usability (Medium Priority)
10. Add rule templates
11. Convert bundle_by to descriptive config
12. Add rule import/export
13. Add handler execution log
14. Add enabled/disabled flag

### Phase 4: Advanced Features (Nice to Have)
15. Time-based rules
16. Rule dependencies
17. Rule suggestions
18. Dashboard and statistics
19. Structured handler configuration
20. Performance optimizations

---

## 🧪 Test Suite

A comprehensive test suite has been created at:
**`tests/test_incident/rule_engine_comprehensive.py`**

### Test Coverage

**Critical Bug Tests:**
- `test_bug_rule_cannot_match_model_fields()` - Exposes Bug #1
- `test_bug_default_ossec_rules_never_match()` - Exposes Bug #1 impact
- `test_bug_bundle_minutes_zero_bundles_forever()` - Exposes Bug #2
- `test_bug_handler_transition_detection_broken()` - Exposes Bug #3

**Field Matching Tests:**
- All comparator types (==, >, >=, <, <=, contains, regex)
- Type conversion (int, float, str)
- Metadata vs model fields

**RuleSet Matching Tests:**
- match_by=0 (all rules must match)
- match_by=1 (any rule can match)
- Priority ordering

**Bundling Tests:**
- Bundle by hostname (bundle_by=1)
- Bundle by model (bundle_by=3)
- Bundle by source_ip (bundle_by=4)
- Time window enforcement

**Threshold Tests:**
- min_count behavior
- window_minutes behavior
- Status transitions (pending → new)

**Handler Tests:**
- Ignore handler
- Task handler
- Handler chaining
- Ticket creation

**Integration Tests:**
- Full event → incident flow
- Priority escalation
- Metadata preservation

### Running Tests

```bash
# Run all rule engine tests
testit tests/test_incident/rule_engine_comprehensive.py

# Run specific test
testit tests/test_incident/rule_engine_comprehensive.py::test_bug_rule_cannot_match_model_fields
```

### Expected Results

Many tests will **FAIL** until the bugs are fixed. This is intentional - the tests document expected behavior and expose the bugs.

---

## 📚 Additional Resources

**Related Files:**
- `mojo/apps/incident/models/rule.py` - RuleSet and Rule models
- `mojo/apps/incident/models/event.py` - Event model and publish logic
- `mojo/apps/incident/models/incident.py` - Incident model
- `mojo/apps/incident/reporter.py` - Event reporting interface
- `mojo/apps/incident/handlers/event_handlers.py` - Handler implementations

**Existing Tests:**
- `tests/test_incident/rule.py` - Basic rule tests (some may be misleading due to bugs)
- `tests/test_incident/reporting_local.py` - Event reporting tests

---

## 🎯 Success Metrics

After implementing fixes and improvements:

1. **Reliability:**
   - All default OSSEC rules work correctly
   - Zero silent handler failures
   - Bundling behaves predictably

2. **Observability:**
   - Can see when rules were last triggered
   - Can debug why rules didn't match
   - Handler execution is logged

3. **Usability:**
   - Can test rules before deploying
   - Can import/export rule configurations
   - Rule configuration is self-documenting

4. **Maintainability:**
   - 100% test coverage of rule engine
   - Documentation matches actual behavior
   - Common patterns have templates

---

## Questions?

This analysis was generated by reviewing:
- Source code in `mojo/apps/incident/`
- Existing tests in `tests/test_incident/`
- Data flow from event creation to incident

For questions or clarifications, please review the test suite which demonstrates expected behavior.
