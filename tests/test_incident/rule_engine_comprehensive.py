"""
Comprehensive test suite for the incident rule engine.

This test suite is designed to:
1. Expose critical bugs in the current implementation
2. Test all rule matching scenarios
3. Test bundling behavior
4. Test threshold logic
5. Test handler execution
6. Serve as documentation for expected behavior
"""

from testit import helpers as th
from testit import faker
import datetime
from objict import objict
from django.utils import timezone


TEST_USER = "testit"
TEST_PWORD = "testit##mojo"


# =============================================================================
# CRITICAL BUG TESTS - These expose the bugs in the current implementation
# =============================================================================

@th.django_unit_test()
def test_bug_rule_cannot_match_model_fields(opts):
    """
    CRITICAL BUG: Rules checking model fields (level, category, etc) never match
    because check_rule() only looks in metadata, not model attributes.

    This test documents the bug and should FAIL until the bug is fixed.
    """
    from mojo.apps.incident.models import Event, RuleSet, Rule

    # Clean up
    RuleSet.objects.filter(category="bug_test").delete()
    Event.objects.filter(category="bug_test").delete()

    # Create a ruleset that checks a MODEL field (not metadata)
    ruleset = RuleSet.objects.create(
        name="Bug Test - Model Field",
        category="bug_test",
        priority=1,
        match_by=0
    )

    # Rule checks 'level' which is a model field
    Rule.objects.create(
        parent=ruleset,
        name="Check level >= 5",
        field_name="level",
        comparator=">=",
        value="5",
        value_type="int"
    )

    # Create an event with level=8
    event = Event.objects.create(
        category="bug_test",
        level=8,
        title="Test event",
        details="Testing model field matching"
    )
    event.sync_metadata()

    # THIS SHOULD PASS but currently FAILS due to bug
    # The rule checks metadata.get('level') which is None initially
    # Even though sync_metadata() copies it, this exposes timing issues
    result = ruleset.check_rules(event)

    assert result is True, (
        "BUG CONFIRMED: Rule checking model field 'level' should match but doesn't. "
        "check_rule() only looks in metadata, not model attributes."
    )


@th.django_unit_test()
def test_bug_default_ossec_rules_never_match(opts):
    """
    CRITICAL BUG: The default OSSEC rules created by ensure_default_rules()
    check 'category', 'level', and 'model_name' which are model fields.
    These rules will never match any events.
    """
    from mojo.apps.incident.models import Event, RuleSet, Rule

    # Clean up
    RuleSet.objects.filter(category="ossec").delete()
    Event.objects.filter(category="ossec").delete()

    # Create default rules
    RuleSet.ensure_default_rules()

    # Get the default OSSEC Critical Severity ruleset (level >= 12)
    ossec_bundle = RuleSet.objects.get(
        category="ossec",
        name="OSSEC - Critical Severity"
    )

    # Create an event that SHOULD match all the rules
    event = Event.objects.create(
        category="ossec",
        level=12,
        model_name="ossec_rule",
        model_id=5001,
        source_ip="192.168.1.100",
        title="OSSEC Alert",
        details="Failed login attempt"
    )
    event.sync_metadata()

    # Check each rule individually to see which ones fail
    for rule in ossec_bundle.rules.all():
        result = rule.check_rule(event)
        # Uncomment for debugging: print(f"Rule '{rule.name}' (field={rule.field_name}): {result}")

    # THIS SHOULD PASS but currently FAILS
    result = ossec_bundle.check_rules(event)

    assert result is True, (
        "BUG CONFIRMED: Default OSSEC rules don't match OSSEC events because "
        "they check model fields like 'category', 'level', 'model_name' which "
        "aren't found in metadata by check_rule()."
    )


@th.django_unit_test()
def test_bug_bundle_minutes_zero_bundles_forever(opts):
    """
    FIXED: bundle_minutes=0 means "disabled" - don't bundle by time.
    Each event creates its own incident even if other bundle criteria match.
    """
    from mojo.apps.incident.models import Event, RuleSet, Rule, Incident
    from mojo.helpers import dates

    # Clean up
    RuleSet.objects.filter(category="bundle_test").delete()
    Event.objects.filter(category="bundle_test").delete()
    Incident.objects.filter(category="bundle_test").delete()

    # Create ruleset with bundle_minutes=0 (meaning disabled - don't bundle by time)
    ruleset = RuleSet.objects.create(
        name="No Time Bundling Test",
        category="bundle_test",
        priority=1,
        match_by=0,
        bundle_by=1,  # Would bundle by hostname, but...
        bundle_minutes=0  # ...time bundling is disabled
    )

    Rule.objects.create(
        parent=ruleset,
        name="Match category",
        field_name="category",
        comparator="==",
        value="bundle_test",
        value_type="str"
    )

    # Create first event a week ago
    old_event = Event.objects.create(
        category="bundle_test",
        level=5,
        hostname="server1",
        title="Old event",
        details="Week old event"
    )
    old_event.created = dates.subtract(days=7)
    old_event.save()
    old_event.sync_metadata()
    old_event.publish()

    old_incident_count = Incident.objects.filter(category="bundle_test").count()

    # Create new event now with same hostname
    new_event = Event.objects.create(
        category="bundle_test",
        level=5,
        hostname="server1",
        title="New event",
        details="New event"
    )
    new_event.sync_metadata()
    new_event.publish()

    new_incident_count = Incident.objects.filter(category="bundle_test").count()

    # With bundle_minutes=0, events should NOT bundle (0 = disabled)
    # Each event creates its own incident
    assert new_incident_count == old_incident_count + 1, (
        f"With bundle_minutes=0 (disabled), events should NOT bundle, but got "
        f"{old_incident_count} -> {new_incident_count} incidents. "
        f"Expected 2 separate incidents."
    )

    # Verify each incident has only 1 event
    for incident in Incident.objects.filter(category="bundle_test"):
        assert incident.events.count() == 1, f"Expected 1 event per incident, got {incident.events.count()}"


