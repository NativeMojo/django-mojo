"""Service contract tests for deployment-scoped Maestro reporting."""

import json
from io import StringIO
from unittest import mock

from testit import helpers as th

PREFIX = "[maestro_svc]"
TEST_KEY = "svc" + "k" * 45
INTEGRATION = "integration-project-a"


@th.django_unit_setup()
def setup_maestro_service(opts):
    from mojo.apps.incident.models import (
        Incident, MaestroBoard, MaestroItemLink, Ticket,
    )
    MaestroItemLink.objects.all().delete()
    Ticket.objects.filter(title__startswith=PREFIX).delete()
    Incident.objects.filter(title__startswith=PREFIX).delete()
    MaestroBoard.objects.filter(name__startswith=PREFIX).delete()


def _make_ticket(**kwargs):
    from mojo.apps.incident.models import Ticket
    defaults = dict(title=f"{PREFIX} ticket", description="svc test", status="open")
    defaults.update(kwargs)
    return Ticket.objects.create(**defaults)


def _make_incident(**kwargs):
    from mojo.apps.incident.models import Incident
    defaults = dict(title=f"{PREFIX} incident", details="svc details", category="test", status="open")
    defaults.update(kwargs)
    return Incident.objects.create(**defaults)


def _settings_patch(maestro_sync, **overrides):
    values = {
        "MAESTRO_API_KEY": TEST_KEY,
        "MAESTRO_API_URL": "https://maestro.example.test",
        "MAESTRO_CALLBACK_BASE": "https://client.example.test",
        "MAESTRO_ALLOW_HTTP": False,
        "MAESTRO_LINK_TIMEOUT": 10,
        "BASE_URL": "https://client.example.test",
        "PROJECT_NAME": "service-tests",
    }
    values.update(overrides)
    return mock.patch.object(
        maestro_sync.settings,
        "get_static",
        side_effect=lambda name, default=None: values.get(name, default),
    )


def _mock_requests(maestro_sync, json_data=None, status=200):
    import requests as real_requests
    patcher = mock.patch.object(maestro_sync, "requests")
    mocked = patcher.start()
    mocked.Timeout = real_requests.Timeout
    response = mock.Mock()
    response.status_code = status
    response.text = json.dumps(json_data or {})
    response.json.return_value = json_data or {}
    mocked.post.return_value = response
    return patcher, mocked


def _create_response(item_id=501, board=5, integration=INTEGRATION):
    return {
        "id": item_id,
        "url": f"/workspaces/board/{board}?item={item_id}",
        "board": {"id": board, "name": "Triage"},
        "integration": {"id": integration},
    }


@th.django_unit_test()
def test_static_config_and_strict_board_selector(opts):
    from mojo.errors import ValueException
    from mojo.apps.incident.services import maestro_sync

    with _settings_patch(maestro_sync):
        assert maestro_sync.get_config() == (
            "https://maestro.example.test", TEST_KEY,
        ), "configured Maestro origin/key must be returned"
        assert maestro_sync.parse_board_selector(True) is None, "true must select the default board"
        assert maestro_sync.parse_board_selector("3") == 3, "numeric remote board must parse"

    for bad in (False, 0, -1, "01", "x"):
        try:
            maestro_sync.parse_board_selector(bad)
            assert False, f"invalid board selector {bad!r} must fail"
        except ValueException:
            pass

    with _settings_patch(maestro_sync, MAESTRO_API_KEY=""):
        try:
            maestro_sync.get_config()
            assert False, "missing MAESTRO_API_KEY must fail"
        except ValueException as err:
            assert "MAESTRO_API_KEY" in err.reason, f"error must name missing setting: {err.reason}"
            assert TEST_KEY not in err.reason, "configuration errors must not disclose keys"

    for name, value in (
        ("MAESTRO_API_URL", "https://user:secret@maestro.example.test"),
        ("MAESTRO_API_URL", "https://maestro.example.test/api"),
        ("MAESTRO_CALLBACK_BASE", "https://client.example.test/callback-base"),
    ):
        with _settings_patch(maestro_sync, **{name: value}):
            try:
                if name == "MAESTRO_API_URL":
                    maestro_sync.get_config()
                else:
                    maestro_sync.get_callback_url()
                assert False, f"non-origin {name} must fail"
            except ValueException:
                pass


