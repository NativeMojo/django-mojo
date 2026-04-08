from testit import helpers as th


@th.django_unit_setup()
def setup(opts):
    from mojo.apps.logit.models import Log
    # clean up from previous runs
    Log.objects.filter(kind__startswith="test:gid").delete()


def _mock_request(group=None):
    from objict import objict
    return objict(
        user=objict(is_authenticated=True, username="testuser", pk=1),
        group=group,
        path="/test",
        duid="test-duid",
        ip="127.0.0.1",
        method="GET",
        user_agent="test-agent"
    )


@th.django_unit_test()
def test_logit_explicit_gid(opts):
    """Log.logit() with explicit gid kwarg stores it"""
    from mojo.apps.logit.models import Log

    entry = Log.logit(None, "explicit gid test", kind="test:gid:explicit", gid=42)
    assert entry.gid == 42, f"Expected gid=42, got gid={entry.gid}"


@th.django_unit_test()
def test_logit_no_gid_defaults_zero(opts):
    """Log.logit() without gid or request.group defaults to 0"""
    from mojo.apps.logit.models import Log

    entry = Log.logit(None, "no gid test", kind="test:gid:none")
    assert entry.gid == 0, f"Expected gid=0, got gid={entry.gid}"


@th.django_unit_test()
def test_logit_request_group(opts):
    """Log.logit() picks up gid from request.group when no explicit gid"""
    from mojo.apps.logit.models import Log
    from objict import objict

    # Use an objict with an id to simulate a group object
    fake_group = objict(id=77)
    mock_req = _mock_request(group=fake_group)

    entry = Log.logit(mock_req, "request group test", kind="test:gid:request_group")
    assert entry.gid == 77, f"Expected gid=77, got gid={entry.gid}"


@th.django_unit_test()
def test_logit_explicit_gid_overrides_request_group(opts):
    """Explicit gid kwarg takes precedence over request.group"""
    from mojo.apps.logit.models import Log
    from objict import objict

    fake_group = objict(id=77)
    mock_req = _mock_request(group=fake_group)

    entry = Log.logit(mock_req, "override test", kind="test:gid:override", gid=999)
    assert entry.gid == 999, f"Expected gid=999, got gid={entry.gid}"


@th.django_unit_test()
def test_logit_request_group_none(opts):
    """Log.logit() with request.group=None defaults gid to 0"""
    from mojo.apps.logit.models import Log

    mock_req = _mock_request(group=None)

    entry = Log.logit(mock_req, "null group test", kind="test:gid:null_group")
    assert entry.gid == 0, f"Expected gid=0, got gid={entry.gid}"


@th.django_unit_test()
def test_logit_request_no_group_attr(opts):
    """Log.logit() with request that has no group attribute defaults gid to 0"""
    from mojo.apps.logit.models import Log
    from objict import objict

    # Build request without group key at all
    mock_req = objict(
        user=objict(is_authenticated=True, username="testuser", pk=1),
        path="/test",
        duid="test-duid",
        ip="127.0.0.1",
        method="GET",
        user_agent="test-agent"
    )

    entry = Log.logit(mock_req, "no group attr test", kind="test:gid:no_attr")
    assert entry.gid == 0, f"Expected gid=0, got gid={entry.gid}"


@th.django_unit_test()
def test_query_logs_by_gid(opts):
    """Can filter Log entries by gid for group audit trail"""
    from mojo.apps.logit.models import Log

    target_gid = 555
    Log.logit(None, "group log 1", kind="test:gid:query", gid=target_gid)
    Log.logit(None, "group log 2", kind="test:gid:query", gid=target_gid)
    Log.logit(None, "other log", kind="test:gid:query", gid=0)

    group_logs = Log.objects.filter(gid=target_gid, kind="test:gid:query")
    assert group_logs.count() == 2, f"Expected 2 logs for gid={target_gid}, got {group_logs.count()}"


@th.django_unit_test()
def test_model_log_with_group_field(opts):
    """MojoModel.log() injects gid from instance with group FK"""
    from mojo.apps.logit.models import Log
    from mojo.apps.incident.models import Ticket
    from mojo.apps.account.models import Group

    # Ticket has a group FK — use it to test gid injection
    group = Group.objects.create(name="gid_test_group_model_log")
    ticket = Ticket(group=group, title="gid test ticket")
    ticket.save()

    entry = ticket.log(log="model log test", kind="test:gid:model_log")
    assert entry.gid == group.id, f"Expected gid={group.id}, got gid={entry.gid}"

    # Cleanup
    ticket.delete()
    group.delete()