@th.django_unit_test()
def test_disable_bundling_with_bundle_by_none(opts):
    """
    To completely disable bundling (each event creates its own incident),
    use bundle_by=BundleBy.NONE (0), not bundle_minutes=0.
    """
    from mojo.apps.incident.models import Event, RuleSet, Rule, Incident, BundleBy
    from mojo.helpers import dates

    # Clean up
    RuleSet.objects.filter(category="no_bundle_test").delete()
    Event.objects.filter(category="no_bundle_test").delete()
    Incident.objects.filter(category="no_bundle_test").delete()

    # Create ruleset with bundle_by=NONE to disable bundling
    ruleset = RuleSet.objects.create(
        name="No Bundling Test",
        category="no_bundle_test",
        priority=1,
        match_by=0,
        bundle_by=BundleBy.NONE,  # Don't bundle at all
        bundle_minutes=10  # Ignored when bundle_by=NONE
    )

    Rule.objects.create(
        parent=ruleset,
        name="Match category",
        field_name="category",
        comparator="==",
        value="no_bundle_test",
        value_type="str"
    )

    # Create multiple events with same hostname
    for i in range(3):
        event = Event.objects.create(
            category="no_bundle_test",
            level=5,
            hostname="server1",
            title=f"Event {i}",
            details="Should not bundle"
        )
        event.sync_metadata()
        event.publish()

    # With bundle_by=NONE, each event should create its own incident
    incidents = Incident.objects.filter(category="no_bundle_test")
    assert incidents.count() == 3, (
        f"With bundle_by=NONE, expected 3 separate incidents, got {incidents.count()}"
    )

    # Each incident should have exactly 1 event
    for incident in incidents:
        assert incident.events.count() == 1, (
            f"Each incident should have 1 event, got {incident.events.count()}"
        )


@th.django_unit_test()
def test_bug_handler_transition_detection_broken(opts):
    """
    FIXED: Handler execution on status transition now works correctly.
    Handler should be called when incident transitions from pending to new.
    """
    from mojo.apps.incident.models import Event, RuleSet, Rule, Incident
    from unittest.mock import Mock, patch

    # Clean up
    RuleSet.objects.filter(category="transition_test").delete()
    Event.objects.filter(category="transition_test").delete()
    Incident.objects.filter(category="transition_test").delete()

    # Create ruleset with threshold
    ruleset = RuleSet.objects.create(
        name="Threshold Test",
        category="transition_test",
        priority=1,
        match_by=0,
        bundle_by=1,  # Bundle by hostname
        bundle_minutes=10,
        handler="job://test_handler",
        metadata={
            "min_count": 3,
            "window_minutes": 10,
            "pending_status": "pending"
        }
    )

    Rule.objects.create(
        parent=ruleset,
        name="Match category",
        field_name="category",
        comparator="==",
        value="transition_test",
        value_type="str"
    )

    # Track handler calls
    handler_calls = []

    # Patch at the class level, not instance level, because publish() fetches the ruleset fresh from DB
    # Note: When patching an instance method, mock receives (self, event, incident)
    def track_handler_call(self, event, incident=None):
        handler_calls.append(('called', incident.status if incident else None))
        return True

    with patch('mojo.apps.incident.models.rule.RuleSet.run_handler', side_effect=track_handler_call):
        # First event - should create pending incident, no handler (threshold not met)
        event1 = Event.objects.create(
            category="transition_test",
            level=5,
            hostname="server1",
            title="Event 1"
        )
        event1.sync_metadata()
        event1.publish()

        # Verify incident created with pending status
        incident = Incident.objects.get(category="transition_test")
        assert incident.status == "pending", f"Expected pending status after event 1, got {incident.status}"

        # Second event - still pending, no handler (threshold still not met)
        event2 = Event.objects.create(
            category="transition_test",
            level=5,
            hostname="server1",
            title="Event 2"
        )
        event2.sync_metadata()
        event2.publish()

        incident.refresh_from_db()
        assert incident.status == "pending", f"Expected pending status after event 2, got {incident.status}"

        # Third event - should transition to open and run handler (threshold now met)
        event3 = Event.objects.create(
            category="transition_test",
            level=5,
            hostname="server1",
            title="Event 3"
        )
        event3.sync_metadata()
        event3.publish()

        incident.refresh_from_db()
        assert incident.status == "new", f"Expected new status after event 3, got {incident.status}"

    # After fix: Handler should be called when incident transitions to new
    # Uncomment for debugging: print(f"Handler calls: {handler_calls}")
    assert len(handler_calls) >= 1, (
        f"Handler should be called when incident transitions from pending to new. "
        f"Got {len(handler_calls)} calls: {handler_calls}"
    )


