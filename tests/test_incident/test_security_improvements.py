"""
Tests for security system improvements (Phases 1-3).

Covers: incident history, incident metrics, handler dispatch,
OSSEC secret validation, LLM handler wiring, ticket LLM hooks.
"""
from testit import helpers as th
from objict import objict


# ---------------------------------------------------------------------------
# Incident History
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_incident_add_history(opts):
    """Incident.add_history() should create IncidentHistory records."""
    from mojo.apps.incident.models import Incident, IncidentHistory

    incident = Incident.objects.create(
        priority=5, state=0, status="new",
        category="test:history", scope="test",
        title="History test incident",
    )

    incident.add_history("created", note="Test incident created")
    incident.add_history("status_changed", note="Status changed to investigating")

    history = IncidentHistory.objects.filter(parent=incident).order_by("created")
    assert history.count() == 2, f"Expected 2 history entries, got {history.count()}"
    assert history[0].kind == "created", f"First entry kind should be 'created', got {history[0].kind}"
    assert history[1].kind == "status_changed", f"Second entry kind should be 'status_changed', got {history[1].kind}"
    assert history[0].priority == 5, f"History should snapshot priority, got {history[0].priority}"

    # Cleanup
    incident.delete()


@th.django_unit_test()
def test_incident_on_rest_saved_tracks_status(opts):
    """on_rest_saved should create history for status changes."""
    from mojo.apps.incident.models import Incident, IncidentHistory

    incident = Incident.objects.create(
        priority=5, state=0, status="open",
        category="test:rest_saved", scope="test",
        title="REST saved test",
    )

    # Simulate a REST save that changed status from "new" to "open"
    # active_request is a ContextVar property — will be None in test context (that's fine)
    incident.on_rest_saved({"status": "new"}, created=False)

    history = IncidentHistory.objects.filter(parent=incident, kind="status_changed")
    assert history.count() == 1, f"Expected 1 status_changed entry, got {history.count()}"
    assert "new" in history[0].note, f"Note should mention old status: {history[0].note}"
    assert "open" in history[0].note, f"Note should mention new status: {history[0].note}"

    # Cleanup
    incident.delete()


@th.django_unit_test()
def test_incident_on_rest_saved_ignores_created(opts):
    """on_rest_saved should skip history for newly created incidents."""
    from mojo.apps.incident.models import Incident, IncidentHistory

    incident = Incident.objects.create(
        priority=5, state=0, status="new",
        category="test:rest_saved_new", scope="test",
        title="REST saved new test",
    )

    incident.on_rest_saved({"status": "new"}, created=True)

    history = IncidentHistory.objects.filter(parent=incident)
    assert history.count() == 0, "on_rest_saved should not create history for created=True"

    # Cleanup
    incident.delete()


# ---------------------------------------------------------------------------
# Event → Incident creation with history
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_event_publish_creates_incident_with_history(opts):
    """Events above INCIDENT_LEVEL_THRESHOLD should create incidents with history."""
    from mojo.apps.incident.models import Event, Incident, IncidentHistory
    from mojo.apps.incident.models.rule import RuleSet

    cat = "test:publish:history"
    Event.objects.filter(category=cat).delete()
    Incident.objects.filter(category=cat).delete()
    RuleSet.objects.filter(category=cat).delete()

    # Create event above threshold (default 7)
    event = Event(
        category=cat, level=8, scope="test",
        title="High level test", details="Testing publish",
        source_ip="10.99.99.99",
    )
    event.sync_metadata()
    event.save()
    event.publish()

    # Should have created an incident
    incident = Incident.objects.filter(category=cat).first()
    assert incident is not None, "Incident should be created for level >= threshold"
    assert incident.priority == 8, f"Incident priority should match event level, got {incident.priority}"

    # Should have history entry
    history = IncidentHistory.objects.filter(parent=incident, kind="created")
    assert history.count() == 1, f"Expected 1 'created' history entry, got {history.count()}"

    # Cleanup
    incident.delete()


