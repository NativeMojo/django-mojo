"""
Service-level tests for the maestro board link client (DM-040) —
mojo/apps/incident/services/maestro_sync.py with the module's `requests`
mocked, called directly in the test process.
"""
import json
from unittest import mock

from testit import helpers as th

PREFIX = "[maestro_svc]"
TEST_KEY = "svc" + "k" * 45


@th.django_unit_setup()
def setup_maestro_service(opts):
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.incident.models import MaestroBoard, Ticket

    Setting.set("MAESTRO_CALLBACK_BASE", "http://client.example.test")
    MaestroBoard.objects.filter(name__startswith=PREFIX).delete()
    Ticket.objects.filter(title__startswith=PREFIX).delete()


def _make_board(**kwargs):
    from mojo.apps.incident.models import MaestroBoard
    defaults = dict(
        name=f"{PREFIX} board",
        api_url="https://maestro.example.test",
        remote_board_id=77,
        is_active=True,
    )
    defaults.update(kwargs)
    board = MaestroBoard(**defaults)
    board.set_secret("link_key", TEST_KEY)
    board.save()
    return board


def _make_ticket(**kwargs):
    from mojo.apps.incident.models import Ticket
    defaults = dict(title=f"{PREFIX} ticket", description="svc test", status="open")
    defaults.update(kwargs)
    return Ticket.objects.create(**defaults)


def _mock_requests(json_data=None, status=200):
    """Patch maestro_sync.requests; returns the patcher. Keeps the real
    exception classes so the service's except clauses still resolve."""
    from mojo.apps.incident.services import maestro_sync
    import requests as real_requests

    patcher = mock.patch.object(maestro_sync, "requests")
    mock_requests = patcher.start()
    mock_requests.Timeout = real_requests.Timeout
    mock_requests.exceptions = real_requests.exceptions
    resp = mock.Mock()
    resp.status_code = status
    resp.text = json.dumps(json_data or {})
    resp.json = mock.Mock(return_value=json_data or {})
    mock_requests.post.return_value = resp
    return patcher, mock_requests


def _maestro_job_count():
    from mojo.apps.jobs.models import Job
    return Job.objects.filter(func__startswith="mojo.apps.incident.asyncjobs.maestro").count()


@th.django_unit_test()
def test_parse_paste_url(opts):
    from mojo.errors import ValueException
    from mojo.apps.incident.services import maestro_sync

    base, key = maestro_sync.parse_paste_url("https://maestromojo.com/api/boards/link/abc123XYZ")
    assert base == "https://maestromojo.com", f"wrong api base parsed: {base}"
    assert key == "abc123XYZ", f"wrong key parsed: {key}"

    for bad in (
        "not-a-url",
        "ftp://maestromojo.com/api/boards/link/abc",
        "https://maestromojo.com/api/boards/link",          # missing key
        "https://maestromojo.com/api/other/link/abc",       # wrong path
        "",
        None,
    ):
        try:
            maestro_sync.parse_paste_url(bad)
            assert False, f"parse_paste_url must reject {bad!r}"
        except ValueException:
            pass


@th.django_unit_test()
def test_register_success_caches_schema(opts):
    from mojo.apps.incident.models import MaestroBoard
    from mojo.apps.incident.services import maestro_sync

    board = MaestroBoard(name="", api_url="https://maestro.example.test")
    board.set_secret("link_key", TEST_KEY)

    columns = [{"slug": "state", "name": "State", "type": "category",
                "options": [{"value": "todo", "label": "Todo", "color": "#111111"}]}]
    patcher, mock_requests = _mock_requests(
        {"v": 1, "label": "Link A", "board": {"id": 5, "name": "Sprint", "columns": columns}})
    try:
        maestro_sync.register(board)
    finally:
        patcher.stop()

    assert board.remote_board_id == 5, f"remote_board_id not cached: {board.remote_board_id}"
    assert board.name == "Sprint", f"board name not cached: {board.name}"
    assert board.schema.get("columns") == columns, f"schema columns not cached: {board.schema}"
    assert board.schema.get("label") == "Link A", f"label not cached: {board.schema}"

    args, kwargs = mock_requests.post.call_args
    assert args[0] == "https://maestro.example.test/api/boards/link/register", f"wrong register url: {args[0]}"
    assert kwargs["headers"]["Authorization"] == f"linkkey {TEST_KEY}", "register must auth with linkkey scheme"
    body = kwargs["json"]
    assert body["v"] == 1, f"payload must be versioned: {body}"
    assert body["callback_url"] == f"http://client.example.test/api/incident/maestro/webhook/{board.callback_token}", (
        f"callback_url must carry the board's callback token: {body['callback_url']}")


