from testit import helpers as th
from testit import faker
import datetime
from objict import objict


TEST_USER = "testit"
TEST_PWORD = "testit##mojo"

ADMIN_USER = "tadmin"
ADMIN_PWORD = "testit##mojo"

@th.django_unit_test()
def test_rule_check(opts):
    from mojo.apps.incident.models.rule import Rule, RuleSet

    # Delete existing rulesets and rules with category 'testing'
    RuleSet.objects.filter(category="testing").delete()

    # Create a mock event with metadata for testing
    event = objict()
    event.metadata = {
        "severity": 5,
        "hostname": "test-server",
        "source_ip": "192.168.1.10",
        "message": "Authentication failure",
        "attempts": 3
    }

    # Test int comparison
    rule = Rule(
        name="High Severity Check",
        comparator=">=",
        field_name="severity",
        value="3",
        value_type="int"
    )
    assert rule.check_rule(event) is True, "Integer >= comparison failed"

    # Test string contains
    rule = Rule(
        name="Message Content Check",
        comparator="contains",
        field_name="message",
        value="failure",
        value_type="str"
    )
    assert rule.check_rule(event) is True, "String contains comparison failed"

    # Test regex comparison
    rule = Rule(
        name="IP Address Pattern Check",
        comparator="regex",
        field_name="source_ip",
        value=r"192\.168\.\d+\.\d+",
        value_type="str"
    )
    assert rule.check_rule(event) is True, "Regex comparison failed"

    # Test field not in metadata
    rule = Rule(
        name="Missing Field Check",
        comparator="==",
        field_name="missing_field",
        value="value",
        value_type="str"
    )
    assert rule.check_rule(event) is False, "Missing field check should return False"

    # Test invalid value type conversion
    rule = Rule(
        name="Invalid Type Conversion",
        comparator="==",
        field_name="message",
        value="123",
        value_type="int"
    )
    assert rule.check_rule(event) is False, "Invalid type conversion should return False"


@th.django_unit_test()
def test_ruleset_check_all_match(opts):
    from mojo.apps.incident.models.rule import Rule, RuleSet

    # Delete existing rulesets and rules with category 'testing'
    RuleSet.objects.filter(category="testing").delete()

    # Create mock event
    event = objict()
    event.metadata = {
        "severity": 5,
        "hostname": "test-server",
        "source_ip": "192.168.1.10",
        "message": "Authentication failure",
        "attempts": 3
    }

    # Create a RuleSet with match_by=0 (all rules must match)
    ruleset = RuleSet.objects.create(
        name="Test RuleSet All Match",
        category="testing",
        priority=1,
        match_by=0  # All rules must match
    )

    # Create rules that will all match
    rule1 = Rule.objects.create(
        parent=ruleset,
        name="Severity Check",
        comparator=">",
        field_name="severity",
        value="3",
        value_type="int",
        index=0
    )

    rule2 = Rule.objects.create(
        parent=ruleset,
        name="Hostname Check",
        comparator="contains",
        field_name="hostname",
        value="server",
        value_type="str",
        index=1
    )

    # All rules match, so check should return True
    assert ruleset.check_rules(event) is True, "RuleSet all_match check should return True when all rules match"

    # Update one rule to not match
    rule2.value = "nonexistent"
    rule2.save()

    # Not all rules match, so check should return False
    assert ruleset.check_rules(event) is False, "RuleSet all_match check should return False when not all rules match"


@th.django_unit_test()
def test_ruleset_check_any_match(opts):
    from mojo.apps.incident.models.rule import Rule, RuleSet

    # Delete existing rulesets and rules with category 'testing'
    RuleSet.objects.filter(category="testing").delete()

    # Create mock event
    event = objict()
    event.metadata = {
        "severity": 5,
        "hostname": "test-server",
        "source_ip": "192.168.1.10",
        "message": "Authentication failure",
        "attempts": 3
    }

    # Create a RuleSet with match_by=1 (any rule can match)
    ruleset = RuleSet.objects.create(
        name="Test RuleSet Any Match",
        category="testing",
        priority=1,
        match_by=1  # Any rule can match
    )

    # Create one rule that will match and one that won't
    rule1 = Rule.objects.create(
        parent=ruleset,
        name="Severity Check",
        comparator=">",
        field_name="severity",
        value="3",
        value_type="int",
        index=0
    )

    rule2 = Rule.objects.create(
        parent=ruleset,
        name="Hostname Check",
        comparator="contains",
        field_name="hostname",
        value="nonexistent",
        value_type="str",
        index=1
    )

    # At least one rule matches, so check should return True
    assert ruleset.check_rules(event) is True, "RuleSet any_match check should return True when at least one rule matches"

    # Update the matching rule to not match
    rule1.value = "10"
    rule1.save()

    # No rules match, so check should return False
    assert ruleset.check_rules(event) is False, "RuleSet any_match check should return False when no rules match"


