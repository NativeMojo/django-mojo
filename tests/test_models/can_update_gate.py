"""Tests for the CAN_UPDATE RestMeta gate in mojo/models/rest.py.

The gate lives in ``on_rest_handle_save`` and blocks updates to existing
instances when ``CAN_UPDATE = False``. ``CAN_SAVE`` is honored as a
deprecated alias for one release.

Most tests here need a real persisted row (and the assistant tool resolves its
target by app/model name), so they use ``incident.RuleSet`` and toggle flags via
``flag_override``, which restores the exact prior attribute state — never a
blanket delete that could strip a flag the model declares for itself.

The delete-gate test deliberately uses ``FlagProbe`` instead: ``CAN_DELETE`` is
read by the assistant's delete tool in ``tests/test_assistant``, and because
RestMeta flags are process-global while testit runs modules as parallel threads,
flipping it on a real model fails that module. See ``_flag_probe.py``.
"""
import json
from testit import helpers as th

from ._flag_probe import REMOVE, FlagProbe, flag_override


TEST_ADMIN_EMAIL = "canupdate_admin@test.com"


@th.django_unit_setup()
def setup_can_update_gate(opts):
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import RuleSet

    User.objects.filter(email=TEST_ADMIN_EMAIL).delete()
    opts.admin = User.objects.create_user(
        username=TEST_ADMIN_EMAIL, email=TEST_ADMIN_EMAIL, password="pass123",
    )
    opts.admin.is_email_verified = True
    opts.admin.save()
    for perm in ["view_admin", "view_security", "manage_security", "security"]:
        opts.admin.add_permission(perm)

    RuleSet.objects.filter(name__startswith="canupdate_").delete()
    opts.ruleset = RuleSet.objects.create(
        name="canupdate_seed", category="canupdate_cat",
    )


def _reset_dedup_set():
    """Reset the once-per-process deprecation warning set so each test
    starts clean and can observe the warning firing."""
    from mojo.models import rest
    rest._DEPRECATED_CAN_SAVE_WARNED.clear()


def _build_request(user, method="PUT", data=None):
    """Synthetic request that satisfies on_rest_handle_save's dependencies."""
    import objict
    req = objict.objict()
    req.user = user
    req.DATA = objict.objict(data or {})
    req.QUERY_PARAMS = objict.objict()
    req.method = method
    req.group = None
    req.bearer = None
    req.ip = "127.0.0.1"
    req.path = "/api/test/ruleset/1"
    req.META = {}
    req.api_key = None
    return req


# ---------------------------------------------------------------------------
# Gate behavior — explicit CAN_UPDATE flag
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_can_update_false_blocks_update(opts):
    from mojo.apps.incident.models import RuleSet
    from mojo.errors import PermissionDeniedException
    with flag_override(RuleSet, CAN_UPDATE=False, CAN_SAVE=REMOVE):
        req = _build_request(opts.admin, data={"description": "should not stick"})
        try:
            RuleSet.on_rest_handle_save(req, opts.ruleset)
            assert False, "Expected PermissionDeniedException with CAN_UPDATE=False"
        except PermissionDeniedException as err:
            assert err.status == 403, f"Expected status 403, got {err.status}"
            assert "UPDATE not allowed" in err.reason, (
                f"Expected 'UPDATE not allowed' in reason, got: {err.reason}"
            )
            assert err.event_type == "feature_disabled", (
                f"Expected event_type=feature_disabled, got {err.event_type}"
            )


@th.django_unit_test()
def test_can_update_true_allows_update(opts):
    from mojo.apps.incident.models import RuleSet
    with flag_override(RuleSet, CAN_UPDATE=True, CAN_SAVE=REMOVE):
        req = _build_request(opts.admin, data={"description": "updated via gate test"})
        response = RuleSet.on_rest_handle_save(req, opts.ruleset)
        assert response.status_code == 200, (
            f"Expected 200 with CAN_UPDATE=True, got {response.status_code}"
        )