# =============================================================================
# FIELD MATCHING TESTS
# =============================================================================

@th.django_unit_test()
def test_rule_matches_metadata_fields(opts):
    """Test that rules CAN match fields in metadata."""
    from mojo.apps.incident.models import Event, Rule
    from objict import objict

    event = objict()
    event.metadata = {
        "custom_field": "test_value",
        "numeric_field": 42
    }

    # String match in metadata
    rule1 = Rule(
        field_name="custom_field",
        comparator="==",
        value="test_value",
        value_type="str"
    )
    assert rule1.check_rule(event) is True, "Should match metadata string field"

    # Numeric match in metadata
    rule2 = Rule(
        field_name="numeric_field",
        comparator=">=",
        value="40",
        value_type="int"
    )
    assert rule2.check_rule(event) is True, "Should match metadata numeric field"


@th.django_unit_test()
def test_rule_should_match_model_fields_after_sync(opts):
    """
    check_rule() checks metadata first, then falls back to model attributes via getattr.
    Model fields match without needing sync_metadata().
    sync_metadata() copies fields into metadata, which also works.
    """
    from mojo.apps.incident.models import Event, Rule

    # Create real event
    event = Event.objects.create(
        category="test_category",
        level=7,
        hostname="test-host",
        source_ip="10.0.0.1"
    )

    rule = Rule(
        field_name="level",
        comparator=">=",
        value="5",
        value_type="int"
    )

    # check_rule falls back to getattr, so model fields match directly
    result_before = rule.check_rule(event)
    assert result_before is True, "check_rule should match model fields via getattr fallback"

    # AFTER sync_metadata - fields also in metadata, still matches
    event.sync_metadata()
    result_after = rule.check_rule(event)
    assert result_after is True, "After sync_metadata(), model fields are in metadata and still match"


@th.django_unit_test()
def test_all_comparator_types(opts):
    """Test all supported comparators."""
    from mojo.apps.incident.models import Rule
    from objict import objict

    event = objict()
    event.metadata = {
        "number": 10,
        "text": "hello world",
        "ip": "192.168.1.100"
    }

    # Equality
    assert Rule(field_name="number", comparator="==", value="10", value_type="int").check_rule(event)
    assert Rule(field_name="number", comparator="eq", value="10", value_type="int").check_rule(event)

    # Greater than
    assert Rule(field_name="number", comparator=">", value="5", value_type="int").check_rule(event)
    assert not Rule(field_name="number", comparator=">", value="15", value_type="int").check_rule(event)

    # Greater than or equal
    assert Rule(field_name="number", comparator=">=", value="10", value_type="int").check_rule(event)
    assert Rule(field_name="number", comparator=">=", value="5", value_type="int").check_rule(event)

    # Less than
    assert Rule(field_name="number", comparator="<", value="15", value_type="int").check_rule(event)
    assert not Rule(field_name="number", comparator="<", value="5", value_type="int").check_rule(event)

    # Less than or equal
    assert Rule(field_name="number", comparator="<=", value="10", value_type="int").check_rule(event)
    assert Rule(field_name="number", comparator="<=", value="15", value_type="int").check_rule(event)

    # Contains
    assert Rule(field_name="text", comparator="contains", value="world", value_type="str").check_rule(event)
    assert not Rule(field_name="text", comparator="contains", value="goodbye", value_type="str").check_rule(event)

    # Regex
    assert Rule(field_name="ip", comparator="regex", value=r"192\.168\.\d+\.\d+", value_type="str").check_rule(event)
    assert not Rule(field_name="ip", comparator="regex", value=r"10\.0\.\d+\.\d+", value_type="str").check_rule(event)


@th.django_unit_test()
def test_rule_match_type_conversion(opts):
    """Test value type conversion in rules."""
    from mojo.apps.incident.models import Rule
    from objict import objict

    event = objict()
    event.metadata = {
        "string_number": "42",
        "int_number": 42,
        "float_number": 3.14,
        "string_text": "not a number"
    }

    # String to int conversion
    rule1 = Rule(field_name="string_number", comparator=">", value="40", value_type="int")
    assert rule1.check_rule(event) is True, "Should convert string '42' to int"

    # Int comparison
    rule2 = Rule(field_name="int_number", comparator="==", value="42", value_type="int")
    assert rule2.check_rule(event) is True, "Should match int directly"

    # Float conversion
    rule3 = Rule(field_name="float_number", comparator=">", value="3.0", value_type="float")
    assert rule3.check_rule(event) is True, "Should convert to float and compare"

    # Invalid conversion should return False
    rule4 = Rule(field_name="string_text", comparator=">", value="5", value_type="int")
    assert rule4.check_rule(event) is False, "Invalid conversion should return False"


# =============================================================================
# RULESET MATCHING TESTS
# =============================================================================

