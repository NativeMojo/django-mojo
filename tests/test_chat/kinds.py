"""
Tests for the chat send service: the kind whitelist, metadata validation and
the byte cap, the card payload rules, the body rule, and the two independent
policy gates.

Nothing here patches or reaches a setting. `tests/test_chat` is scanned strict
(the package carries `@th.tier("core")` tests and declares `default_core` with
no cold budget), so `th.server_settings()` and `mock.patch` both fail the whole
package. The keyword-only sentinel seams on `send_message`
(`validators=`, `max_bytes=`, `publisher=`) are the only legal route.
"""

TESTIT_TIER = "extended"
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_EMAIL_1 = 'chat-kinds-user1@example.com'
TEST_EMAIL_2 = 'chat-kinds-user2@example.com'
TEST_PASSWORD = 'TestPass1!'

ROOM_PREFIX = "test-kinds-"

# Three links score 25 each against a block threshold of 70.
BLOCKED_BODY = (
    "http://spam-one.example http://spam-two.example http://spam-three.example")


@th.django_unit_setup()
@th.requires_app("mojo.apps.chat")
def setup_chat_kinds(opts):
    from mojo.apps.account.models import User
    from mojo.apps.chat.models import ChatRoom

    # Delete before create -- the database is long lived, not fresh.
    User.objects.filter(email__in=[TEST_EMAIL_1, TEST_EMAIL_2]).delete()
    ChatRoom.objects.filter(name__startswith=ROOM_PREFIX).delete()

    opts.user1 = User.objects.create_user(
        username=TEST_EMAIL_1, email=TEST_EMAIL_1, password=TEST_PASSWORD,
    )
    opts.user1.is_email_verified = True
    opts.user1.save()
    opts.user2 = User.objects.create_user(
        username=TEST_EMAIL_2, email=TEST_EMAIL_2, password=TEST_PASSWORD,
    )
    opts.user2.is_email_verified = True
    opts.user2.save()


def _make_room(name, user, rules=None):
    """A fresh room owned by `user`, replacing any prior run's copy."""
    from mojo.apps.chat.models import ChatRoom, ChatMembership

    full_name = f"{ROOM_PREFIX}{name}"
    ChatRoom.objects.filter(name=full_name).delete()
    room = ChatRoom.objects.create(name=full_name, kind="group", user=user)
    if rules is not None:
        merged = dict(room.rules or {})
        merged.update(rules)
        room.rules = merged
        room.save()
    ChatMembership.objects.get_or_create(
        room=room, user=user, defaults={"role": "owner"})
    return room


def _echo_validator(room, user, metadata):
    """A host validator that accepts the payload unchanged."""
    return metadata


# ---------------------------------------------------------------------------
# kind whitelist
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_card_kind_accepted_with_validator(opts):
    """A client may author `card` once the host registers a validator."""
    from mojo.apps.chat.services.messages import send_message

    room = _make_room("card-ok", opts.user1)
    payload = {"card": {"type": "board_item", "id": 3337}}

    msg, error = send_message(
        room, opts.user1, "look at this", kind="card", metadata=payload,
        broadcast=False, validators={"card": _echo_validator})

    assert_eq(error, None, f"expected the card to be accepted, got {error}")
    assert_true(msg is not None, "expected a stored message")
    assert_eq(msg.kind, "card", f"expected kind=card, got {msg.kind}")
    assert_eq(msg.metadata, payload, "expected the payload to be stored intact")


@th.tier("core")
@th.django_unit_test()
def test_card_kind_refused_without_validator(opts):
    """With no validator registered, `card` is off -- fail closed."""
    from mojo.apps.chat.services.messages import send_message

    room = _make_room("card-no-validator", opts.user1)

    msg, error = send_message(
        room, opts.user1, "look at this", kind="card",
        metadata={"card": {"type": "board_item"}},
        broadcast=False, validators={})

    assert_true(msg is None, "expected no message to be stored")
    assert_true(error is not None, "expected a refusal")
    assert_eq(
        error["error"], "Unsupported message kind",
        f"expected the kind whitelist to refuse it, got {error['error']}")


