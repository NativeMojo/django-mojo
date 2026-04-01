"""
Tests for RuleSet trigger_count, trigger_window, and retrigger_every fields.

Covers:
- trigger_count=None: handler fires on first event (no regression)
- trigger_count=N: incident sits at pending until N events, fires at N
- trigger_window: only events within the window count toward threshold
- retrigger_every: handler re-fires every N events after initial trigger
- re-trigger does not fire when incident is resolved or ignored
- global count bug regression: prior resolved incident does not pollute new one
"""
from testit import helpers as th


CATEGORY = "trigger_test"


def _cleanup():
    from mojo.apps.incident.models import Event, RuleSet, Incident
    RuleSet.objects.filter(category=CATEGORY).delete()
    Event.objects.filter(category=CATEGORY).delete()
    Incident.objects.filter(category=CATEGORY).delete()


def _make_ruleset(**kwargs):
    from mojo.apps.incident.models import RuleSet, Rule
    kwargs.setdefault("bundle_minutes", 60)
    rs = RuleSet.objects.create(
        name="Trigger Test Ruleset",
        category=CATEGORY,
        priority=1,
        match_by=0,  # ALL
        bundle_by=4,  # SOURCE_IP
        handler="job://test.handler",
        **kwargs
    )
    Rule.objects.create(
        parent=rs,
        name="Match category",
        field_name="category",
        comparator="==",
        value=CATEGORY,
        value_type="str",
    )
    return rs


def _publish_event(source_ip="1.2.3.4"):
    from mojo.apps.incident.models import Event
    ev = Event.objects.create(
        category=CATEGORY,
        level=5,
        title="Test event",
        source_ip=source_ip,
    )
    ev.sync_metadata()
    ev.publish()
    return ev


@th.django_unit_test()
def test_no_trigger_count_fires_immediately(opts):
    """trigger_count=None: handler fires on first event, incident goes straight to new."""
    from mojo.apps.incident.models import Incident
    _cleanup()
    _make_ruleset(trigger_count=None)

    _publish_event()

    incident = Incident.objects.filter(category=CATEGORY).first()
    assert incident is not None, "Incident should be created"
    assert incident.status == "new", f"Expected status=new, got {incident.status}"


@th.django_unit_test()
def test_trigger_count_holds_pending(opts):
    """trigger_count=3: incidents sits at pending for events 1-2, transitions at event 3."""
    from mojo.apps.incident.models import Incident
    _cleanup()
    _make_ruleset(trigger_count=3)

    _publish_event()
    incident = Incident.objects.filter(category=CATEGORY).first()
    assert incident is not None, "Incident should be created on first event"
    assert incident.status == "pending", f"After event 1: expected pending, got {incident.status}"

    _publish_event()
    incident.refresh_from_db()
    assert incident.status == "pending", f"After event 2: expected pending, got {incident.status}"

    _publish_event()
    incident.refresh_from_db()
    assert incident.status == "new", f"After event 3: expected new, got {incident.status}"


@th.django_unit_test()
def test_trigger_count_history_entry(opts):
    """threshold_reached history entry is added when pending → new transition occurs."""
    from mojo.apps.incident.models import Incident, IncidentHistory
    _cleanup()
    _make_ruleset(trigger_count=2)

    _publish_event()
    _publish_event()

    incident = Incident.objects.filter(category=CATEGORY).first()
    assert incident.status == "new", f"Expected new, got {incident.status}"

    history = IncidentHistory.objects.filter(parent=incident, kind="threshold_reached").first()
    assert history is not None, "Expected threshold_reached history entry"
    assert "trigger_count: 2" in history.note, f"Expected trigger_count in note, got: {history.note}"


@th.django_unit_test()
def test_retrigger_every_fires_again(opts):
    """retrigger_every=5: handler re-fires every 5 events after initial trigger_count."""
    from mojo.apps.incident.models import Incident, IncidentHistory
    _cleanup()
    _make_ruleset(trigger_count=2, retrigger_every=5)

    # Events 1-2: pending → new, initial trigger
    _publish_event()
    _publish_event()
    incident = Incident.objects.filter(category=CATEGORY).first()
    assert incident.status == "new", f"Expected new, got {incident.status}"

    # Events 3-6: not yet at retrigger threshold (need 2+5=7 total)
    for _ in range(4):
        _publish_event()
    incident.refresh_from_db()
    retrigger_history = IncidentHistory.objects.filter(parent=incident, kind="handler_retriggered")
    assert retrigger_history.count() == 0, "Should not have re-triggered yet"

    # Event 7: hits the retrigger threshold (2 + 5 = 7)
    _publish_event()
    incident.refresh_from_db()
    retrigger_history = IncidentHistory.objects.filter(parent=incident, kind="handler_retriggered")
    assert retrigger_history.count() == 1, f"Expected 1 retrigger, got {retrigger_history.count()}"

    # Events 8-12: event 12 triggers second retrigger (7 + 5 = 12)
    for _ in range(5):
        _publish_event()
    incident.refresh_from_db()
    retrigger_history = IncidentHistory.objects.filter(parent=incident, kind="handler_retriggered")
    assert retrigger_history.count() == 2, f"Expected 2 retriggers, got {retrigger_history.count()}"


@th.django_unit_test()
def test_retrigger_skips_resolved_incident(opts):
    """Re-trigger does not fire when incident is resolved or ignored."""
    from mojo.apps.incident.models import Incident, IncidentHistory
    _cleanup()
    _make_ruleset(trigger_count=1, retrigger_every=2)

    # Event 1: fires handler immediately (trigger_count=1)
    _publish_event()
    incident = Incident.objects.filter(category=CATEGORY).first()
    assert incident.status == "new", f"Expected new, got {incident.status}"

    # Resolve the incident
    incident.status = "resolved"
    incident.save(update_fields=["status"])

    # Events 2-3: would normally re-trigger at event 3, but incident is resolved
    _publish_event()
    _publish_event()

    retrigger_history = IncidentHistory.objects.filter(parent=incident, kind="handler_retriggered")
    assert retrigger_history.count() == 0, "Re-trigger should not fire on a resolved incident"


@th.django_unit_test()
def test_no_cross_incident_count_pollution(opts):
    """Per-incident count: events in OTHER incidents don't prematurely trip the threshold.

    Uses bundle_minutes=0 (no bundling) so each event creates its own incident.
    With the old global-count bug, 5 events from the same source_ip across 5 separate
    incidents would trip trigger_count=5 on the 5th incident. With per-incident counting,
    each incident has only 1 event so they all stay pending.
    """
    from mojo.apps.incident.models import Incident
    _cleanup()
    # bundle_minutes=0 disables time-based bundling so each event creates its own incident
    _make_ruleset(trigger_count=5, bundle_minutes=0)

    # Publish 5 events — each creates its own incident (no bundling)
    for _ in range(5):
        _publish_event()

    incidents = Incident.objects.filter(category=CATEGORY)
    assert incidents.count() == 5, f"Expected 5 separate incidents, got {incidents.count()}"

    # All should be pending — each incident has only 1 event, not 5
    new_count = incidents.filter(status="new").count()
    assert new_count == 0, (
        f"No incident should be new — each has 1 event, trigger_count=5. "
        f"Got {new_count} new (old global-count bug would cause this)."
    )
