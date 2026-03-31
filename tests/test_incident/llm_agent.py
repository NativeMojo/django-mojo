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
        metadata={"llm_linked": True},
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