@th.tier("core")
@th.django_unit_test()
def test_client_cannot_send_system_kind(opts):
    """A WebSocket client cannot forge a system message."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatMessage

    room = _make_room("client-system", opts.user1)

    result = handle_chat_message(opts.user1, {
        "type": "chat_message",
        "room_id": room.pk,
        "body": "everyone was banned",
        "kind": "system",
    })

    assert_eq(result["type"], "error", f"expected a refusal, got {result}")
    assert_eq(
        result["error"], "Unsupported message kind",
        f"expected the kind whitelist to refuse system, got {result['error']}")
    assert_eq(
        ChatMessage.objects.filter(room=room).count(), 0,
        "expected no system message to be stored for a client-authored frame")


@th.tier("core")
@th.django_unit_test()
def test_client_cannot_send_file_kind(opts):
    """`file` is server-authored only, even though the model lists the kind."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatMessage

    room = _make_room("client-file", opts.user1)

    result = handle_chat_message(opts.user1, {
        "type": "chat_message",
        "room_id": room.pk,
        "body": "here is a file",
        "kind": "file",
        "metadata": {"file": {"id": 1}},
    })

    assert_eq(result["type"], "error", f"expected a refusal, got {result}")
    assert_eq(
        result["error"], "Unsupported message kind",
        f"expected the kind whitelist to refuse file, got {result['error']}")
    assert_eq(
        ChatMessage.objects.filter(room=room).count(), 0,
        "expected no file message to be stored for a client-authored frame")


@th.django_unit_test()
def test_server_authored_file_kind_accepted(opts):
    """A server-side caller may author `file` with no validator registered."""
    from mojo.apps.chat.services.messages import send_message

    room = _make_room("server-file", opts.user1)
    payload = {"file": {"id": 42, "filename": "report.pdf", "file_size": 1024}}

    msg, error = send_message(
        room, opts.user1, "the quarterly report", kind="file",
        metadata=payload, client_authored=False, broadcast=False,
        validators={})

    assert_eq(error, None, f"expected the file message to be accepted, got {error}")
    assert_eq(msg.kind, "file", f"expected kind=file, got {msg.kind}")
    assert_eq(msg.metadata, payload, "expected the file reference to be stored intact")


@th.tier("core")
@th.django_unit_test()
def test_unknown_kind_refused(opts):
    """An unrecognized kind string is refused on both axes."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.services.messages import send_message

    room = _make_room("unknown-kind", opts.user1)

    result = handle_chat_message(opts.user1, {
        "type": "chat_message",
        "room_id": room.pk,
        "body": "what am i",
        "kind": "sparkle",
    })
    assert_eq(result["type"], "error", f"expected a refusal, got {result}")
    assert_eq(
        result["error"], "Unsupported message kind",
        f"expected an unsupported-kind error, got {result['error']}")

    msg, error = send_message(
        room, opts.user1, "what am i", kind="sparkle",
        client_authored=False, broadcast=False, validators={})
    assert_true(msg is None, "expected a server-side caller to be refused too")
    assert_eq(
        error["error"], "Unsupported message kind",
        f"expected an unsupported-kind error, got {error['error']}")


@th.tier("core")
@th.django_unit_test()
def test_validator_load_failure_refuses_only_that_kind(opts):
    """An unloadable validator kills its own kind and nothing else."""
    from mojo.apps.chat.services.messages import send_message

    room = _make_room("validator-broken", opts.user1)
    broken = {"card": "mojo.apps.chat.services.no_such_module.validate_card"}

    msg, error = send_message(
        room, opts.user1, "a card", kind="card", metadata={"card": {"x": 1}},
        broadcast=False, validators=broken)
    assert_true(msg is None, "expected the card send to be refused")
    assert_true(
        "not available" in error["error"],
        f"expected a fail-closed refusal for card, got {error['error']}")
    assert_true(
        "no_such_module" not in error["error"],
        "expected the refusal not to leak the configured path")

    text_msg, text_error = send_message(
        room, opts.user1, "plain text still works", kind="text",
        broadcast=False, validators=broken)
    assert_eq(
        text_error, None,
        f"expected text to be unaffected by a broken card validator, got {text_error}")
    assert_eq(text_msg.kind, "text", f"expected kind=text, got {text_msg.kind}")


# ---------------------------------------------------------------------------
# metadata validation
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_metadata_byte_cap_enforced(opts):
    """Metadata over the byte cap is refused; under it is accepted."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatMessage
    from mojo.apps.chat.services.messages import send_message

    room = _make_room("metadata-cap", opts.user1)

    over, error = send_message(
        room, opts.user1, "caption", kind="image",
        metadata={"blob": "x" * 200}, broadcast=False, max_bytes=64)
    assert_true(over is None, "expected an oversized payload to be refused")
    assert_true(
        "64 byte limit" in error["error"],
        f"expected the seam's limit in the error, got {error['error']}")

    under, under_error = send_message(
        room, opts.user1, "caption", kind="image",
        metadata={"blob": "x"}, broadcast=False, max_bytes=64)
    assert_eq(
        under_error, None,
        f"expected a payload under the limit to be accepted, got {under_error}")
    assert_true(under is not None, "expected the small payload to be stored")

    # The 4096-byte default applies with no seam, through the real client path.
    result = handle_chat_message(opts.user1, {
        "type": "chat_message",
        "room_id": room.pk,
        "body": "caption",
        "kind": "image",
        "metadata": {"blob": "x" * 5000},
    })
    assert_eq(result["type"], "error", f"expected the default cap to refuse it, got {result}")
    assert_true(
        "4096 byte limit" in result["error"],
        f"expected the 4096 byte default in the error, got {result['error']}")
    assert_eq(
        ChatMessage.objects.filter(room=room).count(), 1,
        "expected only the accepted message to be stored")