# ---------------------------------------------------------------------------
# Handler dispatch (async)
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_run_handler_returns_false_when_no_handler(opts):
    """RuleSet with no handler should return False."""
    from mojo.apps.incident.models.rule import RuleSet
    from mojo.apps.incident.models import Event

    ruleset = RuleSet(category="test:nohandler", handler=None)
    event = Event(category="test:nohandler", level=1)

    result = ruleset.run_handler(event)
    assert result is False, "run_handler with no handler should return False"


@th.django_unit_test()
def test_handler_map_has_all_types(opts):
    """HANDLER_MAP should include all supported handler types."""
    from mojo.apps.incident.handlers.event_handlers import HANDLER_MAP

    expected = {"job", "email", "sms", "notify", "block", "ticket", "llm"}
    actual = set(HANDLER_MAP.keys())
    assert expected == actual, f"HANDLER_MAP keys mismatch: expected {expected}, got {actual}"


@th.django_unit_test()
def test_handler_split_regex_preserves_targets(opts):
    """Handler chain splitting should preserve comma-separated targets within a single handler."""
    import re

    handler = "email://perm@manage_security,protected@incident_emails?template=critical,block://?ttl=3600"
    specs = re.split(r',(?=(?:job|email|sms|notify|ticket|block|llm)://)', handler.strip())

    assert len(specs) == 2, f"Should split into 2 specs, got {len(specs)}: {specs}"
    assert "perm@manage_security,protected@incident_emails" in specs[0], f"First spec should preserve targets: {specs[0]}"
    assert specs[1] == "block://?ttl=3600", f"Second spec should be block handler: {specs[1]}"


# ---------------------------------------------------------------------------
# OSSEC Secret
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_ossec_check_secret_none_passes(opts):
    """When OSSEC_SECRET is None, all requests should pass."""
    import sys
    import mojo.apps.incident.rest.ossec
    rest_ossec = sys.modules['mojo.apps.incident.rest.ossec']

    # Save and override
    original = rest_ossec.OSSEC_SECRET
    rest_ossec.OSSEC_SECRET = None

    request = objict(META={}, ip="10.0.0.1")
    result = rest_ossec._check_ossec_secret(request)
    assert result is None, "No secret configured should pass all requests"

    rest_ossec.OSSEC_SECRET = original


@th.django_unit_test()
def test_ossec_check_secret_valid_passes(opts):
    """When OSSEC_SECRET matches the header, request should pass."""
    import sys
    import mojo.apps.incident.rest.ossec
    rest_ossec = sys.modules['mojo.apps.incident.rest.ossec']

    original = rest_ossec.OSSEC_SECRET
    rest_ossec.OSSEC_SECRET = "test-secret-123"

    request = objict(META={"HTTP_X_OSSEC_SECRET": "test-secret-123"}, ip="10.0.0.1")
    result = rest_ossec._check_ossec_secret(request)
    assert result is None, "Valid secret should pass"

    rest_ossec.OSSEC_SECRET = original


@th.django_unit_test()
def test_ossec_check_secret_invalid_rejects(opts):
    """When OSSEC_SECRET doesn't match, request should be rejected with 403."""
    import sys
    import mojo.apps.incident.rest.ossec
    rest_ossec = sys.modules['mojo.apps.incident.rest.ossec']

    original = rest_ossec.OSSEC_SECRET
    rest_ossec.OSSEC_SECRET = "test-secret-123"

    request = objict(META={"HTTP_X_OSSEC_SECRET": "wrong-secret"}, ip="10.0.0.1")
    result = rest_ossec._check_ossec_secret(request)
    assert result is not None, "Invalid secret should return a response"
    assert result.status_code == 403, f"Should return 403, got {result.status_code}"

    rest_ossec.OSSEC_SECRET = original


