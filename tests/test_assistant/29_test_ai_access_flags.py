"""Tests for DENY_AI_* RestMeta flags in the assistant model tools.

Each flag is toggled via monkey-patching the target model's RestMeta
(setattr in setup, delattr in teardown) so no migrations or test-only
models are required.
"""
from testit import helpers as th


TEST_ADMIN_EMAIL = "aiflags_admin@test.com"


# ---------------------------------------------------------------------------
# Setup / teardown utilities
# ---------------------------------------------------------------------------

@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_ai_flags(opts):
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import RuleSet, Event

    User.objects.filter(email=TEST_ADMIN_EMAIL).delete()
    opts.admin = User.objects.create_user(
        username=TEST_ADMIN_EMAIL, email=TEST_ADMIN_EMAIL, password="pass123",
    )
    opts.admin.is_email_verified = True
    opts.admin.save()
    for perm in ["view_admin", "view_security", "manage_security", "security"]:
        opts.admin.add_permission(perm)

    # Cleanup leftover rows
    RuleSet.objects.filter(name__startswith="aiflags_").delete()
    Event.objects.filter(title__startswith="aiflags_").delete()

    # Seed one row of each for query/aggregate/export/delete/update paths
    opts.ruleset = RuleSet.objects.create(
        name="aiflags_seed", category="aiflags_cat",
    )
    opts.event = Event.objects.create(
        title="aiflags_seed_event",
        details="seed",
        category="aiflags_cat",
        level=3,
        scope="global",
    )


_ALL_FLAGS = ("DENY_AI", "DENY_AI_VIEW", "DENY_AI_CREATE",
              "DENY_AI_UPDATE", "DENY_AI_DELETE")


def _set_flag(model, flag, value=True):
    """Set a RestMeta flag on a model class."""
    setattr(model.RestMeta, flag, value)


def _clear_flags(model):
    """Remove every AI flag we might have set."""
    for f in _ALL_FLAGS:
        if hasattr(model.RestMeta, f):
            delattr(model.RestMeta, f)


def _handler(name):
    from mojo.apps.assistant import get_registry
    return get_registry()[name]["handler"]


# ---------------------------------------------------------------------------
# Helper: _check_ai_access direct
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_helper_returns_none_when_no_flags(opts):
    """Default case: no flags set → helper passes through."""
    from mojo.apps.assistant.services.tools.models import _check_ai_access
    from mojo.apps.incident.models import RuleSet
    _clear_flags(RuleSet)

    for verb in ("view", "create", "update", "delete"):
        result = _check_ai_access(RuleSet, verb, opts.admin)
        assert result is None, (
            f"No flags set, verb={verb} should pass, got: {result}"
        )


@th.django_unit_test()
def test_helper_returns_error_for_specific_flag(opts):
    """DENY_AI_VIEW=True → helper blocks view only."""
    from mojo.apps.assistant.services.tools.models import _check_ai_access
    from mojo.apps.incident.models import RuleSet
    _clear_flags(RuleSet)
    _set_flag(RuleSet, "DENY_AI_VIEW")
    try:
        view_result = _check_ai_access(RuleSet, "view", opts.admin)
        assert view_result is not None, "DENY_AI_VIEW should block view"
        assert "error" in view_result, f"Should return error dict: {view_result}"
        assert "not available to the assistant" in view_result["error"], (
            f"Distinct message expected, got: {view_result['error']}"
        )
        for verb in ("create", "update", "delete"):
            r = _check_ai_access(RuleSet, verb, opts.admin)
            assert r is None, (
                f"DENY_AI_VIEW alone should not block {verb}, got: {r}"
            )
    finally:
        _clear_flags(RuleSet)


@th.django_unit_test()
def test_helper_shorthand_blocks_all(opts):
    """DENY_AI=True → blocks every verb."""
    from mojo.apps.assistant.services.tools.models import _check_ai_access
    from mojo.apps.incident.models import RuleSet
    _clear_flags(RuleSet)
    _set_flag(RuleSet, "DENY_AI")
    try:
        for verb in ("view", "create", "update", "delete"):
            r = _check_ai_access(RuleSet, verb, opts.admin)
            assert r is not None, f"DENY_AI shorthand should block {verb}"
            assert "not available to the assistant" in r["error"], (
                f"Distinct message expected for {verb}, got: {r['error']}"
            )
    finally:
        _clear_flags(RuleSet)


# ---------------------------------------------------------------------------
# View gate: describe_model, query_model, aggregate_model, export_data
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_deny_ai_view_blocks_describe_model(opts):
    from mojo.apps.incident.models import RuleSet
    _clear_flags(RuleSet)
    _set_flag(RuleSet, "DENY_AI_VIEW")
    try:
        result = _handler("describe_model")({
            "app_name": "incident", "model_name": "RuleSet",
        }, opts.admin)
        assert "error" in result, "Describe should be blocked"
        assert "not available to the assistant" in result["error"], (
            f"Distinct message: {result['error']}"
        )
    finally:
        _clear_flags(RuleSet)


