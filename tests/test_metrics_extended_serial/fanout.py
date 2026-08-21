"""Fan-out cap test moved from tests/test_metrics/fanout.py — it mutates
django.conf.settings (METRICS_FANOUT_MAX_CHILDREN) in-process, which is
unsafe under the parallel default tier (maestro item #1839). Runs opt-in
(`extended`) and serial.
"""
from testit import helpers as th


def _build_tree(parent_kind="org", child_kind="location"):
    """Build a parent group with three active children of one kind plus one
    inactive child and one mismatched-kind child for filter coverage. Returns
    (parent, matches, kiosk, inactive)."""
    from mojo.apps.account.models import Group

    Group.objects.filter(name__startswith="fanout_test_").delete()

    parent = Group(name="fanout_test_parent", kind=parent_kind, is_active=True)
    parent.save()

    matches = []
    for i in range(3):
        g = Group(name=f"fanout_test_child_{i}", kind=child_kind, parent=parent, is_active=True)
        g.save()
        matches.append(g)

    other = Group(name="fanout_test_kiosk", kind="kiosk", parent=parent, is_active=True)
    other.save()

    inactive = Group(name="fanout_test_inactive", kind=child_kind, parent=parent, is_active=False)
    inactive.save()

    return parent, matches, other, inactive


def _seed(slug, group, count):
    """Record ``count`` events at the current time into the group's metric
    account. Default-time recording lines up with default-range fetch — both
    apply METRICS_TIMEZONE normalization on `now`."""
    from mojo.apps import metrics
    account = f"group-{group.pk}"
    metrics.delete_metrics_slug(slug, account=account)
    for _ in range(count):
        metrics.record(slug, account=account, min_granularity="hours")


@th.django_unit_test()
def test_fanout_cap_exceeded_in_process(opts):
    """Direct in-process call must enforce the cap when settings are patched
    via the django settings system. Uses ``settings.get_static`` lookup, which
    reads live."""
    from mojo.apps.metrics.rest.helpers import fetch_group_fanout
    import mojo.errors

    parent, matches, _, _ = _build_tree()
    for g in matches:
        _seed("fan_cap", g, 1)

    from django.conf import settings as dj_settings
    original = getattr(dj_settings, "METRICS_FANOUT_MAX_CHILDREN", None)
    dj_settings.METRICS_FANOUT_MAX_CHILDREN = 2
    try:
        raised = False
        try:
            fetch_group_fanout(
                parent.pk, "location", ["fan_cap"],
                granularity="hours", with_labels=True,
            )
        except mojo.errors.ValueException as e:
            raised = True
            assert "METRICS_FANOUT_MAX_CHILDREN" in str(e.reason), \
                f"Cap error must reference setting name, got: {e.reason}"
        assert raised, "fetch_group_fanout should have raised ValueException for cap exceeded"
    finally:
        if original is None:
            del dj_settings.METRICS_FANOUT_MAX_CHILDREN
        else:
            dj_settings.METRICS_FANOUT_MAX_CHILDREN = original
