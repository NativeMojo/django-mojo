"""
Bundle-by-group tests for the new BundleBy.GROUP_* modes.

A RuleSet with no rules and a category match acts as a catch-all
bundler — every event in that category routes through Event.publish()
and bundles per the rule_set's bundle_by mode.
"""
from testit import helpers as th


def _seed_groups(opts):
    from mojo.apps.account.models.group import Group
    Group.objects.filter(name__startswith="bundle-group-").delete()
    g_a = Group.objects.create(name="bundle-group-A", kind="default")
    g_b = Group.objects.create(name="bundle-group-B", kind="default")
    opts.group_a_id = g_a.id
    opts.group_b_id = g_b.id


def _make_ruleset(name, category, bundle_by):
    """Create an always-matching RuleSet for the given category and bundle_by mode."""
    from mojo.apps.incident.models import RuleSet, Rule
    RuleSet.objects.filter(name=name).delete()
    rs = RuleSet.objects.create(
        name=name, category=category, priority=1, match_by=0,
        bundle_by=bundle_by, bundle_minutes=60, bundle_by_rule_set=True,
    )
    # No-op rule: level >= 0 always matches every event.
    Rule.objects.create(
        parent=rs, name="always", field_name="level",
        comparator=">=", value="0", value_type="int",
    )
    return rs


def _publish_event(category, group_id=None, model_name=None, model_id=None, source_ip=None):
    """Create + publish an event the same way the reporter would."""
    from mojo.apps.incident.models import Event
    e = Event(
        category=category, level=2, title=category,
        details=f"event {category}", group_id=group_id,
        model_name=model_name, model_id=model_id, source_ip=source_ip,
    )
    e.sync_metadata()
    e.save()
    e.publish()
    return e


@th.django_unit_setup()
def setup_bundle_by_group(opts):
    from mojo.apps.incident.models import Event, Incident, RuleSet
    _seed_groups(opts)
    RuleSet.objects.filter(category__startswith="bundle_group_").delete()
    Incident.objects.filter(category__startswith="bundle_group_").delete()
    Event.objects.filter(category__startswith="bundle_group_").delete()


@th.django_unit_test()
def test_group_id_bundles_same_group(opts):
    from mojo.apps.incident.models import Event, Incident
    _make_ruleset("bg-rs-1", "bundle_group_id", 10)  # GROUP_ID

    e1 = _publish_event("bundle_group_id", group_id=opts.group_a_id)
    e2 = _publish_event("bundle_group_id", group_id=opts.group_a_id)

    inc_count = Incident.objects.filter(category="bundle_group_id").count()
    assert inc_count == 1, (
        f"Same-group events with BundleBy.GROUP_ID must share one incident, "
        f"got {inc_count} incidents"
    )
    e1.refresh_from_db(); e2.refresh_from_db()
    assert e1.incident_id == e2.incident_id, (
        f"Both events must link to the same incident, got {e1.incident_id} vs {e2.incident_id}"
    )


@th.django_unit_test()
def test_group_id_separates_different_groups(opts):
    from mojo.apps.incident.models import Incident
    _make_ruleset("bg-rs-2", "bundle_group_id_split", 10)  # GROUP_ID

    e_a = _publish_event("bundle_group_id_split", group_id=opts.group_a_id)
    e_b = _publish_event("bundle_group_id_split", group_id=opts.group_b_id)

    inc_count = Incident.objects.filter(category="bundle_group_id_split").count()
    assert inc_count == 2, (
        f"Different-group events with BundleBy.GROUP_ID must split into "
        f"separate incidents, got {inc_count}"
    )
    e_a.refresh_from_db(); e_b.refresh_from_db()
    assert e_a.incident_id != e_b.incident_id, (
        f"Each group must have its own incident; got both at incident_id={e_a.incident_id}"
    )


@th.django_unit_test()
def test_group_and_model_name_and_id_requires_both(opts):
    from mojo.apps.incident.models import Incident
    _make_ruleset("bg-rs-3", "bundle_group_model", 12)  # GROUP_AND_MODEL_NAME_AND_ID

    # Same group, different model_id → separate incidents
    _publish_event("bundle_group_model", group_id=opts.group_a_id, model_name="X", model_id=1)
    _publish_event("bundle_group_model", group_id=opts.group_a_id, model_name="X", model_id=2)
    n1 = Incident.objects.filter(category="bundle_group_model").count()
    assert n1 == 2, (
        f"Different model_id within same group must split, got {n1} incidents"
    )

    # Same group + model_name + model_id → bundles together
    _publish_event("bundle_group_model", group_id=opts.group_a_id, model_name="X", model_id=1)
    n2 = Incident.objects.filter(category="bundle_group_model").count()
    assert n2 == 2, (
        f"Repeating same (group, model_name, model_id) must reuse incident, got {n2}"
    )

    # Different group, same model_name + model_id → splits
    _publish_event("bundle_group_model", group_id=opts.group_b_id, model_name="X", model_id=1)
    n3 = Incident.objects.filter(category="bundle_group_model").count()
    assert n3 == 3, (
        f"Different group with same model must split, got {n3}"
    )


@th.django_unit_test()
def test_group_and_source_ip(opts):
    from mojo.apps.incident.models import Incident
    _make_ruleset("bg-rs-4", "bundle_group_ip", 13)  # GROUP_AND_SOURCE_IP

    _publish_event("bundle_group_ip", group_id=opts.group_a_id, source_ip="1.1.1.1")
    _publish_event("bundle_group_ip", group_id=opts.group_a_id, source_ip="1.1.1.1")
    one = Incident.objects.filter(category="bundle_group_ip").count()
    assert one == 1, f"Same (group, ip) must bundle, got {one}"

    _publish_event("bundle_group_ip", group_id=opts.group_a_id, source_ip="2.2.2.2")
    two = Incident.objects.filter(category="bundle_group_ip").count()
    assert two == 2, f"Same group + new IP must split, got {two}"

    _publish_event("bundle_group_ip", group_id=opts.group_b_id, source_ip="1.1.1.1")
    three = Incident.objects.filter(category="bundle_group_ip").count()
    assert three == 3, f"Different group + same IP must split, got {three}"


@th.django_unit_test()
def test_existing_model_name_and_id_bundling_unchanged(opts):
    """Regression: BundleBy.MODEL_NAME_AND_ID should still bundle the same way."""
    from mojo.apps.incident.models import Incident
    _make_ruleset("bg-rs-5", "bundle_group_regress", 3)  # MODEL_NAME_AND_ID

    _publish_event("bundle_group_regress", model_name="Y", model_id=42)
    _publish_event("bundle_group_regress", model_name="Y", model_id=42)
    one = Incident.objects.filter(category="bundle_group_regress").count()
    assert one == 1, f"MODEL_NAME_AND_ID regression — same model must bundle, got {one}"

    _publish_event("bundle_group_regress", model_name="Y", model_id=43)
    two = Incident.objects.filter(category="bundle_group_regress").count()
    assert two == 2, f"Different model_id must split, got {two}"
