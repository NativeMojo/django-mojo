"""
Regression tests for the assistant WS intermediate-text stream.

When the model produces a turn that interleaves prose with `tool_use` blocks
(e.g. "Both are benign because X. [tool_use bulk_update_incidents]"), the
prose must be:

1. Emitted as an `assistant_text` event over the WS callback BEFORE any
   `assistant_tool_call` events for that same turn.
2. Persisted on the assistant Message row in `content` (text) and `blocks`
   (parsed `assistant_block` fences) — NOT buried inside `tool_calls`.
3. Stripped from `tool_calls` so that field carries only `tool_use` blocks.

Empty intermediate text (turn has only `tool_use` blocks) must NOT fire a
spurious `assistant_text` event.
"""
from contextlib import contextmanager
from unittest import mock
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


TEST_EMAIL = 'interim-text-admin@example.com'
TEST_PASSWORD = 'TestPass1!'


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_user(opts):
    from mojo.apps.account.models import User

    User.objects.filter(email=TEST_EMAIL).delete()
    opts.user = User.objects.create_user(
        username=TEST_EMAIL, email=TEST_EMAIL, password=TEST_PASSWORD,
    )
    opts.user.is_email_verified = True
    opts.user.save()
    for perm in ["view_admin", "assistant"]:
        opts.user.add_permission(perm)


def _build_llm_sequence(turns):
    """Return a side_effect callable that yields each turn's response in order."""
    iterator = iter(turns)

    def _next_call(*args, **kwargs):
        return next(iterator)

    return _next_call


def _capture_events():
    """Return (events_list, on_event_callback) for capturing WS event emits."""
    events = []

    def on_event(event_type, data=None):
        events.append({"type": event_type, "data": data or {}})

    return events, on_event


