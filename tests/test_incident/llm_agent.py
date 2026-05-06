"""
LLM Security Agent tests — mocked agent flow.

These tests publish jobs via jobs.publish() and run them via th.run_pending_jobs(),
exercising the full pipeline: job dispatch → payload parsing → prompt building →
agent loop → tool dispatch → DB side effects.

The only mock is _call_claude (the Anthropic API call). All tool implementations
run for real against the test database.
"""
from testit import helpers as th
from unittest.mock import patch


def _claude_response(stop_reason, content):
    """Build a dict matching the Claude API response shape (model_dump output)."""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-20250514",
        "stop_reason": stop_reason,
        "content": content,
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }


def _tool_use_block(tool_id, name, input_data):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": input_data}


def _text_block(text):
    return {"type": "text", "text": text}


@th.django_unit_test("LLM agent: investigate and ignore")
def test_llm_agent_investigate_and_ignore(opts):
    from mojo.apps.incident.models import Event, Incident, RuleSet
    from mojo.apps.incident.models.history import IncidentHistory
    from mojo.apps.jobs.models import Job
    from mojo.apps import jobs

    # Clean up
    RuleSet.objects.filter(category="llm_test").delete()
    Event.objects.filter(category="llm_test").delete()
    Incident.objects.filter(category="llm_test").delete()
    Job.objects.filter(channel="default").delete()

    # Create test data
    ruleset = RuleSet.objects.create(
        name="LLM Test Rule",
        category="llm_test",
        priority=1,
    )
    event = Event.objects.create(
        category="llm_test",
        level=8,
        title="Test OSSEC alert",
        source_ip="10.0.0.99",
        details="File change in /tmp",
    )
    incident = Incident.objects.create(
        priority=8, state=0, status="new",
        category="llm_test", scope="global",
        title="Test incident",
        source_ip="10.0.0.99",
        rule_set=ruleset,
    )
    event.incident = incident
    event.save(update_fields=["incident"])

    # Scripted Claude responses:
    # Turn 1: query_events + query_ip_history
    # Turn 2: update_incident to ignored
    # Turn 3: final text summary
    mock_responses = [
        _claude_response("tool_use", [
            _tool_use_block("tool_1", "query_events", {"category": "llm_test", "minutes": 60}),
            _tool_use_block("tool_2", "query_ip_history", {"ip": "10.0.0.99"}),
        ]),
        _claude_response("tool_use", [
            _tool_use_block("tool_3", "update_incident", {
                "incident_id": incident.pk,
                "status": "ignored",
                "note": "Routine file change in /tmp, no threat.",
            }),
        ]),
        _claude_response("end_turn", [
            _text_block("Investigated and ignored — routine /tmp file change."),
        ]),
    ]

    with patch("mojo.apps.incident.handlers.llm_agent._call_claude", side_effect=mock_responses):
        with patch("mojo.apps.incident.handlers.llm_agent._get_llm_api_key", return_value="test-key"):
            jobs.publish(
                "mojo.apps.incident.handlers.llm_agent.execute_llm_handler",
                {"event_id": event.pk, "incident_id": incident.pk, "ruleset_id": ruleset.pk},
                channel="default",
            )
            executed = th.run_pending_jobs(channel="default")

    assert executed >= 1, f"Expected at least 1 job executed, got {executed}"

    # Assert incident was updated to ignored
    incident.refresh_from_db()
    assert incident.status == "ignored", f"Expected status=ignored, got {incident.status}"

    # Assert LLM assessment stored in metadata
    assert incident.metadata.get("llm_assessment") is not None, "LLM assessment should be in metadata"
    assert incident.metadata["llm_assessment"]["status"] == "ignored", "Assessment status should be ignored"

    # Assert history entries exist
    history = IncidentHistory.objects.filter(parent=incident, kind="handler:llm")
    assert history.count() >= 1, f"Expected LLM history entries, got {history.count()}"


@th.django_unit_test("LLM agent: investigate and block IP")
def test_llm_agent_investigate_and_block(opts):
    from mojo.apps.incident.models import Event, Incident, RuleSet
    from mojo.apps.incident.models.history import IncidentHistory
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.jobs.models import Job
    from mojo.apps import jobs

    # Clean up
    RuleSet.objects.filter(category="llm_block_test").delete()
    Event.objects.filter(category="llm_block_test").delete()
    Incident.objects.filter(category="llm_block_test").delete()
    GeoLocatedIP.objects.filter(ip_address="10.99.99.1").delete()
    Job.objects.filter(channel="default").delete()

    event = Event.objects.create(
        category="llm_block_test",
        level=10,
        title="SSH brute force",
        source_ip="10.99.99.1",
        details="Multiple failed SSH logins",
    )
    incident = Incident.objects.create(
        priority=10, state=0, status="new",
        category="llm_block_test", scope="global",
        title="SSH brute force",
        source_ip="10.99.99.1",
    )
    event.incident = incident
    event.save(update_fields=["incident"])

    mock_responses = [
        _claude_response("tool_use", [
            _tool_use_block("tool_1", "query_events", {"source_ip": "10.99.99.1", "minutes": 60}),
        ]),
        _claude_response("tool_use", [
            _tool_use_block("tool_2", "block_ip", {
                "ip": "10.99.99.1",
                "reason": "SSH brute force — 50 failed attempts in 10 minutes",
                "ttl": 3600,
                "incident_id": incident.pk,
            }),
            _tool_use_block("tool_3", "update_incident", {
                "incident_id": incident.pk,
                "status": "resolved",
                "note": "Blocked attacker IP for 1 hour.",
            }),
        ]),
        _claude_response("end_turn", [
            _text_block("Blocked 10.99.99.1 for SSH brute force, incident resolved."),
        ]),
    ]

    with patch("mojo.apps.incident.handlers.llm_agent._call_claude", side_effect=mock_responses):
        with patch("mojo.apps.incident.handlers.llm_agent._get_llm_api_key", return_value="test-key"):
            jobs.publish(
                "mojo.apps.incident.handlers.llm_agent.execute_llm_handler",
                {"event_id": event.pk, "incident_id": incident.pk, "ruleset_id": None},
                channel="default",
            )
            executed = th.run_pending_jobs(channel="default")

    assert executed >= 1, f"Expected at least 1 job executed, got {executed}"

    # Assert IP was blocked
    geo = GeoLocatedIP.objects.filter(ip_address="10.99.99.1").first()
    assert geo is not None, "GeoLocatedIP should exist for blocked IP"
    assert geo.is_blocked is True, f"Expected IP to be blocked, got is_blocked={geo.is_blocked}"

    # Assert incident resolved
    incident.refresh_from_db()
    assert incident.status == "resolved", f"Expected status=resolved, got {incident.status}"

    # Assert history has block entry
    history = IncidentHistory.objects.filter(parent=incident, kind="handler:llm")
    block_notes = [h for h in history if "Blocked IP" in (h.note or "")]
    assert len(block_notes) >= 1, "Should have history entry for IP block"


