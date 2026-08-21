"""Threshold-flow regression moved from
tests/test_incident_prompts/test_llm_rule_thresholds.py — it mock.patches the
shared RuleSet model class (RuleSet.run_handler) around in-process event
publishing, which is unsafe under the parallel default tier (maestro item
#1839). Runs opt-in (`extended`) and serial.
"""
from unittest import mock

from testit import helpers as th


FLOW_CATEGORY = "maestro_1124_threshold_flow"


def _cleanup_categories(categories):
    from mojo.apps.incident.models import Event, Incident, RuleSet, Ticket

    Ticket.objects.filter(title__startswith="[Rule Proposal] Maestro #1124").delete()
    Event.objects.filter(category__in=categories).delete()
    Incident.objects.filter(category__in=categories).delete()
    RuleSet.objects.filter(category__in=categories).delete()


@th.django_unit_test("LLM-proposed thresholds persist and govern handler firing")
def test_llm_rule_thresholds(opts):
    from mojo.apps.incident.handlers.llm_agent import _tool_create_rule
    from mojo.apps.incident.handlers.ticket_actions import dispatch_action
    from mojo.apps.incident.models import Event, Incident, RuleSet, Ticket, TicketNote

    _cleanup_categories([FLOW_CATEGORY])

    result = _tool_create_rule({
        "name": "Maestro #1124 threshold flow",
        "category": FLOW_CATEGORY,
        "handler": "job://maestro_1124.handler",
        "rules": [{
            "name": "Match test category",
            "field": "category",
            "comparator": "==",
            "value": FLOW_CATEGORY,
            "value_type": "str",
        }],
        "min_count": 2,
        "window_minutes": 10,
        "bundle_by": 4,
        "bundle_minutes": 30,
        "reasoning": "Exercise the proposed threshold from approval through execution.",
    })

    th.assert_true(result.get("ok"), f"create_rule should succeed, got {result!r}")
    ruleset = RuleSet.objects.get(pk=result["ruleset_id"])
    th.assert_eq(
        ruleset.trigger_count, 2,
        "min_count=2 must persist to RuleSet.trigger_count so runtime enforcement can see it")
    th.assert_eq(
        ruleset.trigger_window, 10,
        "window_minutes=10 must persist to RuleSet.trigger_window so runtime counting uses it")
    th.assert_true(
        "min_count" not in ruleset.metadata and "window_minutes" not in ruleset.metadata,
        f"Canonical threshold fields must replace metadata aliases, got {ruleset.metadata!r}")

    ticket = Ticket.objects.get(pk=result["ticket_id"])
    action_note = TicketNote.objects.filter(
        parent=ticket, metadata__action__isnull=False,
    ).first()
    th.assert_true(action_note is not None, "Rule proposal should include an approval action note")
    th.assert_true(
        "**Threshold**: 2 events within 10 minutes" in action_note.note,
        f"Approval note must show the operative threshold, got {action_note.note!r}")
    th.assert_true(
        "**Bundle Window**: 30 minutes" in action_note.note,
        f"Approval note must show the bundle window, got {action_note.note!r}")

    response_meta = {
        "handler": "incident.rule_approval",
        "action": "approve",
        "context": {"target": {"model": "incident.RuleSet", "pk": ruleset.pk}},
    }
    th.assert_true(
        dispatch_action(ticket, action_note, response_meta),
        "The proposal approval action should activate the persisted RuleSet")
    ruleset.refresh_from_db()
    th.assert_true(ruleset.is_active, "Approved LLM proposal should be active")

    with mock.patch.object(
        RuleSet, "run_handler", autospec=True, return_value=True,
    ) as run_handler:
        first = Event.objects.create(
            category=FLOW_CATEGORY,
            level=5,
            title="First threshold event",
            source_ip="192.0.2.112",
        )
        first.publish()

        incident = Incident.objects.get(category=FLOW_CATEGORY)
        th.assert_eq(
            incident.status, "pending",
            "The first of two required events should leave the incident pending")
        th.assert_eq(
            run_handler.call_count, 0,
            "The handler must not fire before the approved min_count=2 threshold")

        second = Event.objects.create(
            category=FLOW_CATEGORY,
            level=5,
            title="Second threshold event",
            source_ip="192.0.2.112",
        )
        second.publish()

        incident.refresh_from_db()
        th.assert_eq(
            incident.status, "new",
            "The second event should transition the incident from pending to new")
        th.assert_eq(
            run_handler.call_count, 1,
            "The handler should fire exactly once when the approved threshold is reached")