@th.django_unit_test()
def test_metadata_rejects_non_json_types(opts):
    """Only JSON scalars, lists and dicts with string keys are allowed."""
    from mojo.apps.chat.services.messages import send_message

    room = _make_room("metadata-types", opts.user1)

    msg, error = send_message(
        room, opts.user1, "caption", kind="image",
        metadata={"when": object()}, broadcast=False)
    assert_true(msg is None, "expected a non-JSON value to be refused")
    assert_true(
        "unsupported value type" in error["error"],
        f"expected an unsupported-type error, got {error['error']}")

    keyed, key_error = send_message(
        room, opts.user1, "caption", kind="image",
        metadata={7: "int key"}, broadcast=False)
    assert_true(keyed is None, "expected a non-string key to be refused")
    assert_true(
        "keys must be strings" in key_error["error"],
        f"expected a string-key error, got {key_error['error']}")

    listed, list_error = send_message(
        room, opts.user1, "caption", kind="image",
        metadata={"tags": ["a", 1, True, None, 2.5]}, broadcast=False)
    assert_eq(
        list_error, None,
        f"expected a list of JSON scalars to be accepted, got {list_error}")
    assert_eq(
        listed.metadata["tags"], ["a", 1, True, None, 2.5],
        "expected the list to be stored intact")


@th.django_unit_test()
def test_metadata_rejects_non_finite_float(opts):
    """NaN/Infinity are invalid JSON and break every non-Python consumer."""
    from mojo.apps.chat.services.messages import send_message

    room = _make_room("metadata-nan", opts.user1)

    for bad in (float("nan"), float("inf"), float("-inf")):
        msg, error = send_message(
            room, opts.user1, "caption", kind="image",
            metadata={"score": bad}, broadcast=False)
        assert_true(msg is None, f"expected {bad!r} to be refused")
        assert_true(
            "finite" in error["error"],
            f"expected a finiteness error for {bad!r}, got {error['error']}")


@th.django_unit_test()
def test_metadata_depth_cap_enforced(opts):
    """Nesting deeper than the depth cap is refused."""
    from mojo.apps.chat.services.messages import send_message

    room = _make_room("metadata-depth", opts.user1)

    deep = {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}
    msg, error = send_message(
        room, opts.user1, "caption", kind="image", metadata=deep,
        broadcast=False)
    assert_true(msg is None, "expected deeply nested metadata to be refused")
    assert_true(
        "nested deeper" in error["error"],
        f"expected a depth error, got {error['error']}")

    shallow = {"a": {"b": {"c": 1}}}
    ok, ok_error = send_message(
        room, opts.user1, "caption", kind="image", metadata=shallow,
        broadcast=False)
    assert_eq(
        ok_error, None,
        f"expected nesting within the cap to be accepted, got {ok_error}")
    assert_eq(ok.metadata, shallow, "expected the nested payload to be stored intact")