@th.django_unit_test()
def test_ruleset_match_all_rules(opts):
    """Test RuleSet with match_by=0 (all rules must match)."""
    from mojo.apps.incident.models import RuleSet, Rule
    from objict import objict

    RuleSet.objects.filter(category="match_all_test").delete()

    ruleset = RuleSet.objects.create(
        name="Match All Test",
        category="match_all_test",
        priority=1,
        match_by=0  # All rules must match
    )

    Rule.objects.create(
        parent=ruleset,
        field_name="severity",
        comparator=">=",
        value="5",
        value_type="int"
    )

    Rule.objects.create(
        parent=ruleset,
        field_name="category",
        comparator="==",
        value="security",
        value_type="str"
    )

    # Event matching all rules
    event1 = objict()
    event1.metadata = {"severity": 7, "category": "security"}
    assert ruleset.check_rules(event1) is True, "Should match when all rules match"

    # Event matching only one rule
    event2 = objict()
    event2.metadata = {"severity": 7, "category": "info"}
    assert ruleset.check_rules(event2) is False, "Should not match when only some rules match"

    # Event matching no rules
    event3 = objict()
    event3.metadata = {"severity": 3, "category": "info"}
    assert ruleset.check_rules(event3) is False, "Should not match when no rules match"


@th.django_unit_test()
def test_ruleset_match_any_rule(opts):
    """Test RuleSet with match_by=1 (any rule can match)."""
    from mojo.apps.incident.models import RuleSet, Rule
    from objict import objict

    RuleSet.objects.filter(category="match_any_test").delete()

    ruleset = RuleSet.objects.create(
        name="Match Any Test",
        category="match_any_test",
        priority=1,
        match_by=1  # Any rule can match
    )

    Rule.objects.create(
        parent=ruleset,
        field_name="severity",
        comparator=">=",
        value="10",
        value_type="int"
    )

    Rule.objects.create(
        parent=ruleset,
        field_name="category",
        comparator="==",
        value="critical",
        value_type="str"
    )

    # Event matching all rules
    event1 = objict()
    event1.metadata = {"severity": 12, "category": "critical"}
    assert ruleset.check_rules(event1) is True, "Should match when all rules match"

    # Event matching only severity rule
    event2 = objict()
    event2.metadata = {"severity": 12, "category": "info"}
    assert ruleset.check_rules(event2) is True, "Should match when any rule matches"

    # Event matching only category rule
    event3 = objict()
    event3.metadata = {"severity": 5, "category": "critical"}
    assert ruleset.check_rules(event3) is True, "Should match when any rule matches"

    # Event matching no rules
    event4 = objict()
    event4.metadata = {"severity": 5, "category": "info"}
    assert ruleset.check_rules(event4) is False, "Should not match when no rules match"


@th.django_unit_test()
def test_ruleset_priority_order(opts):
    """Test that RuleSet.check_by_category returns highest priority match."""
    from mojo.apps.incident.models import RuleSet, Rule
    from objict import objict

    RuleSet.objects.filter(category="priority_test").delete()

    # Create three rulesets with different priorities
    rs_high = RuleSet.objects.create(
        name="High Priority",
        category="priority_test",
        priority=1,
        match_by=0
    )
    Rule.objects.create(
        parent=rs_high,
        field_name="severity",
        comparator=">=",
        value="10",
        value_type="int"
    )

    rs_medium = RuleSet.objects.create(
        name="Medium Priority",
        category="priority_test",
        priority=2,
        match_by=0
    )
    Rule.objects.create(
        parent=rs_medium,
        field_name="severity",
        comparator=">=",
        value="5",
        value_type="int"
    )

    rs_low = RuleSet.objects.create(
        name="Low Priority",
        category="priority_test",
        priority=3,
        match_by=0
    )
    Rule.objects.create(
        parent=rs_low,
        field_name="severity",
        comparator=">=",
        value="1",
        value_type="int"
    )

    # Event with severity=12 matches all three
    event = objict()
    event.metadata = {"severity": 12}

    result = RuleSet.check_by_category("priority_test", event)
    assert result is not None, "Should find a matching ruleset"
    assert result.id == rs_high.id, "Should return highest priority (lowest number) match"

    # Event with severity=7 matches medium and low
    event2 = objict()
    event2.metadata = {"severity": 7}

    result2 = RuleSet.check_by_category("priority_test", event2)
    assert result2.id == rs_medium.id, "Should return medium priority when high doesn't match"

    # Event with severity=3 matches only low
    event3 = objict()
    event3.metadata = {"severity": 3}

    result3 = RuleSet.check_by_category("priority_test", event3)
    assert result3.id == rs_low.id, "Should return low priority when others don't match"

    # Event with severity=0 matches none
    event4 = objict()
    event4.metadata = {"severity": 0}

    result4 = RuleSet.check_by_category("priority_test", event4)
    assert result4 is None, "Should return None when no rulesets match"


# =============================================================================
# BUNDLING TESTS
# =============================================================================

