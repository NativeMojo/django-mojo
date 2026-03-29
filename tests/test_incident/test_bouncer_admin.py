"""
Tests for bouncer admin visibility: metrics, search fields, default rules.
"""
from testit import helpers as th
from objict import objict


# ---------------------------------------------------------------------------
# Search fields
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_bouncer_device_has_search_fields(opts):
    """BouncerDevice should have SEARCH_FIELDS for admin search."""
    from mojo.apps.account.models.bouncer_device import BouncerDevice

    search = getattr(BouncerDevice.RestMeta, 'SEARCH_FIELDS', None)
    assert search is not None, "BouncerDevice.RestMeta should have SEARCH_FIELDS"
    assert 'muid' in search, "Should be searchable by muid"
    assert 'last_seen_ip' in search, "Should be searchable by IP"


@th.django_unit_test()
def test_bouncer_signal_has_search_fields(opts):
    """BouncerSignal should have SEARCH_FIELDS for admin search."""
    from mojo.apps.account.models.bouncer_signal import BouncerSignal

    search = getattr(BouncerSignal.RestMeta, 'SEARCH_FIELDS', None)
    assert search is not None, "BouncerSignal.RestMeta should have SEARCH_FIELDS"
    assert 'ip_address' in search, "Should be searchable by IP"
    assert 'decision' in search, "Should be searchable by decision"


@th.django_unit_test()
def test_bot_signature_has_search_fields(opts):
    """BotSignature should have SEARCH_FIELDS for admin search."""
    from mojo.apps.account.models.bot_signature import BotSignature

    search = getattr(BotSignature.RestMeta, 'SEARCH_FIELDS', None)
    assert search is not None, "BotSignature.RestMeta should have SEARCH_FIELDS"
    assert 'sig_type' in search, "Should be searchable by sig_type"
    assert 'value' in search, "Should be searchable by value"


# ---------------------------------------------------------------------------
# Default bouncer rules
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_ensure_bouncer_rules_creates_honeypot_rule(opts):
    """ensure_bouncer_rules should create honeypot credential stuffing rule."""
    from mojo.apps.incident.models.rule import RuleSet

    # Clean any existing bouncer rules
    RuleSet.objects.filter(category__startswith="security:bouncer:").delete()

    RuleSet.ensure_bouncer_rules()

    honeypot = RuleSet.objects.filter(category="security:bouncer:honeypot_post").first()
    assert honeypot is not None, "Should create honeypot_post rule"
    assert "block://" in honeypot.handler, f"Handler should include block://, got {honeypot.handler}"
    assert honeypot.rules.count() >= 1, "Should have at least 1 rule"

    # Cleanup
    RuleSet.objects.filter(category__startswith="security:bouncer:").delete()


@th.django_unit_test()
def test_ensure_bouncer_rules_creates_campaign_rule(opts):
    """ensure_bouncer_rules should create campaign detection rule."""
    from mojo.apps.incident.models.rule import RuleSet

    RuleSet.objects.filter(category__startswith="security:bouncer:").delete()

    RuleSet.ensure_bouncer_rules()

    campaign = RuleSet.objects.filter(category="security:bouncer:campaign").first()
    assert campaign is not None, "Should create campaign rule"
    assert "block://" in campaign.handler, f"Handler should include block://, got {campaign.handler}"
    assert "notify://" in campaign.handler, f"Handler should include notify://, got {campaign.handler}"
    assert "ttl=86400" in campaign.handler, f"Campaign should block for 24hr, got {campaign.handler}"

    # Cleanup
    RuleSet.objects.filter(category__startswith="security:bouncer:").delete()


@th.django_unit_test()
def test_ensure_bouncer_rules_creates_high_confidence_rule(opts):
    """ensure_bouncer_rules should create high-confidence block rule."""
    from mojo.apps.incident.models.rule import RuleSet

    RuleSet.objects.filter(category__startswith="security:bouncer:").delete()

    RuleSet.ensure_bouncer_rules()

    block = RuleSet.objects.filter(
        category="security:bouncer:block",
        name="Bouncer - High Confidence Bot Block",
    ).first()
    assert block is not None, "Should create high-confidence block rule"
    assert "block://" in block.handler, f"Handler should include block://, got {block.handler}"

    # Check the rule matches risk_score >= 80
    rules = list(block.rules.all())
    assert len(rules) >= 1, "Should have at least 1 rule"
    score_rule = rules[0]
    assert score_rule.field_name == "risk_score", f"Rule should check risk_score, got {score_rule.field_name}"
    assert score_rule.value == "80", f"Threshold should be 80, got {score_rule.value}"

    # Cleanup
    RuleSet.objects.filter(category__startswith="security:bouncer:").delete()


