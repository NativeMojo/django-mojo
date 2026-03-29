"""
Tests for default incident rules: auth, health, and ensure_default_rules orchestration.
"""
from testit import helpers as th


# ---------------------------------------------------------------------------
# ensure_default_rules orchestration
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_ensure_default_rules_calls_all_categories(opts):
    """ensure_default_rules should create rules across all categories."""
    from mojo.apps.incident.models.rule import RuleSet

    # Clean slate
    RuleSet.objects.all().delete()

    RuleSet.ensure_default_rules()

    ossec = RuleSet.objects.filter(category="ossec").count()
    bouncer = RuleSet.objects.filter(category__startswith="security:bouncer:").count()
    auth = RuleSet.objects.filter(category__in=["login:unknown", "security:bouncer:token_invalid"]).count()
    health = RuleSet.objects.filter(category__startswith="system:health:").count()

    assert ossec >= 4, f"Should create at least 4 OSSEC rules, got {ossec}"
    assert bouncer >= 3, f"Should create at least 3 bouncer rules, got {bouncer}"
    assert auth >= 2, f"Should create at least 2 auth rules, got {auth}"
    assert health >= 3, f"Should create at least 3 health rules, got {health}"

    # Cleanup
    RuleSet.objects.all().delete()


@th.django_unit_test()
def test_ensure_default_rules_is_idempotent(opts):
    """Calling ensure_default_rules twice should not duplicate anything."""
    from mojo.apps.incident.models.rule import RuleSet

    RuleSet.objects.all().delete()

    RuleSet.ensure_default_rules()
    count1 = RuleSet.objects.count()

    RuleSet.ensure_default_rules()
    count2 = RuleSet.objects.count()

    assert count1 == count2, f"Second call should not create duplicates: {count1} vs {count2}"

    # Cleanup
    RuleSet.objects.all().delete()


# ---------------------------------------------------------------------------
# Auth rules
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_auth_credential_stuffing_rule(opts):
    """ensure_auth_rules should create credential stuffing rule."""
    from mojo.apps.incident.models.rule import RuleSet

    RuleSet.objects.filter(category="login:unknown").delete()

    RuleSet.ensure_auth_rules()

    rule = RuleSet.objects.filter(category="login:unknown").first()
    assert rule is not None, "Should create login:unknown rule"
    assert "block://" in rule.handler, f"Should block, got {rule.handler}"
    assert rule.bundle_minutes == 15, f"Should bundle for 15 min, got {rule.bundle_minutes}"
    assert rule.rules.count() >= 1, "Should have at least 1 rule"

    # Cleanup
    RuleSet.objects.filter(category="login:unknown").delete()


@th.django_unit_test()
def test_auth_token_abuse_rule(opts):
    """ensure_auth_rules should create bouncer token abuse rule."""
    from mojo.apps.incident.models.rule import RuleSet

    RuleSet.objects.filter(category="security:bouncer:token_invalid").delete()

    RuleSet.ensure_auth_rules()

    rule = RuleSet.objects.filter(category="security:bouncer:token_invalid").first()
    assert rule is not None, "Should create token_invalid rule"
    assert "block://" in rule.handler, f"Should block, got {rule.handler}"
    assert rule.rules.count() >= 1, "Should have at least 1 rule"

    # Cleanup
    RuleSet.objects.filter(category="security:bouncer:token_invalid").delete()


@th.django_unit_test()
def test_credential_stuffing_rule_matches_event(opts):
    """Credential stuffing rule should match login:unknown events."""
    from mojo.apps.incident.models.rule import RuleSet
    from mojo.apps.incident.models import Event

    RuleSet.objects.filter(category="login:unknown").delete()
    RuleSet.ensure_auth_rules()

    event = Event.objects.create(
        category="login:unknown",
        level=8,
        scope="account",
        details="Unknown user attempted login",
        source_ip="10.0.0.50",
        metadata={"username": "admin@fake.com"},
    )

    ruleset = RuleSet.check_by_category("login:unknown", event)
    assert ruleset is not None, "Should match credential stuffing rule"
    assert "Credential Stuffing" in ruleset.name, f"Wrong rule matched: {ruleset.name}"

    # Cleanup
    event.delete()
    RuleSet.objects.filter(category="login:unknown").delete()