# ---------------------------------------------------------------------------
# card payloads vs room rules
# ---------------------------------------------------------------------------

@th.tier("core")
@th.django_unit_test()
def test_card_payload_respects_allow_urls(opts):
    """A card cannot smuggle a link into a room whose owner disabled links."""
    from mojo.apps.chat.services.messages import send_message
    from mojo.apps.chat.models import ChatMessage

    room = _make_room("card-no-urls", opts.user1, rules={"allow_urls": False})

    msg, error = send_message(
        room, opts.user1, "a clean caption", kind="card",
        metadata={"card": {"link": "https://evil.tld/lure"}},
        broadcast=False, validators={"card": _echo_validator})

    assert_true(msg is None, "expected the payload URL to be refused")
    assert_eq(
        error["error"], "URLs are not allowed in this room",
        f"expected the room's URL rule to refuse it, got {error['error']}")
    assert_eq(
        ChatMessage.objects.filter(room=room).count(), 0,
        "expected nothing to be stored")


@th.django_unit_test()
def test_card_payload_urls_allowed_when_rule_on(opts):
    """The same payload is fine in a room that allows links."""
    from mojo.apps.chat.services.messages import send_message

    room = _make_room("card-urls-ok", opts.user1, rules={"allow_urls": True})
    payload = {"card": {"link": "https://maestromojo.com/app/#/board/116?item=3357"}}

    msg, error = send_message(
        room, opts.user1, "a clean caption", kind="card", metadata=payload,
        broadcast=False, validators={"card": _echo_validator})

    assert_eq(error, None, f"expected the card to be accepted, got {error}")
    assert_eq(
        msg.metadata["card"]["link"],
        "https://maestromojo.com/app/#/board/116?item=3357",
        "expected the URL fragment to survive storage untouched")


# ---------------------------------------------------------------------------
# the body rule
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_body_required_for_text(opts):
    """`text` keeps today's behavior exactly: a body is mandatory."""
    from mojo.apps.chat.services.messages import send_message

    room = _make_room("body-text", opts.user1)

    msg, error = send_message(
        room, opts.user1, "   ", kind="text",
        metadata={"note": "metadata does not substitute for a text body"},
        broadcast=False)

    assert_true(msg is None, "expected an empty text body to be refused")
    assert_eq(
        error["error"], "body is required",
        f"expected the text body error, got {error['error']}")


@th.django_unit_test()
def test_body_optional_when_metadata_present(opts):
    """An uncaptioned image is normal, so metadata alone satisfies the rule."""
    from mojo.apps.chat.services.messages import send_message

    room = _make_room("body-optional", opts.user1)

    msg, error = send_message(
        room, opts.user1, "", kind="image",
        metadata={"url": "stored/image.png"}, broadcast=False)

    assert_eq(error, None, f"expected an uncaptioned image to be accepted, got {error}")
    assert_eq(msg.body, "", "expected an empty body to be stored as empty")
    assert_eq(
        msg.metadata, {"url": "stored/image.png"},
        "expected the image metadata to be stored")


@th.django_unit_test()
def test_body_and_metadata_both_missing_refused(opts):
    """A message with neither a body nor metadata carries nothing."""
    from mojo.apps.chat.services.messages import send_message

    room = _make_room("body-and-metadata-missing", opts.user1)

    msg, error = send_message(
        room, opts.user1, "", kind="image", metadata=None, broadcast=False)

    assert_true(msg is None, "expected an empty image message to be refused")
    assert_eq(
        error["error"], "body or metadata is required",
        f"expected the body-or-metadata error, got {error['error']}")