@th.django_unit_test("LLM agent: create ticket for human review")
def test_llm_agent_create_ticket(opts):
    from mojo.apps.incident.models import Event, Incident, Ticket
    from mojo.apps.jobs.models import Job
    from mojo.apps import jobs

    # Clean up
    Event.objects.filter(category="llm_ticket_test").delete()
    Incident.objects.filter(category="llm_ticket_test").delete()
    Ticket.objects.filter(category="llm_review").delete()
    Job.objects.filter(channel="default").delete()

    event = Event.objects.create(
        category="llm_ticket_test",
        level=9,
        title="Unusual data access pattern",
        source_ip="10.50.50.1",
        details="Large data export from internal API",
    )
    incident = Incident.objects.create(
        priority=9, state=0, status="new",
        category="llm_ticket_test", scope="global",
        title="Unusual data access",
        source_ip="10.50.50.1",
    )
    event.incident = incident
    event.save(update_fields=["incident"])

    mock_responses = [
        _claude_response("tool_use", [
            _tool_use_block("tool_1", "query_events", {"source_ip": "10.50.50.1", "minutes": 60}),
        ]),
        _claude_response("tool_use", [
            _tool_use_block("tool_2", "update_incident", {
                "incident_id": incident.pk,
                "status": "investigating",
                "note": "Unusual pattern — needs human review.",
            }),
            _tool_use_block("tool_3", "create_ticket", {
                "title": "Review unusual data access from 10.50.50.1",
                "note": "Large data export detected. Could be legitimate or exfiltration. Please review.",
                "priority": 7,
                "incident_id": incident.pk,
            }),
        ]),
        _claude_response("end_turn", [
            _text_block("Created ticket for human review."),
        ]),
    ]

    with patch("mojo.apps.incident.handlers.llm_agent._call_claude", side_effect=mock_responses):
        with patch("mojo.apps.incident.handlers.llm_agent._get_llm_api_key", return_value="test-key"):
            jobs.publish(
                "mojo.apps.incident.handlers.llm_agent.execute_llm_handler",
                {"event_id": event.pk, "incident_id": incident.pk, "ruleset_id": None},
                channel="default",
            )
            executed = th.run_pending_jobs(channel="default")

    assert executed >= 1, f"Expected at least 1 job executed, got {executed}"

    # Assert incident is investigating (not resolved — waiting on human)
    incident.refresh_from_db()
    assert incident.status == "investigating", f"Expected status=investigating, got {incident.status}"

    # Assert ticket was created
    tickets = Ticket.objects.filter(category="llm_review", incident=incident)
    assert tickets.count() >= 1, f"Expected at least 1 ticket, got {tickets.count()}"

    ticket = tickets.first()
    assert ticket.metadata.get("llm_linked") is True, "Ticket should be llm_linked"
    assert ticket.priority == 7, f"Expected priority=7, got {ticket.priority}"


@th.django_unit_test("LLM agent: ticket reply re-invocation")
def test_llm_ticket_reply(opts):
    from mojo.apps.incident.models import Ticket, TicketNote
    from mojo.apps.jobs.models import Job
    from mojo.apps import jobs
    from django.contrib.auth import get_user_model
    User = get_user_model()

    # Clean up
    Ticket.objects.filter(category="llm_reply_test").delete()
    Job.objects.filter(channel="default").delete()

    # Need a user for notes
    user = User.objects.filter(is_superuser=True, is_active=True).first()
    if not user:
        user = User.objects.create_user(
            username="llm_test_admin",
            email="llm_test_admin@test.com",
            password="testpass123",
            is_superuser=True,
            is_active=True,
        )

    # Create ticket with LLM-linked metadata
    ticket = Ticket.objects.create(
        title="Review suspicious activity",
        description="LLM created this ticket",
        status="open",
        priority=7,
        category="llm_reply_test",
        metadata={"llm_linked": True, "llm_enabled": True},
    )

    # Add the initial LLM note
    TicketNote.objects.create(
        parent=ticket,
        user=user,
        note="[LLM Agent] I found suspicious data access. Should I block this IP?",
    )

    # Add human reply
    human_note = TicketNote.objects.create(
        parent=ticket,
        user=user,
        note="Yes, go ahead and block it.",
    )

    initial_note_count = TicketNote.objects.filter(parent=ticket).count()

    # Mock: LLM responds with text (no tool use needed for this test)
    mock_responses = [
        _claude_response("end_turn", [
            _text_block("Understood. I've noted your approval. The IP has been blocked."),
        ]),
    ]

    with patch("mojo.apps.incident.handlers.llm_agent._call_claude", side_effect=mock_responses):
        with patch("mojo.apps.incident.handlers.llm_agent._get_llm_api_key", return_value="test-key"):
            jobs.publish(
                "mojo.apps.incident.handlers.llm_agent.execute_llm_ticket_reply",
                {"ticket_id": ticket.pk, "note_id": human_note.pk},
                channel="default",
            )
            executed = th.run_pending_jobs(channel="default")

    assert executed >= 1, f"Expected at least 1 job executed, got {executed}"

    # Assert a new LLM note was added
    final_note_count = TicketNote.objects.filter(parent=ticket).count()
    assert final_note_count > initial_note_count, f"Expected new note, count was {initial_note_count} now {final_note_count}"

    # Assert the new note starts with [LLM Agent]
    latest_note = TicketNote.objects.filter(parent=ticket).order_by("-created").first()
    assert latest_note.note.startswith("[LLM Agent]"), f"LLM note should start with [LLM Agent], got: {latest_note.note[:50]}"