@th.django_unit_test()
def test_ruleset_check_by_category(opts):
    from mojo.apps.incident.models.rule import Rule, RuleSet

    # Delete existing rulesets and rules with category 'testing'
    RuleSet.objects.filter(category="testing").delete()

    # Create mock event
    event = objict()
    event.metadata = {
        "severity": 5,
        "hostname": "test-server",
        "source_ip": "192.168.1.10",
        "message": "Authentication failure",
        "attempts": 3
    }

    # Create multiple RuleSets in the same category with different priorities
    ruleset1 = RuleSet.objects.create(
        name="High Priority RuleSet",
        category="testing",
        priority=1,
        match_by=0  # All rules must match
    )

    ruleset2 = RuleSet.objects.create(
        name="Medium Priority RuleSet",
        category="testing",
        priority=2,
        match_by=0  # All rules must match
    )

    ruleset3 = RuleSet.objects.create(
        name="Low Priority RuleSet",
        category="testing",
        priority=3,
        match_by=0  # All rules must match
    )

    # Create rules for each RuleSet
    # First RuleSet - won't match
    Rule.objects.create(
        parent=ruleset1,
        name="Hostname Check",
        comparator="==",
        field_name="hostname",
        value="nonexistent",
        value_type="str"
    )

    # Second RuleSet - will match
    Rule.objects.create(
        parent=ruleset2,
        name="Severity Check",
        comparator=">",
        field_name="severity",
        value="3",
        value_type="int"
    )

    # Third RuleSet - will match but has lower priority
    Rule.objects.create(
        parent=ruleset3,
        name="IP Check",
        comparator="contains",
        field_name="source_ip",
        value="192.168",
        value_type="str"
    )

    # Check by category should return the highest priority matching RuleSet
    result = RuleSet.check_by_category("testing", event)
    assert result is not None, "check_by_category should return a RuleSet"
    assert result.id == ruleset2.id, "check_by_category should return the highest priority matching RuleSet"
    # If we delete the matching rules from ruleset2, it should return ruleset3
    Rule.objects.filter(parent=ruleset2).delete()

    # Test the check_by_category method
    result = RuleSet.check_by_category("testing", event)

    assert result is not None, "check_by_category should return a RuleSet"
    assert result.id == ruleset3.id, "check_by_category should return the next highest priority matching RuleSet"

    # If no RuleSets match, it should return None
    Rule.objects.filter(parent=ruleset3).delete()
    result = RuleSet.check_by_category("testing", event)
    assert result is None, "check_by_category should return None when no RuleSets match"


@th.django_unit_test()
def test_ruleset_run_handler(opts):
    from mojo.apps.incident.models.rule import RuleSet

    # Delete existing rulesets and rules with category 'testing'
    RuleSet.objects.filter(category="testing").delete()

    # Create mock event
    event = objict()
    event.metadata = {
        "severity": 5,
        "hostname": "test-server",
        "source_ip": "192.168.1.10",
        "message": "Authentication failure"
    }

    # Create RuleSet with task handler
    ruleset_task = RuleSet.objects.create(
        name="Task Handler RuleSet",
        category="testing",
        priority=1,
        handler="task://incident_handler?severity=high&notify=true"
    )

    # Test task handler
    assert ruleset_task.run_handler(event) is True, "Task handler should return True"

    # Create RuleSet with email handler
    ruleset_email = RuleSet.objects.create(
        name="Email Handler RuleSet",
        category="testing",
        priority=2,
        handler="email://admin@example.com"
    )

    # Test email handler
    assert ruleset_email.run_handler(event) is True, "Email handler should return True"

    # Create RuleSet with notify handler
    ruleset_notify = RuleSet.objects.create(
        name="Notify Handler RuleSet",
        category="testing",
        priority=3,
        handler="notify://security-team"
    )

    # Test notify handler
    assert ruleset_notify.run_handler(event) is True, "Notify handler should return True"

    # Create RuleSet with invalid handler
    ruleset_invalid = RuleSet.objects.create(
        name="Invalid Handler RuleSet",
        category="testing",
        priority=4,
        handler="invalid://handler"
    )

    # Test invalid handler
    assert ruleset_invalid.run_handler(event) is False, "Invalid handler should return False"

    # Create RuleSet with no handler
    ruleset_none = RuleSet.objects.create(
        name="No Handler RuleSet",
        category="testing",
        priority=5,
        handler=None
    )

    # Test no handler
    assert ruleset_none.run_handler(event) is False, "No handler should return False"