@th.django_unit_test()
def test_register_uses_apikey_and_returns_safe_routing(opts):
    from mojo.apps.incident.services import maestro_sync

    response = {
        "integration": {"id": INTEGRATION},
        "workspace": {"id": 17, "name": "NativeMojo"},
        "default_board": {"id": 11, "name": "Inbox"},
    }
    patcher, mocked = _mock_requests(maestro_sync, response)
    try:
        with _settings_patch(maestro_sync):
            result = maestro_sync.register()
    finally:
        patcher.stop()

    assert result["integration_id"] == INTEGRATION, f"wrong registration result: {result}"
    args, kwargs = mocked.post.call_args
    assert args[0] == "https://maestro.example.test/api/boards/link/register", f"wrong URL: {args[0]}"
    assert kwargs["headers"]["Authorization"] == f"apikey {TEST_KEY}", (
        "all reporting calls must use the built-in apikey scheme")
    assert kwargs["json"]["callback_url"] == "https://client.example.test/api/incident/maestro/webhook", (
        f"callback must be the fixed signed endpoint: {kwargs['json']}")


@th.django_unit_test()
def test_register_refuses_different_integration(opts):
    from mojo.apps.incident.models import MaestroItemLink
    from mojo.apps.incident.services import maestro_sync

    ticket = _make_ticket(title=f"{PREFIX} existing integration")
    MaestroItemLink.objects.create(
        ticket=ticket, remote_integration_id="old-integration",
        remote_item_id=1, remote_board_id=2,
    )
    response = {
        "integration": {"id": "different-integration"},
        "workspace": {"id": 17, "name": "NativeMojo"},
        "default_board": {"id": 11, "name": "Inbox"},
    }
    patcher, _mocked = _mock_requests(maestro_sync, response)
    try:
        with _settings_patch(maestro_sync):
            try:
                maestro_sync.register()
                assert False, "a different integration key must not adopt existing links"
            except Exception as err:
                assert "different integration" in str(err).lower(), f"wrong failure: {err}"
    finally:
        patcher.stop()


@th.django_unit_test()
def test_push_ticket_default_and_idempotent(opts):
    from mojo.apps.incident.models import MaestroItemLink, TicketNote
    from mojo.apps.incident.services import maestro_sync

    ticket = _make_ticket(
        title=f"{PREFIX} default",
        description="x" * (maestro_sync.DESCRIPTION_MAX + 1),
    )
    patcher, mocked = _mock_requests(maestro_sync, _create_response())
    try:
        with _settings_patch(maestro_sync):
            link = maestro_sync.push_ticket(ticket)
    finally:
        patcher.stop()

    body = mocked.post.call_args.kwargs["json"]
    assert "board" not in body, f"default routing must omit board: {body}"
    assert body["source"]["kind"] == "ticket" and body["source"]["id"] == ticket.pk, (
        f"ticket source identity missing: {body}")
    assert len(body["description"]) == maestro_sync.DESCRIPTION_MAX, (
        "outbound source descriptions must be bounded")
    assert link.remote_integration_id == INTEGRATION, f"integration id not stored: {link}"
    assert link.remote_board_id == 5, f"resolved board not stored: {link.remote_board_id}"
    assert MaestroItemLink.objects.filter(ticket=ticket).count() == 1, "ticket must have one link"
    assert TicketNote.objects.filter(parent=ticket, metadata__type="item_link").count() == 1, (
        "first push must record one local system note")

    patcher, mocked = _mock_requests(maestro_sync, {})
    try:
        with _settings_patch(maestro_sync):
            maestro_sync.push_ticket(ticket, 99)
    finally:
        patcher.stop()
    args, kwargs = mocked.post.call_args
    assert args[0].endswith("/link/item/501"), f"repeat push must update existing item: {args[0]}"
    assert "board" not in kwargs["json"], "repeat push must not silently move an existing item"
    assert MaestroItemLink.objects.filter(ticket=ticket).count() == 1, "repeat push must stay idempotent"


@th.django_unit_test()
def test_push_incident_explicit_board_and_sync(opts):
    from mojo.apps.incident.services import maestro_sync

    incident = _make_incident(status="resolved", priority=8)
    patcher, mocked = _mock_requests(maestro_sync, _create_response(item_id=601, board=3))
    try:
        with _settings_patch(maestro_sync):
            link = maestro_sync.push_incident(incident, 3)
    finally:
        patcher.stop()
    body = mocked.post.call_args.kwargs["json"]
    assert body["board"] == 3, f"explicit remote board missing: {body}"
    assert body["source"]["kind"] == "incident", f"incident source identity missing: {body}"
    assert body["lifecycle"] == "done", f"resolved incident must map to done: {body}"

    incident.title = f"{PREFIX} changed"
    incident.status = "paused"
    patcher, mocked = _mock_requests(maestro_sync, {})
    try:
        with _settings_patch(maestro_sync):
            maestro_sync.sync_change(link, ["title", "status"])
    finally:
        patcher.stop()
    body = mocked.post.call_args.kwargs["json"]
    assert body["title"] == incident.title, f"changed incident title missing: {body}"
    assert body["lifecycle"] == "parked", f"paused incident must map to parked: {body}"