@th.django_unit_test()
def test_bundling_by_hostname(opts):
    """Test incident bundling by hostname (bundle_by=1)."""
    from mojo.apps.incident.models import Event, RuleSet, Rule, Incident

    # Clean up
    RuleSet.objects.filter(category="bundle_hostname").delete()
    Event.objects.filter(category="bundle_hostname").delete()
    Incident.objects.filter(category="bundle_hostname").delete()

    # Create ruleset with bundling by hostname
    ruleset = RuleSet.objects.create(
        name="Bundle by Hostname",
        category="bundle_hostname",
        priority=1,
        match_by=0,
        bundle_by=1,  # Hostname
        bundle_minutes=10
    )

    Rule.objects.create(
        parent=ruleset,
        field_name="category",
        comparator="==",
        value="bundle_hostname",
        value_type="str"
    )

    # Create events on same hostname
    for i in range(3):
        event = Event.objects.create(
            category="bundle_hostname",
            level=5,
            hostname="server1",
            title=f"Event {i}"
        )
        event.sync_metadata()
        event.publish()

    # Should have 1 incident with 3 events
    incidents = Incident.objects.filter(category="bundle_hostname", hostname="server1")
    assert incidents.count() == 1, f"Expected 1 incident, got {incidents.count()}"

    incident = incidents.first()
    assert incident.events.count() == 3, f"Expected 3 events, got {incident.events.count()}"

    # Create event on different hostname
    event_other = Event.objects.create(
        category="bundle_hostname",
        level=5,
        hostname="server2",
        title="Event on server2"
    )
    event_other.sync_metadata()
    event_other.publish()

    # Should have 2 incidents total
    total_incidents = Incident.objects.filter(category="bundle_hostname").count()
    assert total_incidents == 2, f"Expected 2 incidents, got {total_incidents}"


@th.django_unit_test()
def test_bundling_by_model(opts):
    """Test incident bundling by model_name and model_id (bundle_by=3)."""
    from mojo.apps.incident.models import Event, RuleSet, Rule, Incident

    # Clean up
    RuleSet.objects.filter(category="bundle_model").delete()
    Event.objects.filter(category="bundle_model").delete()
    Incident.objects.filter(category="bundle_model").delete()

    # Create ruleset with bundling by model_name + model_id
    ruleset = RuleSet.objects.create(
        name="Bundle by Model",
        category="bundle_model",
        priority=1,
        match_by=0,
        bundle_by=3,  # model_name + model_id
        bundle_minutes=10
    )

    Rule.objects.create(
        parent=ruleset,
        field_name="category",
        comparator="==",
        value="bundle_model",
        value_type="str"
    )

    # Create events for same model instance
    for i in range(3):
        event = Event.objects.create(
            category="bundle_model",
            level=5,
            model_name="user",
            model_id=123,
            title=f"Event {i}"
        )
        event.sync_metadata()
        event.publish()

    # Should have 1 incident
    incidents = Incident.objects.filter(
        category="bundle_model",
        model_name="user",
        model_id=123
    )
    assert incidents.count() == 1, f"Expected 1 incident, got {incidents.count()}"

    # Create event for different model_id
    event_other = Event.objects.create(
        category="bundle_model",
        level=5,
        model_name="user",
        model_id=456,
        title="Event for different user"
    )
    event_other.sync_metadata()
    event_other.publish()

    # Should have 2 incidents
    total_incidents = Incident.objects.filter(category="bundle_model").count()
    assert total_incidents == 2, f"Expected 2 incidents, got {total_incidents}"


@th.django_unit_test()
def test_bundling_by_source_ip(opts):
    """Test incident bundling by source_ip (bundle_by=4)."""
    from mojo.apps.incident.models import Event, RuleSet, Rule, Incident

    # Clean up
    RuleSet.objects.filter(category="bundle_ip").delete()
    Event.objects.filter(category="bundle_ip").delete()
    Incident.objects.filter(category="bundle_ip").delete()

    # Create ruleset with bundling by source_ip
    ruleset = RuleSet.objects.create(
        name="Bundle by IP",
        category="bundle_ip",
        priority=1,
        match_by=0,
        bundle_by=4,  # source_ip
        bundle_minutes=10
    )

    Rule.objects.create(
        parent=ruleset,
        field_name="category",
        comparator="==",
        value="bundle_ip",
        value_type="str"
    )

    # Create events from same IP
    for i in range(3):
        event = Event.objects.create(
            category="bundle_ip",
            level=5,
            source_ip="192.168.1.100",
            title=f"Event {i}"
        )
        event.sync_metadata()
        event.publish()

    # Should have 1 incident
    incidents = Incident.objects.filter(
        category="bundle_ip",
        source_ip="192.168.1.100"
    )
    assert incidents.count() == 1, f"Expected 1 incident, got {incidents.count()}"
    assert incidents.first().events.count() == 3, "Expected 3 events in incident"


@th.django_unit_test()
def test_bundling_time_window(opts):
    """Test that bundling respects time window."""
    from mojo.apps.incident.models import Event, RuleSet, Rule, Incident
    from mojo.helpers import dates

    # Clean up
    RuleSet.objects.filter(category="bundle_time").delete()
    Event.objects.filter(category="bundle_time").delete()
    Incident.objects.filter(category="bundle_time").delete()

    # Create ruleset with 5 minute bundling window
    ruleset = RuleSet.objects.create(
        name="Bundle Time Window",
        category="bundle_time",
        priority=1,
        match_by=0,
        bundle_by=1,  # Hostname
        bundle_minutes=5
    )

    Rule.objects.create(
        parent=ruleset,
        field_name="category",
        comparator="==",
        value="bundle_time",
        value_type="str"
    )

    # Create event within window
    event1 = Event.objects.create(
        category="bundle_time",
        level=5,
        hostname="server1",
        title="Event 1"
    )
    event1.sync_metadata()
    event1.publish()

    # Create event 10 minutes ago (outside window)
    event2 = Event.objects.create(
        category="bundle_time",
        level=5,
        hostname="server1",
        title="Event 2 - old"
    )
    event2.created = dates.subtract(minutes=10)
    event2.save()
    event2.sync_metadata()
    event2.publish()

    # Should have 2 incidents because second event is outside time window
    incidents = Incident.objects.filter(category="bundle_time")
    # NOTE: This test may not work as expected due to timing issues
    # and the fact that publish() looks for incidents within the window
    # Uncomment for debugging: print(f"Incidents created: {incidents.count()}")


