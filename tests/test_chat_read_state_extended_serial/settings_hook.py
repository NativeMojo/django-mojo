"""Actual static selection and serial live REST/WS/hook integration."""
from testit import helpers as th


def _fixture(label):
    from mojo.apps.chat.models import ChatRoom, ChatMembership
    from mojo.apps.account.models import User
    emails = [f"cursor-{label}-{i}@example.com" for i in range(2)]
    User.objects.filter(email__in=emails).delete()
    ChatRoom.objects.filter(name=f"cursor-serial-{label}").delete()
    users = [User.objects.create_user(username=e, email=e, password="TestPass1!") for e in emails]
    for user in users:
        user.is_email_verified = True
        user.save()
    room = ChatRoom.objects.create(name=f"cursor-serial-{label}", user=users[0])
    seats = [ChatMembership.objects.create(room=room, user=u, read_backend="cursor") for u in users]
    return room, users, seats


@th.django_unit_test()
def test_static_cursor_orm_guards_and_maintenance(opts):
    from unittest.mock import patch
    from django.conf import settings as django_settings
    from mojo.apps.chat.models import ChatMessage, ChatMembership, ChatRoom
    from mojo.apps.chat.services.maintenance import relocate_messages, activate_initial
    room, users, seats = _fixture("orm")
    with patch.object(django_settings, "CHAT_READ_STATE_BACKEND", "cursor", create=True):
        other = ChatRoom.objects.create(name="cursor-serial-orm-dest")
        assert other.read_state_generation == 1, "Actual cursor configuration must initialize newly created rooms as activated"
        ChatMembership.objects.bulk_create([ChatMembership(room=other, user=u) for u in users])
        message = ChatMessage.objects.create(room=room, user=users[0], body="direct")
        assert message.room_seq == 1, "Actual static cursor mode must sequence ordinary manager creates"
        rejected = [
            lambda: ChatMessage.objects.bulk_create([ChatMessage(room=room, body="unsupported")]),
            lambda: ChatMessage.objects.filter(pk=message.pk).update(room=other),
            lambda: ChatMessage.objects.filter(pk=message.pk).update(room_seq=999),
            lambda: ChatMessage.objects.bulk_update([message], ["room_seq"]),
            lambda: ChatMembership.objects.filter(pk=seats[0].pk).update(room=other),
        ]
        for write in rejected:
            try:
                write()
                assert False, "Unsupported active cursor bulk/identity writer must fail explicitly"
            except ValueError:
                pass
        message.room_id = other.pk
        try:
            message.save()
            assert False, "Instance identity mutation must fail explicitly under actual cursor settings"
        except ValueError:
            pass
        message.refresh_from_db()
        relocate_messages([room.pk, other.pk], {message.pk: other.pk}, publisher=False)
        message.refresh_from_db()
        assert message.room_id == other.pk and message.room_seq == 1, "Privileged maintenance must resequence successfully while public guards are active"
        assert activate_initial([other.pk]) == 0, "Activation reruns must leave new cursor rooms alone"


@th.django_unit_test()
def test_file_only_backend_and_hook_selection(opts):
    from unittest.mock import patch
    from django.conf import settings as django_settings
    from django.core.exceptions import ImproperlyConfigured
    from mojo.apps.account.models import Setting
    from mojo.apps.chat.services.backend import read_backend
    from mojo.apps.chat.services.read_state import _handler, _UNSET
    Setting.set("CHAT_READ_STATE_BACKEND", "cursor")
    Setting.set("CHAT_READ_HANDLER", "untrusted.invalid")
    try:
        with patch.object(django_settings, "CHAT_READ_STATE_BACKEND", "legacy", create=True), patch.object(
                django_settings, "CHAT_READ_HANDLER", None, create=True):
            assert read_backend() == "legacy" and _handler(_UNSET) is None, "Writable DB settings must never activate cursor mode or a read handler"
        try:
            read_backend("unknown")
            assert False, "Invalid backend values must fail configuration validation"
        except ImproperlyConfigured:
            pass
    finally:
        Setting.remove("CHAT_READ_STATE_BACKEND")
        Setting.remove("CHAT_READ_HANDLER")


