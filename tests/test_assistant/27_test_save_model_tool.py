"""Tests for save_model_instance assistant tool and dispatcher request/conversation threading."""
import objict
from testit import helpers as th


TEST_ADMIN_EMAIL = "savetool_admin@test.com"
TEST_NOPRIV_EMAIL = "savetool_nopriv@test.com"


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_save_tools(opts):
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import RuleSet
    from mojo.apps.assistant.models import Conversation

    # Clean up prior test data
    User.objects.filter(email__in=[TEST_ADMIN_EMAIL, TEST_NOPRIV_EMAIL]).delete()
    RuleSet.objects.filter(name__startswith="savetest_").delete()

    # Admin with full perms
    opts.admin = User.objects.create_user(
        username=TEST_ADMIN_EMAIL, email=TEST_ADMIN_EMAIL, password="pass123",
    )
    opts.admin.is_email_verified = True
    opts.admin.save()
    for perm in ["view_admin", "view_security", "manage_security", "security"]:
        opts.admin.add_permission(perm)

    # User with view_admin only
    opts.nopriv = User.objects.create_user(
        username=TEST_NOPRIV_EMAIL, email=TEST_NOPRIV_EMAIL, password="pass123",
    )
    opts.nopriv.is_email_verified = True
    opts.nopriv.save()
    opts.nopriv.add_permission("view_admin")

    # Conversation for audit metadata correlation
    opts.conversation = Conversation.objects.create(
        user=opts.admin, title="savetool_test_conversation",
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_save_model_instance_registered(opts):
    """save_model_instance must register with mutates=True under view_admin."""
    from mojo.apps.assistant import get_registry

    registry = get_registry()
    assert "save_model_instance" in registry, "save_model_instance should be in registry"
    entry = registry["save_model_instance"]
    assert entry["mutates"] is True, "save_model_instance must declare mutates=True"
    assert entry["permission"] == "view_admin", \
        f"Expected view_admin, got {entry['permission']}"
    assert entry["domain"] == "models", f"Expected models domain, got {entry['domain']}"


# ---------------------------------------------------------------------------
# Create path
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_create_with_perms_succeeds(opts):
    """Create succeeds when user has CREATE_PERMS; row exists; audit written."""
    from mojo.apps.assistant.services.tools.models import _tool_save_model_instance
    from mojo.apps.incident.models import RuleSet
    from mojo.apps.logit.models import Log

    name = "savetest_create_ok"
    result = _tool_save_model_instance({
        "app_name": "incident", "model_name": "RuleSet",
        "data": {"name": name, "category": "savetest_cat"},
    }, opts.admin, conversation=opts.conversation)
    assert result.get("ok") is True, f"Should succeed, got {result}"
    assert result["created"] is True, f"Should be created, got {result}"
    assert result["model"] == "incident.RuleSet", f"Wrong model label: {result['model']}"
    assert isinstance(result["pk"], int), f"pk should be int, got {result['pk']!r}"

    rs = RuleSet.objects.filter(pk=result["pk"]).first()
    assert rs is not None, f"RuleSet pk={result['pk']} should exist after create"
    assert rs.name == name, f"name not persisted: {rs.name!r}"

    # Audit log entry was written under the right kind
    audit = Log.objects.filter(kind="assistant:model:created", uid=opts.admin.pk).order_by("-pk").first()
    assert audit is not None, "Should have an assistant:model:created audit log entry"
    assert "incident.RuleSet" in audit.log, f"Audit message missing model label: {audit.log}"

    # Cleanup
    rs.delete()


@th.django_unit_test()
def test_create_blocked_by_can_create_false(opts):
    """CAN_CREATE=False blocks create even if perms are sufficient."""
    from mojo.apps.assistant.services.tools.models import _tool_save_model_instance

    # account.UserLoginEvent has CAN_CREATE = False
    result = _tool_save_model_instance({
        "app_name": "account", "model_name": "UserLoginEvent",
        "data": {"ip_address": "127.0.0.1"},
    }, opts.admin)
    assert "error" in result, f"Should be denied by CAN_CREATE=False, got {result}"
    assert "not allowed" in result["error"].lower(), \
        f"Error should mention 'not allowed', got: {result['error']}"


@th.django_unit_test()
def test_create_without_create_perms_denied(opts):
    """Permission denial reports incident event and returns sanitized error."""
    from mojo.apps.assistant.services.tools.models import _tool_save_model_instance
    from mojo.apps.incident.models import Event

    before = Event.objects.filter(category="assistant_permission_denied").count()
    result = _tool_save_model_instance({
        "app_name": "incident", "model_name": "RuleSet",
        "data": {"name": "savetest_denied", "category": "savetest_denied"},
    }, opts.nopriv)
    after = Event.objects.filter(category="assistant_permission_denied").count()

    assert "error" in result, f"Should be denied, got {result}"
    assert "Permission denied" in result["error"], f"Error wording: {result['error']}"
    assert after > before, \
        f"Should record security event, before={before} after={after}"


# ---------------------------------------------------------------------------
# Update path
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_update_with_save_perms_succeeds(opts):
    """Update persists field changes; audit captures field NAMES."""
    from mojo.apps.assistant.services.tools.models import _tool_save_model_instance
    from mojo.apps.incident.models import RuleSet
    from mojo.apps.logit.models import Log

    rs = RuleSet.objects.create(name="savetest_update_target", category="savetest_upd")

    new_category = "savetest_upd_changed"
    result = _tool_save_model_instance({
        "app_name": "incident", "model_name": "RuleSet", "pk": rs.pk,
        "data": {"category": new_category},
    }, opts.admin, conversation=opts.conversation)
    assert result.get("ok") is True, f"Should succeed, got {result}"
    assert result["created"] is False, f"Should not be created, got {result}"
    assert result["pk"] == rs.pk, f"pk mismatch: {result['pk']} vs {rs.pk}"

    rs.refresh_from_db()
    assert rs.category == new_category, f"category not persisted: {rs.category!r}"

    # Audit log records field name only — NOT the value
    audit = Log.objects.filter(
        kind="assistant:model:updated", uid=opts.admin.pk, model_id=rs.pk,
    ).order_by("-pk").first()
    assert audit is not None, "Should have an assistant:model:updated audit log entry"
    assert "category" in audit.log, f"Audit message should list field name 'category', got {audit.log}"
    assert new_category not in audit.log, \
        f"Audit must NOT include field value (got {audit.log!r})"

    rs.delete()


@th.django_unit_test()
def test_update_without_save_perms_denied(opts):
    """Update without SAVE_PERMS reports incident event, no mutation occurs."""
    from mojo.apps.assistant.services.tools.models import _tool_save_model_instance
    from mojo.apps.incident.models import RuleSet, Event

    rs = RuleSet.objects.create(name="savetest_upd_perm", category="savetest_upd_perm")
    original_category = rs.category

    before = Event.objects.filter(category="assistant_permission_denied").count()
    result = _tool_save_model_instance({
        "app_name": "incident", "model_name": "RuleSet", "pk": rs.pk,
        "data": {"category": "should_not_apply"},
    }, opts.nopriv)
    after = Event.objects.filter(category="assistant_permission_denied").count()

    assert "error" in result, f"Should be denied, got {result}"
    assert after > before, "Should record security event for denied update"

    rs.refresh_from_db()
    assert rs.category == original_category, \
        f"category should not have changed, got {rs.category!r}"

    rs.delete()


@th.django_unit_test()
def test_update_pk_not_found(opts):
    """Update with nonexistent pk returns clean not-found error."""
    from mojo.apps.assistant.services.tools.models import _tool_save_model_instance

    result = _tool_save_model_instance({
        "app_name": "incident", "model_name": "RuleSet", "pk": 999999,
        "data": {"category": "x"},
    }, opts.admin)
    assert "error" in result, f"Should error, got {result}"
    assert "not found" in result["error"], f"Error should say not found: {result['error']}"


# ---------------------------------------------------------------------------
# Validation / param errors
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_missing_required_params(opts):
    """Missing app_name/model_name/data should error cleanly."""
    from mojo.apps.assistant.services.tools.models import _tool_save_model_instance

    r1 = _tool_save_model_instance({"model_name": "RuleSet", "data": {}}, opts.admin)
    assert "error" in r1, "Should error when app_name missing"

    r2 = _tool_save_model_instance(
        {"app_name": "incident", "model_name": "RuleSet"}, opts.admin,
    )
    assert "error" in r2, "Should error when data missing"

    r3 = _tool_save_model_instance({
        "app_name": "incident", "model_name": "RuleSet", "data": "not a dict",
    }, opts.admin)
    assert "error" in r3, "Should error when data is not a dict"


@th.django_unit_test()
def test_bad_model(opts):
    """Unknown model returns clean error."""
    from mojo.apps.assistant.services.tools.models import _tool_save_model_instance

    result = _tool_save_model_instance({
        "app_name": "fake_app", "model_name": "FakeModel", "data": {"x": 1},
    }, opts.admin)
    assert "error" in result, "Should error for unknown model"
    assert "not found" in result["error"], f"Error wording: {result['error']}"


@th.django_unit_test()
def test_no_rest_model_rejected(opts):
    """NO_REST models cannot be saved via the assistant."""
    from mojo.apps.assistant.services.tools.models import _tool_save_model_instance

    result = _tool_save_model_instance({
        "app_name": "assistant", "model_name": "Message",
        "data": {"role": "user", "content": "x"},
    }, opts.admin)
    assert "error" in result, "Message has NO_REST so save must error"


# ---------------------------------------------------------------------------
# Audit failure path
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_failed_save_writes_save_failed_audit(opts):
    """Exception during save writes save_failed audit, returns sanitized error."""
    from mojo.apps.assistant.services.tools.models import _tool_save_model_instance
    from mojo.apps.logit.models import Log

    # Force a failure by creating with a duplicate handler URL constraint or
    # an invalid category type. RuleSet has no strict validators; instead use
    # an invalid bundle_by value (negative) — saved as int but unique constraints
    # are easier. We use a duplicate name to violate uniqueness via setting an
    # absurdly long category string (charfield max_length=124).
    long_category = "x" * 500  # exceeds CharField(max_length=124)
    result = _tool_save_model_instance({
        "app_name": "incident", "model_name": "RuleSet",
        "data": {"name": "savetest_fail", "category": long_category},
    }, opts.admin, conversation=opts.conversation)

    assert "error" in result, f"Should error, got {result}"
    assert "Save failed" in result["error"], f"Sanitized message expected, got {result['error']}"

    audit = Log.objects.filter(
        kind="assistant:model:save_failed", uid=opts.admin.pk,
    ).order_by("-pk").first()
    assert audit is not None, "Should have a save_failed audit entry"


# ---------------------------------------------------------------------------
# request_meta threading + conversation correlation
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_request_meta_threads_real_ip(opts):
    """When request_meta provides an IP, denial events record it."""
    from mojo.apps.assistant.services.tools.models import _tool_save_model_instance
    from mojo.apps.incident.models import Event

    request_meta = objict.objict(
        ip="203.0.113.42", user_agent="test-agent/1.0", path="/api/assistant", method="POST",
    )
    _tool_save_model_instance({
        "app_name": "incident", "model_name": "RuleSet",
        "data": {"name": "savetest_meta_ip", "category": "savetest_meta"},
    }, opts.nopriv, request_meta=request_meta)

    ev = Event.objects.filter(category="assistant_permission_denied").order_by("-pk").first()
    assert ev is not None, "Should record event"
    assert ev.source_ip == "203.0.113.42", \
        f"Event should record real IP from request_meta, got {ev.source_ip!r}"


@th.django_unit_test()
def test_conversation_id_in_audit_metadata(opts):
    """Audit log entry records conversation_id when conversation is provided."""
    from mojo.apps.assistant.services.tools.models import _tool_save_model_instance
    from mojo.apps.incident.models import RuleSet
    from mojo.apps.logit.models import Log

    rs = RuleSet.objects.create(name="savetest_conv_corr", category="savetest_corr")
    _tool_save_model_instance({
        "app_name": "incident", "model_name": "RuleSet", "pk": rs.pk,
        "data": {"category": "savetest_corr_changed"},
    }, opts.admin, conversation=opts.conversation)

    audit = Log.objects.filter(
        kind="assistant:model:updated", uid=opts.admin.pk, model_id=rs.pk,
    ).order_by("-pk").first()
    assert audit is not None, "Should have audit entry"
    import ujson
    payload = ujson.loads(audit.payload) if audit.payload else {}
    assert payload.get("conversation_id") == opts.conversation.pk, \
        f"Audit should carry conversation_id={opts.conversation.pk}, got {payload}"

    rs.delete()


# ---------------------------------------------------------------------------
# Dispatcher signature inspection — handler kwargs are optional
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_dispatcher_passes_kwargs_only_when_handler_accepts(opts):
    """_call_handler inspects handler signatures and only passes kwargs they declare."""
    from mojo.apps.assistant.services.agent import _call_handler

    captured = {}

    def legacy_handler(params, user):
        captured["legacy"] = (params, user)
        return {"ok": True}

    def aware_handler(params, user, *, request_meta=None, conversation=None):
        captured["aware"] = {
            "params": params, "user": user,
            "request_meta": request_meta, "conversation": conversation,
        }
        return {"ok": True}

    rm = objict.objict(ip="10.0.0.1")
    _call_handler(legacy_handler, {"a": 1}, opts.admin, rm, opts.conversation)
    assert captured["legacy"] == ({"a": 1}, opts.admin), \
        f"Legacy handler must receive (params, user) only, got {captured['legacy']}"

    _call_handler(aware_handler, {"b": 2}, opts.admin, rm, opts.conversation)
    a = captured["aware"]
    assert a["params"] == {"b": 2}, f"params mismatch: {a['params']}"
    assert a["user"] is opts.admin, "user should be passed through"
    assert a["request_meta"] is rm, "request_meta should be threaded to aware handler"
    assert a["conversation"] is opts.conversation, \
        "conversation should be threaded to aware handler"


# ---------------------------------------------------------------------------
# Delete tool retrofit — audit trail
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_delete_writes_audit_log(opts):
    """delete_model_instance now writes assistant:model:deleted audit entry."""
    from mojo.apps.assistant.services.tools.models import _tool_delete_model_instance
    from mojo.apps.incident.models import RuleSet
    from mojo.apps.logit.models import Log

    rs = RuleSet.objects.create(name="savetest_del_audit", category="savetest_del_audit")
    rs_pk = rs.pk
    result = _tool_delete_model_instance({
        "app_name": "incident", "model_name": "RuleSet", "pk": rs_pk,
    }, opts.admin, conversation=opts.conversation)
    assert result.get("ok") is True, f"Delete should succeed, got {result}"

    audit = Log.objects.filter(
        kind="assistant:model:deleted", uid=opts.admin.pk, model_id=rs_pk,
    ).order_by("-pk").first()
    assert audit is not None, "Delete should write assistant:model:deleted audit entry"
    import ujson
    payload = ujson.loads(audit.payload) if audit.payload else {}
    assert payload.get("conversation_id") == opts.conversation.pk, \
        f"Delete audit should carry conversation_id, got {payload}"
