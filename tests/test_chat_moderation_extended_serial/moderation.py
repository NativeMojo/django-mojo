"""Runtime moderation overrides; serial because Setting and classifier are shared."""
from contextlib import contextmanager

from testit import helpers as th

KEY = "CHAT_MODERATION_ENABLED"
BODY = "http://one.example http://two.example http://three.example"
PREFIX = "test-chat-moderation-toggle-"
EMAILS = [PREFIX + role + "@example.com" for role in ("author", "reader")]
FIELDS = ("moderation_decision", "moderation_reasons", "moderation_score")
UNSCORED = ("allow", [], None)
SCORED = ("masked", ["spam_link"], 75)


@contextmanager
def _restore_setting():
    from mojo.apps.account.models import Setting

    original = Setting.objects.filter(key=KEY, group=None).first()
    original_value = original.get_value() if original else None
    try:
        yield Setting
    finally:
        if original:
            Setting.set(KEY, original_value, is_secret=original.is_secret)
        else:
            Setting.remove(KEY)


@th.django_unit_setup()
def setup_moderation_toggle(opts):
    from mojo.apps.account.models import User
    from mojo.apps.chat.models import ChatRoom

    ChatRoom.objects.filter(name__startswith=PREFIX).delete()
    User.objects.filter(email__in=EMAILS).delete()
    opts.author, opts.reader = [
        User.objects.create_user(username=email, email=email, password="TestPass1!")
        for email in EMAILS
    ]


def _room(opts, name, **rules):
    from mojo.apps.chat.models import ChatRoom, ChatMembership

    ChatRoom.objects.filter(name=PREFIX + name).delete()
    room = ChatRoom.objects.create(
        name=PREFIX + name, kind="group", user=opts.author,
        rules={"rate_limit": 0, **rules})
    ChatMembership.objects.create(room=room, user=opts.author, role="owner")
    ChatMembership.objects.create(room=room, user=opts.reader)
    return room


def _assert_state(message, expected, *frames):
    message.refresh_from_db()
    th.assert_eq(tuple(getattr(message, key) for key in FIELDS), expected,
                 "persist all three moderation fields")
    for frame in frames:
        for key, value in zip(FIELDS, expected):
            th.assert_true(key in frame, f"wire frame must include {key}, even null/empty")
            th.assert_eq(frame[key], value, f"wire {key} must match persisted state")


@th.django_unit_test()
def test_runtime_toggle_and_default_control_both_adapters(opts):
    from unittest.mock import patch
    from mojo.apps.chat.rules import check_moderation, check_moderation_scored

    with _restore_setting() as Setting:
        Setting.remove(KEY)
        th.assert_eq(check_moderation_scored(BODY), SCORED, "absent setting defaults to scoring")
        th.assert_eq(check_moderation(BODY), SCORED[:2], "legacy adapter defaults to scoring")
        for value in (False, "false"):
            Setting.set(KEY, value)
            with patch("mojo.helpers.content_guard.check_text") as classifier:
                th.assert_eq(check_moderation_scored(BODY), UNSCORED,
                             f"runtime {value!r} disables scoring")
                th.assert_eq(check_moderation(BODY), UNSCORED[:2],
                             "legacy adapter retains its two-tuple while disabled")
                th.assert_eq(classifier.call_count, 0, "disabled adapter must not invoke classifier")
            Setting.set(KEY, True)
            th.assert_eq(check_moderation_scored(BODY), SCORED, "re-enable applies without reload")
            th.assert_eq(check_moderation(BODY), SCORED[:2], "legacy adapter re-enables too")


