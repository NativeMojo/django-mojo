"""Core contract: incident.report_event persists a real Event row.

The 2026-08-11 and 2026-08-20 release gates lost incident Event rows while
overlapping mock.patch("mojo.apps.incident.report_event") windows from
parallel modules swallowed unrelated reports. This contract proves the real
pipeline end to end — no mocks anywhere near it — using a category no other
module can collide with, and cleans up only its own rows.
"""
import uuid

from testit import helpers as th


@th.django_unit_test("incident core: report_event persists a queryable Event row")
def test_report_event_persists_row(opts):
    from mojo.apps.incident.models import Event
    from mojo.apps.incident import report_event

    category = f"testit:incident_core:{uuid.uuid4().hex}"
    Event.objects.filter(category=category).delete()

    try:
        event = report_event(
            details="incident_core persistence contract",
            title="incident_core contract",
            category=category,
            level=3,
            scope="global",
        )

        assert event is not None and event.pk, (
            "report_event must return the persisted Event instance"
        )
        row = Event.objects.filter(category=category).first()
        assert row is not None, (
            f"an Event row with category {category} must exist after report_event"
        )
        assert row.pk == event.pk, (
            f"the queried row must be the returned event, got {row.pk} vs {event.pk}"
        )
        assert row.details == "incident_core persistence contract", (
            f"details must round-trip, got {row.details!r}"
        )
        assert row.level == 3, f"level must round-trip, got {row.level}"
        count = Event.objects.filter(category=category).count()
        assert count == 1, (
            f"exactly one row must exist for this run's unique category, got {count}"
        )
    finally:
        Event.objects.filter(category=category).delete()
