"""
CAN_*=False feature-disabled exceptions.

When a model turns off an HTTP verb wholesale (CAN_UPDATE / CAN_DELETE /
CAN_CREATE / CAN_BATCH = False), the framework raises
PermissionDeniedException with `event_type="feature_disabled"` and a
distinct `branch` so the dispatcher emits a categorized incident
(rather than a generic permission denial).

These tests run in-process — `setattr(model.RestMeta, ...)` does not
cross into the testit server process, so we exercise the handler
methods directly and assert on the raised exception.
"""
import objict
from testit import helpers as th


_FLAGS = ("CAN_UPDATE", "CAN_SAVE", "CAN_DELETE", "CAN_CREATE", "CAN_BATCH")


def _clear_flags(model):
    for f in _FLAGS:
        if hasattr(model.RestMeta, f):
            delattr(model.RestMeta, f)


def _build_request(user, method="PUT", data=None, path="/api/incident/event/ruleset/1"):
    req = objict.objict()
    req.user = user
    req.DATA = objict.objict(data or {})
    req.QUERY_PARAMS = objict.objict()
    req.method = method
    req.group = None
    req.bearer = None
    req.ip = "127.0.0.1"
    req.path = path
    req.META = {}
    req.api_key = None
    return req


@th.django_unit_setup()
def setup_feature_disabled(opts):
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import RuleSet

    user = User.objects.filter(email="feature_disabled_admin@test.com").last()
    if user is None:
        user = User.objects.create_user(
            username="feature_disabled_admin",
            email="feature_disabled_admin@test.com",
            password="testit##mojo",
        )
    user.is_email_verified = True
    user.save()
    for perm in ["view_security", "manage_security", "security"]:
        user.add_permission(perm)
    opts.admin = user

    RuleSet.objects.filter(name="feature-disabled-fixture").delete()
    rs = RuleSet.objects.create(name="feature-disabled-fixture", category="feature_disabled_cat")
    opts.ruleset = rs


@th.django_unit_test()
def test_can_update_false_raises_feature_disabled(opts):
    from mojo.apps.incident.models import RuleSet
    from mojo.errors import PermissionDeniedException

    _clear_flags(RuleSet)
    setattr(RuleSet.RestMeta, "CAN_UPDATE", False)
    try:
        req = _build_request(opts.admin, data={"description": "blocked"})
        try:
            RuleSet.on_rest_handle_save(req, opts.ruleset)
            assert False, "Expected PermissionDeniedException"
        except PermissionDeniedException as err:
            assert err.status == 403, f"Expected 403, got {err.status}"
            assert err.event_type == "feature_disabled", (
                f"Expected event_type=feature_disabled, got {err.event_type!r}"
            )
            assert err.branch == "can_update_false", (
                f"Expected branch=can_update_false, got {err.branch!r}"
            )
            assert err.model_name == "RuleSet", (
                f"Expected model_name=RuleSet, got {err.model_name!r}"
            )
            assert "UPDATE not allowed" in err.reason, (
                f"Expected 'UPDATE not allowed' in reason, got {err.reason!r}"
            )
    finally:
        _clear_flags(RuleSet)


@th.django_unit_test()
def test_can_delete_false_raises_feature_disabled(opts):
    from mojo.apps.incident.models import RuleSet
    from mojo.errors import PermissionDeniedException

    _clear_flags(RuleSet)
    original = getattr(RuleSet.RestMeta, "CAN_DELETE", None)
    setattr(RuleSet.RestMeta, "CAN_DELETE", False)
    try:
        req = _build_request(opts.admin, method="DELETE")
        try:
            RuleSet.on_rest_handle_delete(req, opts.ruleset)
            assert False, "Expected PermissionDeniedException"
        except PermissionDeniedException as err:
            assert err.status == 403, f"Expected 403, got {err.status}"
            assert err.event_type == "feature_disabled", (
                f"Expected feature_disabled, got {err.event_type!r}"
            )
            assert err.branch == "can_delete_false", (
                f"Expected can_delete_false, got {err.branch!r}"
            )
            assert "DELETE not allowed" in err.reason, (
                f"Expected 'DELETE not allowed', got {err.reason!r}"
            )
    finally:
        _clear_flags(RuleSet)
        if original is None:
            if hasattr(RuleSet.RestMeta, "CAN_DELETE"):
                delattr(RuleSet.RestMeta, "CAN_DELETE")
        else:
            setattr(RuleSet.RestMeta, "CAN_DELETE", original)


@th.django_unit_test()
def test_can_create_false_raises_feature_disabled(opts):
    from mojo.apps.incident.models import RuleSet
    from mojo.errors import PermissionDeniedException

    _clear_flags(RuleSet)
    setattr(RuleSet.RestMeta, "CAN_CREATE", False)
    try:
        req = _build_request(
            opts.admin, method="POST",
            data={"name": "should-not-create", "category": "feature_disabled_cat"},
        )
        try:
            RuleSet.on_rest_handle_create(req)
            assert False, "Expected PermissionDeniedException"
        except PermissionDeniedException as err:
            assert err.status == 403, f"Expected 403, got {err.status}"
            assert err.event_type == "feature_disabled", (
                f"Expected feature_disabled, got {err.event_type!r}"
            )
            assert err.branch == "can_create_false", (
                f"Expected can_create_false, got {err.branch!r}"
            )
    finally:
        _clear_flags(RuleSet)


@th.django_unit_test()
def test_can_batch_false_raises_feature_disabled(opts):
    from mojo.apps.incident.models import RuleSet
    from mojo.errors import PermissionDeniedException

    _clear_flags(RuleSet)
    setattr(RuleSet.RestMeta, "CAN_BATCH", False)
    try:
        req = _build_request(
            opts.admin, method="POST",
            data={"batched": [{"name": "rs1", "category": "feature_disabled_cat"}]},
        )
        try:
            RuleSet.on_rest_handle_batch(req)
            assert False, "Expected PermissionDeniedException"
        except PermissionDeniedException as err:
            assert err.status == 403, f"Expected 403, got {err.status}"
            assert err.event_type == "feature_disabled", (
                f"Expected feature_disabled, got {err.event_type!r}"
            )
            assert err.branch == "can_batch_false", (
                f"Expected can_batch_false, got {err.branch!r}"
            )
    finally:
        _clear_flags(RuleSet)
