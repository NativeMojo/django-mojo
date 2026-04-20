"""Tests for the CAN_UPDATE RestMeta gate in mojo/models/rest.py.

The gate lives in ``on_rest_handle_save`` and blocks updates to existing
instances when ``CAN_UPDATE = False``. ``CAN_SAVE`` is honored as a
deprecated alias for one release.

Tests monkey-patch flags onto ``incident.RuleSet.RestMeta`` using
``setattr`` / ``delattr`` so no test-only models or migrations are
required. Cleanup is done per-test in a ``finally`` block.
"""
import json
from testit import helpers as th


TEST_ADMIN_EMAIL = "canupdate_admin@test.com"

_FLAGS = ("CAN_UPDATE", "CAN_SAVE")


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


def _clear_flags(model):
    for f in _FLAGS:
        if hasattr(model.RestMeta, f):
            delattr(model.RestMeta, f)


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
    _clear_flags(RuleSet)
    setattr(RuleSet.RestMeta, "CAN_UPDATE", False)
    try:
        req = _build_request(opts.admin, data={"description": "should not stick"})
        response = RuleSet.on_rest_handle_save(req, opts.ruleset)
        assert response.status_code == 403, (
            f"Expected 403 with CAN_UPDATE=False, got {response.status_code}"
        )
        body = json.loads(response.content)
        assert "UPDATE not allowed" in body.get("error", ""), (
            f"Expected distinct 'UPDATE not allowed' message, got: {body}"
        )
    finally:
        _clear_flags(RuleSet)


@th.django_unit_test()
def test_can_update_true_allows_update(opts):
    from mojo.apps.incident.models import RuleSet
    _clear_flags(RuleSet)
    setattr(RuleSet.RestMeta, "CAN_UPDATE", True)
    try:
        req = _build_request(opts.admin, data={"description": "updated via gate test"})
        response = RuleSet.on_rest_handle_save(req, opts.ruleset)
        assert response.status_code == 200, (
            f"Expected 200 with CAN_UPDATE=True, got {response.status_code}"
        )
    finally:
        _clear_flags(RuleSet)


@th.django_unit_test()
def test_flag_unset_defaults_to_allowed(opts):
    """No CAN_UPDATE, no CAN_SAVE → default True, update passes."""
    from mojo.apps.incident.models import RuleSet
    _clear_flags(RuleSet)
    try:
        req = _build_request(opts.admin, data={"description": "unset defaults"})
        response = RuleSet.on_rest_handle_save(req, opts.ruleset)
        assert response.status_code == 200, (
            f"Expected 200 with flags unset (default True), got {response.status_code}"
        )
    finally:
        _clear_flags(RuleSet)


# ---------------------------------------------------------------------------
# Deprecation — CAN_SAVE alias
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_can_save_false_still_blocks_update(opts):
    from mojo.apps.incident.models import RuleSet
    _clear_flags(RuleSet)
    _reset_dedup_set()
    setattr(RuleSet.RestMeta, "CAN_SAVE", False)
    try:
        req = _build_request(opts.admin, data={"description": "should not stick"})
        response = RuleSet.on_rest_handle_save(req, opts.ruleset)
        assert response.status_code == 403, (
            f"CAN_SAVE=False must still block (deprecated alias), got {response.status_code}"
        )
    finally:
        _clear_flags(RuleSet)


@th.django_unit_test()
def test_can_save_deprecation_dedupes_per_class(opts):
    """The deprecation warning fires only once per class per process."""
    from mojo.apps.incident.models import RuleSet
    from mojo.models import rest

    _clear_flags(RuleSet)
    _reset_dedup_set()
    setattr(RuleSet.RestMeta, "CAN_SAVE", False)
    try:
        req = _build_request(opts.admin, data={"description": "hit 1"})
        RuleSet.on_rest_handle_save(req, opts.ruleset)
        RuleSet.on_rest_handle_save(req, opts.ruleset)
        RuleSet.on_rest_handle_save(req, opts.ruleset)
        warned = rest._DEPRECATED_CAN_SAVE_WARNED
        assert "RuleSet" in warned, (
            f"RuleSet should be in warned set, got: {warned}"
        )
        # Idempotency is the dedup guarantee — the class appears once.
        assert sum(1 for name in warned if name == "RuleSet") == 1, (
            f"RuleSet should only be recorded once, got set: {warned}"
        )
    finally:
        _clear_flags(RuleSet)


@th.django_unit_test()
def test_can_update_wins_over_can_save(opts):
    """When both flags are set, CAN_UPDATE takes precedence."""
    from mojo.apps.incident.models import RuleSet
    _clear_flags(RuleSet)
    setattr(RuleSet.RestMeta, "CAN_UPDATE", True)
    setattr(RuleSet.RestMeta, "CAN_SAVE", False)
    try:
        req = _build_request(opts.admin, data={"description": "new wins"})
        response = RuleSet.on_rest_handle_save(req, opts.ruleset)
        assert response.status_code == 200, (
            f"CAN_UPDATE=True should override CAN_SAVE=False, got {response.status_code}"
        )
    finally:
        _clear_flags(RuleSet)


# ---------------------------------------------------------------------------
# Create + Delete unaffected
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_can_update_false_does_not_block_create(opts):
    """CAN_UPDATE only gates updates — create path must still work."""
    from mojo.apps.incident.models import RuleSet
    _clear_flags(RuleSet)
    setattr(RuleSet.RestMeta, "CAN_UPDATE", False)
    try:
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
    finally:
        _clear_flags(RuleSet)


@th.django_unit_test()
def test_can_update_false_does_not_affect_delete_gate(opts):
    """Delete remains gated solely by CAN_DELETE, independent of CAN_UPDATE.

    Force CAN_DELETE=False on RuleSet (which normally defaults to True in its
    own RestMeta) so we can observe that the denial cites the DELETE gate,
    not the UPDATE gate — proves they are independent.
    """
    from mojo.apps.incident.models import RuleSet
    _clear_flags(RuleSet)
    setattr(RuleSet.RestMeta, "CAN_UPDATE", False)
    original_can_delete = getattr(RuleSet.RestMeta, "CAN_DELETE", None)
    setattr(RuleSet.RestMeta, "CAN_DELETE", False)
    try:
        req = _build_request(opts.admin, method="DELETE")
        response = RuleSet.on_rest_handle_delete(req, opts.ruleset)
        assert response.status_code == 403, (
            f"CAN_DELETE=False should block delete, got {response.status_code}"
        )
        body = json.loads(response.content)
        error_msg = body.get("error", "")
        assert "DELETE not allowed" in error_msg, (
            f"Error must cite DELETE gate, not UPDATE gate: {error_msg}"
        )
    finally:
        _clear_flags(RuleSet)
        if original_can_delete is None:
            if hasattr(RuleSet.RestMeta, "CAN_DELETE"):
                delattr(RuleSet.RestMeta, "CAN_DELETE")
        else:
            setattr(RuleSet.RestMeta, "CAN_DELETE", original_can_delete)


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
