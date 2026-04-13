"""Tests for delete_rule and delete_model_instance assistant tools."""
from testit import helpers as th


TEST_ADMIN_EMAIL = "deltools_admin@test.com"
TEST_NOPRIV_EMAIL = "deltools_nopriv@test.com"


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_delete_tools(opts):
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import RuleSet, Rule

    # Clean up prior test data
    User.objects.filter(email__in=[TEST_ADMIN_EMAIL, TEST_NOPRIV_EMAIL]).delete()
    RuleSet.objects.filter(name__startswith="deltest_").delete()

    # Admin with full perms
    opts.admin = User.objects.create_user(
        username=TEST_ADMIN_EMAIL, email=TEST_ADMIN_EMAIL, password="pass123",
    )
    opts.admin.is_email_verified = True
    opts.admin.save()
    for perm in ["view_admin", "view_security", "manage_security"]:
        opts.admin.add_permission(perm)

    # User with view_admin only (no manage_security)
    opts.nopriv = User.objects.create_user(
        username=TEST_NOPRIV_EMAIL, email=TEST_NOPRIV_EMAIL, password="pass123",
    )
    opts.nopriv.is_email_verified = True
    opts.nopriv.save()
    opts.nopriv.add_permission("view_admin")

    # Create a RuleSet with multiple rules for delete_rule tests
    opts.ruleset = RuleSet.objects.create(
        name="deltest_ruleset", category="deltest", handler="block://?ttl=300",
        bundle_by=4, bundle_minutes=30, is_active=False,
    )
    opts.rule1 = Rule.objects.create(
        parent=opts.ruleset, name="Level check", index=0,
        field_name="level", comparator=">=", value="5", value_type="int",
    )
    opts.rule2 = Rule.objects.create(
        parent=opts.ruleset, name="Category check", index=1,
        field_name="category", comparator="==", value="test", value_type="str",
    )


# ---------------------------------------------------------------------------
# delete_rule — happy path
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_delete_rule_success(opts):
    """delete_rule should remove a single rule and return remaining count."""
    from mojo.apps.assistant.services.tools.security.rules import _tool_delete_rule
    from mojo.apps.incident.models import Rule

    result = _tool_delete_rule({"rule_id": opts.rule2.pk}, opts.admin)
    assert result.get("ok") is True, f"Should succeed, got {result}"
    assert result["rule_id"] == opts.rule2.pk, f"Should return deleted rule ID, got {result['rule_id']}"
    assert result["ruleset_id"] == opts.ruleset.pk, f"Should return parent ruleset ID, got {result['ruleset_id']}"
    assert result["remaining_rules"] == 1, f"Should have 1 remaining rule, got {result['remaining_rules']}"

    # Verify rule is gone from DB
    assert not Rule.objects.filter(pk=opts.rule2.pk).exists(), "Deleted rule should not exist in DB"
    # Verify other rule still exists
    assert Rule.objects.filter(pk=opts.rule1.pk).exists(), "Other rule should still exist"


@th.django_unit_test()
def test_delete_rule_not_found(opts):
    """delete_rule should return error for nonexistent rule ID."""
    from mojo.apps.assistant.services.tools.security.rules import _tool_delete_rule

    result = _tool_delete_rule({"rule_id": 999999}, opts.admin)
    assert "error" in result, "Should return error for missing rule"
    assert "not found" in result["error"], f"Error should say not found: {result['error']}"


# ---------------------------------------------------------------------------
# delete_rule — registration
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_delete_rule_registered(opts):
    """delete_rule should be registered with correct metadata."""
    from mojo.apps.assistant import get_registry

    registry = get_registry()
    assert "delete_rule" in registry, "delete_rule should be in registry"
    entry = registry["delete_rule"]
    assert entry["mutates"] is True, "delete_rule should have mutates=True"
    assert entry["permission"] == "manage_security", \
        f"Permission should be manage_security, got {entry['permission']}"
    assert entry["domain"] == "security", f"Domain should be security, got {entry['domain']}"


# ---------------------------------------------------------------------------
# delete_model_instance — happy path
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_delete_model_instance_success(opts):
    """delete_model_instance should delete an instance on a CAN_DELETE model."""
    from mojo.apps.assistant.services.tools.models import _tool_delete_model_instance
    from mojo.apps.incident.models import RuleSet

    # Create a throwaway ruleset
    rs = RuleSet.objects.create(name="deltest_generic_delete", category="deltest_gen")
    rs_pk = rs.pk

    result = _tool_delete_model_instance({
        "app_name": "incident", "model_name": "RuleSet", "pk": rs_pk,
    }, opts.admin)
    assert result.get("ok") is True, f"Should succeed, got {result}"
    assert result["model"] == "incident.RuleSet", f"Should return model label, got {result['model']}"
    assert result["pk"] == rs_pk, f"Should return pk, got {result['pk']}"

    # Verify deleted
    assert not RuleSet.objects.filter(pk=rs_pk).exists(), "Instance should be deleted from DB"