# ---------------------------------------------------------------------------
# Health rules
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_health_runner_down_rule(opts):
    """ensure_health_rules should create runner down rule with notify+ticket."""
    from mojo.apps.incident.models.rule import RuleSet

    RuleSet.objects.filter(category__startswith="system:health:").delete()

    RuleSet.ensure_health_rules()

    rule = RuleSet.objects.filter(category="system:health:runner").first()
    assert rule is not None, "Should create runner down rule"
    assert "notify://" in rule.handler, f"Should notify, got {rule.handler}"
    assert "ticket://" in rule.handler, f"Should create ticket, got {rule.handler}"
    # Must NOT block IPs for health events
    assert "block://" not in rule.handler, f"Health rules must not block IPs, got {rule.handler}"

    # Cleanup
    RuleSet.objects.filter(category__startswith="system:health:").delete()


@th.django_unit_test()
def test_health_scheduler_missing_rule(opts):
    """ensure_health_rules should create scheduler missing rule."""
    from mojo.apps.incident.models.rule import RuleSet

    RuleSet.objects.filter(category__startswith="system:health:").delete()

    RuleSet.ensure_health_rules()

    rule = RuleSet.objects.filter(category="system:health:scheduler").first()
    assert rule is not None, "Should create scheduler missing rule"
    assert "notify://" in rule.handler, f"Should notify, got {rule.handler}"
    assert "ticket://" in rule.handler, f"Should create ticket, got {rule.handler}"
    assert "block://" not in rule.handler, f"Health rules must not block IPs, got {rule.handler}"

    # Cleanup
    RuleSet.objects.filter(category__startswith="system:health:").delete()


@th.django_unit_test()
def test_health_tcp_overload_rule(opts):
    """ensure_health_rules should create TCP overload rule with notify only."""
    from mojo.apps.incident.models.rule import RuleSet

    RuleSet.objects.filter(category__startswith="system:health:").delete()

    RuleSet.ensure_health_rules()

    rule = RuleSet.objects.filter(category="system:health:tcp").first()
    assert rule is not None, "Should create TCP overload rule"
    assert "notify://" in rule.handler, f"Should notify, got {rule.handler}"
    # TCP overload should NOT create a ticket — often self-resolving
    assert "ticket://" not in rule.handler, f"TCP rule should not create ticket, got {rule.handler}"
    assert "block://" not in rule.handler, f"Health rules must not block IPs, got {rule.handler}"

    # Cleanup
    RuleSet.objects.filter(category__startswith="system:health:").delete()


@th.django_unit_test()
def test_health_rules_never_block_ips(opts):
    """No health rule should ever use block:// handler."""
    from mojo.apps.incident.models.rule import RuleSet

    RuleSet.objects.filter(category__startswith="system:health:").delete()

    RuleSet.ensure_health_rules()

    health_rules = RuleSet.objects.filter(category__startswith="system:health:")
    for rule in health_rules:
        assert "block://" not in (rule.handler or ""), \
            f"Health rule '{rule.name}' must not block IPs, got handler: {rule.handler}"

    # Cleanup
    RuleSet.objects.filter(category__startswith="system:health:").delete()


@th.django_unit_test()
def test_health_runner_rule_matches_event(opts):
    """Runner down rule should match system:health:runner events."""
    from mojo.apps.incident.models.rule import RuleSet
    from mojo.apps.incident.models import Event

    RuleSet.objects.filter(category__startswith="system:health:").delete()
    RuleSet.ensure_health_rules()

    event = Event.objects.create(
        category="system:health:runner",
        level=10,
        scope="system",
        details="Runner web-1 not responding",
        hostname="web-1",
        metadata={},
    )

    ruleset = RuleSet.check_by_category("system:health:runner", event)
    assert ruleset is not None, "Should match runner down rule"
    assert "Runner Down" in ruleset.name, f"Wrong rule matched: {ruleset.name}"

    # Cleanup
    event.delete()
    RuleSet.objects.filter(category__startswith="system:health:").delete()