@th.django_unit_test("LLM agent: create_ticket deduplicates on the same incident")
def test_llm_agent_create_ticket_deduplicates(opts):
    from mojo.apps.incident.models import Event, Incident, Ticket, TicketNote
    from mojo.apps.jobs.models import Job
    from mojo.apps import jobs
    from django.contrib.auth import get_user_model
    User = get_user_model()

    # Clean up
    Event.objects.filter(category="llm_dedup_ticket").delete()
    Incident.objects.filter(category="llm_dedup_ticket").delete()
    Ticket.objects.filter(category="llm_review").delete()
    Job.objects.filter(channel="default").delete()

    # Need a system user for LLM notes
    if not User.objects.filter(is_superuser=True, is_active=True).exists():
        User.objects.create_user(
            username="llm_dedup_admin",
            email="llm_dedup_admin@test.com",
            password="testpass123",
            is_superuser=True,
            is_active=True,
        )

    event = Event.objects.create(
        category="llm_dedup_ticket",
        level=8,
        title="Dedup ticket test",
        source_ip="10.77.0.1",
    )
    incident = Incident.objects.create(
        priority=8, state=0, status="new",
        category="llm_dedup_ticket", scope="global",
        title="Dedup ticket test",
        source_ip="10.77.0.1",
    )
    event.incident = incident
    event.save(update_fields=["incident"])

    # Two create_ticket calls in a single agent loop for the same incident.
    mock_responses = [
        _claude_response("tool_use", [
            _tool_use_block("t1", "create_ticket", {
                "title": "Review 10.77.0.1",
                "note": "First observation.",
                "priority": 5,
                "incident_id": incident.pk,
            }),
        ]),
        _claude_response("tool_use", [
            _tool_use_block("t2", "create_ticket", {
                "title": "Review 10.77.0.1 — follow up",
                "note": "Second observation.",
                "priority": 5,
                "incident_id": incident.pk,
            }),
        ]),
        _claude_response("end_turn", [_text_block("Done.")]),
    ]

    with patch("mojo.apps.incident.handlers.llm_agent._call_claude", side_effect=mock_responses):
        with patch("mojo.apps.incident.handlers.llm_agent._get_llm_api_key", return_value="test-key"):
            jobs.publish(
                "mojo.apps.incident.handlers.llm_agent.execute_llm_handler",
                {"event_id": event.pk, "incident_id": incident.pk, "ruleset_id": None},
                channel="default",
            )
            executed = th.run_pending_jobs(channel="default")

    assert executed >= 1, f"Expected at least 1 job executed, got {executed}"

    tickets = Ticket.objects.filter(incident=incident, category="llm_review")
    assert tickets.count() == 1, f"Expected exactly 1 ticket after dedup, got {tickets.count()}"

    ticket = tickets.first()
    notes = TicketNote.objects.filter(parent=ticket).order_by("created")
    assert notes.count() == 2, f"Expected 2 ticket notes (one per create_ticket call), got {notes.count()}"
    assert all(n.note.startswith("[LLM Agent]") for n in notes), \
        f"All notes should be tagged as LLM Agent, got: {[n.note[:30] for n in notes]}"


@th.django_unit_test("LLM agent: create_rule deduplicates against a pending proposal")
def test_llm_agent_create_rule_deduplicates_pending(opts):
    from mojo.apps.incident.models import Event, Incident, RuleSet, Rule, Ticket, TicketNote
    from mojo.apps.jobs.models import Job
    from mojo.apps import jobs
    from django.contrib.auth import get_user_model
    User = get_user_model()

    # Clean up
    RuleSet.objects.filter(category="llm_dedup_rule").delete()
    Event.objects.filter(category="llm_dedup_rule").delete()
    Incident.objects.filter(category="llm_dedup_rule").delete()
    Ticket.objects.filter(category="llm_review").delete()
    Job.objects.filter(channel="default").delete()

    if not User.objects.filter(is_superuser=True, is_active=True).exists():
        User.objects.create_user(
            username="llm_dedup_rule_admin",
            email="llm_dedup_rule_admin@test.com",
            password="testpass123",
            is_superuser=True,
            is_active=True,
        )

    event = Event.objects.create(
        category="llm_dedup_rule",
        level=9,
        title="Rule dedup test",
        source_ip="10.77.1.1",
    )
    incident = Incident.objects.create(
        priority=9, state=0, status="new",
        category="llm_dedup_rule", scope="global",
        title="Rule dedup test",
        source_ip="10.77.1.1",
    )
    event.incident = incident
    event.save(update_fields=["incident"])

    rule_payload = {
        "name": "Dedup test rule",
        "category": "llm_dedup_rule",
        "handler": "block://?ttl=3600",
        "rules": [
            {"name": "Level high", "field": "level", "comparator": ">=", "value": "9", "value_type": "int"},
        ],
        "reasoning": "Recurring high-level pattern.",
        "bundle_by": 4,
        "bundle_minutes": 30,
    }

    # Two create_rule calls with identical payloads in a single agent loop.
    mock_responses = [
        _claude_response("tool_use", [
            _tool_use_block("t1", "create_rule", rule_payload),
        ]),
        _claude_response("tool_use", [
            _tool_use_block("t2", "create_rule", rule_payload),
        ]),
        _claude_response("end_turn", [_text_block("Done.")]),
    ]

    with patch("mojo.apps.incident.handlers.llm_agent._call_claude", side_effect=mock_responses):
        with patch("mojo.apps.incident.handlers.llm_agent._get_llm_api_key", return_value="test-key"):
            jobs.publish(
                "mojo.apps.incident.handlers.llm_agent.execute_llm_handler",
                {"event_id": event.pk, "incident_id": incident.pk, "ruleset_id": None},
                channel="default",
            )
            executed = th.run_pending_jobs(channel="default")

    assert executed >= 1, f"Expected at least 1 job executed, got {executed}"

    rulesets = RuleSet.objects.filter(category="llm_dedup_rule", metadata__llm_proposed=True)
    assert rulesets.count() == 1, f"Expected exactly 1 llm_proposed RuleSet after dedup, got {rulesets.count()}"

    ruleset = rulesets.first()
    assert (ruleset.metadata or {}).get("occurrence_count") == 2, \
        f"Expected occurrence_count=2, got {(ruleset.metadata or {}).get('occurrence_count')}"

    approval_tickets = Ticket.objects.filter(metadata__ruleset_id=ruleset.pk)
    assert approval_tickets.count() == 1, \
        f"Expected exactly 1 approval ticket after dedup, got {approval_tickets.count()}"

    ticket = approval_tickets.first()
    notes = TicketNote.objects.filter(parent=ticket).order_by("created")
    # One note from the initial create + one "Pattern seen again" note from dedup
    assert notes.count() == 2, \
        f"Expected 2 notes on approval ticket (initial + dedup), got {notes.count()}"
    latest = notes.order_by("-created").first()
    assert "Pattern seen again" in latest.note, \
        f"Latest note should reference the dedup bump, got: {latest.note[:80]}"