@th.django_unit_test()
def test_deny_ai_view_blocks_query_model(opts):
    from mojo.apps.incident.models import RuleSet
    _clear_flags(RuleSet)
    _set_flag(RuleSet, "DENY_AI_VIEW")
    try:
        result = _handler("query_model")({
            "app_name": "incident", "model_name": "RuleSet",
        }, opts.admin)
        assert "error" in result, "Query should be blocked"
        assert "not available to the assistant" in result["error"], (
            f"Distinct message: {result['error']}"
        )
    finally:
        _clear_flags(RuleSet)


@th.django_unit_test()
def test_deny_ai_view_blocks_aggregate_model(opts):
    from mojo.apps.incident.models import RuleSet
    _clear_flags(RuleSet)
    _set_flag(RuleSet, "DENY_AI_VIEW")
    try:
        result = _handler("aggregate_model")({
            "app_name": "incident", "model_name": "RuleSet",
            "aggregations": [{"field": "id", "func": "count"}],
        }, opts.admin)
        assert "error" in result, "Aggregate should be blocked"
        assert "not available to the assistant" in result["error"], (
            f"Distinct message: {result['error']}"
        )
    finally:
        _clear_flags(RuleSet)


@th.django_unit_test()
def test_deny_ai_view_blocks_export_data(opts):
    from mojo.apps.incident.models import RuleSet
    _clear_flags(RuleSet)
    _set_flag(RuleSet, "DENY_AI_VIEW")
    try:
        result = _handler("export_data")({
            "app_name": "incident", "model_name": "RuleSet",
        }, opts.admin)
        assert "error" in result, "Export should be blocked"
        assert "not available to the assistant" in result["error"], (
            f"Distinct message: {result['error']}"
        )
    finally:
        _clear_flags(RuleSet)


# ---------------------------------------------------------------------------
# Delete gate
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_deny_ai_delete_blocks_delete_tool(opts):
    """Even with CAN_DELETE=True and full perms, DENY_AI_DELETE wins."""
    from mojo.apps.incident.models import RuleSet
    _clear_flags(RuleSet)
    _set_flag(RuleSet, "DENY_AI_DELETE")
    try:
        result = _handler("delete_model_instance")({
            "app_name": "incident", "model_name": "RuleSet",
            "pk": opts.ruleset.pk,
        }, opts.admin)
        assert "error" in result, "Delete should be blocked"
        assert "not available to the assistant" in result["error"], (
            f"Distinct message: {result['error']}"
        )
        # Row still exists
        assert RuleSet.objects.filter(pk=opts.ruleset.pk).exists(), (
            "Row should not have been deleted"
        )
    finally:
        _clear_flags(RuleSet)


# ---------------------------------------------------------------------------
# Save gate (create + update verbs)
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_deny_ai_create_blocks_save_create_allows_update(opts):
    from mojo.apps.incident.models import RuleSet
    _clear_flags(RuleSet)
    _set_flag(RuleSet, "DENY_AI_CREATE")
    try:
        # Create path (no pk) — blocked
        create_result = _handler("save_model_instance")({
            "app_name": "incident", "model_name": "RuleSet",
            "data": {"name": "aiflags_create_attempt"},
        }, opts.admin)
        assert "error" in create_result, "Create should be blocked"
        assert "not available to the assistant" in create_result["error"], (
            f"Distinct message: {create_result['error']}"
        )
        # Update path on the seeded row — allowed
        update_result = _handler("save_model_instance")({
            "app_name": "incident", "model_name": "RuleSet",
            "pk": opts.ruleset.pk,
            "data": {"description": "updated via ai"},
        }, opts.admin)
        assert update_result.get("ok") is True, (
            f"Update should pass with only DENY_AI_CREATE, got: {update_result}"
        )
    finally:
        _clear_flags(RuleSet)


@th.django_unit_test()
def test_deny_ai_update_blocks_save_update_allows_create(opts):
    from mojo.apps.incident.models import RuleSet
    _clear_flags(RuleSet)
    _set_flag(RuleSet, "DENY_AI_UPDATE")
    try:
        update_result = _handler("save_model_instance")({
            "app_name": "incident", "model_name": "RuleSet",
            "pk": opts.ruleset.pk,
            "data": {"description": "should not update"},
        }, opts.admin)
        assert "error" in update_result, "Update should be blocked"
        assert "not available to the assistant" in update_result["error"], (
            f"Distinct message: {update_result['error']}"
        )
        # Create path still allowed
        create_result = _handler("save_model_instance")({
            "app_name": "incident", "model_name": "RuleSet",
            "data": {"name": "aiflags_create_allowed"},
        }, opts.admin)
        assert create_result.get("ok") is True, (
            f"Create should pass with only DENY_AI_UPDATE, got: {create_result}"
        )
        # Cleanup the created row
        RuleSet.objects.filter(name="aiflags_create_allowed").delete()
    finally:
        _clear_flags(RuleSet)