@contextmanager
def _enable_assistant():
    """Patch settings + llm so run_assistant_ws executes its main body."""
    from mojo.helpers.settings import settings
    from mojo.helpers import llm

    orig_get = settings.get

    def patched_get(name, *args, **kwargs):
        if name == "LLM_ADMIN_ENABLED":
            return True
        return orig_get(name, *args, **kwargs)

    with mock.patch.object(settings, "get", side_effect=patched_get):
        with mock.patch.object(llm, "get_api_key", return_value="test-key"):
            yield


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_intermediate_text_emits_assistant_text_event(opts):
    """Turn 1 has [text + tool_use]; assistant_text fires before tool_call."""
    from mojo.apps.assistant.services.agent import run_assistant_ws
    from mojo.apps.assistant.models import Conversation, Message

    Conversation.objects.filter(user=opts.user, title="interim-text-1").delete()
    conv = Conversation.objects.create(user=opts.user, title="interim-text-1")
    # The user message is normally stored by the WS handler before
    # run_assistant_ws is invoked; mirror that here.
    Message.objects.create(conversation=conv, role="user", content="hi")

    # Turn 1: model writes prose AND calls a tool in the same turn.
    # Turn 2: terminal text-only turn (final wrap-up).
    turns = [
        {
            "stop_reason": "tool_use",
            "content": [
                {"type": "text", "text": "Looking up your memory now."},
                {"type": "tool_use", "id": "tu_1", "name": "read_memory", "input": {}},
            ],
        },
        {
            "stop_reason": "end_turn",
            "content": [
                {"type": "text", "text": "Done — nothing stored yet."},
            ],
        },
    ]

    events, on_event = _capture_events()

    with _enable_assistant(), mock.patch(
        "mojo.helpers.llm.call",
        side_effect=_build_llm_sequence(turns),
    ):
        result = run_assistant_ws(opts.user, "hi", conv.pk, on_event=on_event)

    assert_true("error" not in result, f"WS run should succeed, got: {result}")

    # Find the index of the first assistant_text and first assistant_tool_call.
    text_idx = next((i for i, e in enumerate(events) if e["type"] == "text"), -1)
    tool_idx = next((i for i, e in enumerate(events) if e["type"] == "tool_call"), -1)

    assert_true(
        text_idx >= 0,
        f"assistant_text event must fire; events={[e['type'] for e in events]}",
    )
    assert_true(
        tool_idx >= 0,
        f"assistant_tool_call event must fire; events={[e['type'] for e in events]}",
    )
    assert_true(
        text_idx < tool_idx,
        f"assistant_text must precede assistant_tool_call; got text@{text_idx} tool@{tool_idx}",
    )

    text_event = events[text_idx]
    assert_eq(
        text_event["data"]["text"], "Looking up your memory now.",
        "assistant_text payload must carry the intermediate prose verbatim",
    )
    assert_true(
        "blocks" in text_event["data"],
        "assistant_text payload must include a 'blocks' key (null when none)",
    )


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_intermediate_text_persisted_on_message(opts):
    """Intermediate text lands in Message.content; tool_calls only has tool_use."""
    from mojo.apps.assistant.services.agent import run_assistant_ws
    from mojo.apps.assistant.models import Conversation, Message

    Conversation.objects.filter(user=opts.user, title="interim-text-2").delete()
    conv = Conversation.objects.create(user=opts.user, title="interim-text-2")
    Message.objects.create(conversation=conv, role="user", content="hi")

    turns = [
        {
            "stop_reason": "tool_use",
            "content": [
                {"type": "text", "text": "About to look this up for you."},
                {"type": "tool_use", "id": "tu_x", "name": "read_memory", "input": {}},
            ],
        },
        {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "All clear."}],
        },
    ]

    events, on_event = _capture_events()

    with _enable_assistant(), mock.patch(
        "mojo.helpers.llm.call",
        side_effect=_build_llm_sequence(turns),
    ):
        result = run_assistant_ws(opts.user, "hi", conv.pk, on_event=on_event)

    assert_true("error" not in result, f"WS run should succeed, got: {result}")

    # Find the assistant Message row created during the tool turn (not the
    # final terminal one). It is the one with non-empty tool_calls.
    interim = (
        Message.objects.filter(conversation=conv, role="assistant")
        .exclude(tool_calls__isnull=True)
        .order_by("created")
        .first()
    )
    assert_true(interim is not None, "Intermediate assistant Message row must exist")
    assert_eq(
        interim.content, "About to look this up for you.",
        f"Intermediate Message.content must hold the prose, got: {interim.content!r}",
    )
    assert_true(
        isinstance(interim.tool_calls, list),
        f"Intermediate Message.tool_calls must be a list, got: {type(interim.tool_calls).__name__}",
    )
    assert_true(
        all(b.get("type") == "tool_use" for b in interim.tool_calls),
        f"Intermediate Message.tool_calls must contain only tool_use blocks, got: {interim.tool_calls}",
    )
    assert_true(
        not any(b.get("type") == "text" for b in interim.tool_calls),
        f"Intermediate Message.tool_calls must NOT contain text blocks, got: {interim.tool_calls}",
    )


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_intermediate_text_parses_assistant_blocks(opts):
    """`assistant_block` fences in intermediate text are parsed into Message.blocks."""
    from mojo.apps.assistant.services.agent import run_assistant_ws
    from mojo.apps.assistant.models import Conversation, Message

    Conversation.objects.filter(user=opts.user, title="interim-text-3").delete()
    conv = Conversation.objects.create(user=opts.user, title="interim-text-3")
    Message.objects.create(conversation=conv, role="user", content="hi")

    fenced_text = (
        "Here is what I found:\n"
        '```assistant_block\n'
        '{"type": "stat", "items": [{"label": "Open", "value": 3}]}\n'
        '```\n'
        "Now triggering the action."
    )

    turns = [
        {
            "stop_reason": "tool_use",
            "content": [
                {"type": "text", "text": fenced_text},
                {"type": "tool_use", "id": "tu_b", "name": "read_memory", "input": {}},
            ],
        },
        {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "Done."}],
        },
    ]

    events, on_event = _capture_events()

    with _enable_assistant(), mock.patch(
        "mojo.helpers.llm.call",
        side_effect=_build_llm_sequence(turns),
    ):
        result = run_assistant_ws(opts.user, "hi", conv.pk, on_event=on_event)

    assert_true("error" not in result, f"WS run should succeed, got: {result}")

    text_event = next((e for e in events if e["type"] == "text"), None)
    assert_true(text_event is not None, "assistant_text event must fire")
    assert_true(
        "```assistant_block" not in text_event["data"]["text"],
        f"Block fence must be stripped from event text, got: {text_event['data']['text']!r}",
    )
    assert_true(
        text_event["data"]["blocks"] is not None,
        "assistant_text event blocks payload must be populated",
    )
    assert_eq(
        text_event["data"]["blocks"][0]["type"], "stat",
        f"Parsed block must carry the stat type, got: {text_event['data']['blocks']}",
    )

    interim = (
        Message.objects.filter(conversation=conv, role="assistant")
        .exclude(tool_calls__isnull=True)
        .order_by("created")
        .first()
    )
    assert_true(
        interim.blocks is not None and len(interim.blocks) == 1,
        f"Intermediate Message.blocks must hold one block, got: {interim.blocks}",
    )
    assert_eq(
        interim.blocks[0]["type"], "stat",
        f"Persisted block type must be 'stat', got: {interim.blocks}",
    )
    assert_true(
        "```assistant_block" not in interim.content,
        f"Block fence must be stripped from Message.content, got: {interim.content!r}",
    )


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_no_event_when_intermediate_text_empty(opts):
    """A turn with only tool_use (no text) must NOT emit assistant_text."""
    from mojo.apps.assistant.services.agent import run_assistant_ws
    from mojo.apps.assistant.models import Conversation, Message

    Conversation.objects.filter(user=opts.user, title="interim-text-4").delete()
    conv = Conversation.objects.create(user=opts.user, title="interim-text-4")
    Message.objects.create(conversation=conv, role="user", content="hi")

    turns = [
        {
            "stop_reason": "tool_use",
            "content": [
                {"type": "tool_use", "id": "tu_silent", "name": "read_memory", "input": {}},
            ],
        },
        {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "Done quietly."}],
        },
    ]

    events, on_event = _capture_events()

    with _enable_assistant(), mock.patch(
        "mojo.helpers.llm.call",
        side_effect=_build_llm_sequence(turns),
    ):
        result = run_assistant_ws(opts.user, "hi", conv.pk, on_event=on_event)

    assert_true("error" not in result, f"WS run should succeed, got: {result}")
    text_events = [e for e in events if e["type"] == "text"]
    assert_eq(
        len(text_events), 0,
        f"No assistant_text event should fire when intermediate text is empty, got: {text_events}",
    )

    # And the persisted intermediate row must have empty content.
    interim = (
        Message.objects.filter(conversation=conv, role="assistant")
        .exclude(tool_calls__isnull=True)
        .order_by("created")
        .first()
    )
    assert_eq(
        interim.content, "",
        f"Intermediate Message.content must be empty when model wrote no prose, got: {interim.content!r}",
    )
    assert_true(
        interim.blocks is None,
        f"Intermediate Message.blocks must be None when no fences, got: {interim.blocks}",
    )


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_terminal_response_still_fires(opts):
    """The final turn must still produce a normal terminal return value."""
    from mojo.apps.assistant.services.agent import run_assistant_ws
    from mojo.apps.assistant.models import Conversation, Message

    Conversation.objects.filter(user=opts.user, title="interim-text-5").delete()
    conv = Conversation.objects.create(user=opts.user, title="interim-text-5")
    Message.objects.create(conversation=conv, role="user", content="hi")

    turns = [
        {
            "stop_reason": "tool_use",
            "content": [
                {"type": "text", "text": "Doing the lookup."},
                {"type": "tool_use", "id": "tu_q", "name": "read_memory", "input": {}},
            ],
        },
        {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "Final answer here."}],
        },
    ]

    events, on_event = _capture_events()

    with _enable_assistant(), mock.patch(
        "mojo.helpers.llm.call",
        side_effect=_build_llm_sequence(turns),
    ):
        result = run_assistant_ws(opts.user, "hi", conv.pk, on_event=on_event)

    assert_true("error" not in result, f"WS run should succeed, got: {result}")
    # The final response text comes back via the return value (the WS handler
    # converts that to assistant_response), not via on_event.
    assert_eq(
        result["response"], "Final answer here.",
        f"Final response text should be the terminal turn, got: {result['response']!r}",
    )
    assert_true(
        result.get("message_id") is not None,
        "Terminal return must include message_id for the final assistant Message",
    )
