"""Advisory moderation storage and wire contracts, with test-owned state only."""

TESTIT_TIER = "core"
from testit import helpers as th

EMAILS = ["chat-score-author@example.com", "chat-score-reader@example.com"]
PASSWORD = "TestPass1!"
PREFIX = "test-chat-score-"
FIELDS = ("moderation_decision", "moderation_reasons", "moderation_score")
LINKS = "http://one.example http://two.example http://three.example"


@th.django_unit_setup()
@th.requires_app("mojo.apps.chat")
def setup_moderation(opts):
    from mojo.apps.account.models import User
    from mojo.apps.chat.models import ChatRoom

    ChatRoom.objects.filter(name__startswith=PREFIX).delete()
    User.objects.filter(email__in=EMAILS).delete()
    users = []
    for email in EMAILS:
        user = User.objects.create_user(username=email, email=email, password=PASSWORD)
        user.is_email_verified = True
        user.save()
        users.append(user)
    opts.author, opts.reader = users


def _room(opts, name, **rules):
    from mojo.apps.chat.models import ChatRoom, ChatMembership

    ChatRoom.objects.filter(name=PREFIX + name).delete()
    room = ChatRoom.objects.create(
        name=PREFIX + name, kind="group", user=opts.author,
        rules={"rate_limit": 0, **rules})
    ChatMembership.objects.create(room=room, user=opts.author, role="owner")
    ChatMembership.objects.create(room=room, user=opts.reader)
    return room


def _state(message):
    return {key: getattr(message, key) for key in FIELDS}


def _assert_state(frame, message, surface):
    for key in FIELDS:
        th.assert_true(key in frame, f"{surface} must carry {key}, even when empty/null")
        th.assert_eq(frame[key], getattr(message, key), f"{surface} must use persisted {key}")


@th.django_unit_test()
def test_advisory_adapters_preserve_classifier(opts):
    from mojo.apps.chat.rules import check_moderation, check_moderation_scored
    from mojo.helpers import content_guard

    cases = [
        ("hello there", "allow", 0, []),
        ("http://one.example", "allow", 25, ["spam_link"]),
        ("http://one.example http://two.example", "warn", 50, ["spam_link"]),
        (LINKS, "masked", 75, ["spam_link"]),
        ("bastard bitch", "masked", 75, ["deny_hit", "repeated_profanity"]),
        ("fuck", "warn", 50, ["high_severity"]),
        ("fuck bastard", "masked", 95, ["high_severity", "deny_hit", "repeated_profanity"]),
    ]
    for body, decision, score, reasons in cases:
        raw = content_guard.check_text(body, surface="chat")
        actual = check_moderation_scored(body)
        th.assert_eq(actual[0], decision, f"advisory decision for {body!r}")
        th.assert_eq(actual[2], score, f"raw numeric score for {body!r}")
        th.assert_eq(actual[2], raw.score, "adapter must retain the classifier score")
        th.assert_eq(actual[1], list(raw.reasons), "adapter must retain exact reason order")
        th.assert_eq(set(actual[1]), set(reasons), f"reason categories for {body!r}")
        th.assert_eq(check_moderation(body), actual[:2], "legacy API must remain a two-tuple")