# ---------------------------------------------------------------------------
# delete_model_instance — CAN_DELETE gate
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_delete_model_instance_no_can_delete(opts):
    """delete_model_instance should reject models without CAN_DELETE=True."""
    from mojo.apps.assistant.services.tools.models import _tool_delete_model_instance

    # Event model does not have CAN_DELETE = True
    result = _tool_delete_model_instance({
        "app_name": "incident", "model_name": "Event", "pk": 1,
    }, opts.admin)
    assert "error" in result, "Should return error for model without CAN_DELETE"
    assert "not allowed" in result["error"], f"Error should mention not allowed: {result['error']}"


# ---------------------------------------------------------------------------
# delete_model_instance — permission denied
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_delete_model_instance_permission_denied(opts):
    """delete_model_instance should reject users without DELETE_PERMS."""
    from mojo.apps.assistant.services.tools.models import _tool_delete_model_instance
    from mojo.apps.incident.models import RuleSet

    # Create a throwaway ruleset
    rs = RuleSet.objects.create(name="deltest_perm_check", category="deltest_perm")

    # nopriv has view_admin but not manage_security
    result = _tool_delete_model_instance({
        "app_name": "incident", "model_name": "RuleSet", "pk": rs.pk,
    }, opts.nopriv)
    assert "error" in result, "Should return error for insufficient permissions"
    assert "Permission denied" in result["error"], f"Error should mention permission denied: {result['error']}"

    # Verify NOT deleted
    assert RuleSet.objects.filter(pk=rs.pk).exists(), "Instance should still exist after permission denial"

    # Clean up
    rs.delete()


# ---------------------------------------------------------------------------
# delete_model_instance — instance not found
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_delete_model_instance_not_found(opts):
    """delete_model_instance should return error for nonexistent pk."""
    from mojo.apps.assistant.services.tools.models import _tool_delete_model_instance

    result = _tool_delete_model_instance({
        "app_name": "incident", "model_name": "RuleSet", "pk": 999999,
    }, opts.admin)
    assert "error" in result, "Should return error for missing instance"
    assert "not found" in result["error"], f"Error should say not found: {result['error']}"


# ---------------------------------------------------------------------------
# delete_model_instance — NO_REST model
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_delete_model_instance_no_rest(opts):
    """delete_model_instance should reject NO_REST models."""
    from mojo.apps.assistant.services.tools.models import _tool_delete_model_instance

    result = _tool_delete_model_instance({
        "app_name": "assistant", "model_name": "Message", "pk": 1,
    }, opts.admin)
    assert "error" in result, "Should return error for NO_REST model"


# ---------------------------------------------------------------------------
# delete_model_instance — bad model
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_delete_model_instance_bad_model(opts):
    """delete_model_instance should return error for nonexistent model."""
    from mojo.apps.assistant.services.tools.models import _tool_delete_model_instance

    result = _tool_delete_model_instance({
        "app_name": "fake_app", "model_name": "FakeModel", "pk": 1,
    }, opts.admin)
    assert "error" in result, "Should return error for nonexistent model"
    assert "not found" in result["error"], f"Error should say not found: {result['error']}"


# ---------------------------------------------------------------------------
# delete_model_instance — registration
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_delete_model_instance_registered(opts):
    """delete_model_instance should be registered with correct metadata."""
    from mojo.apps.assistant import get_registry

    registry = get_registry()
    assert "delete_model_instance" in registry, "delete_model_instance should be in registry"
    entry = registry["delete_model_instance"]
    assert entry["mutates"] is True, "delete_model_instance should have mutates=True"
    assert entry["permission"] == "view_admin", \
        f"Permission should be view_admin, got {entry['permission']}"
    assert entry["domain"] == "models", f"Domain should be models, got {entry['domain']}"


# ---------------------------------------------------------------------------
# delete_model_instance — security event on denial
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_delete_model_instance_reports_security_event(opts):
    """Permission denial should create a security event."""
    from mojo.apps.assistant.services.tools.models import _tool_delete_model_instance
    from mojo.apps.incident.models import RuleSet, Event

    rs = RuleSet.objects.create(name="deltest_sec_event", category="deltest_sec")

    before_count = Event.objects.filter(category="assistant_permission_denied").count()
    _tool_delete_model_instance({
        "app_name": "incident", "model_name": "RuleSet", "pk": rs.pk,
    }, opts.nopriv)
    after_count = Event.objects.filter(category="assistant_permission_denied").count()

    assert after_count > before_count, \
        f"Should create security event, before={before_count} after={after_count}"

    # Clean up
    rs.delete()


# ---------------------------------------------------------------------------
# delete_model_instance — missing params
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_delete_model_instance_missing_params(opts):
    """delete_model_instance should error when required params are missing."""
    from mojo.apps.assistant.services.tools.models import _tool_delete_model_instance

    result = _tool_delete_model_instance({"app_name": "incident", "model_name": "RuleSet"}, opts.admin)
    assert "error" in result, "Should return error when pk is missing"