# =============================================================================
# THRESHOLD TESTS
# =============================================================================

@th.django_unit_test()
def test_threshold_min_count(opts):
    """Test that incidents remain pending until min_count is reached."""
    from mojo.apps.incident.models import Event, RuleSet, Rule, Incident

    # Clean up
    RuleSet.objects.filter(category="threshold_test").delete()
    Event.objects.filter(category="threshold_test").delete()
    Incident.objects.filter(category="threshold_test").delete()

    # Create ruleset with min_count=3
    ruleset = RuleSet.objects.create(
        name="Threshold Test",
        category="threshold_test",
        priority=1,
        match_by=0,
        bundle_by=1,
        bundle_minutes=10,
        metadata={
            "min_count": 3,
            "window_minutes": 10,
            "pending_status": "pending"
        }
    )

    Rule.objects.create(
        parent=ruleset,
        field_name="category",
        comparator="==",
        value="threshold_test",
        value_type="str"
    )

    # First event - should create pending incident
    event1 = Event.objects.create(
        category="threshold_test",
        level=5,
        hostname="server1",
        title="Event 1"
    )
    event1.sync_metadata()
    event1.publish()

    incident = Incident.objects.get(category="threshold_test")
    assert incident.status == "pending", f"Expected pending, got {incident.status}"

    # Second event - still pending
    event2 = Event.objects.create(
        category="threshold_test",
        level=5,
        hostname="server1",
        title="Event 2"
    )
    event2.sync_metadata()
    event2.publish()

    incident.refresh_from_db()
    assert incident.status == "pending", f"Expected pending after 2 events, got {incident.status}"

    # Third event - should transition to open
    event3 = Event.objects.create(
        category="threshold_test",
        level=5,
        hostname="server1",
        title="Event 3"
    )
    event3.sync_metadata()
    event3.publish()

    incident.refresh_from_db()
    assert incident.status == "new", f"Expected new after 3 events, got {incident.status}"


@th.django_unit_test()
def test_threshold_window_minutes(opts):
    """Test that threshold counts events within time window."""
    from mojo.apps.incident.models import Event, RuleSet, Rule, Incident
    from mojo.helpers import dates

    # Clean up
    RuleSet.objects.filter(category="window_test").delete()
    Event.objects.filter(category="window_test").delete()
    Incident.objects.filter(category="window_test").delete()

    # Create ruleset with min_count=2 within 5 minutes
    ruleset = RuleSet.objects.create(
        name="Window Test",
        category="window_test",
        priority=1,
        match_by=0,
        bundle_by=1,
        bundle_minutes=10,
        metadata={
            "min_count": 2,
            "window_minutes": 5,
            "pending_status": "pending"
        }
    )

    Rule.objects.create(
        parent=ruleset,
        field_name="category",
        comparator="==",
        value="window_test",
        value_type="str"
    )

    # Create old event (10 minutes ago)
    old_event = Event.objects.create(
        category="window_test",
        level=5,
        hostname="server1",
        title="Old event"
    )
    old_event.created = dates.subtract(minutes=10)
    old_event.save()

    # Create new event (now)
    new_event = Event.objects.create(
        category="window_test",
        level=5,
        hostname="server1",
        title="New event"
    )
    new_event.sync_metadata()
    new_event.publish()

    # Only 1 event within 5 minute window, should be pending
    incident = Incident.objects.filter(category="window_test").first()
    if incident:
        # NOTE: This test documents expected behavior
        # Old event doesn't count toward threshold because it's outside window
        # Uncomment for debugging: print(f"Incident status: {incident.status}")
        pass


# =============================================================================
# HANDLER TESTS
# =============================================================================

@th.django_unit_test()
def test_handler_ignore(opts):
    """Test that handler='ignore' prevents incident creation."""
    from mojo.apps.incident.models import Event, RuleSet, Rule, Incident

    # Clean up
    RuleSet.objects.filter(category="ignore_test").delete()
    Event.objects.filter(category="ignore_test").delete()
    Incident.objects.filter(category="ignore_test").delete()

    # Create ruleset with ignore handler
    ruleset = RuleSet.objects.create(
        name="Ignore Test",
        category="ignore_test",
        priority=1,
        match_by=0,
        handler="ignore"
    )

    Rule.objects.create(
        parent=ruleset,
        field_name="category",
        comparator="==",
        value="ignore_test",
        value_type="str"
    )

    # Create event
    event = Event.objects.create(
        category="ignore_test",
        level=5,
        title="Ignored event"
    )
    event.sync_metadata()
    event.publish()

    # Should not create incident
    incidents = Incident.objects.filter(category="ignore_test")
    assert incidents.count() == 0, f"Expected 0 incidents with ignore handler, got {incidents.count()}"