# ---------------------------------------------------------------------------
# wire shape
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_ack_and_broadcast_carry_kind_and_metadata(opts):
    """A client renders from the ack and the broadcast without a second fetch."""
    from mojo.apps.chat.handler import _handle_send

    room = _make_room("wire-shape", opts.user1)
    payload = {"url": "stored/photo.png", "width": 800}
    captured = []

    def capture(topic, frame):
        captured.append((topic, frame))

    result = _handle_send(opts.user1, {
        "type": "chat_message",
        "room_id": room.pk,
        "body": "look",
        "kind": "image",
        "metadata": payload,
        "client_key": "ck-kinds-wire",
    }, publisher=capture)

    assert_eq(result["type"], "chat_message_ack", f"expected an ack, got {result}")
    assert_eq(result.get("kind"), "image", f"expected kind on the ack, got {result}")
    assert_eq(result.get("metadata"), payload, f"expected metadata on the ack, got {result}")
    assert_eq(
        result.get("client_key"), "ck-kinds-wire",
        "expected #3377's client_key echo to survive on the ack")

    assert_eq(len(captured), 1, f"expected exactly one broadcast, got {len(captured)}")
    topic, frame = captured[0]
    assert_eq(topic, room.topic, f"expected the room topic, got {topic}")
    assert_eq(frame.get("kind"), "image", f"expected kind on the broadcast, got {frame}")
    assert_eq(
        frame.get("metadata"), payload, f"expected metadata on the broadcast, got {frame}")
    assert_eq(
        frame.get("client_key"), "ck-kinds-wire",
        "expected the client_key echo to survive on the broadcast")


# ---------------------------------------------------------------------------
# the two policy gates
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_server_authored_skips_rate_limit(opts):
    """enforce_room_policy=False keeps a join message off the rate limiter."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.services.messages import send_message

    room = _make_room("gate-rate-limit", opts.user1, rules={"rate_limit": 1})

    first = handle_chat_message(opts.user1, {
        "type": "chat_message", "room_id": room.pk, "body": "first in the window",
    })
    assert_eq(first["type"], "chat_message_ack", f"expected an ack, got {first}")

    blocked = handle_chat_message(opts.user1, {
        "type": "chat_message", "room_id": room.pk, "body": "second in the window",
    })
    assert_eq(blocked["type"], "error", f"expected the limiter to be exhausted, got {blocked}")
    assert_true(
        "Rate limit" in blocked["error"],
        f"expected a rate limit error, got {blocked['error']}")

    msg, error = send_message(
        room, opts.user1, "someone joined", kind="system",
        client_authored=False, enforce_room_policy=False, broadcast=False)
    assert_eq(
        error, None,
        f"expected a server-authored system message to skip the limiter, got {error}")
    assert_eq(msg.kind, "system", f"expected kind=system, got {msg.kind}")


@th.tier("core")
@th.django_unit_test()
def test_server_authored_file_still_moderates_caption(opts):
    """The C10 regression: the two gates are independent.

    A server-authored file message carries a user-written caption. Turning off
    the kind whitelist must NOT turn off moderation -- `enforce_room_policy`
    is left at its default and the caption still runs the classifier.
    """
    from mojo.apps.chat.services.messages import send_message
    from mojo.apps.chat.models import ChatMessage

    room = _make_room("gate-moderation", opts.user1)

    msg, error = send_message(
        room, opts.user1, BLOCKED_BODY, kind="file",
        metadata={"file": {"id": 7, "filename": "report.pdf"}},
        client_authored=False, broadcast=False, validators={})

    assert_true(msg is None, "expected the caption to be moderated and blocked")
    assert_eq(
        error["error"], "Message blocked by moderation",
        f"expected a moderation block, got {error['error']}")
    assert_eq(
        ChatMessage.objects.filter(room=room).count(), 0,
        "expected the blocked file message not to be stored")

    # And the room's own rules still apply to a server-authored caption.
    strict = _make_room("gate-rules", opts.user1, rules={"max_message_length": 10})
    long_msg, long_error = send_message(
        strict, opts.user1, "x" * 50, kind="file",
        metadata={"file": {"id": 8}},
        client_authored=False, broadcast=False, validators={})
    assert_true(long_msg is None, "expected the room's length rule to apply")
    assert_true(
        "max length" in long_error["error"],
        f"expected a max-length error, got {long_error['error']}")

    # With the policy gate explicitly off, the same caption goes through --
    # that is what the join/leave sites use.
    allowed, allowed_error = send_message(
        room, opts.user1, BLOCKED_BODY, kind="system",
        client_authored=False, enforce_room_policy=False, broadcast=False)
    assert_eq(
        allowed_error, None,
        f"expected enforce_room_policy=False to skip moderation, got {allowed_error}")
    assert_true(allowed is not None, "expected the unmoderated system message to store")