@th.django_unit_test()
def test_send_event_ack_retry_and_graphs_share_persisted_state(opts):
    from mojo.apps.chat.handler import _handle_send
    from mojo.apps.chat.models import ChatMessage
    from mojo.apps.chat.rules import check_moderation_scored

    room = _room(opts, "send")
    events = []
    capture = lambda topic, frame: events.append((topic, frame))
    bodies = ["hello there", "http://one.example", "http://one.example http://two.example",
              LINKS, "bastard bitch", "fuck", "fuck bastard"]
    for i, body in enumerate(bodies):
        payload = {
            "room_id": room.pk, "body": "  " + body + "  ", "client_key": f"score-{i}",
            "moderation_decision": "allow", "moderation_score": 0,
            "moderation_reasons": [],
            "metadata": {"moderation_score": 0, "moderation_reasons": ["opaque"]},
        }
        ack = _handle_send(opts.author, payload, publisher=capture)
        th.assert_eq(ack["type"], "chat_message_ack", f"every language score must send: {ack}")
        msg = ChatMessage.objects.get(pk=ack["message_id"])
        th.assert_eq(msg.body, body, "store the real body with existing whitespace trimming")
        th.assert_eq(tuple(_state(msg).values()), check_moderation_scored(body),
                     "send must persist all raw classifier state despite client-supplied fields")
        th.assert_eq(msg.metadata, payload["metadata"], "kind metadata must remain untouched")
        _assert_state(ack, msg, "fresh ack")
        th.assert_eq(events[-1][0], room.topic, "publish to the correct room")
        event = events[-1][1]
        th.assert_eq(event["body"], body, "authorized event must retain real body")
        _assert_state(event, msg, "new-message event")
        for graph in ("list", "default"):
            _assert_state(msg.to_dict(graph=graph), msg, graph + " graph")
        retry = _handle_send(opts.author, payload, publisher=capture)
        th.assert_eq(retry, ack, "retry must return the same persisted state and message id")
        th.assert_eq(len(events), i + 1, "retry must not publish another event")
    th.assert_eq(ChatMessage.objects.filter(room=room).count(), len(bodies), "each logical send stores once")


@th.django_unit_test()
def test_racing_sends_ack_the_winning_moderation_state(opts):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from django.db import connection, IntegrityError
    from mojo.apps.chat.handler import _handle_send
    from mojo.apps.chat.models import ChatMessage

    room = _room(opts, "race")
    barrier = Barrier(2)
    collisions, events = [], []
    payload = {"room_id": room.pk, "body": "bastard bitch", "client_key": "score-race"}

    def rendezvous(execute, sql, params, many, context):
        if sql.startswith('INSERT INTO "chat_chatmessage"'):
            # Per-thread connection hook: both real sends have passed dedupe
            # before either INSERT starts. No shared application patch.
            barrier.wait(timeout=10)
            try:
                return execute(sql, params, many, context)
            except IntegrityError:
                collisions.append(True)
                raise
        return execute(sql, params, many, context)

    def send():
        try:
            with connection.execute_wrapper(rendezvous):
                return _handle_send(
                    opts.author, payload,
                    publisher=lambda topic, frame: events.append(frame))
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(send), pool.submit(send)]
        acks = [future.result(timeout=20) for future in futures]
    th.assert_eq(len(collisions), 1, "must exercise the unique-constraint race recovery")
    th.assert_eq(acks[0], acks[1], "winner and race loser must acknowledge identical persisted state")
    th.assert_eq(acks[0]["type"], "chat_message_ack", f"both race sends should succeed: {acks}")
    msg = ChatMessage.objects.get(room=room)
    _assert_state(acks[0], msg, "race ack")
    th.assert_eq(msg.moderation_score, 75, "racing message must retain the classifier score")
    th.assert_eq(len(events), 1, "only the winning insert may publish")
    _assert_state(events[0], msg, "race event")


@th.django_unit_test()
def test_edits_replace_state_and_refusals_change_nothing(opts):
    from mojo.apps.chat.handler import _handle_edit
    from mojo.apps.chat.services.messages import send_message
    from mojo.apps.chat.rules import check_moderation_scored

    room = _room(opts, "edit")
    msg, error = send_message(room, opts.author, "hello there", broadcast=False)
    th.assert_eq(error, None, f"initial send should succeed: {error}")
    events = []
    capture = lambda topic, frame: events.append(frame)
    for body in [LINKS, "fuck", "fuck bastard", "hello again"]:
        ack = _handle_edit(opts.author, {"message_id": msg.pk, "body": body}, publisher=capture)
        th.assert_eq(ack["type"], "chat_edit_ack", f"all language edits must store: {ack}")
        msg.refresh_from_db()
        decision, reasons, score = check_moderation_scored(body)
        th.assert_eq(_state(msg), dict(zip(FIELDS, [decision, reasons, score])), "edit replaces all moderation fields")
        th.assert_eq(msg.body, body, "edit stores its actual body")
        _assert_state(ack, msg, "edit ack")
        _assert_state(events[-1], msg, "edit event")
        th.assert_eq(events[-1]["body"], body, "edit event retains real body")
    th.assert_eq(_state(msg), dict(zip(FIELDS, ["allow", [], 0])), "clean edit clears prior hidden state")
    before = (msg.body, msg.edited_at, msg.created, _state(msg))
    room.rules["allow_urls"] = False
    room.save()
    for actor, body, expected in [
        (opts.author, LINKS, "URLs are not allowed in this room"),
        (opts.reader, "another edit", "Cannot edit this message"),
    ]:
        refusal = _handle_edit(actor, {"message_id": msg.pk, "body": body}, publisher=capture)
        th.assert_eq(refusal["error"], expected, "existing edit refusal must retain its reason")
        msg.refresh_from_db()
        th.assert_eq((msg.body, msg.edited_at, msg.created, _state(msg)), before, "refused edit must leave the entire message unchanged")
    th.assert_eq(len(events), 4, "refused edits must not publish")