# ---------------------------------------------------------------------------
# Shorthand — DENY_AI covers every verb
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_deny_ai_shorthand_blocks_all_verbs(opts):
    from mojo.apps.incident.models import RuleSet
    _clear_flags(RuleSet)
    _set_flag(RuleSet, "DENY_AI")
    try:
        # view
        r = _handler("query_model")({
            "app_name": "incident", "model_name": "RuleSet",
        }, opts.admin)
        assert "error" in r and "not available to the assistant" in r["error"], (
            f"DENY_AI should block query_model: {r}"
        )
        # create
        r = _handler("save_model_instance")({
            "app_name": "incident", "model_name": "RuleSet",
            "data": {"name": "aiflags_sh_create"},
        }, opts.admin)
        assert "error" in r and "not available to the assistant" in r["error"], (
            f"DENY_AI should block create: {r}"
        )
        # update
        r = _handler("save_model_instance")({
            "app_name": "incident", "model_name": "RuleSet",
            "pk": opts.ruleset.pk,
            "data": {"description": "sh"},
        }, opts.admin)
        assert "error" in r and "not available to the assistant" in r["error"], (
            f"DENY_AI should block update: {r}"
        )
        # delete
        r = _handler("delete_model_instance")({
            "app_name": "incident", "model_name": "RuleSet",
            "pk": opts.ruleset.pk,
        }, opts.admin)
        assert "error" in r and "not available to the assistant" in r["error"], (
            f"DENY_AI should block delete: {r}"
        )
    finally:
        _clear_flags(RuleSet)


# ---------------------------------------------------------------------------
# Default state (no flags) preserves behavior
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_default_state_allows_query(opts):
    """No flags → query works normally."""
    from mojo.apps.incident.models import RuleSet
    _clear_flags(RuleSet)
    result = _handler("query_model")({
        "app_name": "incident", "model_name": "RuleSet",
        "filters": {"name__startswith": "aiflags_"},
    }, opts.admin)
    assert "error" not in result, (
        f"Default state should allow query, got: {result.get('error')}"
    )
    assert result["count"] >= 1, f"Seeded row should appear: {result}"


# ---------------------------------------------------------------------------
# Security event fired on denial
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_denial_reports_security_event(opts):
    from mojo.apps.incident.models import RuleSet, Event
    _clear_flags(RuleSet)
    _set_flag(RuleSet, "DENY_AI_VIEW")
    try:
        before = Event.objects.filter(category="assistant_ai_denied").count()
        _handler("query_model")({
            "app_name": "incident", "model_name": "RuleSet",
        }, opts.admin)
        after = Event.objects.filter(category="assistant_ai_denied").count()
        assert after > before, (
            f"AI denial should emit event: before={before} after={after}"
        )
        # Level 4 — informational
        event = (
            Event.objects.filter(category="assistant_ai_denied")
            .order_by("-id").first()
        )
        assert event is not None, "Event should exist"
        assert event.level == 4, f"Expected level 4, got {event.level}"
    finally:
        _clear_flags(RuleSet)


# ---------------------------------------------------------------------------
# Distinct-message check: must NOT contain "Permission denied"
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_denial_message_is_not_permission_denied(opts):
    """Users should not chase a perm fix for a policy block."""
    from mojo.apps.incident.models import RuleSet
    _clear_flags(RuleSet)
    _set_flag(RuleSet, "DENY_AI_VIEW")
    try:
        result = _handler("query_model")({
            "app_name": "incident", "model_name": "RuleSet",
        }, opts.admin)
        assert "Permission denied" not in result["error"], (
            f"Denial message should not say 'Permission denied': {result['error']}"
        )
    finally:
        _clear_flags(RuleSet)


# ---------------------------------------------------------------------------
# Ordering: AI gate runs BEFORE permission check
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_ai_gate_fires_before_permission_check(opts):
    """Unprivileged user on a DENY_AI model should get the AI message,
    not the permission-denied message."""
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import RuleSet

    # Temporarily drop admin's perms so REST perm check would fail
    nopriv_email = "aiflags_nopriv@test.com"
    User.objects.filter(email=nopriv_email).delete()
    nopriv = User.objects.create_user(
        username=nopriv_email, email=nopriv_email, password="pass123",
    )
    nopriv.is_email_verified = True
    nopriv.save()
    nopriv.add_permission("view_admin")

    _clear_flags(RuleSet)
    _set_flag(RuleSet, "DENY_AI_VIEW")
    try:
        result = _handler("query_model")({
            "app_name": "incident", "model_name": "RuleSet",
        }, nopriv)
        # Must be the AI message, not the perm message.
        assert "not available to the assistant" in result["error"], (
            f"Ordering broken — got perm message instead: {result['error']}"
        )
    finally:
        _clear_flags(RuleSet)
        User.objects.filter(email=nopriv_email).delete()