@th.django_unit_test()
def test_register_failure_raises(opts):
    import requests as real_requests
    from mojo.errors import ValueException
    from mojo.apps.incident.models import MaestroBoard
    from mojo.apps.incident.services import maestro_sync

    board = MaestroBoard(api_url="https://maestro.example.test")
    board.set_secret("link_key", TEST_KEY)

    # Remote rejects the key (401)
    patcher, _ = _mock_requests({"error": "invalid link key"}, status=401)
    try:
        try:
            maestro_sync.register(board)
            assert False, "register must raise on a 401 from maestro"
        except ValueException as err:
            assert "registration failed" in err.reason, f"unexpected reason: {err.reason}"
            assert "invalid link key" not in err.reason, "remote error bodies must not be echoed to users"
    finally:
        patcher.stop()

    # Remote times out
    patcher, mock_requests = _mock_requests()
    mock_requests.post.side_effect = real_requests.Timeout("boom")
    try:
        try:
            maestro_sync.register(board)
            assert False, "register must raise on timeout"
        except ValueException:
            pass
    finally:
        patcher.stop()

    # No key at all (paste_url never provided)
    board2 = MaestroBoard(api_url="")
    try:
        maestro_sync.register(board2)
        assert False, "register must require a pasted link"
    except ValueException:
        pass


@th.django_unit_test()
def test_push_ticket_creates_link_and_note(opts):
    from mojo.apps.incident.models import MaestroBoardLink, TicketNote
    from mojo.apps.incident.services import maestro_sync

    board = _make_board()
    ticket = _make_ticket()

    patcher, mock_requests = _mock_requests({"id": 501, "url": "/workspaces/board/5?item=501"})
    try:
        link = maestro_sync.push_ticket(board, ticket)
    finally:
        patcher.stop()

    assert link.remote_item_id == 501, f"link must store the remote item id: {link.remote_item_id}"
    assert link.remote_url == "https://maestro.example.test/workspaces/board/5?item=501", (
        f"relative remote url must be absolutized: {link.remote_url}")
    assert link.last_synced is not None, "last_synced must be stamped on push"
    assert MaestroBoardLink.objects.filter(ticket=ticket, maestro_board=board).count() == 1, (
        "exactly one link row per (ticket, board)")

    args, kwargs = mock_requests.post.call_args
    assert args[0].endswith("/api/boards/link/item"), f"first push must create: {args[0]}"
    body = kwargs["json"]
    assert body["title"] == ticket.title, f"item title mismatch: {body}"
    assert body["source"]["ticket_id"] == ticket.pk, f"source.ticket_id mismatch: {body}"

    note = TicketNote.objects.filter(parent=ticket, metadata__type="board_link").first()
    assert note is not None, "push must add a ticket note with the remote item url"
    assert note.user is None, "board-link note must be a system note (user=None)"
    assert note.metadata.get("origin") == "maestro", f"note must carry the sync origin marker: {note.metadata}"
    assert link.remote_url in (note.note or ""), f"note must contain the remote url: {note.note}"


@th.django_unit_test()
def test_push_ticket_is_idempotent(opts):
    from mojo.apps.incident.models import MaestroBoardLink, TicketNote
    from mojo.apps.incident.services import maestro_sync

    board = _make_board()
    ticket = _make_ticket(title=f"{PREFIX} idempotent")

    patcher, _ = _mock_requests({"id": 601, "url": "/workspaces/board/5?item=601"})
    try:
        maestro_sync.push_ticket(board, ticket)
    finally:
        patcher.stop()

    notes_before = TicketNote.objects.filter(parent=ticket).count()

    patcher, mock_requests = _mock_requests({"id": 601})
    try:
        maestro_sync.push_ticket(board, ticket)
    finally:
        patcher.stop()

    args, _ = mock_requests.post.call_args
    assert args[0].endswith("/api/boards/link/item/601"), (
        f"second push must update the existing item, not create: {args[0]}")
    assert MaestroBoardLink.objects.filter(ticket=ticket, maestro_board=board).count() == 1, (
        "re-push must not create a duplicate link row")
    assert TicketNote.objects.filter(parent=ticket).count() == notes_before, (
        "re-push must not add another board-link note")


@th.django_unit_test()
def test_sync_ticket_change_sends_only_changed_and_mapped(opts):
    from mojo.apps.incident.services import maestro_sync

    board = _make_board(status_map={"column": "state", "map": {"open": "todo", "closed": "done"}})
    ticket = _make_ticket(title=f"{PREFIX} sync", status="closed")

    patcher, _ = _mock_requests({"id": 701, "url": "/w"})
    try:
        link = maestro_sync.push_ticket(board, ticket)
    finally:
        patcher.stop()

    patcher, mock_requests = _mock_requests({})
    try:
        maestro_sync.sync_ticket_change(link, ["title", "status"])
    finally:
        patcher.stop()

    args, kwargs = mock_requests.post.call_args
    assert args[0].endswith("/api/boards/link/item/701"), f"sync must target the item: {args[0]}"
    body = kwargs["json"]
    assert body["title"] == ticket.title, f"changed title must be sent: {body}"
    assert body["values"] == {"state": "done"}, f"status must map through status_map: {body}"
    assert "description" not in body, f"unchanged fields must not be sent: {body}"

    # A status with no mapping and nothing else changed -> no HTTP call at all
    ticket.status = "weird_state"
    ticket.save(update_fields=["status"])
    link.refresh_from_db()
    patcher, mock_requests = _mock_requests({})
    try:
        maestro_sync.sync_ticket_change(link, ["status"])
    finally:
        patcher.stop()
    assert not mock_requests.post.called, "unmapped status change alone must not call maestro"