@th.django_unit_test()
def test_unscored_bypass_and_broadcast_extras_cannot_forge_state(opts):
    from mojo.apps.chat.services.messages import send_message
    from mojo.apps.chat.handler import _send_ack

    room = _room(opts, "bypass", allow_urls=False)
    events = []
    msg, error = send_message(
        room, opts.author, LINKS, kind="system", client_authored=False,
        enforce_room_policy=False, publisher=lambda topic, frame: events.append(frame),
        broadcast_extra={"body": "forged", "moderation_score": 99,
                         "moderation_reasons": ["forged"], "moderation_decision": "masked",
                         "consumer_field": "retained"})
    th.assert_eq(error, None, f"explicit trusted bypass should store: {error}")
    th.assert_eq(_state(msg), dict(zip(FIELDS, ["allow", [], None])), "bypass must remain distinctly unscored")
    _assert_state(events[0], msg, "bypass event")
    _assert_state(_send_ack(msg, room, None), msg, "bypass ack")
    th.assert_eq(events[0]["body"], LINKS, "extras cannot overwrite authoritative body")
    th.assert_eq(events[0]["consumer_field"], "retained", "extras may still add consumer fields")


@th.django_unit_test()
def test_moderation_fields_are_protected_from_generic_rest(opts):
    from types import SimpleNamespace
    from mojo.apps.chat.services.messages import send_message

    room = _room(opts, "protected")
    msg, error = send_message(room, opts.author, LINKS, broadcast=False)
    th.assert_eq(error, None, f"initial send should succeed: {error}")
    before = _state(msg)
    data = {"moderation_decision": "allow", "moderation_reasons": [], "moderation_score": 0}
    request = SimpleNamespace(user=opts.author, group=None, DATA=data)
    # ChatMessage has no generic write route; exercise the actual model save
    # dispatcher its NO_SAVE_FIELDS protects, without inventing an endpoint.
    msg.on_rest_save(request, data)
    th.assert_eq(_state(msg), before, "generic REST must not mutate moderation even in memory")
    msg.refresh_from_db()
    th.assert_eq(_state(msg), before, "generic REST must not persist forged moderation")


@th.django_unit_test()
def test_old_writer_omitting_columns_uses_database_defaults(opts):
    from django.db import connection
    from mojo.apps.chat.models import ChatMessage
    from mojo.helpers import dates

    room = _room(opts, "old-writer")
    # An application rollback INSERT names only old columns. This is not a
    # schema workaround: the two new columns must already exist after migrate.
    with connection.cursor() as cursor:
        cursor.execute('''INSERT INTO chat_chatmessage
            (room_id, user_id, body, kind, moderation_decision, is_flagged, metadata, created)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id''',
            [room.pk, opts.author.pk, "legacy text", "text", "warn", False, "{}", dates.utcnow()])
        message_id = cursor.fetchone()[0]
        cursor.execute('''SELECT column_default, is_nullable FROM information_schema.columns
            WHERE table_name = 'chat_chatmessage' AND column_name = 'moderation_reasons' ''')
        reasons_default, nullable = cursor.fetchone()
    msg = ChatMessage.objects.get(pk=message_id)
    th.assert_eq(_state(msg), dict(zip(FIELDS, ["warn", [], None])), "old writer must receive unknown score and empty reasons")
    th.assert_true(reasons_default and "[]" in reasons_default, "reasons must keep a persistent database empty-list default")
    th.assert_eq(nullable, "NO", "reasons must remain a non-null list column")
    for graph in ("list", "default"):
        _assert_state(msg.to_dict(graph=graph), msg, "old-writer " + graph)


