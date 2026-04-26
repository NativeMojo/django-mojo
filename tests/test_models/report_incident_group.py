"""
MojoModel.report_incident / class_report_incident / class_report_incident_for_user
auto-stamp `group` so callers don't have to thread it through manually.

Precedence:
  1. Caller-supplied `group=` kwarg (including `None` to suppress).
  2. `self.group` for instance-level `report_incident`.
  3. `request.group` for class-level helpers.
"""
import objict
from testit import helpers as th


def _make_request_with_group(group):
    req = th.get_mock_request(ip="127.0.0.1", path="/test/g")
    req.group = group
    req.bearer = None
    return req


@th.django_unit_setup()
def setup_report_group(opts):
    from mojo.apps.account.models.group import Group
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.incident.models.event import Event

    Group.objects.filter(name__startswith="rep-group-").delete()
    g_a = Group.objects.create(name="rep-group-A", kind="default")
    g_b = Group.objects.create(name="rep-group-B", kind="default")
    opts.group_a_id = g_a.id
    opts.group_b_id = g_b.id

    Setting.objects.filter(key__startswith="rep-group-test-").delete()
    Event.objects.filter(category__startswith="rep_group_test_").delete()


@th.django_unit_test()
def test_instance_report_incident_uses_self_group(opts):
    """Setting has a .group FK; instance auto-stamps from it."""
    from mojo.apps.account.models.group import Group
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.incident.models.event import Event

    Event.objects.filter(category="rep_group_test_instance").delete()
    g = Group.objects.get(pk=opts.group_a_id)
    s = Setting.objects.create(key="rep-group-test-instance", value="v", group=g)

    s.report_incident("instance reports", event_type="rep_group_test_instance", level=1)

    ev = Event.objects.filter(category="rep_group_test_instance").last()
    assert ev is not None, "Event should be reported"
    assert ev.group_id == opts.group_a_id, (
        f"Event.group must come from self.group; expected {opts.group_a_id}, "
        f"got {ev.group_id!r}"
    )


@th.django_unit_test()
def test_instance_report_incident_no_group_attr_falls_through(opts):
    """A Group instance has no `.group` attribute itself — auto-stamp must skip
    silently, then fall through to request.group via class_report_incident."""
    from mojo.apps.account.models.group import Group
    from mojo.apps.incident.models.event import Event
    from mojo.models import rest as rest_module

    Event.objects.filter(category="rep_group_test_no_attr").delete()
    g_main = Group.objects.get(pk=opts.group_a_id)
    g_request = Group.objects.get(pk=opts.group_b_id)

    # Inject a request via ACTIVE_REQUEST so class_report_incident sees it.
    req = _make_request_with_group(g_request)
    token = rest_module.ACTIVE_REQUEST.set(req)
    try:
        g_main.report_incident("group has no .group attr",
                               event_type="rep_group_test_no_attr", level=1)
    finally:
        rest_module.ACTIVE_REQUEST.reset(token)

    ev = Event.objects.filter(category="rep_group_test_no_attr").last()
    assert ev is not None, "Event should be reported"
    # Group has no .group attribute (it has .parent), so the instance stamp is
    # skipped and the request.group fallback in class_report_incident wins.
    assert ev.group_id == opts.group_b_id, (
        f"Expected fallback to request.group ({opts.group_b_id}), got {ev.group_id!r}"
    )


@th.django_unit_test()
def test_class_report_incident_for_user_uses_request_group(opts):
    from mojo.apps.account.models.group import Group
    from mojo.apps.account.models.user import User
    from mojo.apps.incident.models.event import Event

    Event.objects.filter(category="rep_group_test_user").delete()
    g = Group.objects.get(pk=opts.group_a_id)

    # Use a mock unauthenticated request so class_report_incident_for_user
    # falls through to class_report_incident — that path is the one we
    # most care about for the auto-stamp behavior, and avoids the parallel
    # User-creation flake when running with other test packages.
    req = _make_request_with_group(g)
    req.user = objict.objict()
    req.user.is_authenticated = False

    User.class_report_incident_for_user(
        "from request", event_type="rep_group_test_user",
        level=1, request=req,
    )

    ev = Event.objects.filter(category="rep_group_test_user").last()
    assert ev is not None, "Event should be reported"
    assert ev.group_id == opts.group_a_id, (
        f"class_report_incident_for_user must auto-stamp request.group; "
        f"expected {opts.group_a_id}, got {ev.group_id!r}"
    )


@th.django_unit_test()
def test_explicit_none_suppresses_auto_stamp(opts):
    from mojo.apps.account.models.group import Group
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.incident.models.event import Event

    Event.objects.filter(category="rep_group_test_none").delete()
    g = Group.objects.get(pk=opts.group_a_id)
    s = Setting.objects.create(key="rep-group-test-none", value="v", group=g)

    s.report_incident("suppressed", event_type="rep_group_test_none",
                      level=1, group=None)

    ev = Event.objects.filter(category="rep_group_test_none").last()
    assert ev.group_id is None, (
        f"Explicit group=None must suppress auto-stamp; got {ev.group_id!r}"
    )


@th.django_unit_test()
def test_class_report_incident_with_request(opts):
    """class_report_incident (no user) honors request.group when request is set."""
    from mojo.apps.account.models.group import Group
    from mojo.apps.incident.models.event import Event

    Event.objects.filter(category="rep_group_test_class").delete()
    g = Group.objects.get(pk=opts.group_a_id)
    req = _make_request_with_group(g)
    # Make the request unauthenticated so class_report_incident_for_user
    # falls through to class_report_incident.
    req.user = objict.objict()
    req.user.is_authenticated = False

    Group.class_report_incident(
        "from class+request", event_type="rep_group_test_class",
        level=1, request=req,
    )

    ev = Event.objects.filter(category="rep_group_test_class").last()
    assert ev is not None, "Event should be reported"
    assert ev.group_id == opts.group_a_id, (
        f"class_report_incident must auto-stamp request.group; "
        f"expected {opts.group_a_id}, got {ev.group_id!r}"
    )
