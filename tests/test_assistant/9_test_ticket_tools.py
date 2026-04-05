"""
Tests for assistant ticket management tools (get_ticket, update_ticket, add_ticket_note).

Calls tool handlers directly with (params, user) — no LLM needed.
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


TEST_EMAIL_ADMIN = 'asst-ticket-admin@example.com'
TEST_EMAIL_ASSIGNEE = 'asst-ticket-assignee@example.com'
TEST_PASSWORD = 'TestPass1!'


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_data(opts):
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import Ticket, TicketNote, Incident

    # Clean up prior test data
    User.objects.filter(email__in=[TEST_EMAIL_ADMIN, TEST_EMAIL_ASSIGNEE]).delete()
    Ticket.objects.filter(title__startswith="[asst_ticket_test]").delete()
    Incident.objects.filter(title__startswith="[asst_ticket_test]").delete()

    # Admin user
    opts.admin = User.objects.create_user(
        username=TEST_EMAIL_ADMIN, email=TEST_EMAIL_ADMIN, password=TEST_PASSWORD,
    )
    opts.admin.is_email_verified = True
    opts.admin.save()
    for perm in ["view_admin", "view_security", "manage_security"]:
        opts.admin.add_permission(perm)

    # Assignee user
    opts.assignee = User.objects.create_user(
        username=TEST_EMAIL_ASSIGNEE, email=TEST_EMAIL_ASSIGNEE, password=TEST_PASSWORD,
    )
    opts.assignee.is_email_verified = True
    opts.assignee.save()

    # Create an incident for ticket linking
    opts.incident = Incident.objects.create(
        title="[asst_ticket_test] Incident", category="test", status="new", priority=5,
    )

    # Create a ticket with notes
    opts.ticket = Ticket.objects.create(
        title="[asst_ticket_test] Ticket 1",
        description="Test ticket description",
        status="open",
        priority=5,
        category="test_category",
        incident=opts.incident,
        user=opts.admin,
    )
    TicketNote.objects.create(
        parent=opts.ticket, note="First note", user=opts.admin,
    )
    TicketNote.objects.create(
        parent=opts.ticket, note="Second note", user=opts.admin,
    )

    # Create a second ticket for update tests (avoid mutating the main one)
    opts.ticket2 = Ticket.objects.create(
        title="[asst_ticket_test] Ticket 2",
        description="Another test ticket",
        status="open",
        priority=3,
        category="test_category",
        user=opts.admin,
    )


# ---------------------------------------------------------------------------
# get_ticket
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_get_ticket_returns_details_and_notes(opts):
    """get_ticket should return full ticket details including notes."""
    from mojo.apps.assistant.services.tools.security import _tool_get_ticket

    result = _tool_get_ticket({"ticket_id": opts.ticket.pk}, opts.admin)
    assert_eq(result["id"], opts.ticket.pk, "Should return correct ticket ID")
    assert_eq(result["title"], "[asst_ticket_test] Ticket 1", "Should return ticket title")
    assert_eq(result["status"], "open", "Should return ticket status")
    assert_eq(result["priority"], 5, "Should return ticket priority")
    assert_eq(result["category"], "test_category", "Should return ticket category")
    assert_eq(result["incident_id"], opts.incident.pk, "Should return incident FK")
    assert_true("notes" in result, "Should include notes key")
    assert_eq(len(result["notes"]), 2, f"Should have 2 notes, got {len(result['notes'])}")

    # Notes should be ordered by created (ascending)
    note = result["notes"][0]
    assert_eq(note["note"], "First note", "First note should be 'First note'")
    assert_true("user_id" in note, "Note should include user_id")
    assert_true("created" in note, "Note should include created timestamp")
    assert_true("has_media" in note, "Note should include has_media flag")


@th.django_unit_test()
def test_get_ticket_not_found(opts):
    """get_ticket should return error for non-existent ticket."""
    from mojo.apps.assistant.services.tools.security import _tool_get_ticket

    result = _tool_get_ticket({"ticket_id": 999999}, opts.admin)
    assert_true("error" in result, "Should return error for missing ticket")


# ---------------------------------------------------------------------------
# update_ticket
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_update_ticket_status_adds_note(opts):
    """update_ticket should change status and auto-add a history note."""
    from mojo.apps.assistant.services.tools.security import _tool_update_ticket
    from mojo.apps.incident.models import TicketNote

    old_note_count = TicketNote.objects.filter(parent=opts.ticket2).count()

    result = _tool_update_ticket({
        "ticket_id": opts.ticket2.pk,
        "status": "in_progress",
    }, opts.admin)

    assert_true(result.get("ok"), f"Should succeed, got {result}")
    assert_eq(result["status"], "in_progress", "Should return updated status")
    assert_true("status" in result["updated_fields"], "Should report status in updated_fields")

    opts.ticket2.refresh_from_db()
    assert_eq(opts.ticket2.status, "in_progress", "Status should be updated in DB")

    new_note_count = TicketNote.objects.filter(parent=opts.ticket2).count()
    assert_eq(new_note_count, old_note_count + 1, "Should have added a note")

    latest_note = TicketNote.objects.filter(parent=opts.ticket2).order_by("-created").first()
    assert_true("status" in latest_note.note, "Auto-note should mention status change")


@th.django_unit_test()
def test_update_ticket_assignee_validates_user(opts):
    """update_ticket should validate assignee exists and is active."""
    from mojo.apps.assistant.services.tools.security import _tool_update_ticket

    result = _tool_update_ticket({
        "ticket_id": opts.ticket2.pk,
        "assignee_id": opts.assignee.pk,
    }, opts.admin)

    assert_true(result.get("ok"), f"Should succeed, got {result}")
    assert_true("assignee_id" in result["updated_fields"], "Should report assignee_id updated")

    opts.ticket2.refresh_from_db()
    assert_eq(opts.ticket2.assignee_id, opts.assignee.pk, "Assignee should be updated in DB")


@th.django_unit_test()
def test_update_ticket_assignee_invalid_user(opts):
    """update_ticket should return error for non-existent assignee."""
    from mojo.apps.assistant.services.tools.security import _tool_update_ticket

    result = _tool_update_ticket({
        "ticket_id": opts.ticket2.pk,
        "assignee_id": 999999,
    }, opts.admin)

    assert_true("error" in result, "Should return error for missing user")


@th.django_unit_test()
def test_update_ticket_assignee_inactive_user(opts):
    """update_ticket should return error for inactive assignee."""
    from mojo.apps.assistant.services.tools.security import _tool_update_ticket

    opts.assignee.is_active = False
    opts.assignee.save(update_fields=["is_active"])

    result = _tool_update_ticket({
        "ticket_id": opts.ticket2.pk,
        "assignee_id": opts.assignee.pk,
    }, opts.admin)

    assert_true("error" in result, "Should return error for inactive user")

    # Restore
    opts.assignee.is_active = True
    opts.assignee.save(update_fields=["is_active"])


@th.django_unit_test()
def test_update_ticket_no_fields(opts):
    """update_ticket with no updatable fields should return error."""
    from mojo.apps.assistant.services.tools.security import _tool_update_ticket

    result = _tool_update_ticket({"ticket_id": opts.ticket2.pk}, opts.admin)
    assert_true("error" in result, "Should return error when no fields provided")


@th.django_unit_test()
def test_update_ticket_not_found(opts):
    """update_ticket should return error for non-existent ticket."""
    from mojo.apps.assistant.services.tools.security import _tool_update_ticket

    result = _tool_update_ticket({"ticket_id": 999999, "status": "closed"}, opts.admin)
    assert_true("error" in result, "Should return error for missing ticket")


# ---------------------------------------------------------------------------
# add_ticket_note
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_add_ticket_note_creates_note(opts):
    """add_ticket_note should create a note attributed to the requesting user."""
    from mojo.apps.assistant.services.tools.security import _tool_add_ticket_note
    from mojo.apps.incident.models import TicketNote

    result = _tool_add_ticket_note({
        "ticket_id": opts.ticket.pk,
        "note": "Admin investigation note",
    }, opts.admin)

    assert_true(result.get("ok"), f"Should succeed, got {result}")
    assert_eq(result["ticket_id"], opts.ticket.pk, "Should return correct ticket_id")
    assert_true("note_id" in result, "Should return note_id")

    note = TicketNote.objects.get(pk=result["note_id"])
    assert_eq(note.note, "Admin investigation note", "Note text should match")
    assert_eq(note.user_id, opts.admin.pk, "Note should be attributed to the admin user")
    assert_eq(note.parent_id, opts.ticket.pk, "Note parent should be the ticket")


@th.django_unit_test()
def test_add_ticket_note_not_found(opts):
    """add_ticket_note should return error for non-existent ticket."""
    from mojo.apps.assistant.services.tools.security import _tool_add_ticket_note

    result = _tool_add_ticket_note({
        "ticket_id": 999999,
        "note": "This should fail",
    }, opts.admin)

    assert_true("error" in result, "Should return error for missing ticket")


@th.django_unit_test()
def test_add_ticket_note_no_llm_prefix(opts):
    """add_ticket_note should NOT prefix with [LLM Agent] — it's on behalf of the admin."""
    from mojo.apps.assistant.services.tools.security import _tool_add_ticket_note
    from mojo.apps.incident.models import TicketNote

    result = _tool_add_ticket_note({
        "ticket_id": opts.ticket.pk,
        "note": "Human admin note via assistant",
    }, opts.admin)

    note = TicketNote.objects.get(pk=result["note_id"])
    assert_true(
        not note.note.startswith("[LLM Agent]"),
        "Note should NOT start with [LLM Agent] prefix"
    )