@th.django_unit_test()
def test_ensure_bouncer_rules_is_idempotent(opts):
    """Calling ensure_bouncer_rules twice should not duplicate rules."""
    from mojo.apps.incident.models.rule import RuleSet

    RuleSet.objects.filter(category__startswith="security:bouncer:").delete()

    RuleSet.ensure_bouncer_rules()
    count1 = RuleSet.objects.filter(category__startswith="security:bouncer:").count()

    RuleSet.ensure_bouncer_rules()
    count2 = RuleSet.objects.filter(category__startswith="security:bouncer:").count()

    assert count1 == count2, f"Second call should not create duplicates: {count1} vs {count2}"
    assert count1 == 3, f"Should create exactly 3 bouncer rulesets, got {count1}"

    # Cleanup
    RuleSet.objects.filter(category__startswith="security:bouncer:").delete()


@th.django_unit_test()
def test_high_confidence_rule_matches_high_score_event(opts):
    """High-confidence bouncer rule should match events with risk_score >= 80."""
    from mojo.apps.incident.models.rule import RuleSet
    from mojo.apps.incident.models import Event

    RuleSet.objects.filter(category__startswith="security:bouncer:").delete()
    RuleSet.ensure_bouncer_rules()

    # Create a high-score bouncer block event
    event = Event.objects.create(
        category="security:bouncer:block",
        level=8,
        scope="account",
        details="Test bouncer block",
        source_ip="10.0.0.99",
        metadata={"risk_score": 85, "decision": "block"},
    )

    ruleset = RuleSet.check_by_category("security:bouncer:block", event)
    assert ruleset is not None, "High-score event should match the bouncer block rule"
    assert "High Confidence" in ruleset.name, f"Should match high confidence rule, got {ruleset.name}"

    # Cleanup
    event.delete()
    RuleSet.objects.filter(category__startswith="security:bouncer:").delete()


@th.django_unit_test()
def test_high_confidence_rule_skips_low_score_event(opts):
    """High-confidence bouncer rule should NOT match events with risk_score < 80."""
    from mojo.apps.incident.models.rule import RuleSet
    from mojo.apps.incident.models import Event

    RuleSet.objects.filter(category__startswith="security:bouncer:").delete()
    RuleSet.ensure_bouncer_rules()

    # Create a medium-score bouncer block event
    event = Event.objects.create(
        category="security:bouncer:block",
        level=8,
        scope="account",
        details="Test bouncer block medium",
        source_ip="10.0.0.100",
        metadata={"risk_score": 65, "decision": "block"},
    )

    ruleset = RuleSet.check_by_category("security:bouncer:block", event)
    assert ruleset is None, "Medium-score event should NOT match any bouncer block rule"

    # Cleanup
    event.delete()
    RuleSet.objects.filter(category__startswith="security:bouncer:").delete()


# ---------------------------------------------------------------------------
# Metrics instrumentation (verify code paths exist)
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_assess_imports_metrics(opts):
    """assess.py should import metrics module."""
    import mojo.apps.account.rest.bouncer.assess as assess_mod
    # Verify the module has access to metrics
    assert hasattr(assess_mod, 'metrics'), "assess module should import metrics"


@th.django_unit_test()
def test_views_imports_metrics(opts):
    """views.py should import metrics module."""
    import mojo.apps.account.rest.bouncer.views as views_mod
    assert hasattr(views_mod, 'metrics'), "views module should import metrics"


@th.django_unit_test()
def test_ensure_bouncer_defaults_exists(opts):
    """Lazy bouncer defaults initializer should exist in assess module."""
    import mojo.apps.account.rest.bouncer.assess as assess_mod
    assert hasattr(assess_mod, '_ensure_bouncer_defaults'), "Should have _ensure_bouncer_defaults function"
    assert callable(assess_mod._ensure_bouncer_defaults), "Should be callable"
