"""
Tests for incident delete-on-resolution feature.

Covers: check_delete_on_resolution() from REST, BlockHandler, and LLM agent paths,
do_not_delete override, and null rule_set safety.
"""
from testit import helpers as th

CATEGORY = "test_delete_on_res"


@th.django_unit_setup()
def setup_delete_on_resolution(opts):
    from mojo.apps.incident.models import Incident, Event, RuleSet, Rule

    # Clean up from previous runs
    Incident.objects.filter(category__startswith=CATEGORY).delete()
    Event.objects.filter(category__startswith=CATEGORY).delete()
    RuleSet.objects.filter(category__startswith=CATEGORY).delete()

    # RuleSet WITH delete_on_resolution
    opts.ruleset_delete = RuleSet.objects.create(
        name="Test Delete on Resolution",
        category=CATEGORY,
        priority=1,
        match_by=0,
        bundle_by=4,
        bundle_minutes=30,
        metadata={"delete_on_resolution": True},
    )
    Rule.objects.create(
        parent=opts.ruleset_delete,
        name="Match level",
        field_name="level",
        comparator=">=",
        value="1",
        value_type="int",
    )

    # RuleSet WITHOUT delete_on_resolution
    opts.ruleset_keep = RuleSet.objects.create(
        name="Test Keep on Resolution",
        category=f"{CATEGORY}_keep",
        priority=1,
        match_by=0,
        bundle_by=4,
        bundle_minutes=30,
        metadata={},
    )
    Rule.objects.create(
        parent=opts.ruleset_keep,
        name="Match level",
        field_name="level",
        comparator=">=",
        value="1",
        value_type="int",
    )


def _create_incident(ruleset, status="new", metadata=None):
    from mojo.apps.incident.models import Incident
    return Incident.objects.create(
        category=ruleset.category if ruleset else CATEGORY,
        title="Test incident",
        status=status,
        rule_set=ruleset,
        metadata=metadata or {},
    )


@th.django_unit_test()
def test_resolved_deletes_with_flag(opts):
    """Incident with delete_on_resolution ruleset is deleted when resolved."""
    from mojo.apps.incident.models import Incident

    incident = _create_incident(opts.ruleset_delete, status="resolved")
    pk = incident.pk
    result = incident.check_delete_on_resolution()
    assert result is True, "check_delete_on_resolution should return True"
    assert not Incident.objects.filter(pk=pk).exists(), "Incident should be deleted"


@th.django_unit_test()
def test_closed_deletes_with_flag(opts):
    """Incident with delete_on_resolution ruleset is deleted when closed."""
    from mojo.apps.incident.models import Incident

    incident = _create_incident(opts.ruleset_delete, status="closed")
    pk = incident.pk
    result = incident.check_delete_on_resolution()
    assert result is True, "check_delete_on_resolution should return True for closed"
    assert not Incident.objects.filter(pk=pk).exists(), "Incident should be deleted on closed"


@th.django_unit_test()
def test_no_delete_without_flag(opts):
    """Incident with ruleset missing delete_on_resolution is NOT deleted."""
    from mojo.apps.incident.models import Incident

    incident = _create_incident(opts.ruleset_keep, status="resolved")
    pk = incident.pk
    result = incident.check_delete_on_resolution()
    assert result is False, "check_delete_on_resolution should return False without flag"
    assert Incident.objects.filter(pk=pk).exists(), "Incident should still exist"
    incident.delete()


@th.django_unit_test()
def test_do_not_delete_overrides(opts):
    """do_not_delete on incident metadata prevents deletion even with delete_on_resolution."""
    from mojo.apps.incident.models import Incident

    incident = _create_incident(opts.ruleset_delete, status="resolved",
        metadata={"do_not_delete": True})
    pk = incident.pk
    result = incident.check_delete_on_resolution()
    assert result is False, "do_not_delete should override delete_on_resolution"
    assert Incident.objects.filter(pk=pk).exists(), "Incident with do_not_delete should survive"
    incident.delete()


@th.django_unit_test()
def test_null_ruleset_no_crash(opts):
    """Incident with no rule_set does not crash and is not deleted."""
    from mojo.apps.incident.models import Incident

    incident = _create_incident(None, status="resolved")
    pk = incident.pk
    result = incident.check_delete_on_resolution()
    assert result is False, "Null rule_set should return False"
    assert Incident.objects.filter(pk=pk).exists(), "Incident with null rule_set should survive"
    incident.delete()


@th.django_unit_test()
def test_non_terminal_status_no_delete(opts):
    """Incident with status 'new' is not deleted even with delete_on_resolution."""
    from mojo.apps.incident.models import Incident

    incident = _create_incident(opts.ruleset_delete, status="new")
    pk = incident.pk
    result = incident.check_delete_on_resolution()
    assert result is False, "Non-terminal status should not trigger delete"
    assert Incident.objects.filter(pk=pk).exists(), "Active incident should survive"
    incident.delete()