@th.django_unit_test()
def test_rule_value_conversion(opts):
    from mojo.apps.incident.models.rule import Rule

    # Create a rule for testing
    rule = Rule(name="Test Rule")

    # Test integer conversion
    rule.value_type = "int"
    field_value, comp_value = rule._convert_values("123", "456")
    assert field_value == 123, "Integer conversion for field_value failed"
    assert comp_value == 456, "Integer conversion for comp_value failed"

    # Test float conversion
    rule.value_type = "float"
    field_value, comp_value = rule._convert_values("123.45", "456.78")
    assert field_value == 123.45, "Float conversion for field_value failed"
    assert comp_value == 456.78, "Float conversion for comp_value failed"

    # Test invalid conversion
    rule.value_type = "int"
    field_value, comp_value = rule._convert_values("not-a-number", "456")
    assert field_value is None, "Invalid conversion should return None"
    assert comp_value is None, "Invalid conversion should return None"

    # Test string values for contains comparison
    rule.comparator = "contains"
    rule.value_type = "int"  # Should be ignored for contains
    field_value, comp_value = rule._convert_values("hello world", "world")
    assert field_value == "hello world", "String should not be converted for contains comparison"
    assert comp_value == "world", "String should not be converted for contains comparison"


@th.django_unit_test()
def test_rule_comparison(opts):
    from mojo.apps.incident.models.rule import Rule

    # Create a rule for testing
    rule = Rule(name="Test Rule")

    # Test equality comparison
    rule.comparator = "=="
    assert rule._compare(123, 123) is True, "Equality comparison failed"
    assert rule._compare(123, 456) is False, "Equality comparison failed"

    # Test eq alias
    rule.comparator = "eq"
    assert rule._compare(123, 123) is True, "Eq alias comparison failed"

    # Test greater than comparison
    rule.comparator = ">"
    assert rule._compare(456, 123) is True, "Greater than comparison failed"
    assert rule._compare(123, 456) is False, "Greater than comparison failed"

    # Test greater than or equal comparison
    rule.comparator = ">="
    assert rule._compare(456, 123) is True, "Greater than or equal comparison failed"
    assert rule._compare(123, 123) is True, "Greater than or equal comparison failed"
    assert rule._compare(123, 456) is False, "Greater than or equal comparison failed"

    # Test less than comparison
    rule.comparator = "<"
    assert rule._compare(123, 456) is True, "Less than comparison failed"
    assert rule._compare(456, 123) is False, "Less than comparison failed"

    # Test less than or equal comparison
    rule.comparator = "<="
    assert rule._compare(123, 456) is True, "Less than or equal comparison failed"
    assert rule._compare(123, 123) is True, "Less than or equal comparison failed"
    assert rule._compare(456, 123) is False, "Less than or equal comparison failed"

    # Test contains comparison
    rule.comparator = "contains"
    assert rule._compare("hello world", "world") is True, "Contains comparison failed"
    assert rule._compare("hello world", "goodbye") is False, "Contains comparison failed"

    # Test regex comparison
    rule.comparator = "regex"
    assert rule._compare("hello world", r"h.+o") is True, "Regex comparison failed"
    assert rule._compare("hello world", r"^goodbye") is False, "Regex comparison failed"

    # Test invalid comparator
    rule.comparator = "invalid"
    assert rule._compare(123, 456) is False, "Invalid comparator should return False"