@th.django_unit_test("LLM agent: create_rule skips when an active rule already matches")
def test_llm_agent_create_rule_deduplicates_active(opts):
    from mojo.apps.incident.models import Event, Incident, RuleSet, Rule, Ticket
    from mojo.apps.jobs.models import Job
    from mojo.apps import jobs
    from django.contrib.auth import get_user_model
    User = get_user_model()

    # Clean up
    RuleSet.objects.filter(category="llm_dedup_active").delete()
    Event.objects.filter(category="llm_dedup_active").delete()
    Incident.objects.filter(category="llm_dedup_active").delete()
    Ticket.objects.filter(category="llm_review").delete()
    Job.objects.filter(channel="default").delete()

    if not User.objects.filter(is_superuser=True, is_active=True).exists():
        User.objects.create_user(
            username="llm_dedup_active_admin",
            email="llm_dedup_active_admin@test.com",
            password="testpass123",
            is_superuser=True,
            is_active=True,
        )

    # Pre-seed an ACTIVE (is_active=True) llm_proposed RuleSet with the same signature.
    existing_ruleset = RuleSet.objects.create(
        name="Already active rule",
        category="llm_dedup_active",
        handler="block://?ttl=3600",
        bundle_by=4,
        bundle_minutes=30,
        is_active=True,
        metadata={
            "llm_proposed": True,
            "llm_reasoning": "Approved previously",
        },
    )
    Rule.objects.create(
        parent=existing_ruleset,
        name="Level high",
        index=0,
        field_name="level",
        comparator=">=",
        value="9",
        value_type="int",
    )

    event = Event.objects.create(
        category="llm_dedup_active",
        level=9,
        title="Active dedup test",
        source_ip="10.77.2.1",
    )
    incident = Incident.objects.create(
        priority=9, state=0, status="new",
        category="llm_dedup_active", scope="global",
        title="Active dedup test",
        source_ip="10.77.2.1",
    )
    event.incident = incident
    event.save(update_fields=["incident"])

    rule_payload = {
        "name": "Duplicate proposal",
        "category": "llm_dedup_active",
        "handler": "block://?ttl=3600",
        "rules": [
            {"name": "Level high", "field": "level", "comparator": ">=", "value": "9", "value_type": "int"},
        ],
        "reasoning": "Same signature, different name.",
        "bundle_by": 4,
        "bundle_minutes": 30,
    }

    mock_responses = [
        _claude_response("tool_use", [
            _tool_use_block("t1", "create_rule", rule_payload),
        ]),
        _claude_response("end_turn", [_text_block("Done.")]),
    ]

    with patch("mojo.apps.incident.handlers.llm_agent._call_claude", side_effect=mock_responses):
        with patch("mojo.apps.incident.handlers.llm_agent._get_llm_api_key", return_value="test-key"):
            jobs.publish(
                "mojo.apps.incident.handlers.llm_agent.execute_llm_handler",
                {"event_id": event.pk, "incident_id": incident.pk, "ruleset_id": None},
                channel="default",
            )
            executed = th.run_pending_jobs(channel="default")

    assert executed >= 1, f"Expected at least 1 job executed, got {executed}"

    rulesets = RuleSet.objects.filter(category="llm_dedup_active", metadata__llm_proposed=True)
    assert rulesets.count() == 1, \
        f"Expected no new RuleSet (active match), got {rulesets.count()}"

    tickets = Ticket.objects.filter(metadata__ruleset_id=existing_ruleset.pk)
    assert tickets.count() == 0, \
        f"Expected no approval ticket (rule already active), got {tickets.count()}"