@th.django_unit_test()
def test_on_rest_saved_triggers_delete(opts):
    """on_rest_saved triggers delete when status changes to resolved."""
    from mojo.apps.incident.models import Incident

    incident = _create_incident(opts.ruleset_delete, status="new")
    pk = incident.pk

    # Simulate REST save with status change
    incident.status = "resolved"
    incident.save(update_fields=["status"])
    incident.on_rest_saved({"status": "new"}, created=False)

    assert not Incident.objects.filter(pk=pk).exists(), \
        "on_rest_saved should trigger delete on resolution"


@th.django_unit_test()
def test_on_rest_saved_no_delete_without_flag(opts):
    """on_rest_saved does NOT delete when ruleset lacks delete_on_resolution."""
    from mojo.apps.incident.models import Incident

    incident = _create_incident(opts.ruleset_keep, status="new")
    pk = incident.pk

    incident.status = "resolved"
    incident.save(update_fields=["status"])
    incident.on_rest_saved({"status": "new"}, created=False)

    assert Incident.objects.filter(pk=pk).exists(), \
        "on_rest_saved should not delete without delete_on_resolution flag"
    Incident.objects.filter(pk=pk).delete()


@th.django_unit_test()
def test_llm_tool_update_deletes(opts):
    """_tool_update_incident triggers delete on resolution."""
    from mojo.apps.incident.models import Incident
    from mojo.apps.incident.handlers.llm_agent import _tool_update_incident

    incident = _create_incident(opts.ruleset_delete, status="new")
    pk = incident.pk

    result = _tool_update_incident({
        "incident_id": pk,
        "status": "resolved",
        "note": "Noise pattern, auto-resolved",
    })

    assert result.get("deleted") is True, f"Expected deleted=True in result, got {result}"
    assert not Incident.objects.filter(pk=pk).exists(), \
        "LLM update_incident should trigger delete on resolution"


@th.django_unit_test()
def test_llm_do_not_delete_prevents_deletion(opts):
    """_tool_update_incident with do_not_delete=True prevents deletion."""
    from mojo.apps.incident.models import Incident
    from mojo.apps.incident.handlers.llm_agent import _tool_update_incident

    incident = _create_incident(opts.ruleset_delete, status="new")
    pk = incident.pk

    result = _tool_update_incident({
        "incident_id": pk,
        "status": "resolved",
        "note": "Real threat, preserving",
        "do_not_delete": True,
    })

    assert result.get("deleted") is None or result.get("deleted") is not True, \
        f"do_not_delete should prevent deletion, got {result}"
    assert Incident.objects.filter(pk=pk).exists(), \
        "Incident with do_not_delete should survive LLM resolution"
    inc = Incident.objects.get(pk=pk)
    assert inc.metadata.get("do_not_delete") is True, \
        "do_not_delete should be stored in metadata"
    inc.delete()


@th.django_unit_test()
def test_llm_create_rule_with_delete_on_resolution(opts):
    """_tool_create_rule stores delete_on_resolution in metadata."""
    from mojo.apps.incident.models import RuleSet
    from mojo.apps.incident.handlers.llm_agent import _tool_create_rule

    # Clean up any previous test rule
    RuleSet.objects.filter(name="Test LLM Noise Rule").delete()

    result = _tool_create_rule({
        "name": "Test LLM Noise Rule",
        "category": f"{CATEGORY}_llm",
        "handler": "block://?ttl=600",
        "reasoning": "Test noise pattern",
        "delete_on_resolution": True,
        "bundle_by": 4,
        "bundle_minutes": 30,
    })

    assert result.get("ok") is True, f"create_rule should succeed, got {result}"
    ruleset = RuleSet.objects.get(pk=result["ruleset_id"])
    assert ruleset.metadata.get("delete_on_resolution") is True, \
        f"delete_on_resolution should be in metadata, got {ruleset.metadata}"
    ruleset.delete()


@th.django_unit_test()
def test_cascade_deletes_events_and_history(opts):
    """When incident is deleted, its events and history are cascade-deleted."""
    from mojo.apps.incident.models import Incident, Event, IncidentHistory

    incident = _create_incident(opts.ruleset_delete, status="resolved")
    pk = incident.pk

    # Create an event linked to this incident
    event = Event.objects.create(
        category=CATEGORY,
        level=5,
        title="Test event",
        incident=incident,
    )
    event_pk = event.pk

    # Create a history entry
    incident.add_history("test", note="Test history entry")
    history_count = IncidentHistory.objects.filter(parent=incident).count()
    assert history_count >= 1, "Should have at least one history entry"

    # Delete via check_delete_on_resolution
    incident.check_delete_on_resolution()

    assert not Incident.objects.filter(pk=pk).exists(), "Incident should be deleted"
    assert not Event.objects.filter(pk=event_pk).exists(), "Events should be cascade-deleted"
    assert not IncidentHistory.objects.filter(parent_id=pk).exists(), \
        "History should be cascade-deleted"