@th.django_unit_test()
def test_ossec_check_secret_missing_header_rejects(opts):
    """When OSSEC_SECRET is set but header is missing, request should be rejected."""
    import sys
    import mojo.apps.incident.rest.ossec
    rest_ossec = sys.modules['mojo.apps.incident.rest.ossec']

    original = rest_ossec.OSSEC_SECRET
    rest_ossec.OSSEC_SECRET = "test-secret-123"

    request = objict(META={}, ip="10.0.0.1")
    result = rest_ossec._check_ossec_secret(request)
    assert result is not None, "Missing header should return a response"
    assert result.status_code == 403, f"Should return 403, got {result.status_code}"

    rest_ossec.OSSEC_SECRET = original


# ---------------------------------------------------------------------------
# User resolution
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_resolve_users_empty_targets(opts):
    """Empty targets should return empty list."""
    from mojo.apps.incident.handlers.event_handlers import _resolve_users

    users = _resolve_users([])
    assert users == [], f"Empty targets should return empty list, got {users}"


@th.django_unit_test()
def test_resolve_users_unknown_username(opts):
    """Unknown username should be skipped."""
    from mojo.apps.incident.handlers.event_handlers import _resolve_users

    users = _resolve_users(["nonexistent_user_xyz_12345"])
    assert users == [], f"Unknown username should return empty list, got {users}"


@th.django_unit_test()
def test_resolve_users_deduplicates(opts):
    """Same user referenced twice should only appear once."""
    from mojo.apps.incident.handlers.event_handlers import _resolve_users
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.filter(is_active=True).first()
    if not user:
        return  # Skip if no users in test DB

    users = _resolve_users([user.username, user.username])
    assert len(users) == 1, f"Duplicate username should be deduped, got {len(users)} users"


# ---------------------------------------------------------------------------
# Ticket LLM hook
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_ticket_note_is_llm_note_detection(opts):
    """_is_llm_note should detect LLM Agent prefix."""
    from mojo.apps.incident.models.ticket import TicketNote

    note = TicketNote()
    note.note = "[LLM Agent] This is an LLM response"
    assert note._is_llm_note() is True, "Should detect LLM Agent prefix"

    note.note = "Human wrote this"
    assert note._is_llm_note() is False, "Should not flag human notes"

    note.note = None
    assert note._is_llm_note() is False, "Should handle None note"


@th.django_unit_test()
def test_ticket_is_llm_ticket_detection(opts):
    """_is_llm_ticket should check parent ticket metadata."""
    from mojo.apps.incident.models.ticket import Ticket, TicketNote

    # Create a ticket with llm_linked metadata
    ticket = Ticket.objects.create(
        title="LLM test ticket",
        metadata={"llm_linked": True},
    )

    note = TicketNote()
    note.parent = ticket
    assert note._is_llm_ticket() is True, "Should detect llm_linked ticket"

    # Non-LLM ticket
    ticket2 = Ticket.objects.create(
        title="Normal ticket",
        metadata={},
    )
    note2 = TicketNote()
    note2.parent = ticket2
    assert note2._is_llm_ticket() is False, "Should not flag non-LLM ticket"

    # Cleanup
    ticket.delete()
    ticket2.delete()


# ---------------------------------------------------------------------------
# LLM Agent tools (unit tests for tool implementations)
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_llm_tool_query_events(opts):
    """query_events tool should return events matching criteria."""
    from mojo.apps.incident.models import Event
    from mojo.apps.incident.handlers.llm_agent import _tool_query_events

    cat = "test:llm:query_events"
    Event.objects.filter(category=cat).delete()

    Event.objects.create(category=cat, level=5, source_ip="10.0.0.1", title="test1")
    Event.objects.create(category=cat, level=5, source_ip="10.0.0.2", title="test2")

    results = _tool_query_events({"category": cat, "minutes": 5})
    assert len(results) == 2, f"Should return 2 events, got {len(results)}"

    results_filtered = _tool_query_events({"category": cat, "source_ip": "10.0.0.1", "minutes": 5})
    assert len(results_filtered) == 1, f"Should return 1 event for filtered IP, got {len(results_filtered)}"

    # Cleanup
    Event.objects.filter(category=cat).delete()


