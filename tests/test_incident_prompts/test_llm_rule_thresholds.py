"""Regressions for operative thresholds on LLM-proposed incident rules."""
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


@th.django_unit_test("LLM rule tool rejects invalid or unreachable thresholds")
def test_llm_rule_threshold_validation(opts):
    from mojo.apps.incident.handlers.llm_agent import _tool_create_rule
    from mojo.apps.incident.models import RuleSet, Ticket

    invalid_cases = [
        ("boolean_count", {"min_count": True}),
        ("fractional_count", {"min_count": 2.5}),
        ("negative_count", {"min_count": -1}),
        ("window_without_count", {"window_minutes": 10}),
        ("boolean_window", {"min_count": 2, "window_minutes": False}),
        ("no_bundle_key", {"min_count": 2, "bundle_by": 0}),
        ("zero_bundle_window", {"min_count": 2, "bundle_minutes": 0}),
        ("short_bundle_window", {
            "min_count": 2,
            "window_minutes": 10,
            "bundle_minutes": 5,
        }),
    ]
    categories = [f"maestro_1124_invalid_{name}" for name, _ in invalid_cases]
    _cleanup_categories(categories)

    for (name, overrides), category in zip(invalid_cases, categories):
        params = {
            "name": f"Maestro #1124 invalid {name}",
            "category": category,
            "handler": "job://maestro_1124.handler",
            "rules": [],
            "bundle_by": 4,
            "bundle_minutes": 30,
            "reasoning": f"Reject invalid threshold case {name}.",
        }
        params.update(overrides)

        result = _tool_create_rule(params)

        th.assert_true(
            result.get("ok") is False and bool(result.get("error")),
            f"Invalid threshold case {name} should return a tool-style error, got {result!r}")
        th.assert_true(
            not RuleSet.objects.filter(category=category).exists(),
            f"Invalid threshold case {name} must not create a partial RuleSet")
        th.assert_true(
            not Ticket.objects.filter(title=f"[Rule Proposal] {params['name']}").exists(),
            f"Invalid threshold case {name} must not create an approval ticket")