@th.django_unit_test()
def test_toggle_send_and_edit_persist_and_publish_current_state(opts):
    from mojo.apps.chat.handler import _handle_edit, _handle_send
    from mojo.apps.chat.models import ChatMessage
    from mojo.apps.chat.services.messages import send_message

    room = _room(opts, "wire")
    events = []
    capture = lambda topic, frame: events.append(frame)
    with _restore_setting() as Setting:
        Setting.set(KEY, True)
        old, error = send_message(room, opts.author, BODY, publisher=capture)
        th.assert_eq(error, None, "enabled send succeeds")
        _assert_state(old, SCORED, events[-1])
        Setting.set(KEY, False)
        msg, error = send_message(room, opts.author, BODY, publisher=capture)
        th.assert_eq(error, None, "disabled send succeeds")
        _assert_state(msg, UNSCORED, events[-1], msg.to_dict(graph="default"))
        ack = _handle_send(opts.author, {"room_id": room.pk, "body": BODY}, publisher=capture)
        th.assert_eq(ack["type"], "chat_message_ack", "disabled send acknowledges")
        sent = ChatMessage.objects.get(pk=ack["message_id"])
        _assert_state(sent, UNSCORED, ack, events[-1])
        ack = _handle_edit(opts.author, {"message_id": old.pk, "body": BODY + " edited"}, publisher=capture)
        th.assert_eq(ack["type"], "chat_edit_ack", "disabled edit acknowledges")
        _assert_state(old, UNSCORED, ack, events[-1])
        th.assert_eq(old.body, BODY + " edited", "disabled edit retains real text")
        Setting.set(KEY, True)
        ack = _handle_edit(opts.author, {"message_id": old.pk, "body": BODY}, publisher=capture)
        th.assert_eq(ack["type"], "chat_edit_ack", "re-enabled edit acknowledges")
        _assert_state(old, SCORED, ack, events[-1])
        fresh, error = send_message(room, opts.author, BODY, publisher=capture)
        th.assert_eq(error, None, "re-enabled send succeeds")
        _assert_state(fresh, SCORED, events[-1])


@th.django_unit_test()
def test_disabled_moderation_preserves_room_and_authorization_refusals(opts):
    from mojo.apps.chat.handler import _handle_edit, _handle_send
    from mojo.apps.chat.models import ChatMessage, ChatMembership
    from mojo.apps.chat.services.messages import send_message

    events = []
    capture = lambda topic, frame: events.append(frame)
    with _restore_setting() as Setting:
        Setting.set(KEY, False)
        for name, rules, body, expected in [
            ("urls", {"allow_urls": False}, BODY, "URLs are not allowed in this room"),
            ("phones", {"allow_phone_numbers": False}, "call +1 (415) 555-1212",
             "Phone numbers are not allowed in this room"),
            ("length", {"max_message_length": 8}, "a" * 9, "Message exceeds max length of 8"),
        ]:
            room = _room(opts, name, **rules)
            msg, error = send_message(room, opts.author, body, publisher=capture)
            th.assert_eq(msg, None, "room policy still refuses send")
            th.assert_eq(error["error"], expected, "send preserves room policy error")
            existing = ChatMessage.objects.create(room=room, user=opts.author, body="original")
            ack = _handle_edit(opts.author, {"message_id": existing.pk, "body": body}, publisher=capture)
            th.assert_eq(ack["error"], expected, "edit preserves room policy error")
            existing.refresh_from_db()
            th.assert_eq(existing.body, "original", "refused edit leaves stored text unchanged")
            th.assert_eq(ChatMessage.objects.filter(room=room).count(), 1, "refused send stores no row")
        room = _room(opts, "permission")
        membership = ChatMembership.objects.get(room=room, user=opts.author)
        membership.status = "muted"
        membership.save(update_fields=["status"])
        ack = _handle_send(opts.author, {"room_id": room.pk, "body": BODY}, publisher=capture)
        th.assert_eq(ack["error"], "Cannot send messages (status: muted)", "mute still blocks sending")
        existing = ChatMessage.objects.create(room=room, user=opts.author, body="original")
        ack = _handle_edit(opts.reader, {"message_id": existing.pk, "body": BODY}, publisher=capture)
        th.assert_eq(ack["error"], "Cannot edit this message", "another member cannot edit")
        existing.refresh_from_db()
        th.assert_eq(existing.body, "original", "unauthorized edit leaves stored text unchanged")
        th.assert_eq(events, [], "refused sends and edits never publish")


@th.django_unit_test()
def test_disabled_moderation_preserves_rate_limit(opts):
    import time
    from mojo.apps.chat.services.messages import send_message
    from mojo.helpers.redis.client import get_connection

    room = _room(opts, "rate", rate_limit=1)
    redis = get_connection()
    key = f"chat:rate:{room.pk}:{opts.author.pk}"
    events = []
    try:
        with _restore_setting() as Setting:
            Setting.set(KEY, False)
            # A test-owned room key, future-dated to avoid wall-clock flakiness.
            redis.zadd(key, {"existing": time.time() + 60})
            redis.expire(key, 90)
            msg, error = send_message(
                room, opts.author, BODY, publisher=lambda topic, frame: events.append(frame))
            th.assert_eq(msg, None, "rate-limited send stores no message")
            th.assert_eq(error["error"], "Rate limit exceeded", "rate limit remains active")
            th.assert_eq(events, [], "rate-limited send never publishes")
    finally:
        redis.delete(key)