@th.django_unit_test()
def test_llm_tool_update_incident(opts):
    """update_incident tool should change status and create history."""
    from mojo.apps.incident.models import Incident, IncidentHistory
    from mojo.apps.incident.handlers.llm_agent import _tool_update_incident

    incident = Incident.objects.create(
        priority=5, state=0, status="new",
        category="test:llm:update", scope="test",
        title="LLM update test",
    )

    result = _tool_update_incident({
        "incident_id": incident.pk,
        "status": "investigating",
        "note": "Starting investigation",
    })

    assert result["ok"] is True, "Tool should return ok=True"

    incident.refresh_from_db()
    assert incident.status == "investigating", f"Status should be investigating, got {incident.status}"
    assert incident.metadata.get("llm_assessment") is not None, "Should store llm_assessment in metadata"

    history = IncidentHistory.objects.filter(parent=incident, kind="status_changed")
    assert history.count() == 1, f"Should create 1 status_changed history, got {history.count()}"
    assert "[LLM Agent]" in history[0].note, f"History note should have LLM prefix: {history[0].note}"

    # Cleanup
    incident.delete()


@th.django_unit_test()
def test_llm_tool_add_note(opts):
    """add_note tool should create IncidentHistory entry."""
    from mojo.apps.incident.models import Incident, IncidentHistory
    from mojo.apps.incident.handlers.llm_agent import _tool_add_note

    incident = Incident.objects.create(
        priority=3, state=0, status="new",
        category="test:llm:note", scope="test",
        title="LLM note test",
    )

    result = _tool_add_note({
        "incident_id": incident.pk,
        "note": "This is noise from the deploy process",
    })

    assert result["ok"] is True, "Tool should return ok=True"

    history = IncidentHistory.objects.filter(parent=incident, kind="handler:llm")
    assert history.count() == 1, f"Should create 1 handler:llm history, got {history.count()}"
    assert "[LLM Agent]" in history[0].note, f"Note should have LLM prefix: {history[0].note}"

    # Cleanup
    incident.delete()


@th.django_unit_test()
def test_llm_tool_update_rule_memory(opts):
    """update_rule_memory tool should append to agent_memory."""
    from mojo.apps.incident.models.rule import RuleSet
    from mojo.apps.incident.handlers.llm_agent import _tool_update_rule_memory

    ruleset = RuleSet.objects.create(
        category="test:llm:memory",
        name="Memory test rule",
        metadata={},
    )

    _tool_update_rule_memory({"ruleset_id": ruleset.pk, "memory": "OSSEC 5710 is always false positive from deploys"})
    ruleset.refresh_from_db()
    assert "5710" in ruleset.metadata.get("agent_memory", ""), "First memory should be stored"

    _tool_update_rule_memory({"ruleset_id": ruleset.pk, "memory": "SSH from 10.x is internal"})
    ruleset.refresh_from_db()
    memory = ruleset.metadata.get("agent_memory", "")
    assert "5710" in memory, "First memory should persist"
    assert "SSH" in memory, "Second memory should be appended"

    # Cleanup
    ruleset.delete()


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_incident_models_use_security_perms(opts):
    """All incident app models should use view_security/manage_security."""
    from mojo.apps.incident.models import Incident, Event, IncidentHistory
    from mojo.apps.incident.models.rule import RuleSet, Rule
    from mojo.apps.incident.models.ticket import Ticket, TicketNote

    models = [Incident, Event, IncidentHistory, RuleSet, Rule, Ticket, TicketNote]

    for model in models:
        meta = model.RestMeta
        name = model.__name__

        view_perms = getattr(meta, "VIEW_PERMS", [])
        assert "view_security" in view_perms, f"{name}.VIEW_PERMS should contain 'view_security', got {view_perms}"

        save_perms = getattr(meta, "SAVE_PERMS", None)
        if save_perms is not None:
            assert "manage_security" in save_perms, f"{name}.SAVE_PERMS should contain 'manage_security', got {save_perms}"
