"""
Incident.group inheritance from bundled events.

When the seed event has a group, the new Incident inherits it. When a
later event linking to that incident has a different group, the
incident's group is downgraded to None and `metadata.group_mismatch`
is stamped True (audit-stable — the flag never clears).
"""
from testit import helpers as th


def _make_always_match_ruleset(name, category, bundle_by):
    from mojo.apps.incident.models import RuleSet, Rule
    RuleSet.objects.filter(name=name).delete()
    rs = RuleSet.objects.create(
        name=name, category=category, priority=1, match_by=0,
        bundle_by=bundle_by, bundle_minutes=60, bundle_by_rule_set=True,
    )
    Rule.objects.create(
        parent=rs, name="always", field_name="level",
        comparator=">=", value="0", value_type="int",
    )
    return rs


def _publish(category, group_id=None, model_name="X", model_id=1):
    from mojo.apps.incident.models import Event
    e = Event(
        category=category, level=2, title=category, details=category,
        group_id=group_id, model_name=model_name, model_id=model_id,
    )
    e.sync_metadata()
    e.save()
    e.publish()
    return e


@th.django_unit_setup()
def setup_inherit(opts):
    from mojo.apps.account.models.group import Group
    from mojo.apps.incident.models import Event, Incident, RuleSet

    Group.objects.filter(name__startswith="inherit-group-").delete()
    g_a = Group.objects.create(name="inherit-group-A", kind="default")
    g_b = Group.objects.create(name="inherit-group-B", kind="default")
    opts.group_a_id = g_a.id
    opts.group_b_id = g_b.id

    RuleSet.objects.filter(category__startswith="inherit_test_").delete()
    Incident.objects.filter(category__startswith="inherit_test_").delete()
    Event.objects.filter(category__startswith="inherit_test_").delete()


@th.django_unit_test()
def test_seed_event_with_group_sets_incident_group(opts):
    from mojo.apps.incident.models import Incident
    # MODEL_NAME_AND_ID — bundles by model so we don't gate on group
    _make_always_match_ruleset("inherit-1", "inherit_test_seed", 3)

    _publish("inherit_test_seed", group_id=opts.group_a_id, model_id=1)

    inc = Incident.objects.filter(category="inherit_test_seed").last()
    assert inc is not None, "Incident should be created"
    assert inc.group_id == opts.group_a_id, (
        f"Incident must inherit seed event's group; expected {opts.group_a_id}, "
        f"got {inc.group_id!r}"
    )


@th.django_unit_test()
def test_seed_event_without_group_leaves_incident_null(opts):
    from mojo.apps.incident.models import Incident
    _make_always_match_ruleset("inherit-2", "inherit_test_no_seed", 3)

    _publish("inherit_test_no_seed", group_id=None, model_id=1)

    inc = Incident.objects.filter(category="inherit_test_no_seed").last()
    assert inc is not None, "Incident should be created"
    assert inc.group_id is None, (
        f"Incident.group must be None when seed event has none; got {inc.group_id!r}"
    )


@th.django_unit_test()
def test_same_group_event_keeps_incident_group(opts):
    from mojo.apps.incident.models import Incident
    _make_always_match_ruleset("inherit-3", "inherit_test_same", 3)

    _publish("inherit_test_same", group_id=opts.group_a_id, model_id=1)
    _publish("inherit_test_same", group_id=opts.group_a_id, model_id=1)

    inc = Incident.objects.filter(category="inherit_test_same").last()
    assert inc.group_id == opts.group_a_id, (
        f"Same-group second event must not change incident.group; "
        f"got {inc.group_id!r}"
    )
    assert "group_mismatch" not in (inc.metadata or {}), (
        f"Homogeneous bundle must not flag group_mismatch: {inc.metadata!r}"
    )


@th.django_unit_test()
def test_different_group_event_downgrades_to_null_and_flags(opts):
    from mojo.apps.incident.models import Incident
    _make_always_match_ruleset("inherit-4", "inherit_test_mismatch", 3)

    _publish("inherit_test_mismatch", group_id=opts.group_a_id, model_id=1)
    _publish("inherit_test_mismatch", group_id=opts.group_b_id, model_id=1)

    inc = Incident.objects.filter(category="inherit_test_mismatch").last()
    assert inc.group_id is None, (
        f"Heterogeneous-group bundle must downgrade Incident.group to None; "
        f"got {inc.group_id!r}"
    )
    assert (inc.metadata or {}).get("group_mismatch") is True, (
        f"Expected metadata.group_mismatch=True after heterogeneous link, "
        f"got metadata={inc.metadata!r}"
    )


@th.django_unit_test()
def test_group_mismatch_flag_is_audit_stable(opts):
    """Once group_mismatch is set, a later same-group event must not clear it."""
    from mojo.apps.incident.models import Incident
    _make_always_match_ruleset("inherit-5", "inherit_test_stable", 3)

    _publish("inherit_test_stable", group_id=opts.group_a_id, model_id=1)
    _publish("inherit_test_stable", group_id=opts.group_b_id, model_id=1)
    # Now another A — incident.group_id is None already; flag should stay set.
    _publish("inherit_test_stable", group_id=opts.group_a_id, model_id=1)

    inc = Incident.objects.filter(category="inherit_test_stable").last()
    assert (inc.metadata or {}).get("group_mismatch") is True, (
        f"group_mismatch flag must stay True across subsequent events; "
        f"got metadata={inc.metadata!r}"
    )
    assert inc.group_id is None, (
        f"Incident.group must remain None once heterogeneous mix is recorded; "
        f"got {inc.group_id!r}"
    )
