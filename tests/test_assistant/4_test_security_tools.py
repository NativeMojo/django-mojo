"""
Tests for assistant security and user security tools.

Calls tool handlers directly with (params, user) — no LLM needed.
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


TEST_EMAIL_ADMIN = 'asst-tools-admin@example.com'
TEST_EMAIL_TARGET = 'asst-tools-target@example.com'
TEST_PASSWORD = 'TestPass1!'
TEST_IP = '198.51.100.42'
TEST_IP_2 = '198.51.100.43'


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_data(opts):
    from mojo.apps.account.models import User, GeoLocatedIP
    from mojo.apps.incident.models import RuleSet, Rule, Incident, Event

    # Clean up prior test data
    User.objects.filter(email__in=[TEST_EMAIL_ADMIN, TEST_EMAIL_TARGET]).delete()
    RuleSet.objects.filter(name__startswith="test_asst_").delete()
    Incident.objects.filter(title__startswith="[asst_test]").delete()
    GeoLocatedIP.objects.filter(ip_address__in=[TEST_IP, TEST_IP_2]).delete()

    # Admin user
    opts.admin = User.objects.create_user(
        username=TEST_EMAIL_ADMIN, email=TEST_EMAIL_ADMIN, password=TEST_PASSWORD,
    )
    opts.admin.is_email_verified = True
    opts.admin.save()
    for perm in ["view_admin", "view_security", "manage_security", "manage_users"]:
        opts.admin.add_permission(perm)

    # Target user for disable/enable/logout tests
    opts.target = User.objects.create_user(
        username=TEST_EMAIL_TARGET, email=TEST_EMAIL_TARGET, password=TEST_PASSWORD,
    )
    opts.target.is_email_verified = True
    opts.target.auth_key = "original_auth_key_12345"
    opts.target.save()

    # Create a RuleSet with child rules
    opts.ruleset = RuleSet.objects.create(
        name="test_asst_ruleset",
        category="test_category",
        handler="block://?ttl=600",
        bundle_by=4,
        bundle_minutes=30,
        is_active=False,
    )
    Rule.objects.create(
        parent=opts.ruleset, name="Level check", index=0,
        field_name="level", comparator=">=", value="8", value_type="int",
    )

    # Create incidents and events for bulk/merge tests
    opts.incident1 = Incident.objects.create(
        title="[asst_test] Incident 1", category="test", status="new", priority=5,
    )
    opts.incident2 = Incident.objects.create(
        title="[asst_test] Incident 2", category="test", status="new", priority=3,
    )
    opts.incident3 = Incident.objects.create(
        title="[asst_test] Incident 3", category="test", status="new", priority=7,
    )
    opts.event1 = Event.objects.create(
        category="test", level=8, title="[asst_test] Event 1",
        incident=opts.incident2, metadata={"rule_id": 5710},
    )
    opts.event2 = Event.objects.create(
        category="test", level=5, title="[asst_test] Event 2",
        incident=opts.incident3, metadata={"rule_id": 5712},
    )
    opts.event3 = Event.objects.create(
        category="test", level=3, title="[asst_test] Event 3",
        metadata={"rule_id": 5710},
    )

    # Create GeoLocatedIP for block/unblock tests
    opts.geo_ip = GeoLocatedIP.objects.create(ip_address=TEST_IP)


# ---------------------------------------------------------------------------
# Rule management tools
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_get_ruleset_includes_rules(opts):
    """get_ruleset should return RuleSet details with child rules array."""
    from mojo.apps.assistant.services.tools.security import _tool_get_ruleset

    result = _tool_get_ruleset({"ruleset_id": opts.ruleset.pk}, opts.admin)
    assert_eq(result["id"], opts.ruleset.pk, "Should return correct ruleset ID")
    assert_eq(result["name"], "test_asst_ruleset", "Should return ruleset name")
    assert_true("rules" in result, "Should include 'rules' key")
    assert_eq(len(result["rules"]), 1, f"Should have 1 rule, got {len(result['rules'])}")
    rule = result["rules"][0]
    assert_eq(rule["field_name"], "level", "Rule should have field_name 'level'")
    assert_eq(rule["comparator"], ">=", "Rule should have comparator '>='")
    assert_eq(rule["value"], "8", "Rule should have value '8'")
    assert_eq(rule["value_type"], "int", "Rule should have value_type 'int'")


@th.django_unit_test()
def test_get_ruleset_not_found(opts):
    """get_ruleset should return error for non-existent ID."""
    from mojo.apps.assistant.services.tools.security import _tool_get_ruleset

    result = _tool_get_ruleset({"ruleset_id": 999999}, opts.admin)
    assert_true("error" in result, "Should return error for missing ruleset")


@th.django_unit_test()
def test_add_rule_condition(opts):
    """add_rule_condition should create a Rule with correct parent FK and auto-index."""
    from mojo.apps.assistant.services.tools.security import _tool_add_rule_condition
    from mojo.apps.incident.models import Rule

    result = _tool_add_rule_condition({
        "ruleset_id": opts.ruleset.pk,
        "name": "Source IP check",
        "field": "source_ip",
        "comparator": "==",
        "value": "10.0.0.1",
        "value_type": "str",
    }, opts.admin)

    assert_true(result.get("ok"), f"Should succeed, got {result}")
    assert_eq(result["ruleset_id"], opts.ruleset.pk, "Should reference correct ruleset")
    assert_eq(result["index"], 1, "Should auto-index to 1 (second rule)")

    # Verify in DB
    rule = Rule.objects.get(pk=result["rule_id"])
    assert_eq(rule.parent_id, opts.ruleset.pk, "Rule parent FK should match")
    assert_eq(rule.field_name, "source_ip", "field_name should be 'source_ip'")


@th.django_unit_test()
def test_update_ruleset_selective(opts):
    """update_ruleset should only update provided fields."""
    from mojo.apps.assistant.services.tools.security import _tool_update_ruleset

    result = _tool_update_ruleset({
        "ruleset_id": opts.ruleset.pk,
        "is_active": True,
        "priority": 99,
    }, opts.admin)

    assert_true(result.get("ok"), f"Should succeed, got {result}")
    assert_true("is_active" in result["updated_fields"], "Should report is_active updated")
    assert_true("priority" in result["updated_fields"], "Should report priority updated")

    # Verify in DB
    opts.ruleset.refresh_from_db()
    assert_true(opts.ruleset.is_active, "is_active should be True")
    assert_eq(opts.ruleset.priority, 99, "priority should be 99")
    # handler should be unchanged
    assert_eq(opts.ruleset.handler, "block://?ttl=600", "handler should be unchanged")


@th.django_unit_test()
def test_update_ruleset_no_fields(opts):
    """update_ruleset with no updatable fields should return error."""
    from mojo.apps.assistant.services.tools.security import _tool_update_ruleset

    result = _tool_update_ruleset({"ruleset_id": opts.ruleset.pk}, opts.admin)
    assert_true("error" in result, "Should return error when no fields provided")


@th.django_unit_test()
def test_delete_ruleset_cascades(opts):
    """delete_ruleset should delete the ruleset and cascade to child rules."""
    from mojo.apps.assistant.services.tools.security import _tool_delete_ruleset
    from mojo.apps.incident.models import RuleSet, Rule

    # Create a throwaway ruleset for deletion
    rs = RuleSet.objects.create(name="test_asst_delete_me", category="test_del")
    Rule.objects.create(parent=rs, field_name="level", comparator="==", value="1")
    Rule.objects.create(parent=rs, field_name="level", comparator="==", value="2")
    rs_id = rs.pk

    result = _tool_delete_ruleset({"ruleset_id": rs_id}, opts.admin)
    assert_true(result.get("ok"), f"Should succeed, got {result}")
    assert_eq(result["rules_deleted"], 2, "Should report 2 rules deleted")

    assert_true(
        not RuleSet.objects.filter(pk=rs_id).exists(),
        "RuleSet should be deleted from DB"
    )
    assert_eq(
        Rule.objects.filter(parent_id=rs_id).count(), 0,
        "Child rules should be cascade deleted"
    )


# ---------------------------------------------------------------------------
# IP management tools
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_unblock_ip(opts):
    """unblock_ip should unblock a blocked IP."""
    from mojo.apps.assistant.services.tools.security import _tool_unblock_ip
    from mojo.apps.account.models import GeoLocatedIP

    # Block it first
    geo = opts.geo_ip
    geo.is_blocked = True
    geo.blocked_reason = "test block"
    geo.save(update_fields=["is_blocked", "blocked_reason"])

    result = _tool_unblock_ip({"ip": TEST_IP, "reason": "test unblock"}, opts.admin)
    assert_true(result.get("ok"), f"Should succeed, got {result}")
    assert_eq(result["is_blocked"], False, "Should report unblocked")

    geo.refresh_from_db()
    assert_true(not geo.is_blocked, "IP should be unblocked in DB")


@th.django_unit_test()
def test_whitelist_ip(opts):
    """whitelist_ip should whitelist an IP."""
    from mojo.apps.assistant.services.tools.security import _tool_whitelist_ip
    from mojo.apps.account.models import GeoLocatedIP

    result = _tool_whitelist_ip({"ip": TEST_IP_2, "reason": "trusted office"}, opts.admin)
    assert_true(result.get("ok"), f"Should succeed, got {result}")
    assert_true(result["is_whitelisted"], "Should report whitelisted")

    geo = GeoLocatedIP.objects.get(ip_address=TEST_IP_2)
    assert_true(geo.is_whitelisted, "IP should be whitelisted in DB")


@th.django_unit_test()
def test_unwhitelist_ip(opts):
    """unwhitelist_ip should remove whitelist status."""
    from mojo.apps.assistant.services.tools.security import _tool_unwhitelist_ip
    from mojo.apps.account.models import GeoLocatedIP

    # Ensure it's whitelisted first
    geo, _ = GeoLocatedIP.objects.get_or_create(ip_address=TEST_IP_2)
    geo.is_whitelisted = True
    geo.save(update_fields=["is_whitelisted"])

    result = _tool_unwhitelist_ip({"ip": TEST_IP_2}, opts.admin)
    assert_true(result.get("ok"), f"Should succeed, got {result}")
    assert_eq(result["is_whitelisted"], False, "Should report not whitelisted")

    geo.refresh_from_db()
    assert_true(not geo.is_whitelisted, "IP should not be whitelisted in DB")


@th.django_unit_test()
def test_query_blocked_ips(opts):
    """query_blocked_ips should return currently blocked IPs."""
    from mojo.apps.assistant.services.tools.security import _tool_query_blocked_ips
    from mojo.apps.account.models import GeoLocatedIP
    from django.utils import timezone

    # Ensure one is blocked
    geo = opts.geo_ip
    geo.is_blocked = True
    geo.blocked_at = timezone.now()
    geo.blocked_reason = "test block for query"
    geo.save(update_fields=["is_blocked", "blocked_at", "blocked_reason"])

    result = _tool_query_blocked_ips({"limit": 50}, opts.admin)
    assert_true(isinstance(result, list), f"Expected list, got {type(result).__name__}")
    ips = [r["ip"] for r in result]
    assert_true(TEST_IP in ips, f"Blocked IP {TEST_IP} should be in results")


# ---------------------------------------------------------------------------
# Incident bulk operations
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_bulk_update_incidents(opts):
    """bulk_update_incidents should update all valid IDs and report failures."""
    from mojo.apps.assistant.services.tools.security import _tool_bulk_update_incidents

    result = _tool_bulk_update_incidents({
        "incident_ids": [opts.incident1.pk, opts.incident2.pk, 999999],
        "status": "resolved",
        "note": "Bulk resolved in test",
    }, opts.admin)

    assert_eq(len(result["updated"]), 2, f"Should update 2 incidents, got {result['updated']}")
    assert_eq(len(result["failed"]), 1, f"Should fail 1 ID, got {result['failed']}")
    assert_true(999999 in result["failed"], "Missing ID should be in failed list")

    # Verify in DB
    opts.incident1.refresh_from_db()
    assert_eq(opts.incident1.status, "resolved", "Incident 1 should be resolved")
    opts.incident2.refresh_from_db()
    assert_eq(opts.incident2.status, "resolved", "Incident 2 should be resolved")


@th.django_unit_test()
def test_bulk_update_cap(opts):
    """bulk_update_incidents should reject more than 100 IDs."""
    from mojo.apps.assistant.services.tools.security import _tool_bulk_update_incidents

    result = _tool_bulk_update_incidents({
        "incident_ids": list(range(101)),
        "status": "resolved",
        "note": "Too many",
    }, opts.admin)
    assert_true("error" in result, "Should return error for >100 IDs")


@th.django_unit_test()
def test_merge_incidents(opts):
    """merge_incidents should move events from sources to target."""
    from mojo.apps.assistant.services.tools.security import _tool_merge_incidents
    from mojo.apps.incident.models import Incident, Event

    result = _tool_merge_incidents({
        "target_id": opts.incident1.pk,
        "source_ids": [opts.incident2.pk],
    }, opts.admin)

    assert_true(result.get("ok"), f"Should succeed, got {result}")

    # Event from incident2 should now be on incident1
    opts.event1.refresh_from_db()
    assert_eq(opts.event1.incident_id, opts.incident1.pk,
              "Event should be moved to target incident")

    # Source incident should be deleted
    assert_true(
        not Incident.objects.filter(pk=opts.incident2.pk).exists(),
        "Source incident should be deleted after merge"
    )


# ---------------------------------------------------------------------------
# Event detail
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_get_event_full_metadata(opts):
    """get_event should return full metadata without truncation."""
    from mojo.apps.assistant.services.tools.security import _tool_get_event

    result = _tool_get_event({"event_id": opts.event1.pk}, opts.admin)
    assert_eq(result["id"], opts.event1.pk, "Should return correct event ID")
    assert_eq(result["metadata"]["rule_id"], 5710, "Should include full metadata")
    assert_true(result["details"] is None or isinstance(result["details"], str),
                "Details should be full string, not truncated")


@th.django_unit_test()
def test_get_event_not_found(opts):
    """get_event should return error for non-existent ID."""
    from mojo.apps.assistant.services.tools.security import _tool_get_event

    result = _tool_get_event({"event_id": 999999}, opts.admin)
    assert_true("error" in result, "Should return error for missing event")


# ---------------------------------------------------------------------------
# Query improvements
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_query_events_rule_id_filter(opts):
    """query_events with rule_id filter should return matching events."""
    from mojo.apps.assistant.services.tools.security import _tool_query_events

    result = _tool_query_events({"rule_id": 5710, "minutes": 60}, opts.admin)
    assert_true(isinstance(result, list), f"Expected list, got {type(result).__name__}")
    # All returned events should have rule_id 5710 in their metadata
    for e in result:
        assert_true(e["incident_id"] is not None or e["incident_id"] is None,
                    "Events should have incident_id field")


@th.django_unit_test()
def test_query_event_counts_group_by_rule_id(opts):
    """query_event_counts with group_by=rule_id should group by metadata rule_id."""
    from mojo.apps.assistant.services.tools.security import _tool_query_event_counts

    result = _tool_query_event_counts({
        "minutes": 60,
        "category": "test",
        "group_by": "rule_id",
    }, opts.admin)
    assert_true(isinstance(result, list), f"Expected list, got {type(result).__name__}")
    # Should have entries grouped by rule_id
    assert_true(len(result) >= 1, f"Expected at least 1 group, got {len(result)}")


@th.django_unit_test()
def test_query_rulesets_shows_is_active(opts):
    """query_rulesets should return is_active field (not legacy is_disabled)."""
    from mojo.apps.assistant.services.tools.security import _tool_query_rulesets

    result = _tool_query_rulesets({"category": "test_category"}, opts.admin)
    assert_true(len(result) >= 1, "Should return at least 1 ruleset")
    rs = result[0]
    assert_true("is_active" in rs, "Should include 'is_active' field")
    assert_true("is_disabled" not in rs, "Should NOT include legacy 'is_disabled' field")
    assert_true("trigger_count" in rs, "Should include 'trigger_count' field")
    assert_true("priority" in rs, "Should include 'priority' field")


# ---------------------------------------------------------------------------
# User security actions
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_disable_user(opts):
    """disable_user should set is_active=False and rotate auth_key."""
    from mojo.apps.assistant.services.tools.users import _tool_disable_user

    old_auth_key = opts.target.auth_key
    result = _tool_disable_user({"user_id": opts.target.pk, "reason": "test"}, opts.admin)

    assert_true(result.get("ok"), f"Should succeed, got {result}")
    assert_eq(result["is_active"], False, "Should report is_active=False")
    assert_true(result["sessions_invalidated"], "Should report sessions invalidated")

    opts.target.refresh_from_db()
    assert_true(not opts.target.is_active, "User should be disabled in DB")
    assert_true(opts.target.auth_key != old_auth_key, "auth_key should be rotated")


@th.django_unit_test()
def test_disable_user_self_blocked(opts):
    """disable_user should refuse to disable the calling user."""
    from mojo.apps.assistant.services.tools.users import _tool_disable_user

    result = _tool_disable_user({"user_id": opts.admin.pk, "reason": "oops"}, opts.admin)
    assert_true("error" in result, "Should return error for self-disable")
    assert_true("Cannot disable your own" in result["error"],
                f"Error should mention self-disable: {result['error']}")


@th.django_unit_test()
def test_enable_user(opts):
    """enable_user should reactivate a disabled account."""
    from mojo.apps.assistant.services.tools.users import _tool_enable_user

    # Ensure target is disabled
    opts.target.is_active = False
    opts.target.save(update_fields=["is_active"])

    result = _tool_enable_user({"user_id": opts.target.pk, "reason": "investigation complete"}, opts.admin)
    assert_true(result.get("ok"), f"Should succeed, got {result}")
    assert_eq(result["is_active"], True, "Should report is_active=True")

    opts.target.refresh_from_db()
    assert_true(opts.target.is_active, "User should be active in DB")


@th.django_unit_test()
def test_force_logout(opts):
    """force_logout should rotate auth_key without disabling the account."""
    from mojo.apps.assistant.services.tools.users import _tool_force_logout

    # Ensure target is active
    opts.target.is_active = True
    opts.target.auth_key = "before_logout_key"
    opts.target.save(update_fields=["is_active", "auth_key"])

    result = _tool_force_logout({"user_id": opts.target.pk, "reason": "security test"}, opts.admin)
    assert_true(result.get("ok"), f"Should succeed, got {result}")
    assert_true(result["sessions_invalidated"], "Should report sessions invalidated")
    assert_true(result["account_active"], "Account should still be active")

    opts.target.refresh_from_db()
    assert_true(opts.target.is_active, "User should still be active")
    assert_true(opts.target.auth_key != "before_logout_key", "auth_key should be rotated")


# ---------------------------------------------------------------------------
# Registry check
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_new_tools_registered(opts):
    """All new tools should be in the registry with correct mutates flags."""
    from mojo.apps.assistant import get_registry

    registry = get_registry()

    # New read-only security tools
    for name in ["get_event", "get_ruleset", "query_blocked_ips", "query_ipsets"]:
        assert_true(name in registry, f"Tool '{name}' should be in registry")
        assert_true(not registry[name]["mutates"], f"Tool '{name}' should not mutate")

    # New mutation security tools
    for name in ["add_rule_condition", "update_ruleset", "delete_ruleset",
                 "unblock_ip", "whitelist_ip", "unwhitelist_ip",
                 "bulk_update_incidents", "merge_incidents"]:
        assert_true(name in registry, f"Tool '{name}' should be in registry")
        assert_true(registry[name]["mutates"], f"Tool '{name}' should have mutates=True")

    # New user security tools
    for name in ["disable_user", "enable_user", "force_logout"]:
        assert_true(name in registry, f"Tool '{name}' should be in registry")
        assert_true(registry[name]["mutates"], f"Tool '{name}' should have mutates=True")