@th.django_unit_test("LLM agent: create_rule deduplicates variant signatures via open-ticket check")
def test_llm_agent_create_rule_deduplicates_variant(opts):
    """Two independent triage jobs propose rules with different signatures for the
    same category.  The second should be folded into the first because an open
    proposal ticket already exists for this category."""
    from mojo.apps.incident.models import Event, Incident, RuleSet, Rule, Ticket, TicketNote
    from mojo.apps.jobs.models import Job
    from mojo.apps import jobs
    from django.contrib.auth import get_user_model
    User = get_user_model()

    # Clean up
    RuleSet.objects.filter(category="llm_dedup_variant").delete()
    Event.objects.filter(category="llm_dedup_variant").delete()
    Incident.objects.filter(category="llm_dedup_variant").delete()
    Ticket.objects.filter(category="llm_review").delete()
    Job.objects.filter(channel="default").delete()

    if not User.objects.filter(is_superuser=True, is_active=True).exists():
        User.objects.create_user(
            username="llm_dedup_variant_admin",
            email="llm_dedup_variant_admin@test.com",
            password="testpass123",
            is_superuser=True,
            is_active=True,
        )

    # --- First triage job: proposes rule with one set of fields ---
    event1 = Event.objects.create(
        category="llm_dedup_variant", level=9,
        title="Credential scan /etc/shadow", source_ip="10.88.1.1",
    )
    incident1 = Incident.objects.create(
        priority=9, state=0, status="new",
        category="llm_dedup_variant", scope="global",
        title="Credential scan /etc/shadow", source_ip="10.88.1.1",
    )
    event1.incident = incident1
    event1.save(update_fields=["incident"])

    rule_payload_1 = {
        "name": "Credential harvesting scan",
        "category": "llm_dedup_variant",
        "handler": "block://?ttl=3600",
        "rules": [
            {"name": "Path shadow", "field": "path", "comparator": "contains", "value": "/etc/shadow"},
        ],
        "reasoning": "Recurring credential file access attempts.",
        "bundle_by": 4,
        "bundle_minutes": 30,
    }

    mock_responses_1 = [
        _claude_response("tool_use", [
            _tool_use_block("t1", "update_incident", {"incident_id": incident1.pk, "status": "investigating"}),
        ]),
        _claude_response("tool_use", [
            _tool_use_block("t2", "create_rule", rule_payload_1),
        ]),
        _claude_response("end_turn", [_text_block("Done.")]),
    ]

    with patch("mojo.apps.incident.handlers.llm_agent._call_claude", side_effect=mock_responses_1):
        with patch("mojo.apps.incident.handlers.llm_agent._get_llm_api_key", return_value="test-key"):
            jobs.publish(
                "mojo.apps.incident.handlers.llm_agent.execute_llm_handler",
                {"event_id": event1.pk, "incident_id": incident1.pk, "ruleset_id": None},
                channel="default",
            )
            executed = th.run_pending_jobs(channel="default")

    assert executed >= 1, f"First triage job should execute, got {executed}"
    rulesets_after_first = RuleSet.objects.filter(
        category="llm_dedup_variant", metadata__llm_proposed=True)
    assert rulesets_after_first.count() == 1, \
        f"Expected 1 RuleSet after first triage, got {rulesets_after_first.count()}"
    first_rs = rulesets_after_first.first()

    # --- Second triage job: different incident, proposes a VARIANT rule ---
    event2 = Event.objects.create(
        category="llm_dedup_variant", level=9,
        title="Credential scan /etc/passwd", source_ip="10.88.2.2",
    )
    incident2 = Incident.objects.create(
        priority=9, state=0, status="new",
        category="llm_dedup_variant", scope="global",
        title="Credential scan /etc/passwd", source_ip="10.88.2.2",
    )
    event2.incident = incident2
    event2.save(update_fields=["incident"])

    rule_payload_2 = {
        "name": "Config file harvesting",
        "category": "llm_dedup_variant",
        "handler": "block://?ttl=3600",
        "rules": [
            {"name": "Path cred files", "field": "path", "comparator": "regex",
             "value": "/(etc|var)/(shadow|passwd)"},
        ],
        "reasoning": "Same credential harvesting pattern with broader regex.",
        "bundle_by": 4,
        "bundle_minutes": 30,
    }

    mock_responses_2 = [
        _claude_response("tool_use", [
            _tool_use_block("t3", "update_incident", {"incident_id": incident2.pk, "status": "investigating"}),
        ]),
        _claude_response("tool_use", [
            _tool_use_block("t4", "create_rule", rule_payload_2),
        ]),
        _claude_response("end_turn", [_text_block("Done.")]),
    ]

    with patch("mojo.apps.incident.handlers.llm_agent._call_claude", side_effect=mock_responses_2):
        with patch("mojo.apps.incident.handlers.llm_agent._get_llm_api_key", return_value="test-key"):
            jobs.publish(
                "mojo.apps.incident.handlers.llm_agent.execute_llm_handler",
                {"event_id": event2.pk, "incident_id": incident2.pk, "ruleset_id": None},
                channel="default",
            )
            executed = th.run_pending_jobs(channel="default")

    assert executed >= 1, f"Second triage job should execute, got {executed}"

    # Should still be exactly 1 RuleSet — the variant was folded into the first
    rulesets_final = RuleSet.objects.filter(
        category="llm_dedup_variant", metadata__llm_proposed=True)
    assert rulesets_final.count() == 1, \
        f"Expected 1 RuleSet after variant dedup, got {rulesets_final.count()}"

    first_rs.refresh_from_db()
    assert (first_rs.metadata or {}).get("occurrence_count") == 2, \
        f"Expected occurrence_count=2, got {(first_rs.metadata or {}).get('occurrence_count')}"

    approval_tickets = Ticket.objects.filter(metadata__ruleset_id=first_rs.pk)
    assert approval_tickets.count() == 1, \
        f"Expected 1 approval ticket after variant dedup, got {approval_tickets.count()}"

    ticket = approval_tickets.first()
    notes = TicketNote.objects.filter(parent=ticket).order_by("created")
    latest = notes.order_by("-created").first()
    assert "pattern seen again" in latest.note.lower(), \
        f"Latest note should reference dedup, got: {latest.note[:100]}"