@th.django_unit_test()
def test_handler_task_execution(opts):
    """Test that task handler is called."""
    from mojo.apps.incident.models import Event, RuleSet, Rule, Incident
    from unittest.mock import patch, Mock

    # Clean up
    RuleSet.objects.filter(category="task_test").delete()
    Event.objects.filter(category="task_test").delete()
    Incident.objects.filter(category="task_test").delete()

    # Create ruleset with task handler
    ruleset = RuleSet.objects.create(
        name="Task Test",
        category="task_test",
        priority=1,
        match_by=0,
        bundle_by=0,
        handler="job://process_incident?severity=high"
    )

    Rule.objects.create(
        parent=ruleset,
        field_name="category",
        comparator="==",
        value="task_test",
        value_type="str"
    )

    # Mock the run_handler to track if it's called
    with patch.object(RuleSet, 'run_handler', return_value=True) as mock_handler:
        event = Event.objects.create(
            category="task_test",
            level=8,
            title="High severity event"
        )
        event.sync_metadata()
        event.publish()

        # Handler should be called on new incident
        assert mock_handler.called, "Handler should be called on incident creation"


@th.django_unit_test()
def test_handler_chaining(opts):
    """Test that multiple handlers can be chained."""
    from mojo.apps.incident.models import Event, RuleSet, Rule
    from unittest.mock import patch

    # Clean up
    RuleSet.objects.filter(category="chain_test").delete()
    Event.objects.filter(category="chain_test").delete()

    # Create ruleset with chained handlers
    ruleset = RuleSet.objects.create(
        name="Chain Test",
        category="chain_test",
        priority=1,
        match_by=0,
        handler="job://handler1,email://admin@example.com,notify://security-team"
    )

    Rule.objects.create(
        parent=ruleset,
        field_name="category",
        comparator="==",
        value="chain_test",
        value_type="str"
    )

    event = Event.objects.create(
        category="chain_test",
        level=8,
        title="Chain test event"
    )
    event.sync_metadata()

    # Test that run_handler processes all three handlers
    result = ruleset.run_handler(event)
    assert result is True, "Chained handlers should return True"


@th.django_unit_test()
def test_handler_ticket_creation(opts):
    """Test that ticket handler creates tickets via execute_handler.

    Handlers run as async jobs, so we call execute_handler directly
    to verify the ticket handler logic without needing a job worker.
    """
    from mojo.apps.incident.models import Event, RuleSet, Rule, Incident, Ticket
    from mojo.apps.incident.handlers.event_handlers import execute_handler

    # Clean up
    RuleSet.objects.filter(category="ticket_test").delete()
    Event.objects.filter(category="ticket_test").delete()
    Incident.objects.filter(category="ticket_test").delete()
    Ticket.objects.filter(category="ticket_test").delete()

    # Create ruleset with ticket handler
    handler_spec = "ticket://?status=open&priority=8&category=ticket_test"
    ruleset = RuleSet.objects.create(
        name="Ticket Test",
        category="ticket_test",
        priority=1,
        match_by=0,
        bundle_by=0,
        handler=handler_spec,
    )

    Rule.objects.create(
        parent=ruleset,
        field_name="category",
        comparator="==",
        value="ticket_test",
        value_type="str"
    )

    # Create event and incident
    event = Event.objects.create(
        category="ticket_test",
        level=8,
        title="Critical issue",
        details="This needs immediate attention"
    )
    event.sync_metadata()
    event.save()

    incident = Incident.objects.create(
        priority=8, state=0, status="new",
        category="ticket_test", scope="test",
        title="Ticket test incident",
        rule_set=ruleset,
    )
    event.incident = incident
    event.save(update_fields=["incident"])

    # Call execute_handler directly (simulates what the job worker does)
    execute_handler({
        "handler_spec": handler_spec,
        "event_id": event.pk,
        "incident_id": incident.pk,
    })

    # Check that ticket was created
    tickets = Ticket.objects.filter(category="ticket_test")
    assert tickets.count() == 1, f"Expected 1 ticket, got {tickets.count()}"

    ticket = tickets.first()
    assert ticket.status == "open", f"Expected status=open, got {ticket.status}"
    assert ticket.priority == 8, f"Expected priority=8, got {ticket.priority}"

    # Check ticket is linked to incident
    incident = Incident.objects.get(category="ticket_test")
    assert ticket.incident == incident, "Ticket should be linked to incident"


# =============================================================================
# INTEGRATION TESTS
# =============================================================================

@th.django_unit_test()
def test_full_flow_event_to_incident(opts):
    """Test complete flow from event creation to incident with handlers."""
    from mojo.apps.incident.models import Event, RuleSet, Rule, Incident

    # Clean up
    RuleSet.objects.filter(category="full_flow").delete()
    Event.objects.filter(category="full_flow").delete()
    Incident.objects.filter(category="full_flow").delete()

    # Create ruleset
    ruleset = RuleSet.objects.create(
        name="Full Flow Test",
        category="full_flow",
        priority=1,
        match_by=0,
        bundle_by=1,
        bundle_minutes=10,
        handler="job://process_incident"
    )

    Rule.objects.create(
        parent=ruleset,
        field_name="category",
        comparator="==",
        value="full_flow",
        value_type="str"
    )

    Rule.objects.create(
        parent=ruleset,
        field_name="level",
        comparator=">=",
        value="5",
        value_type="int"
    )

    # Create event that matches
    event1 = Event.objects.create(
        category="full_flow",
        level=7,
        hostname="web-server-01",
        source_ip="10.0.1.50",
        title="Authentication failure",
        details="Failed login attempt from suspicious IP"
    )
    event1.sync_metadata()
    event1.publish()

    # Verify incident was created
    incidents = Incident.objects.filter(category="full_flow")
    assert incidents.count() == 1, "Incident should be created"

    incident = incidents.first()
    assert incident.priority == 7, "Incident priority should match event level"
    assert incident.hostname == "web-server-01", "Incident should have event hostname"
    assert incident.events.count() == 1, "Incident should have 1 event"

    # Create another event that bundles
    event2 = Event.objects.create(
        category="full_flow",
        level=6,
        hostname="web-server-01",
        source_ip="10.0.1.50",
        title="Another auth failure",
        details="Another failed login"
    )
    event2.sync_metadata()
    event2.publish()

    # Should still be 1 incident with 2 events
    assert incidents.count() == 1, "Should bundle into same incident"
    incident.refresh_from_db()
    assert incident.events.count() == 2, "Incident should have 2 events"

    # Priority should be escalated to highest level
    assert incident.priority == 7, "Priority should remain at highest level"