@th.django_unit_test()
def test_flag_unset_defaults_to_allowed(opts):
    """No CAN_UPDATE, no CAN_SAVE → default True, update passes."""
    from mojo.apps.incident.models import RuleSet
    with flag_override(RuleSet, CAN_UPDATE=REMOVE, CAN_SAVE=REMOVE):
        req = _build_request(opts.admin, data={"description": "unset defaults"})
        response = RuleSet.on_rest_handle_save(req, opts.ruleset)
        assert response.status_code == 200, (
            f"Expected 200 with flags unset (default True), got {response.status_code}"
        )


# ---------------------------------------------------------------------------
# Deprecation — CAN_SAVE alias
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_can_save_false_still_blocks_update(opts):
    from mojo.apps.incident.models import RuleSet
    from mojo.errors import PermissionDeniedException
    _reset_dedup_set()
    with flag_override(RuleSet, CAN_UPDATE=REMOVE, CAN_SAVE=False):
        req = _build_request(opts.admin, data={"description": "should not stick"})
        raised = False
        try:
            RuleSet.on_rest_handle_save(req, opts.ruleset)
        except PermissionDeniedException as err:
            raised = True
            assert err.status == 403, f"Expected 403, got {err.status}"
        assert raised, "CAN_SAVE=False must still raise PermissionDeniedException (deprecated alias)"


@th.django_unit_test()
def test_can_save_deprecation_dedupes_per_class(opts):
    """The deprecation warning fires only once per class per process."""
    from mojo.apps.incident.models import RuleSet
    from mojo.models import rest

    from mojo.errors import PermissionDeniedException
    _reset_dedup_set()
    with flag_override(RuleSet, CAN_UPDATE=REMOVE, CAN_SAVE=False):
        req = _build_request(opts.admin, data={"description": "hit 1"})
        for _ in range(3):
            try:
                RuleSet.on_rest_handle_save(req, opts.ruleset)
            except PermissionDeniedException:
                pass
        warned = rest._DEPRECATED_CAN_SAVE_WARNED
        assert "RuleSet" in warned, (
            f"RuleSet should be in warned set, got: {warned}"
        )
        # Idempotency is the dedup guarantee — the class appears once.
        assert sum(1 for name in warned if name == "RuleSet") == 1, (
            f"RuleSet should only be recorded once, got set: {warned}"
        )


@th.django_unit_test()
def test_can_update_wins_over_can_save(opts):
    """When both flags are set, CAN_UPDATE takes precedence."""
    from mojo.apps.incident.models import RuleSet
    with flag_override(RuleSet, CAN_UPDATE=True, CAN_SAVE=False):
        req = _build_request(opts.admin, data={"description": "new wins"})
        response = RuleSet.on_rest_handle_save(req, opts.ruleset)
        assert response.status_code == 200, (
            f"CAN_UPDATE=True should override CAN_SAVE=False, got {response.status_code}"
        )


# ---------------------------------------------------------------------------
# Create + Delete unaffected
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_can_update_false_does_not_block_create(opts):
    """CAN_UPDATE only gates updates — create path must still work."""
    from mojo.apps.incident.models import RuleSet
    with flag_override(RuleSet, CAN_UPDATE=False, CAN_SAVE=REMOVE):
        req = _build_request(
            opts.admin, method="POST",
            data={"name": "canupdate_new", "category": "canupdate_cat"},
        )
        response = RuleSet.on_rest_handle_create(req)
        assert response.status_code != 403, (
            f"CAN_UPDATE=False must not block create, got 403"
        )
        # Cleanup the row we just created
        RuleSet.objects.filter(name="canupdate_new").delete()


@th.django_unit_test()
def test_can_update_false_does_not_affect_delete_gate(opts):
    """Delete remains gated solely by CAN_DELETE, independent of CAN_UPDATE.

    With both flags False, the denial must cite the DELETE gate, not the UPDATE
    gate — proving they are independent.

    Uses FlagProbe, NOT a real model: CAN_DELETE is read by the assistant's
    delete tool in tests/test_assistant, and RestMeta flags are process-global
    while testit runs modules as parallel threads. Flipping CAN_DELETE on a
    shared model fails that module (see _flag_probe.py).
    """
    from mojo.errors import PermissionDeniedException
    with flag_override(FlagProbe, CAN_UPDATE=False, CAN_DELETE=False):
        req = _build_request(opts.admin, method="DELETE")
        try:
            FlagProbe.on_rest_handle_delete(req, FlagProbe())
            assert False, "Expected PermissionDeniedException with CAN_DELETE=False"
        except PermissionDeniedException as err:
            assert err.status == 403, f"Expected 403, got {err.status}"
            assert "DELETE not allowed" in err.reason, (
                f"Error must cite DELETE gate, not UPDATE gate: {err.reason}"
            )
            assert err.event_type == "feature_disabled", (
                f"Expected feature_disabled, got {err.event_type}"
            )