@th.django_unit_test()
def test_callback_routes_ticket_by_integration_and_dedupes_note(opts):
    from mojo.apps.incident.models import MaestroItemLink, TicketNote
    from mojo.apps.incident.services import maestro_sync

    ticket = _make_ticket(title=f"{PREFIX} callback ticket")
    MaestroItemLink.objects.create(
        ticket=ticket, remote_integration_id=INTEGRATION,
        remote_item_id=701, remote_board_id=3,
    )
    payload = {
        "v": 1,
        "integration_id": INTEGRATION,
        "event": "note.created",
        "item": {"id": 701, "board": 4, "url": "/board/4?item=701"},
        "note": {"id": 9, "text": "looks good", "author": "Alice"},
    }
    assert maestro_sync.handle_webhook(payload) == {"status": True}, "signed callback must apply"
    assert maestro_sync.handle_webhook(payload).get("ignored") is True, "replayed note must be ignored"
    assert TicketNote.objects.filter(
        parent=ticket, metadata__remote_note_id=9).count() == 1, "remote note must dedupe"
    link = ticket.maestro_links.get()
    assert link.remote_board_id == 4, f"move must refresh cached board: {link.remote_board_id}"

    wrong = dict(payload, integration_id="other-integration")
    assert maestro_sync.handle_webhook(wrong).get("ignored") is True, (
        "same item id from another integration must not route to this ticket")
    malformed = dict(payload, item={"id": [701]})
    assert maestro_sync.handle_webhook(malformed).get("ignored") is True, (
        "malformed signed callback identities must be ignored without an ORM error")


@th.django_unit_test()
def test_callback_routes_incident_history_and_lifecycle(opts):
    from mojo.apps.incident.models import IncidentHistory, MaestroItemLink
    from mojo.apps.incident.services import maestro_sync

    incident = _make_incident(title=f"{PREFIX} callback incident", status="open")
    MaestroItemLink.objects.create(
        incident=incident, remote_integration_id=INTEGRATION,
        remote_item_id=801, remote_board_id=5,
    )
    payload = {
        "integration_id": INTEGRATION,
        "event": "item.updated",
        "item": {"id": 801, "board": 5, "lifecycle": "done"},
        "changes": [{"column": "stage", "old": "building", "new": "done"}],
    }
    result = maestro_sync.handle_webhook(payload)
    assert result == {"status": True}, f"incident callback must apply: {result}"
    incident.refresh_from_db()
    assert incident.status == "resolved", f"done lifecycle must resolve incident: {incident.status}"
    history = IncidentHistory.objects.filter(parent=incident, metadata__origin="maestro").first()
    assert history is not None, "incident callback must append history"
    assert history.metadata.get("remote_item_id") == 801, f"callback metadata missing: {history.metadata}"


@th.django_unit_test()
def test_item_link_constraints(opts):
    from django.db import IntegrityError, transaction
    from mojo.apps.incident.models import MaestroItemLink

    ticket = _make_ticket(title=f"{PREFIX} constraints")
    incident = _make_incident(title=f"{PREFIX} constraints")
    invalid_rows = [
        {},
        {"ticket": ticket, "incident": incident},
    ]
    for fields in invalid_rows:
        try:
            with transaction.atomic():
                MaestroItemLink.objects.create(
                    remote_integration_id=INTEGRATION, remote_item_id=900,
                    **fields,
                )
            assert False, f"exactly-one-source constraint must reject {fields}"
        except IntegrityError:
            pass


@th.django_unit_test()
def test_register_management_command_never_prints_key(opts):
    from django.core.management import call_command
    from mojo.apps.incident.models import MaestroItemLink
    from mojo.apps.incident.services import maestro_sync

    MaestroItemLink.objects.all().delete()
    response = {
        "integration": {"id": INTEGRATION},
        "workspace": {"id": 17, "name": "NativeMojo"},
        "default_board": {"id": 11, "name": "Inbox"},
    }
    output = StringIO()
    patcher, _mocked = _mock_requests(maestro_sync, response)
    try:
        with _settings_patch(maestro_sync):
            call_command("register_maestro", stdout=output)
    finally:
        patcher.stop()
    rendered = output.getvalue()
    assert INTEGRATION in rendered and "NativeMojo" in rendered, f"safe routing summary missing: {rendered}"
    assert TEST_KEY not in rendered, "management command must never print MAESTRO_API_KEY"
