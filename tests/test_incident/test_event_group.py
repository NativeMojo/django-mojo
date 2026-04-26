"""
Group context resolution in the incident reporter.

Asserts the precedence:
  1. Caller-supplied ``group=`` kwarg (including ``None`` to suppress).
  2. ``request.group`` if the caller did not pass ``group``.
  3. None otherwise.

Higher MojoModel layers pre-resolve ``self.group`` and pass it as
``group=``, so the reporter sees a single source of truth.

Also asserts metadata mirroring (group_id / group_name) and the
isinstance guard against non-Group ``.group`` values.
"""
from testit import helpers as th


@th.django_unit_setup()
def setup_event_group(opts):
    from mojo.apps.account.models.group import Group
    from mojo.apps.incident.models import Event

    Group.objects.filter(name__startswith="event-group-").delete()
    g_a = Group.objects.create(name="event-group-A", kind="default")
    g_b = Group.objects.create(name="event-group-B", kind="default")
    opts.group_a_id = g_a.id
    opts.group_b_id = g_b.id

    Event.objects.filter(category__startswith="event_group_test_").delete()


@th.django_unit_test()
def test_request_group_populates_event_fk_and_metadata(opts):
    from mojo.apps.account.models.group import Group
    from mojo.apps.incident import report_event
    from mojo.apps.incident.models import Event

    Event.objects.filter(category="event_group_test_req").delete()
    req = th.get_mock_request(ip="10.0.0.1", path="/test/g")
    req.group = Group.objects.get(pk=opts.group_a_id)
    req.bearer = None

    report_event("from-request", category="event_group_test_req", request=req)

    event = Event.objects.filter(category="event_group_test_req").last()
    assert event is not None, "Expected event to be created"
    assert event.group_id == opts.group_a_id, (
        f"Expected event.group_id={opts.group_a_id} from request.group, got {event.group_id!r}"
    )
    assert event.metadata.get("group_id") == opts.group_a_id, (
        f"Expected metadata.group_id={opts.group_a_id}, got {event.metadata.get('group_id')!r}"
    )
    assert event.metadata.get("group_name") == "event-group-A", (
        f"Expected metadata.group_name='event-group-A', got {event.metadata.get('group_name')!r}"
    )


@th.django_unit_test()
def test_caller_group_kwarg_overrides_request(opts):
    from mojo.apps.account.models.group import Group
    from mojo.apps.incident import report_event
    from mojo.apps.incident.models import Event

    Event.objects.filter(category="event_group_test_caller").delete()
    req = th.get_mock_request(ip="10.0.0.1", path="/test/g")
    req.group = Group.objects.get(pk=opts.group_a_id)
    req.bearer = None
    explicit = Group.objects.get(pk=opts.group_b_id)

    report_event(
        "explicit-wins", category="event_group_test_caller",
        request=req, group=explicit,
    )

    event = Event.objects.filter(category="event_group_test_caller").last()
    assert event.group_id == opts.group_b_id, (
        f"Caller-supplied group must win over request.group: "
        f"expected {opts.group_b_id}, got {event.group_id!r}"
    )


@th.django_unit_test()
def test_caller_group_none_suppresses_request(opts):
    from mojo.apps.account.models.group import Group
    from mojo.apps.incident import report_event
    from mojo.apps.incident.models import Event

    Event.objects.filter(category="event_group_test_suppress").delete()
    req = th.get_mock_request(ip="10.0.0.1", path="/test/g")
    req.group = Group.objects.get(pk=opts.group_a_id)
    req.bearer = None

    report_event(
        "explicit-none", category="event_group_test_suppress",
        request=req, group=None,
    )

    event = Event.objects.filter(category="event_group_test_suppress").last()
    assert event.group_id is None, (
        f"Explicit group=None must suppress request.group; got {event.group_id!r}"
    )
    assert "group_id" not in event.metadata, (
        f"metadata must not carry group_id when no group was resolved: {event.metadata!r}"
    )


@th.django_unit_test()
def test_no_group_fallthrough_leaves_event_clean(opts):
    from mojo.apps.incident import report_event
    from mojo.apps.incident.models import Event

    Event.objects.filter(category="event_group_test_none").delete()
    req = th.get_mock_request(ip="10.0.0.1", path="/test/g")
    req.group = None
    req.bearer = None

    report_event("no-group", category="event_group_test_none", request=req)

    event = Event.objects.filter(category="event_group_test_none").last()
    assert event.group_id is None, (
        f"Expected event.group_id=None with no resolved group, got {event.group_id!r}"
    )
    assert "group_id" not in event.metadata, (
        f"metadata.group_id must be absent: {event.metadata!r}"
    )
    assert "group_name" not in event.metadata, (
        f"metadata.group_name must be absent: {event.metadata!r}"
    )


@th.django_unit_test()
def test_non_group_value_is_ignored_by_isinstance_guard(opts):
    from mojo.apps.incident import report_event
    from mojo.apps.incident.models import Event

    Event.objects.filter(category="event_group_test_isinstance").delete()
    req = th.get_mock_request(ip="10.0.0.1", path="/test/g")
    req.group = "not-a-group"  # something truthy that is not a Group instance
    req.bearer = None

    report_event(
        "non-group-value", category="event_group_test_isinstance",
        request=req,
    )

    event = Event.objects.filter(category="event_group_test_isinstance").last()
    assert event.group_id is None, (
        f"Non-Group .group value must be ignored by isinstance guard, "
        f"got group_id={event.group_id!r}"
    )


@th.django_unit_test()
def test_metadata_snapshot_survives_group_deletion(opts):
    from mojo.apps.account.models.group import Group
    from mojo.apps.incident import report_event
    from mojo.apps.incident.models import Event

    Event.objects.filter(category="event_group_test_delete").delete()

    Group.objects.filter(name="event-group-temp").delete()
    temp = Group.objects.create(name="event-group-temp", kind="default")
    req = th.get_mock_request(ip="10.0.0.1", path="/test/g")
    req.group = temp
    req.bearer = None

    report_event("temp-group", category="event_group_test_delete", request=req)

    event = Event.objects.filter(category="event_group_test_delete").last()
    assert event.group_id == temp.id, "Group should be linked initially"
    assert event.metadata.get("group_name") == "event-group-temp", (
        f"Snapshot must capture group name: {event.metadata!r}"
    )

    # Delete the group — SET_NULL drops the FK but the metadata snapshot stays.
    temp_id = temp.id
    temp.delete()

    event.refresh_from_db()
    assert event.group_id is None, (
        f"After group deletion, event.group_id must be None (SET_NULL); got {event.group_id!r}"
    )
    assert event.metadata.get("group_name") == "event-group-temp", (
        f"Snapshot survives deletion: {event.metadata!r}"
    )
    assert event.metadata.get("group_id") == temp_id, (
        f"Snapshot group_id survives deletion: {event.metadata!r}"
    )
