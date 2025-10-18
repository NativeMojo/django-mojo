"""
Simple test to debug handler transition detection.
"""
from testit import helpers as th


@th.django_unit_test()
def test_handler_transition_simple(opts):
    """Simple test to verify handler is called on status transition."""
    from mojo.apps.incident.models import Event, RuleSet, Rule, Incident

    # Clean up
    RuleSet.objects.filter(category="simple_test").delete()
    Event.objects.filter(category="simple_test").delete()
    Incident.objects.filter(category="simple_test").delete()

    # Create ruleset with threshold
    ruleset = RuleSet.objects.create(
        name="Simple Test",
        category="simple_test",
        priority=1,
        match_by=0,
        bundle_by=1,  # Bundle by hostname
        bundle_minutes=10,
        handler="job://test_handler",
        metadata={
            "min_count": 2,  # Only need 2 events
            "window_minutes": 10,
            "pending_status": "pending"
        }
    )

    Rule.objects.create(
        parent=ruleset,
        name="Match category",
        field_name="category",
        comparator="==",
        value="simple_test",
        value_type="str"
    )

    # Event 1 - creates pending incident
    event1 = Event.objects.create(
        category="simple_test",
        level=5,
        hostname="server1",
        title="Event 1"
    )
    event1.sync_metadata()
    event1.publish()

    incident = Incident.objects.get(category="simple_test")
    assert incident.status == "pending", f"Expected pending, got {incident.status}"

    # Event 2 - should transition to open
    event2 = Event.objects.create(
        category="simple_test",
        level=5,
        hostname="server1",
        title="Event 2"
    )
    event2.sync_metadata()
    event2.publish()

    incident.refresh_from_db()
    assert incident.status == "open", f"Expected open, got {incident.status}"