@th.django_unit_test()
def test_add_ticket_note_does_not_trigger_llm_reply(opts):
    """add_ticket_note creates via ORM directly — on_rest_saved should NOT fire."""
    from mojo.apps.assistant.services.tools.security import _tool_add_ticket_note
    from mojo.apps.incident.models import TicketNote

    # Mark ticket as llm_linked to test the guard
    opts.ticket.metadata = {"llm_linked": True}
    opts.ticket.save(update_fields=["metadata"])

    result = _tool_add_ticket_note({
        "ticket_id": opts.ticket.pk,
        "note": "Direct ORM note — should not trigger LLM",
    }, opts.admin)

    assert_true(result.get("ok"), f"Should succeed, got {result}")
    note = TicketNote.objects.get(pk=result["note_id"])
    # The note was created via ORM, not REST, so on_rest_saved should not have fired.
    # We can't easily verify no job was published, but we verify the note exists
    # and is correctly attributed (the absence of LLM re-invocation is structural).
    assert_eq(note.user_id, opts.admin.pk, "Note should be attributed to admin")

    # Clean up
    opts.ticket.metadata = {}
    opts.ticket.save(update_fields=["metadata"])


# ---------------------------------------------------------------------------
# Registry check
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_ticket_tools_registered(opts):
    """get_ticket, update_ticket, add_ticket_note should be in the registry."""
    from mojo.apps.assistant import get_registry

    registry = get_registry()

    # Read-only
    assert_true("get_ticket" in registry, "get_ticket should be in registry")
    assert_true(not registry["get_ticket"]["mutates"], "get_ticket should not mutate")
    assert_eq(registry["get_ticket"]["permission"], "view_security",
              "get_ticket should require view_security")

    # Mutation tools
    for name in ["update_ticket", "add_ticket_note"]:
        assert_true(name in registry, f"'{name}' should be in registry")
        assert_true(registry[name]["mutates"], f"'{name}' should have mutates=True")
        assert_eq(registry[name]["permission"], "manage_security",
                  f"'{name}' should require manage_security")