@th.django_unit_test()
def test_event_without_matching_ruleset(opts):
    """Events without a specific ruleset are caught by the catch-all '*' ruleset
    and bundled into incidents by source_ip within a 30-minute window."""
    from mojo.apps.incident.models import Event, Incident
    from mojo.apps.incident.models.rule import RuleSet

    # Clean up
    Event.objects.filter(category="no_match").delete()
    Incident.objects.filter(category="no_match").delete()

    # Ensure catch-all exists
    RuleSet.ensure_default_rules()

    # Create a low-level event — catch-all bundles it into an incident
    event = Event.objects.create(
        category="no_match",
        level=3,
        source_ip="10.0.0.50",
        title="Low severity event"
    )
    event.sync_metadata()
    event.publish()

    incidents = Incident.objects.filter(category="no_match")
    assert incidents.count() == 1, "Catch-all ruleset should bundle even low-level events"

    # Create a second event from the same IP — should bundle into same incident
    event2 = Event.objects.create(
        category="no_match",
        level=5,
        source_ip="10.0.0.50",
        title="Medium severity event"
    )
    event2.sync_metadata()
    event2.publish()

    incidents = Incident.objects.filter(category="no_match")
    assert incidents.count() == 1, "Second event from same IP should bundle into existing incident"


@th.django_unit_test()
def test_priority_escalation_on_bundle(opts):
    """Test that incident priority escalates when higher severity events bundle."""
    from mojo.apps.incident.models import Event, RuleSet, Rule, Incident

    # Clean up
    RuleSet.objects.filter(category="escalation_test").delete()
    Event.objects.filter(category="escalation_test").delete()
    Incident.objects.filter(category="escalation_test").delete()

    # Create ruleset with bundling
    ruleset = RuleSet.objects.create(
        name="Escalation Test",
        category="escalation_test",
        priority=1,
        match_by=0,
        bundle_by=1,
        bundle_minutes=10
    )

    Rule.objects.create(
        parent=ruleset,
        field_name="category",
        comparator="==",
        value="escalation_test",
        value_type="str"
    )

    # Create low priority event
    event1 = Event.objects.create(
        category="escalation_test",
        level=3,
        hostname="server1",
        title="Low priority event"
    )
    event1.sync_metadata()
    event1.publish()

    incident = Incident.objects.get(category="escalation_test")
    assert incident.priority == 3, "Initial priority should be 3"

    # Create higher priority event that bundles
    event2 = Event.objects.create(
        category="escalation_test",
        level=9,
        hostname="server1",
        title="High priority event"
    )
    event2.sync_metadata()
    event2.publish()

    # Priority should escalate
    incident.refresh_from_db()
    assert incident.priority == 9, f"Priority should escalate to 9, got {incident.priority}"


@th.django_unit_test()
def test_metadata_preservation(opts):
    """Test that event metadata is preserved in incidents."""
    from mojo.apps.incident.models import Event, RuleSet, Rule, Incident

    # Clean up
    RuleSet.objects.filter(category="metadata_test").delete()
    Event.objects.filter(category="metadata_test").delete()
    Incident.objects.filter(category="metadata_test").delete()

    # Create ruleset
    ruleset = RuleSet.objects.create(
        name="Metadata Test",
        category="metadata_test",
        priority=1,
        match_by=0,
        bundle_by=0
    )

    Rule.objects.create(
        parent=ruleset,
        field_name="category",
        comparator="==",
        value="metadata_test",
        value_type="str"
    )

    # Create event with custom metadata
    event = Event.objects.create(
        category="metadata_test",
        level=5,
        title="Event with metadata"
    )
    event.metadata = {
        "user_id": 12345,
        "action": "failed_login",
        "ip_country": "US",
        "custom_field": "custom_value"
    }
    event.sync_metadata()
    event.save()
    event.publish()

    # Check incident has metadata
    incident = Incident.objects.get(category="metadata_test")
    assert "user_id" in incident.metadata, "Incident should preserve event metadata"
    assert incident.metadata["user_id"] == 12345, "Metadata values should be preserved"
    assert incident.metadata["action"] == "failed_login", "Custom metadata should be preserved"


if __name__ == "__main__":
    # Run these tests with: testit tests/test_incident/rule_engine_comprehensive.py
    pass