# ---------------------------------------------------------------------------
# Real model migration — LoginEvent + Click
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_login_event_update_is_blocked(opts):
    """UserLoginEvent carries CAN_UPDATE=False after migration."""
    from mojo.apps.account.models.login_event import UserLoginEvent
    assert UserLoginEvent.get_rest_meta_prop("CAN_UPDATE", None) is False, (
        "UserLoginEvent must declare CAN_UPDATE=False"
    )


@th.django_unit_test()
def test_shortlink_click_update_is_blocked(opts):
    """ShortLinkClick carries CAN_UPDATE=False after migration."""
    from mojo.apps.shortlink.models.click import ShortLinkClick
    assert ShortLinkClick.get_rest_meta_prop("CAN_UPDATE", None) is False, (
        "ShortLinkClick must declare CAN_UPDATE=False"
    )


# ---------------------------------------------------------------------------
# Assistant save_model_instance — must enforce the same CAN_UPDATE gate
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_assistant_save_respects_can_update_false(opts):
    """Assistant `save_model_instance` update path must honor CAN_UPDATE=False.

    Regression guard for the bypass caught in security review of 709e08f:
    the assistant tool calls `instance.on_rest_save` directly, bypassing
    `on_rest_handle_save`. The gate must be re-enforced at the tool layer.
    """
    from mojo.apps.assistant.services.tools.models import _tool_save_model_instance
    from mojo.apps.incident.models import RuleSet
    with flag_override(RuleSet, CAN_UPDATE=False, CAN_SAVE=REMOVE):
        result = _tool_save_model_instance({
            "app_name": "incident", "model_name": "RuleSet",
            "pk": opts.ruleset.pk,
            "data": {"description": "assistant should not update this"},
        }, opts.admin)
        assert "error" in result, (
            f"Assistant update must be blocked when CAN_UPDATE=False, got: {result}"
        )
        assert "not allowed" in result["error"].lower(), (
            f"Error must cite the gate, not a perm failure: {result['error']}"
        )


@th.django_unit_test()
def test_assistant_save_respects_can_save_alias_false(opts):
    """Assistant update path honors the deprecated CAN_SAVE=False alias too."""
    from mojo.apps.assistant.services.tools.models import _tool_save_model_instance
    from mojo.apps.incident.models import RuleSet
    with flag_override(RuleSet, CAN_UPDATE=REMOVE, CAN_SAVE=False):
        result = _tool_save_model_instance({
            "app_name": "incident", "model_name": "RuleSet",
            "pk": opts.ruleset.pk,
            "data": {"description": "alias should still block"},
        }, opts.admin)
        assert "error" in result, (
            f"Assistant update must be blocked when CAN_SAVE=False (alias), got: {result}"
        )


@th.django_unit_test()
def test_assistant_save_create_unaffected_by_can_update(opts):
    """CAN_UPDATE=False must not block the create path in the assistant tool."""
    from mojo.apps.assistant.services.tools.models import _tool_save_model_instance
    from mojo.apps.incident.models import RuleSet
    # Make sure no leftover row with this name exists
    RuleSet.objects.filter(name="canupdate_assistant_create").delete()
    with flag_override(RuleSet, CAN_UPDATE=False, CAN_SAVE=REMOVE):
        result = _tool_save_model_instance({
            "app_name": "incident", "model_name": "RuleSet",
            "data": {"name": "canupdate_assistant_create", "category": "canupdate_cat"},
        }, opts.admin)
        assert result.get("ok") is True, (
            f"Create must not be blocked by CAN_UPDATE=False, got: {result}"
        )
        RuleSet.objects.filter(name="canupdate_assistant_create").delete()
