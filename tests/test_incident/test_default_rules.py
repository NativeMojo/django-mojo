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


# ---------------------------------------------------------------------------
# New OSSEC noise-reduction rules
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_ossec_login_session_noise_ignores_event(opts):
    """Login session opened/closed events should match the ignore ruleset."""
    from mojo.apps.incident.models.rule import RuleSet
    from mojo.apps.incident.models import Event

    RuleSet.objects.filter(category="ossec", name="OSSEC - Login Session Noise").delete()
    RuleSet.ensure_ossec_rules()

    noise_rule = RuleSet.objects.filter(name="OSSEC - Login Session Noise").first()
    assert noise_rule is not None, "OSSEC - Login Session Noise ruleset should exist"
    assert noise_rule.handler == "ignore", f"Handler should be 'ignore', got {noise_rule.handler!r}"

    event = Event.objects.create(
        category="ossec", level=3, scope="ossec",
        details="Login session opened.",
        source_ip=None,
    )
    matched = RuleSet.check_by_category("ossec", event)
    assert matched is not None, "Login session event should match a rule"
    assert matched.handler == "ignore", f"Should match ignore rule, got {matched.handler!r}"

    event.delete()
    RuleSet.objects.filter(name="OSSEC - Login Session Noise").delete()


@th.django_unit_test()
def test_ossec_ssh_single_probe_matches_rule_5710(opts):
    """SSH single-probe events (OSSEC rule 5710) should be caught and blocked."""
    from mojo.apps.incident.models.rule import RuleSet
    from mojo.apps.incident.models import Event

    RuleSet.objects.filter(category="ossec", name="OSSEC - SSH Single Probe (5710)").delete()
    RuleSet.ensure_ossec_rules()

    probe_rule = RuleSet.objects.filter(name="OSSEC - SSH Single Probe (5710)").first()
    assert probe_rule is not None, "OSSEC - SSH Single Probe (5710) ruleset should exist"
    assert "block://" in probe_rule.handler, f"Should block, got {probe_rule.handler!r}"

    event = Event.objects.create(
        category="ossec", level=5, scope="ossec",
        details="Attempt to login using a non-existent user Source IP: 1.2.3.4",
        source_ip="1.2.3.4",
        metadata={"rule_id": 5710},
    )
    matched = RuleSet.check_by_category("ossec", event)
    assert matched is not None, "SSH single probe event should match a rule"
    assert "Single Probe" in matched.name, f"Should match single-probe rule, got {matched.name!r}"

    event.delete()
    RuleSet.objects.filter(name="OSSEC - SSH Single Probe (5710)").delete()


@th.django_unit_test()
def test_ossec_generic_web_errors_matched_and_blocked(opts):
    """Generic web 400/404/405 events should match the web errors ruleset."""
    from mojo.apps.incident.models.rule import RuleSet
    from mojo.apps.incident.models import Event

    RuleSet.objects.filter(category="ossec", name="OSSEC - Generic Web Errors").delete()
    RuleSet.ensure_ossec_rules()

    web_rule = RuleSet.objects.filter(name="OSSEC - Generic Web Errors").first()
    assert web_rule is not None, "OSSEC - Generic Web Errors ruleset should exist"
    assert "block://" in web_rule.handler, f"Should block, got {web_rule.handler!r}"

    for status, detail in [
        (400, "Web 400 GET http://example.com/ from 1.2.3.4"),
        (404, "Web 404 GET https://example.com/sitemap.xml from 1.2.3.4"),
        (405, "Web 405 POST https://example.com/ from 1.2.3.4"),
        (404, "Web Attack 404 GET https://example.com/vendor/phpunit/... from 1.2.3.4"),
    ]:
        event = Event.objects.create(
            category="ossec", level=5, scope="ossec",
            details=detail, source_ip="1.2.3.4",
        )
        matched = RuleSet.check_by_category("ossec", event)
        assert matched is not None, f"Web {status} event should match a rule: {detail!r}"
        assert "Web Error" in matched.name or "Bot/Scanner" in matched.name, \
            f"Web {status} should match web error or bot/scanner rule, got {matched.name!r}"
        event.delete()

    RuleSet.objects.filter(name="OSSEC - Generic Web Errors").delete()


@th.django_unit_test()
def test_ossec_login_session_noise_does_not_match_sudo(opts):
    """Sudo to root events should NOT be caught by the login session noise rule."""
    from mojo.apps.incident.models.rule import RuleSet
    from mojo.apps.incident.models import Event

    RuleSet.objects.filter(category="ossec").delete()
    RuleSet.ensure_ossec_rules()

    event = Event.objects.create(
        category="ossec", level=3, scope="ossec",
        details="Successful sudo to ROOT executed",
        source_ip=None,
    )
    matched = RuleSet.check_by_category("ossec", event)
    # Should NOT match the ignore rule — sudo events are security-relevant
    assert matched is None or matched.handler != "ignore", \
        f"Sudo event should not be silently ignored, got {matched.name if matched else None!r}"

    event.delete()
    RuleSet.objects.filter(category="ossec").delete()


@th.django_unit_test()
def test_ossec_ssh_success_not_ignored(opts):
    """SSHD authentication success events should not match the login session noise rule."""
    from mojo.apps.incident.models.rule import RuleSet
    from mojo.apps.incident.models import Event

    RuleSet.objects.filter(category="ossec").delete()
    RuleSet.ensure_ossec_rules()

    event = Event.objects.create(
        category="ossec", level=3, scope="ossec",
        details="SSHD authentication success. Source IP: 88.184.56.101",
        source_ip="88.184.56.101",
    )
    matched = RuleSet.check_by_category("ossec", event)
    assert matched is None or matched.handler != "ignore", \
        f"SSH success should not be silently ignored, got {matched.name if matched else None!r}"

    event.delete()
    RuleSet.objects.filter(category="ossec").delete()


@th.django_unit_test()
def test_ossec_ensure_idempotent_with_new_rules(opts):
    """ensure_ossec_rules should create exactly 7 rulesets, idempotent on repeat calls."""
    from mojo.apps.incident.models.rule import RuleSet

    RuleSet.objects.filter(category="ossec").delete()
    RuleSet.ensure_ossec_rules()
    count1 = RuleSet.objects.filter(category="ossec").count()
    RuleSet.ensure_ossec_rules()
    count2 = RuleSet.objects.filter(category="ossec").count()

    assert count1 == 7, f"Should create exactly 7 OSSEC rulesets, got {count1}"
    assert count1 == count2, f"Second call should not create duplicates: {count1} vs {count2}"

    RuleSet.objects.filter(category="ossec").delete()


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