@th.django_unit_test()
def test_webhook_note_creates_system_note_no_echo(opts):
    from mojo.apps.incident.models import TicketNote
    from mojo.apps.incident.services import maestro_sync

    board = _make_board(name=f"{PREFIX} wh")
    ticket = _make_ticket(title=f"{PREFIX} wh ticket")
    patcher, _ = _mock_requests({"id": 801, "url": "/w"})
    try:
        maestro_sync.push_ticket(board, ticket)
    finally:
        patcher.stop()

    jobs_before = _maestro_job_count()
    result = maestro_sync.handle_board_webhook(board, {
        "v": 1, "event": "note.created", "board": 77,
        "item": {"id": 801, "title": ticket.title, "values": {}, "is_active": True},
        "note": {"id": 9, "text": "looks good", "author": "Alice"},
    })
    assert result == {"status": True}, f"webhook apply must succeed: {result}"

    note = TicketNote.objects.filter(parent=ticket, metadata__event="note.created").first()
    assert note is not None, "board comment must become a ticket note"
    assert note.user is None, "sync note must have no human identity (user=None)"
    assert note.metadata.get("origin") == "maestro", f"sync note must carry origin marker: {note.metadata}"
    assert "Alice" in note.note and "looks good" in note.note, f"note must carry author + text: {note.note}"

    # Echo suppression: applying the webhook must not enqueue any outbound
    # maestro sync work (notes were written via ORM, not the REST pipeline).
    assert _maestro_job_count() == jobs_before, (
        "webhook-applied changes must NOT enqueue outbound maestro jobs (echo)")


@th.django_unit_test()
def test_webhook_item_updated_status_map_and_echo(opts):
    from mojo.apps.incident.models import TicketNote
    from mojo.apps.incident.services import maestro_sync

    board = _make_board(name=f"{PREFIX} wh2",
                        status_map={"column": "state", "map": {"open": "todo", "closed": "done"}})
    ticket = _make_ticket(title=f"{PREFIX} wh2 ticket", status="open")
    patcher, _ = _mock_requests({"id": 802, "url": "/w"})
    try:
        maestro_sync.push_ticket(board, ticket)
    finally:
        patcher.stop()

    jobs_before = _maestro_job_count()
    payload = {
        "v": 1, "event": "item.updated", "board": 77,
        "item": {"id": 802, "title": ticket.title, "values": {"state": "done"}, "is_active": True},
        "changes": [{"column": "state", "old": "todo", "new": "done"}],
    }
    result = maestro_sync.handle_board_webhook(board, payload)
    assert result == {"status": True}, f"webhook apply must succeed: {result}"

    ticket.refresh_from_db()
    assert ticket.status == "closed", f"mapped column change must update ticket status: {ticket.status}"
    note = TicketNote.objects.filter(parent=ticket, metadata__event="item.updated").first()
    assert note is not None, "item.updated must add a describing ticket note"
    assert _maestro_job_count() == jobs_before, (
        "webhook-applied status change must NOT enqueue outbound maestro jobs (echo)")

    # Repeat delivery: status already matches -> compare-before-write no-op
    modified_before = ticket.modified
    maestro_sync.handle_board_webhook(board, payload)
    ticket.refresh_from_db()
    assert ticket.status == "closed", "repeat delivery must be a no-op on status"
    assert ticket.modified == modified_before, "repeat delivery must not rewrite the ticket"


@th.django_unit_test()
def test_webhook_without_status_map_notes_only(opts):
    from mojo.apps.incident.services import maestro_sync

    board = _make_board(name=f"{PREFIX} wh3")  # no status_map
    ticket = _make_ticket(title=f"{PREFIX} wh3 ticket", status="open")
    patcher, _ = _mock_requests({"id": 803, "url": "/w"})
    try:
        maestro_sync.push_ticket(board, ticket)
    finally:
        patcher.stop()

    maestro_sync.handle_board_webhook(board, {
        "v": 1, "event": "item.updated", "board": 77,
        "item": {"id": 803, "title": ticket.title, "values": {"state": "done"}, "is_active": True},
        "changes": [{"column": "state", "old": "todo", "new": "done"}],
    })
    ticket.refresh_from_db()
    assert ticket.status == "open", "without status_map a board column change must NOT touch ticket status"


@th.django_unit_test()
def test_webhook_unlinked_item_ignored(opts):
    from mojo.apps.incident.services import maestro_sync

    board = _make_board(name=f"{PREFIX} wh4")
    result = maestro_sync.handle_board_webhook(board, {
        "v": 1, "event": "item.updated", "board": 77,
        "item": {"id": 999999, "title": "x", "values": {}, "is_active": True},
        "changes": [],
    })
    assert result.get("ignored") is True, f"unknown item must be ignored, not an error: {result}"