@th.django_unit_test()
def test_live_history_visibility_and_read_counts_ignore_language_score(opts):
    from datetime import timedelta
    from mojo.apps.chat.handler import _handle_send
    from mojo.apps.chat.models import ChatMessage, ChatMembership
    from mojo.helpers import dates

    room = _room(opts, "history", disappearing_ttl=60)
    # Make the expired fixture post-join so TTL is tested independently.
    ChatMembership.objects.filter(room=room, user=opts.reader).update(
        joined_at=dates.utcnow() - timedelta(seconds=300))
    messages = []
    for body in ["hello there", "http://one.example", LINKS, "fuck"]:
        ack = _handle_send(opts.author, {"room_id": room.pk, "body": body}, publisher=lambda *args: None)
        th.assert_eq(ack["type"], "chat_message_ack", f"history fixture send should succeed: {ack}")
        messages.append(ChatMessage.objects.get(pk=ack["message_id"]))
    legacy = ChatMessage.objects.create(room=room, user=opts.author, body="legacy text", moderation_decision="warn")
    messages.append(legacy)
    hidden = ChatMessage.objects.create(room=room, user=opts.author, body=LINKS, moderation_decision="masked", moderation_score=75, is_flagged=True)
    expired = ChatMessage.objects.create(room=room, user=opts.author, body=LINKS, moderation_decision="masked", moderation_score=75)
    ChatMessage.objects.filter(pk=expired.pk).update(created=dates.utcnow() - timedelta(seconds=120))
    opts.client.login(EMAILS[1], PASSWORD)
    response = opts.client.get('/api/chat/room/messages', params={"room_id": room.pk})
    th.assert_eq(response.status_code, 200, f"member history must succeed: {response.json}")
    rows = {row["id"]: row for row in response.json.data}
    th.assert_eq(set(rows), {msg.pk for msg in messages}, "scores must not filter authorized history; flags and TTL still do")
    for msg in messages:
        _assert_state(rows[msg.pk], msg, "live history")
        th.assert_eq(rows[msg.pk]["body"], msg.body, "history retains real content")
    unread = opts.client.get('/api/chat/unread')
    row = next(row for row in unread.json.data if row["room_id"] == room.pk)
    th.assert_eq(row["unread_count"], len(messages), "masked and warned messages count as unread")
    membership = ChatMembership.objects.get(room=room, user=opts.reader)
    membership.joined_at = legacy.created
    membership.save(update_fields=["joined_at"])
    joined = opts.client.get('/api/chat/room/messages', params={"room_id": room.pk})
    th.assert_eq([row["id"] for row in joined.json.data], [legacy.pk], "join bound still excludes earlier scored messages")
    membership.status = "banned"
    membership.save(update_fields=["status"])
    refused = opts.client.get('/api/chat/room/messages', params={"room_id": room.pk})
    th.assert_eq(refused.status_code, 403, "room bans still refuse history")


@th.django_unit_test()
def test_non_language_send_refusals_keep_client_key(opts):
    from mojo.apps.chat.handler import _handle_send
    from mojo.apps.chat.models import ChatMessage, ChatMembership

    room = _room(opts, "refusal", allow_urls=False)
    events = []
    capture = lambda topic, frame: events.append(frame)
    payload = {"room_id": room.pk, "body": LINKS, "client_key": "score-refusal"}
    refusal = _handle_send(opts.author, payload, publisher=capture)
    th.assert_eq(refusal["error"], "URLs are not allowed in this room", "explicit room URL rule still refuses")
    th.assert_eq(refusal["client_key"], payload["client_key"], "room refusal must correlate to send")
    membership = ChatMembership.objects.get(room=room, user=opts.author)
    membership.status = "muted"
    membership.save(update_fields=["status"])
    refusal = _handle_send(opts.author, {**payload, "body": "hello"}, publisher=capture)
    th.assert_eq(refusal["error"], "Cannot send messages (status: muted)", "membership status still gates sending")
    th.assert_eq(refusal["client_key"], payload["client_key"], "permission refusal must correlate to send")
    th.assert_eq(ChatMessage.objects.filter(room=room).count(), 0, "refusals must not store a row")
    th.assert_eq(events, [], "refusals must not publish")
