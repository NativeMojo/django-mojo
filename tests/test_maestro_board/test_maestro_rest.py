"""
REST end-to-end tests for the maestro board integration (DM-040): board CRUD
permissions, fail-closed registration, the push_to_board ticket action, the
outbound sync triggers, the signed webhook receiver, and the rules handler
board= param.

Outbound sync is asserted at the enqueue boundary (jobs.models.Job rows) —
the HTTP layer itself is covered by test_maestro_service with mocks.
"""
from testit import helpers as th

PREFIX = "[maestro_rest]"
TEST_KEY = "rest" + "k" * 44
PWORD = "maestro##mojo77"


@th.django_unit_setup()
def setup_maestro_rest(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.incident.models import MaestroBoard, Ticket

    Setting.set("MAESTRO_CALLBACK_BASE", "http://client.example.test")
    MaestroBoard.objects.filter(name__startswith=PREFIX).delete()
    Ticket.objects.filter(title__startswith=PREFIX).delete()

    admin = User.objects.filter(username="maestro_admin").last()
    if admin is None:
        admin = User(username="maestro_admin", email="maestro_admin@example.com")
        admin.save()
    admin.is_email_verified = True
    admin.save_password(PWORD)
    admin.remove_all_permissions()
    admin.add_permission("view_security")
    admin.add_permission("manage_security")

    viewer = User.objects.filter(username="maestro_viewer").last()
    if viewer is None:
        viewer = User(username="maestro_viewer", email="maestro_viewer@example.com")
        viewer.save()
    viewer.is_email_verified = True
    viewer.save_password(PWORD)
    viewer.remove_all_permissions()
    viewer.add_permission("view_security")

    opts.admin_name = "maestro_admin"
    opts.viewer_name = "maestro_viewer"


def _make_board(**kwargs):
    from mojo.apps.incident.models import MaestroBoard
    defaults = dict(
        name=f"{PREFIX} board",
        api_url="https://maestro.example.test",
        remote_board_id=42,
        is_active=True,
    )
    defaults.update(kwargs)
    board = MaestroBoard(**defaults)
    board.set_secret("link_key", TEST_KEY)
    board.save()
    return board


def _make_ticket(**kwargs):
    from mojo.apps.incident.models import Ticket
    defaults = dict(title=f"{PREFIX} ticket", description="rest test", status="open")
    defaults.update(kwargs)
    return Ticket.objects.create(**defaults)


def _make_link(board, ticket, remote_item_id=9000):
    from mojo.apps.incident.models import MaestroBoardLink
    return MaestroBoardLink.objects.create(
        ticket=ticket, maestro_board=board, remote_item_id=remote_item_id)


def _clear_maestro_jobs():
    from mojo.apps.jobs.models import Job
    Job.objects.filter(func__startswith="mojo.apps.incident.asyncjobs.maestro").delete()


def _maestro_jobs(func_suffix):
    from mojo.apps.jobs.models import Job
    return list(Job.objects.filter(
        func=f"mojo.apps.incident.asyncjobs.{func_suffix}").order_by("-created"))


@th.django_unit_test()
def test_board_crud_requires_manage_security(opts):
    board = _make_board(name=f"{PREFIX} perms")

    assert opts.client.login(opts.viewer_name, PWORD), "viewer login failed"
    resp = opts.client.get("/api/incident/maestro/board")
    assert resp.status_code in (401, 403), (
        f"view_security-only user must be denied board list, got {resp.status_code}: {resp.body}")
    resp = opts.client.post(f"/api/incident/maestro/board/{board.pk}", json={"name": "hax"})
    assert resp.status_code in (401, 403), (
        f"view_security-only user must be denied board save, got {resp.status_code}: {resp.body}")

    assert opts.client.login(opts.admin_name, PWORD), "admin login failed"
    resp = opts.client.get(f"/api/incident/maestro/board/{board.pk}")
    assert resp.status_code == 200, f"manage_security must read boards, got {resp.status_code}: {resp.body}"
    data = resp.response["data"]
    assert "mojo_secrets" not in data, "encrypted secrets must never serialize"
    assert TEST_KEY not in str(resp.body), "the raw link key must never appear in any response"


@th.django_unit_test()
def test_board_create_fail_closed(opts):
    from mojo.apps.incident.models import MaestroBoard

    assert opts.client.login(opts.admin_name, PWORD), "admin login failed"

    # Malformed paste URL -> 400, nothing persisted
    resp = opts.client.post("/api/incident/maestro/board",
                            json={"name": f"{PREFIX} badpaste", "paste_url": "not-a-url"})
    assert resp.status_code == 400, f"malformed paste must 400, got {resp.status_code}: {resp.body}"

    # Unreachable maestro endpoint -> registration fails -> 400, nothing persisted
    resp = opts.client.post("/api/incident/maestro/board", json={
        "name": f"{PREFIX} badpaste",
        "paste_url": "http://127.0.0.1:1/api/boards/link/deadbeefdeadbeef"})
    assert resp.status_code == 400, f"failed registration must 400, got {resp.status_code}: {resp.body}"

    # No paste_url at all -> 400
    resp = opts.client.post("/api/incident/maestro/board", json={"name": f"{PREFIX} badpaste"})
    assert resp.status_code == 400, f"create without paste_url must 400, got {resp.status_code}: {resp.body}"

    assert not MaestroBoard.objects.filter(name=f"{PREFIX} badpaste").exists(), (
        "a board whose registration failed must never persist (fail-closed)")


@th.django_unit_test()
def test_push_to_board_action_enqueues_job(opts):
    from mojo.apps.account.models import Group

    board = _make_board(name=f"{PREFIX} pushable")
    ticket = _make_ticket(title=f"{PREFIX} push me")
    _clear_maestro_jobs()

    assert opts.client.login(opts.admin_name, PWORD), "admin login failed"
    resp = opts.client.post(f"/api/incident/ticket/{ticket.pk}", json={"push_to_board": board.pk})
    assert resp.status_code == 200, f"push_to_board action failed: {resp.status_code}: {resp.body}"

    jobs = _maestro_jobs("maestro_push_ticket")
    assert len(jobs) == 1, f"expected exactly one push job, got {len(jobs)}"
    assert jobs[0].payload.get("ticket_id") == ticket.pk, f"job payload wrong: {jobs[0].payload}"
    assert jobs[0].payload.get("board_id") == board.pk, f"job payload wrong: {jobs[0].payload}"

    # Inactive board -> rejected, nothing enqueued
    board.is_active = False
    board.save(update_fields=["is_active"])
    _clear_maestro_jobs()
    resp = opts.client.post(f"/api/incident/ticket/{ticket.pk}", json={"push_to_board": board.pk})
    assert resp.status_code == 400, f"inactive board must 400, got {resp.status_code}: {resp.body}"
    assert not _maestro_jobs("maestro_push_ticket"), "inactive board must not enqueue a push"

    # Group-scoped board vs group-less ticket -> denied
    Group.objects.filter(name=f"{PREFIX} grp").delete()
    grp = Group.objects.create(name=f"{PREFIX} grp", kind="organization")
    grouped_board = _make_board(name=f"{PREFIX} grouped", group=grp)
    resp = opts.client.post(f"/api/incident/ticket/{ticket.pk}", json={"push_to_board": grouped_board.pk})
    assert resp.status_code == 403, f"group-mismatch board must 403, got {resp.status_code}: {resp.body}"
    assert not _maestro_jobs("maestro_push_ticket"), "group-mismatch board must not enqueue a push"


@th.django_unit_test()
def test_linked_ticket_edit_enqueues_sync(opts):
    board = _make_board(name=f"{PREFIX} syncsrc")
    ticket = _make_ticket(title=f"{PREFIX} edit me")
    link = _make_link(board, ticket)
    _clear_maestro_jobs()

    assert opts.client.login(opts.admin_name, PWORD), "admin login failed"
    resp = opts.client.post(f"/api/incident/ticket/{ticket.pk}", json={"title": f"{PREFIX} edited"})
    assert resp.status_code == 200, f"ticket edit failed: {resp.status_code}: {resp.body}"

    jobs = _maestro_jobs("maestro_sync_change")
    assert len(jobs) == 1, f"linked ticket edit must enqueue one sync job, got {len(jobs)}"
    assert jobs[0].payload.get("link_id") == link.pk, f"sync job link wrong: {jobs[0].payload}"
    assert "title" in jobs[0].payload.get("changed", []), f"changed fields wrong: {jobs[0].payload}"

    # Non-synced field -> no job
    _clear_maestro_jobs()
    resp = opts.client.post(f"/api/incident/ticket/{ticket.pk}", json={"priority": 7})
    assert resp.status_code == 200, f"ticket edit failed: {resp.status_code}: {resp.body}"
    assert not _maestro_jobs("maestro_sync_change"), "non-synced field edits must not enqueue sync"

    # Inactive board -> no job
    board.is_active = False
    board.save(update_fields=["is_active"])
    _clear_maestro_jobs()
    resp = opts.client.post(f"/api/incident/ticket/{ticket.pk}", json={"title": f"{PREFIX} edited again"})
    assert resp.status_code == 200, f"ticket edit failed: {resp.status_code}: {resp.body}"
    assert not _maestro_jobs("maestro_sync_change"), "inactive board must not receive sync jobs"


@th.django_unit_test()
def test_note_create_enqueues_push(opts):
    board = _make_board(name=f"{PREFIX} notesrc")
    ticket = _make_ticket(title=f"{PREFIX} note me")
    link = _make_link(board, ticket, remote_item_id=9001)
    _clear_maestro_jobs()

    assert opts.client.login(opts.admin_name, PWORD), "admin login failed"
    resp = opts.client.post("/api/incident/ticket/note",
                            json={"parent": ticket.pk, "note": "human comment"})
    assert resp.status_code == 200, f"note create failed: {resp.status_code}: {resp.body}"

    jobs = _maestro_jobs("maestro_push_note")
    assert len(jobs) == 1, f"note on a linked ticket must enqueue one push, got {len(jobs)}"
    assert jobs[0].payload.get("link_id") == link.pk, f"note job link wrong: {jobs[0].payload}"

    # A sync-origin note must NOT bounce back to the board
    _clear_maestro_jobs()
    resp = opts.client.post("/api/incident/ticket/note", json={
        "parent": ticket.pk, "note": "sync echo",
        "metadata": {"origin": "maestro"}})
    assert resp.status_code == 200, f"note create failed: {resp.status_code}: {resp.body}"
    assert not _maestro_jobs("maestro_push_note"), (
        "maestro-origin notes must never be pushed back (echo suppression)")

    # Per-board opt-out
    board.sync_notes = False
    board.save(update_fields=["sync_notes"])
    _clear_maestro_jobs()
    resp = opts.client.post("/api/incident/ticket/note",
                            json={"parent": ticket.pk, "note": "another human comment"})
    assert resp.status_code == 200, f"note create failed: {resp.status_code}: {resp.body}"
    assert not _maestro_jobs("maestro_push_note"), "sync_notes=False must disable note mirroring"


@th.django_unit_test()
def test_webhook_endpoint_signed_note(opts):
    from mojo.helpers.crypto.sign import generate_signature, get_signature_header
    from mojo.apps.incident.models import TicketNote

    board = _make_board(name=f"{PREFIX} whboard")
    ticket = _make_ticket(title=f"{PREFIX} wh ticket")
    _make_link(board, ticket, remote_item_id=9100)

    payload = {
        "v": 1, "event": "note.created", "board": 42,
        "item": {"id": 9100, "title": ticket.title, "values": {}, "is_active": True},
        "note": {"id": 3, "text": "from the board", "author": "Bob"},
    }
    header = get_signature_header()
    sig = generate_signature(payload, TEST_KEY)

    resp = opts.client.post(f"/api/incident/maestro/webhook/{board.callback_token}",
                            json=payload, headers={header: sig})
    assert resp.status_code == 200, f"signed webhook must succeed: {resp.status_code}: {resp.body}"

    note = TicketNote.objects.filter(parent=ticket, metadata__event="note.created").first()
    assert note is not None, "webhook note.created must add a ticket note"
    assert note.user is None, "webhook note must be a system note (user=None)"
    assert note.metadata.get("origin") == "maestro", f"origin marker missing: {note.metadata}"
    assert "Bob" in note.note and "from the board" in note.note, f"note content wrong: {note.note}"


@th.django_unit_test()
def test_webhook_endpoint_rejects_bad_requests(opts):
    from mojo.helpers.crypto.sign import generate_signature, get_signature_header
    from mojo.apps.incident.models import TicketNote

    board = _make_board(name=f"{PREFIX} whsec")
    ticket = _make_ticket(title=f"{PREFIX} whsec ticket")
    _make_link(board, ticket, remote_item_id=9200)

    payload = {
        "v": 1, "event": "note.created", "board": 42,
        "item": {"id": 9200, "title": ticket.title, "values": {}, "is_active": True},
        "note": {"id": 4, "text": "evil", "author": "Mallory"},
    }
    header = get_signature_header()
    notes_before = TicketNote.objects.filter(parent=ticket).count()

    # Bad signature
    resp = opts.client.post(f"/api/incident/maestro/webhook/{board.callback_token}",
                            json=payload, headers={header: "0" * 64})
    assert resp.status_code == 401, f"bad signature must 401, got {resp.status_code}: {resp.body}"

    # Missing signature
    resp = opts.client.post(f"/api/incident/maestro/webhook/{board.callback_token}", json=payload)
    assert resp.status_code == 401, f"missing signature must 401, got {resp.status_code}: {resp.body}"

    # Unknown token
    resp = opts.client.post("/api/incident/maestro/webhook/nosuchtoken",
                            json=payload, headers={header: generate_signature(payload, TEST_KEY)})
    assert resp.status_code == 401, f"unknown token must 401, got {resp.status_code}: {resp.body}"

    # Inactive board — even with a VALID signature
    board.is_active = False
    board.save(update_fields=["is_active"])
    resp = opts.client.post(f"/api/incident/maestro/webhook/{board.callback_token}",
                            json=payload, headers={header: generate_signature(payload, TEST_KEY)})
    assert resp.status_code == 401, f"inactive board must 401, got {resp.status_code}: {resp.body}"

    assert TicketNote.objects.filter(parent=ticket).count() == notes_before, (
        "no rejected webhook may write a ticket note")


@th.django_unit_test()
def test_webhook_status_map_applies_status(opts):
    from mojo.helpers.crypto.sign import generate_signature, get_signature_header

    board = _make_board(name=f"{PREFIX} whmap",
                        status_map={"column": "state", "map": {"open": "todo", "closed": "done"}})
    ticket = _make_ticket(title=f"{PREFIX} whmap ticket", status="open")
    _make_link(board, ticket, remote_item_id=9300)

    payload = {
        "v": 1, "event": "item.updated", "board": 42,
        "item": {"id": 9300, "title": ticket.title, "values": {"state": "done"}, "is_active": True},
        "changes": [{"column": "state", "old": "todo", "new": "done"}],
    }
    header = get_signature_header()
    resp = opts.client.post(f"/api/incident/maestro/webhook/{board.callback_token}",
                            json=payload, headers={header: generate_signature(payload, TEST_KEY)})
    assert resp.status_code == 200, f"signed webhook must succeed: {resp.status_code}: {resp.body}"

    ticket.refresh_from_db()
    assert ticket.status == "closed", f"status_map must apply the board column change: {ticket.status}"

    # Unknown item id -> 200 ignored (keeps maestro's retry queue quiet)
    payload2 = {
        "v": 1, "event": "item.updated", "board": 42,
        "item": {"id": 999999, "title": "x", "values": {}, "is_active": True},
        "changes": [],
    }
    resp = opts.client.post(f"/api/incident/maestro/webhook/{board.callback_token}",
                            json=payload2, headers={header: generate_signature(payload2, TEST_KEY)})
    assert resp.status_code == 200, f"unlinked item must 200, got {resp.status_code}: {resp.body}"
    assert resp.response["data"]["ignored"] is True, f"unlinked item must be flagged ignored: {resp.body}"


@th.django_unit_test()
def test_rules_ticket_handler_board_param(opts):
    from objict import objict
    from mojo.apps.incident.models import Ticket
    from mojo.apps.incident.handlers.event_handlers import TicketHandler

    board = _make_board(name=f"{PREFIX} rules")
    _clear_maestro_jobs()

    event = objict(title=f"{PREFIX} rule event", details="rule details",
                   level=3, incident=None, metadata={}, pk=0)
    handler = TicketHandler(None, board=str(board.pk), title=f"{PREFIX} rule ticket")
    assert handler.run(event) is True, "TicketHandler must succeed"

    ticket = Ticket.objects.filter(title=f"{PREFIX} rule ticket").first()
    assert ticket is not None, "TicketHandler must create the ticket"
    jobs = _maestro_jobs("maestro_push_ticket")
    assert len(jobs) == 1, f"board= param must enqueue a push, got {len(jobs)}"
    assert jobs[0].payload.get("ticket_id") == ticket.pk, f"push job payload wrong: {jobs[0].payload}"

    # Unknown board -> ticket still created, nothing enqueued
    _clear_maestro_jobs()
    event2 = objict(title=f"{PREFIX} rule event 2", details="", level=3,
                    incident=None, metadata={}, pk=0)
    handler2 = TicketHandler(None, board="99999999", title=f"{PREFIX} rule ticket 2")
    assert handler2.run(event2) is True, "TicketHandler must not fail on an unknown board"
    assert Ticket.objects.filter(title=f"{PREFIX} rule ticket 2").exists(), (
        "ticket creation must not depend on the board push")
    assert not _maestro_jobs("maestro_push_ticket"), "unknown board must not enqueue a push"