@th.django_unit_test("LLM agent: action_response approval activates rule and closes ticket")
def test_llm_ticket_approval_activates_rule(opts):
    """When a note with action_response={action: approve, handler: incident.rule_approval}
    is created, the linked ruleset should be activated and the ticket closed
    deterministically via the action dispatch system — no LLM call needed."""
    from mojo.apps.incident.models import RuleSet, Rule, Ticket, TicketNote
    from django.contrib.auth import get_user_model
    User = get_user_model()

    # Clean up
    RuleSet.objects.filter(category="llm_approval_test").delete()
    Ticket.objects.filter(category="llm_review").delete()

    user = User.objects.filter(is_superuser=True, is_active=True).first()
    if not user:
        user = User.objects.create_user(
            username="llm_approval_admin",
            email="llm_approval_admin@test.com",
            password="testpass123",
            is_superuser=True,
            is_active=True,
        )

    ruleset = RuleSet.objects.create(
        name="Credential scan blocker",
        category="llm_approval_test",
        handler="block://?ttl=3600",
        bundle_by=4,
        bundle_minutes=30,
        is_active=False,
        metadata={
            "llm_proposed": True,
            "llm_reasoning": "Recurring credential file access",
            "occurrence_count": 3,
        },
    )
    Rule.objects.create(
        parent=ruleset, name="Path match", index=0,
        field_name="path", comparator="contains", value="/etc/shadow",
    )

    ticket = Ticket.objects.create(
        title=f"[Rule Proposal] {ruleset.name}",
        description="Please review and approve.",
        status="open",
        priority=3,
        category="llm_review",
        metadata={"llm_linked": True, "llm_enabled": True, "ruleset_id": ruleset.pk},
    )

    TicketNote.objects.create(
        parent=ticket, user=user,
        note="[LLM Agent] I've detected a recurring pattern and propose a new rule.",
        metadata={
            "action": {
                "type": "approval",
                "handler": "incident.rule_approval",
                "label": "Approve rule?",
                "context": {"target": {"model": "incident.RuleSet", "pk": ruleset.pk}},
            }
        },
    )

    # Dispatch the approval directly via the action system
    from mojo.apps.incident.handlers.ticket_actions import dispatch_action
    response_meta = {
        "handler": "incident.rule_approval",
        "action": "approve",
        "context": {"target": {"model": "incident.RuleSet", "pk": ruleset.pk}},
    }

    approval_note = TicketNote.objects.create(
        parent=ticket, user=user,
        note="Approved",
        metadata={"action_response": response_meta},
    )

    result = dispatch_action(ticket, approval_note, response_meta)
    assert result is True, "dispatch_action should return True on success"

    ruleset.refresh_from_db()
    assert ruleset.is_active, \
        f"RuleSet should be active (is_active=True), got is_active={ruleset.is_active}"

    ticket.refresh_from_db()
    assert ticket.status == "resolved", \
        f"Ticket should be resolved, got {ticket.status}"

    activation_note = (
        TicketNote.objects.filter(parent=ticket)
        .order_by("-created")
        .first()
    )
    assert "activated" in activation_note.note.lower(), \
        f"Expected activation note, got: {activation_note.note[:100]}"


@th.django_unit_test("LLM agent: non-approval reply still invokes LLM")
def test_llm_ticket_non_approval_still_invokes_llm(opts):
    """A non-approval reply on a rule proposal ticket should still go through
    the normal LLM flow (not the fast path)."""
    from mojo.apps.incident.models import RuleSet, Ticket, TicketNote
    from mojo.apps.jobs.models import Job
    from mojo.apps import jobs
    from django.contrib.auth import get_user_model
    User = get_user_model()

    # Clean up
    RuleSet.objects.filter(category="llm_nonapproval_test").delete()
    Ticket.objects.filter(category="llm_review").delete()
    Job.objects.filter(channel="default").delete()

    user = User.objects.filter(is_superuser=True, is_active=True).first()
    if not user:
        user = User.objects.create_user(
            username="llm_nonapproval_admin",
            email="llm_nonapproval_admin@test.com",
            password="testpass123",
            is_superuser=True,
            is_active=True,
        )

    ruleset = RuleSet.objects.create(
        name="Test rule",
        category="llm_nonapproval_test",
        handler="block://?ttl=3600",
        is_active=False,
        metadata={"llm_proposed": True},
    )

    ticket = Ticket.objects.create(
        title=f"[Rule Proposal] {ruleset.name}",
        description="Please review.",
        status="open",
        priority=3,
        category="llm_review",
        metadata={"llm_linked": True, "llm_enabled": True, "ruleset_id": ruleset.pk},
    )

    TicketNote.objects.create(
        parent=ticket, user=user,
        note="[LLM Agent] Proposed a new rule.",
    )

    # Human asks a question (NOT an action response — no metadata.action_response)
    question_note = TicketNote.objects.create(
        parent=ticket, user=user,
        note="What's the false positive rate on this pattern?",
    )

    mock_responses = [
        _claude_response("end_turn", [
            _text_block("Based on the last 30 days, the false positive rate is approximately 2%."),
        ]),
    ]

    with patch("mojo.apps.incident.handlers.llm_agent._call_claude", side_effect=mock_responses) as mock_claude:
        with patch("mojo.apps.incident.handlers.llm_agent._get_llm_api_key", return_value="test-key"):
            jobs.publish(
                "mojo.apps.incident.handlers.llm_agent.execute_llm_ticket_reply",
                {"ticket_id": ticket.pk, "note_id": question_note.pk},
                channel="default",
            )
            executed = th.run_pending_jobs(channel="default")

        # The LLM SHOULD have been called for non-approval replies
        assert mock_claude.call_count >= 1, \
            f"Expected LLM call for non-approval reply, got {mock_claude.call_count}"

    assert executed >= 1, f"Expected at least 1 job executed, got {executed}"

    # Ruleset should still be inactive
    ruleset.refresh_from_db()
    assert not ruleset.is_active, \
        f"RuleSet should still be inactive after a question"

    # Ticket should still be open
    ticket.refresh_from_db()
    assert ticket.status == "open", \
        f"Ticket should still be open, got {ticket.status}"


@th.django_unit_test("Action system: denial deletes ruleset and closes ticket")
def test_ticket_action_denial_deletes_rule(opts):
    from mojo.apps.incident.models import RuleSet, Rule, Ticket, TicketNote
    from mojo.apps.incident.handlers.ticket_actions import dispatch_action
    from django.contrib.auth import get_user_model
    User = get_user_model()

    RuleSet.objects.filter(category="action_deny_test").delete()
    Ticket.objects.filter(category="llm_review").delete()

    user = User.objects.filter(is_superuser=True, is_active=True).first()
    if not user:
        user = User.objects.create_user(
            username="action_deny_admin", email="action_deny@test.com",
            password="testpass123", is_superuser=True, is_active=True,
        )

    ruleset = RuleSet.objects.create(
        name="Deny test rule", category="action_deny_test",
        handler="block://?ttl=3600", is_active=False,
        metadata={"llm_proposed": True},
    )
    Rule.objects.create(
        parent=ruleset, name="Match", index=0,
        field_name="level", comparator=">=", value="9", value_type="int",
    )
    rs_pk = ruleset.pk

    ticket = Ticket.objects.create(
        title="[Rule Proposal] Deny test",
        status="open", priority=3, category="llm_review",
        metadata={"llm_linked": True, "ruleset_id": rs_pk},
    )

    response_meta = {
        "handler": "incident.rule_approval",
        "action": "deny",
        "context": {"target": {"model": "incident.RuleSet", "pk": rs_pk}},
    }
    deny_note = TicketNote.objects.create(
        parent=ticket, user=user, note="Denied",
        metadata={"action_response": response_meta},
    )

    result = dispatch_action(ticket, deny_note, response_meta)
    assert result is True, "dispatch_action should return True"

    assert not RuleSet.objects.filter(pk=rs_pk).exists(), \
        "RuleSet should be deleted after denial"

    ticket.refresh_from_db()
    assert ticket.status == "closed", \
        f"Ticket should be closed after denial, got {ticket.status}"


