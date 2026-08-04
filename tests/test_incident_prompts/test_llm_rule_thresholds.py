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
        ("string_bundle_key", {"min_count": 2, "bundle_by": "4"}),
        ("null_bundle_key", {"min_count": 2, "bundle_by": None}),
        ("boolean_bundle_key", {"min_count": 2, "bundle_by": True}),
        ("unsupported_bundle_key", {"min_count": 2, "bundle_by": 99}),
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


def _seed_legacy_proposal(category, is_active):
    from mojo.apps.incident.models import Rule, RuleSet, Ticket

    ruleset = RuleSet.objects.create(
        name=f"Maestro #1124 legacy {category}",
        category=category,
        handler="job://maestro_1124.handler",
        bundle_by=4,
        bundle_minutes=30,
        trigger_count=None,
        trigger_window=None,
        is_active=is_active,
        metadata={
            "llm_proposed": True,
            "min_count": 2,
            "window_minutes": 10,
            "occurrence_count": 1,
        },
    )
    Rule.objects.create(
        parent=ruleset,
        name="Match test category",
        field_name="category",
        comparator="==",
        value=category,
        value_type="str",
    )
    if not is_active:
        Ticket.objects.create(
            title=f"[Rule Proposal] Maestro #1124 legacy {category}",
            description="Legacy metadata-only proposal",
            category="llm_review",
            metadata={"ruleset_id": ruleset.pk, "requires_approval": True},
        )
    return ruleset


def _threshold_proposal_params(category):
    return {
        "name": f"Maestro #1124 canonical {category}",
        "category": category,
        "handler": "job://maestro_1124.handler",
        "rules": [{
            "name": "Match test category",
            "field": "category",
            "comparator": "==",
            "value": category,
            "value_type": "str",
        }],
        "min_count": 2,
        "window_minutes": 10,
        "bundle_by": 4,
        "bundle_minutes": 30,
        "reasoning": "Canonical threshold policy must remain separately reviewable.",
    }


@th.django_unit_test("active legacy metadata threshold cannot swallow canonical policy")
def test_active_legacy_threshold_policy_does_not_deduplicate(opts):
    from mojo.apps.incident.handlers.llm_agent import _tool_create_rule
    from mojo.apps.incident.models import RuleSet, Ticket

    category = "maestro_1124_legacy_active"
    _cleanup_categories([category])
    legacy = _seed_legacy_proposal(category, is_active=True)

    result = _tool_create_rule(_threshold_proposal_params(category))

    th.assert_true(result.get("ok"), f"Canonical proposal should be created, got {result!r}")
    th.assert_true(
        not result.get("deduplicated"),
        "Canonical threshold policy must not deduplicate against active metadata-only policy")
    th.assert_true(
        result.get("ruleset_id") != legacy.pk,
        "Canonical threshold proposal must remain distinct from the active legacy RuleSet")
    th.assert_eq(
        RuleSet.objects.filter(category=category).count(), 2,
        "Active legacy and canonical threshold policies should both remain reviewable")
    th.assert_true(
        Ticket.objects.filter(pk=result.get("ticket_id")).exists(),
        "The distinct canonical policy should receive its own approval ticket")


@th.django_unit_test("pending legacy metadata threshold cannot swallow canonical policy")
def test_pending_legacy_threshold_policy_does_not_deduplicate(opts):
    from mojo.apps.incident.handlers.llm_agent import _tool_create_rule
    from mojo.apps.incident.models import RuleSet, Ticket

    category = "maestro_1124_legacy_pending"
    _cleanup_categories([category])
    legacy = _seed_legacy_proposal(category, is_active=False)

    result = _tool_create_rule(_threshold_proposal_params(category))

    th.assert_true(result.get("ok"), f"Canonical proposal should be created, got {result!r}")
    th.assert_true(
        not result.get("deduplicated"),
        "Canonical threshold policy must not deduplicate against pending metadata-only policy")
    th.assert_true(
        result.get("ruleset_id") != legacy.pk,
        "Canonical threshold proposal must remain distinct from the pending legacy RuleSet")
    th.assert_eq(
        RuleSet.objects.filter(category=category).count(), 2,
        "Pending legacy and canonical threshold policies should both remain reviewable")
    th.assert_eq(
        Ticket.objects.filter(metadata__ruleset_id__in=[legacy.pk, result["ruleset_id"]]).count(),
        2,
        "Mismatched pending policies should retain separate approval tickets")