@th.django_unit_test()
def test_live_rest_ws_share_cursor_and_configured_hook(opts):
    from mojo.apps.chat.models import ChatMessage, ChatReadReceipt
    from testit.ws_client import WsClient
    from . import _capture
    room, users, seats = _fixture("live")
    _capture.clear(room.pk)
    with th.server_settings(CHAT_READ_STATE_BACKEND="cursor",
            CHAT_READ_HANDLER="tests.test_chat_read_state_extended_serial._capture.capture"):
        assert opts.client.login(users[0].email, "TestPass1!"), "Writer must log in for real socket send"
        writer_token = opts.client.access_token
        ws = WsClient(WsClient.build_url_from_host(opts.host, path="ws/realtime/"), logger=opts.logger)
        try:
            ws.connect(timeout=10)
            auth = ws.authenticate(writer_token, wait=True, timeout=10)
            assert auth.get("type") == "auth_success", f"Writer socket must authenticate: {auth}"
            ws.send_json({"type": "chat_message", "room_id": room.pk,
                          "body": "live cursor", "client_key": "cursor-live"})
            ack = ws.wait_for_types({"chat_message_ack", "error"}, timeout=10).data
            assert ack.get("type") == "chat_message_ack" and ack.get("room_seq") == 1 and ack.get("generation") == 1, f"Live send acknowledgment must expose authoritative sequence and generation: {ack}"
            message_id = ack["message_id"]
            ws.send_json({"type": "chat_message", "room_id": room.pk,
                          "body": "live cursor", "client_key": "cursor-live"})
            duplicate = ws.wait_for_types({"chat_message_ack", "error"}, timeout=10).data
            assert duplicate["message_id"] == message_id and duplicate["room_seq"] == 1, "Real socket retry must reuse original PK/sequence"
        finally:
            ws.close()
        assert opts.client.login(users[1].email, "TestPass1!"), "Reader must log in for real REST acknowledgment"
        counts = opts.client.get("/api/chat/unread")
        assert counts.status_code == 200 and any(p.room_id == room.pk and p.unread_count == 1 for p in counts.json.data), "Real unread endpoint must use durable arrival counts"
        response = opts.client.post("/api/chat/room/read", {"room_id": room.pk, "up_to_message_id": message_id})
        assert response.status_code == 200 and response.json.data.position.read_seq == 1, f"REST read must return effective durable cursor: {response.json}"
        reader_token = opts.client.access_token
        ws = WsClient(WsClient.build_url_from_host(opts.host, path="ws/realtime/"), logger=opts.logger)
        try:
            ws.connect(timeout=10)
            ws.authenticate(reader_token, wait=True, timeout=10)
            ws.send_json({"type": "chat_read", "room_id": room.pk, "up_to_message_id": message_id})
            ack = ws.wait_for_types({"chat_read_ack", "error"}, timeout=10).data
            assert ack.get("type") == "chat_read_ack" and ack["position"]["read_seq"] == 1 and not ack["changed"], "Real WS duplicate read must share the REST state and report a valid no-op"
        finally:
            ws.close()
        calls = _capture.read(room.pk)
        assert len(calls) == 3, f"Configured hook must run exactly once for send, REST read and WS duplicate: {calls}"
        assert [call["position"]["reason"] for call in calls] == ["own_send", "explicit", "explicit"], "Configured hook must preserve the actual source of each acknowledgment"
        assert not ChatReadReceipt.objects.filter(message__room=room).exists(), "Actual cursor REST/WS traffic must leave the normal receipt table empty"
    _capture.clear(room.pk)


@th.django_unit_test()
def test_reader_scope_uses_primary_for_durable_chat_state(opts):
    from unittest.mock import patch
    from django.db import router, connection
    from django.test.utils import CaptureQueriesContext
    from mojo.db import use_reader
    from mojo.db.router import ReaderRouter
    from mojo.apps.chat.models import ChatMessage, ChatMembership
    from mojo.apps.chat.services.read_state import recipient_positions, unread_counts, unread_queryset
    room, users, seats = _fixture("reader-routing")
    # No replica connection is needed: accidental reader routing raises for an
    # absent reader alias instead of silently proving freshness on primary.
    with patch.object(router, "routers", [ReaderRouter()]), use_reader():
        own = ChatMessage.objects.create(room=room, user=users[0], body="routing",
            read_backend="cursor", read_handler=None, read_publisher=False)
        assert own._state.db == "default", "Custom message create must resolve the write alias even inside a reader scope"
        seats[1].delete()
        with use_reader():
            created = ChatMembership.objects.bulk_create([
                ChatMembership(room=room, user=users[1])], read_backend="cursor")
        assert created[0]._state.db == "default" and created[0].last_read_seq == 1, "Bulk memberships must lock and initialize on the same writer alias even in an unpinned reader scope"
        seats[1] = created[0]
        # Make the new member's one existing arrival unread for snapshot checks.
        ChatMembership.objects.filter(pk=seats[1].pk).update(last_read_seq=0)
        seats[1].refresh_from_db()
        with use_reader(), CaptureQueriesContext(connection) as captured:
            positions = recipient_positions(room, backend="cursor")
            counts = unread_counts(users[1], backend="cursor")
            ids = list(unread_queryset(seats[1]).values_list("pk", flat=True))
        assert positions[users[1].pk].unread_count == 1 and counts[room.pk].unread_count == 1, "Fresh durable snapshots must stay on primary inside an explicit reader scope"
        assert ids == [own.pk] and len(captured) == 3, "Deferred eligibility must also remain primary and bounded when evaluated by its caller"