@th.django_unit_test("Action system: double approval is idempotent")
def test_ticket_action_double_approval(opts):
    from mojo.apps.incident.models import RuleSet, Ticket, TicketNote
    from mojo.apps.incident.handlers.ticket_actions import dispatch_action
    from django.contrib.auth import get_user_model
    User = get_user_model()

    RuleSet.objects.filter(category="action_double_test").delete()
    Ticket.objects.filter(category="llm_review").delete()

    user = User.objects.filter(is_superuser=True, is_active=True).first()
    if not user:
        user = User.objects.create_user(
            username="action_double_admin", email="action_double@test.com",
            password="testpass123", is_superuser=True, is_active=True,
        )

    ruleset = RuleSet.objects.create(
        name="Double approve test", category="action_double_test",
        handler="block://?ttl=3600", is_active=False,
        metadata={"llm_proposed": True},
    )

    ticket = Ticket.objects.create(
        title="[Rule Proposal] Double test",
        status="open", priority=3, category="llm_review",
        metadata={"llm_linked": True, "ruleset_id": ruleset.pk},
    )

    response_meta = {
        "handler": "incident.rule_approval",
        "action": "approve",
        "context": {"target": {"model": "incident.RuleSet", "pk": ruleset.pk}},
    }

    note1 = TicketNote.objects.create(
        parent=ticket, user=user, note="Approved",
        metadata={"action_response": response_meta},
    )
    dispatch_action(ticket, note1, response_meta)

    ruleset.refresh_from_db()
    assert ruleset.is_active, "RuleSet should be active after first approval"

    ticket.refresh_from_db()
    note2 = TicketNote.objects.create(
        parent=ticket, user=user, note="Approved again",
        metadata={"action_response": response_meta},
    )
    result = dispatch_action(ticket, note2, response_meta)
    assert result is True, "Second approval should still succeed (idempotent)"

    ruleset.refresh_from_db()
    assert ruleset.is_active, "RuleSet should still be active after second approval"


@th.django_unit_test("LLM tool: suggest_rule_update creates ticket with action note")
def test_suggest_rule_update_creates_ticket(opts):
    from mojo.apps.incident.models import RuleSet, Rule, Ticket, TicketNote
    from django.contrib.auth import get_user_model
    User = get_user_model()

    RuleSet.objects.filter(category="suggest_update_test").delete()
    Ticket.objects.filter(category="llm_review").delete()

    if not User.objects.filter(is_superuser=True, is_active=True).exists():
        User.objects.create_user(
            username="suggest_update_admin", email="suggest_update@test.com",
            password="testpass123", is_superuser=True, is_active=True,
        )

    ruleset = RuleSet.objects.create(
        name="Existing rule", category="suggest_update_test",
        handler="block://?ttl=3600", is_active=True,
    )
    Rule.objects.create(
        parent=ruleset, name="Level check", index=0,
        field_name="level", comparator=">=", value="8", value_type="int",
    )

    from mojo.apps.incident.handlers.llm_agent import _tool_suggest_rule_update
    result = _tool_suggest_rule_update({
        "ruleset_id": ruleset.pk,
        "proposed_rules": [
            {"field_name": "level", "comparator": ">=", "value": "7", "value_type": "int"},
            {"field_name": "source_ip", "comparator": "regex", "value": "10\\..*"},
        ],
        "reasoning": "Widen level threshold and add IP filter",
    })

    assert result["ok"] is True, "suggest_rule_update should succeed"
    assert result["ruleset_id"] == ruleset.pk, "Should reference the existing ruleset"

    ticket = Ticket.objects.get(pk=result["ticket_id"])
    assert ticket.category == "llm_review", "Ticket should be llm_review category"
    assert ticket.metadata.get("update_suggestion") is True, "Ticket should have update_suggestion flag"

    notes = TicketNote.objects.filter(parent=ticket)
    assert notes.count() >= 1, "Should have at least one note"

    action_note = notes.filter(metadata__action__isnull=False).first()
    assert action_note is not None, "Should have a note with action metadata"
    assert action_note.metadata["action"]["handler"] == "incident.rule_update", \
        "Action handler should be incident.rule_update"
    assert action_note.metadata["action"]["context"]["target"]["pk"] == ruleset.pk, \
        "Action context should reference the target ruleset"
    assert len(action_note.metadata["action"]["context"]["proposed_rules"]) == 2, \
        "Should have 2 proposed rules in context"


@th.django_unit_test("LLM tool: request_approval creates action note on ticket")
def test_request_approval_creates_action_note(opts):
    from mojo.apps.incident.models import Ticket, TicketNote
    from django.contrib.auth import get_user_model
    User = get_user_model()

    Ticket.objects.filter(category="req_approval_test").delete()

    if not User.objects.filter(is_superuser=True, is_active=True).exists():
        User.objects.create_user(
            username="req_approval_admin", email="req_approval@test.com",
            password="testpass123", is_superuser=True, is_active=True,
        )

    ticket = Ticket.objects.create(
        title="Test approval request",
        status="open", priority=5,
        category="req_approval_test",
        metadata={"llm_linked": True, "llm_enabled": True},
    )

    from mojo.apps.incident.handlers.llm_agent import _tool_request_approval
    result = _tool_request_approval({
        "ticket_id": ticket.pk,
        "handler": "incident.block_confirm",
        "label": "Block IP 10.0.0.1?",
        "context": {"ip": "10.0.0.1", "reason": "SSH brute force"},
        "reasoning": "50 failed SSH attempts in 10 minutes",
    })

    assert result["ok"] is True, "request_approval should succeed"

    notes = TicketNote.objects.filter(parent=ticket)
    action_note = notes.filter(metadata__action__isnull=False).first()
    assert action_note is not None, "Should have a note with action metadata"
    assert action_note.metadata["action"]["handler"] == "incident.block_confirm", \
        "Action handler should be incident.block_confirm"
    assert action_note.metadata["action"]["context"]["ip"] == "10.0.0.1", \
        "Action context should contain the IP"


@th.django_unit_test("LLM agent: active rules included in incident prompt")
def test_active_rules_in_prompt(opts):
    from mojo.apps.incident.models import Event, RuleSet, Rule

    RuleSet.objects.filter(category="prompt_ctx_test").delete()
    Event.objects.filter(category="prompt_ctx_test").delete()

    ruleset = RuleSet.objects.create(
        name="Existing blocker", category="prompt_ctx_test",
        handler="block://?ttl=3600", is_active=True,
    )
    Rule.objects.create(
        parent=ruleset, name="Level", index=0,
        field_name="level", comparator=">=", value="9", value_type="int",
    )

    event = Event.objects.create(
        category="prompt_ctx_test", level=8,
        title="Prompt context test", source_ip="10.0.0.1",
    )

    from mojo.apps.incident.handlers.llm_agent import _build_incident_message
    message = _build_incident_message(event, None)

    assert "Active Rules for This Category" in message, \
        "Prompt should contain active rules section"
    assert "Existing blocker" in message, \
        "Prompt should reference the active ruleset name"
    assert "level >= 9" in message, \
        "Prompt should show rule conditions"


@th.django_unit_test("LLM agent: create_rule includes action block on proposal note")
def test_create_rule_includes_action_block(opts):
    from mojo.apps.incident.models import RuleSet, Rule, Ticket, TicketNote
    from django.contrib.auth import get_user_model
    User = get_user_model()

    RuleSet.objects.filter(category="action_block_test").delete()
    Ticket.objects.filter(category="llm_review").delete()

    if not User.objects.filter(is_superuser=True, is_active=True).exists():
        User.objects.create_user(
            username="action_block_admin", email="action_block@test.com",
            password="testpass123", is_superuser=True, is_active=True,
        )

    from mojo.apps.incident.handlers.llm_agent import _tool_create_rule
    result = _tool_create_rule({
        "name": "Action block test rule",
        "category": "action_block_test",
        "handler": "block://?ttl=3600",
        "rules": [
            {"name": "Level", "field": "level", "comparator": ">=", "value": "9", "value_type": "int"},
        ],
        "reasoning": "Testing action block creation",
        "bundle_by": 4,
        "bundle_minutes": 30,
    })

    assert result["ok"] is True, "create_rule should succeed"

    ticket = Ticket.objects.get(pk=result["ticket_id"])
    assert ticket.metadata.get("requires_approval") is True, \
        "Ticket should have requires_approval metadata"
    assert ticket.metadata.get("llm_enabled") is True, \
        "Ticket should have llm_enabled metadata"

    notes = TicketNote.objects.filter(parent=ticket)
    action_note = notes.filter(metadata__action__isnull=False).first()
    assert action_note is not None, "Should have a note with action metadata"
    assert action_note.metadata["action"]["handler"] == "incident.rule_approval", \
        "Action handler should be incident.rule_approval"
    assert action_note.metadata["action"]["context"]["target"]["model"] == "incident.RuleSet", \
        "Action context should reference RuleSet model"
    assert action_note.metadata["action"]["context"]["target"]["pk"] == result["ruleset_id"], \
        "Action context should reference the created ruleset"

    ruleset = RuleSet.objects.get(pk=result["ruleset_id"])
    assert not ruleset.is_active, "RuleSet should be inactive (pending approval)"


@th.django_unit_test("Action system: rule_update handler replaces rules on approval")
def test_rule_update_approval(opts):
    from mojo.apps.incident.models import RuleSet, Rule, Ticket, TicketNote
    from mojo.apps.incident.handlers.ticket_actions import dispatch_action
    from django.contrib.auth import get_user_model
    User = get_user_model()

    RuleSet.objects.filter(category="rule_update_test").delete()
    Ticket.objects.filter(category="llm_review").delete()

    user = User.objects.filter(is_superuser=True, is_active=True).first()
    if not user:
        user = User.objects.create_user(
            username="rule_update_admin", email="rule_update@test.com",
            password="testpass123", is_superuser=True, is_active=True,
        )

    ruleset = RuleSet.objects.create(
        name="Updatable rule", category="rule_update_test",
        handler="block://?ttl=3600", is_active=True,
    )
    Rule.objects.create(
        parent=ruleset, name="Old condition", index=0,
        field_name="level", comparator=">=", value="9", value_type="int",
    )

    ticket = Ticket.objects.create(
        title="[Rule Update] Updatable rule",
        status="open", priority=3, category="llm_review",
        metadata={"llm_linked": True, "ruleset_id": ruleset.pk, "update_suggestion": True},
    )

    proposed_rules = [
        {"name": "Wider level", "field_name": "level", "comparator": ">=", "value": "7", "value_type": "int"},
        {"name": "IP filter", "field_name": "source_ip", "comparator": "regex", "value": "10\\..*"},
    ]

    response_meta = {
        "handler": "incident.rule_update",
        "action": "approve",
        "context": {
            "target": {"model": "incident.RuleSet", "pk": ruleset.pk},
            "proposed_rules": proposed_rules,
        },
    }

    note = TicketNote.objects.create(
        parent=ticket, user=user, note="Approved update",
        metadata={"action_response": response_meta},
    )

    result = dispatch_action(ticket, note, response_meta)
    assert result is True, "dispatch_action should succeed"

    new_rules = list(ruleset.rules.all().order_by("index"))
    assert len(new_rules) == 2, f"Expected 2 rules after update, got {len(new_rules)}"
    assert new_rules[0].field_name == "level", f"First rule should match level, got {new_rules[0].field_name}"
    assert new_rules[0].value == "7", f"First rule value should be 7, got {new_rules[0].value}"
    assert new_rules[1].field_name == "source_ip", f"Second rule should match source_ip, got {new_rules[1].field_name}"

    ticket.refresh_from_db()
    assert ticket.status == "resolved", f"Ticket should be resolved, got {ticket.status}"
