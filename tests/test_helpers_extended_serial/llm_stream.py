"""Guarded Bedrock streaming and tool use (#10437).

Serial and opt-in for the same reason as llm_safety.py: the guard reads the
settings singleton and the protected policy-hash row, which _guard_case
patches for the fixed installation identity.
"""

from unittest import mock
import threading
import time
import uuid

from testit import helpers as th

from .llm_safety import (
    _assert_budget_total, _assert_denied, _bedrock_reply, _case_keys, _guard_case,
    _guard_policy,
)


TOOLS = [{"name": "lookup_item", "description": "Look up a board item",
          "input_schema": {"type": "object", "properties": {"id": {"type": "integer"}}}}]
USAGE = {"inputTokens": 7, "outputTokens": 3, "cacheReadInputTokens": 2,
         "cacheWriteInputTokens": 1, "totalTokens": 13}
HELLO = [{"role": "user", "content": "hello"}]


class _Raw:
    def __init__(self):
        self.stopped = threading.Event()
        self.shutdowns = 0

    def shutdown(self):
        self.shutdowns += 1
        self.stopped.set()


class _EventStream:
    """In-order events; an exception item raises; "block" waits for shutdown."""

    def __init__(self, events):
        self.events = list(events)
        self._raw_stream = _Raw()
        self.closed = False

    def __iter__(self):
        from urllib3.exceptions import ProtocolError
        for item in self.events:
            if item == "block":
                self._raw_stream.stopped.wait(5)
                raise ProtocolError("connection shut down")
            if isinstance(item, BaseException):
                raise item
            yield item

    def close(self):
        self.closed = True


class _Client:
    """Stub bedrock-runtime client: queued converse and converse_stream answers."""

    def __init__(self, replies=(), streams=()):
        self.replies = list(replies)
        self.streams = [_EventStream(events) for events in streams]
        self.opened = list(self.streams)
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(("converse", kwargs))
        return self.replies.pop(0)

    def converse_stream(self, **kwargs):
        self.calls.append(("converse_stream", kwargs))
        return {"ResponseMetadata": {"RequestId": "bedrock-stream-10437"},
                "stream": self.streams.pop(0)}


def _events(parts=("Hel", "lo"), stop="end_turn", tool_input=None):
    events = [{"messageStart": {"role": "assistant"}}]
    events += [{"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": part}}}
               for part in parts]
    events.append({"contentBlockStop": {"contentBlockIndex": 0}})
    if tool_input is not None:
        events += [
            {"contentBlockStart": {"contentBlockIndex": 1, "start": {
                "toolUse": {"toolUseId": "tooluse_1", "name": "lookup_item"}}}},
            {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {
                "toolUse": {"input": tool_input}}}},
            {"contentBlockStop": {"contentBlockIndex": 1}}]
    events += [{"messageStop": {"stopReason": stop}},
               {"metadata": {"usage": dict(USAGE), "metrics": {"latencyMs": 4}}}]
    return events


def _tool_reply():
    return _bedrock_reply(stop="tool_use", content=[
        {"text": "Checking."},
        {"toolUse": {"toolUseId": "tooluse_1", "name": "lookup_item", "input": {"id": 7}}}])


def _tool_policy():
    raw = _guard_policy()
    raw["routes"]["assistant"]["capabilities"] = ["text", "tools"]
    return raw


def _factory(client):
    from mojo.helpers.llm_providers.bedrock import BedrockProvider
    return lambda name, **kwargs: BedrockProvider(client=client, **kwargs)


def _guarded(case, client, stream=True, **kwargs):
    from mojo.apps.account.services import llm_safety

    values = dict(
        feature=case["feature"], operation=case["marker"],
        context={"operation_id": uuid.uuid4().hex, "job_id": case["marker"]},
        max_tokens=8, provider_factory=_factory(client), redis=case["redis"],
        policy_raw=case["raw"])
    messages = kwargs.pop("messages", HELLO)
    values.update(kwargs)
    run = llm_safety.stream_guarded_for_test if stream else llm_safety.execute_guarded_for_test
    return run(messages, **values)


def _transcript(rounds):
    messages = list(HELLO)
    for number in range(rounds):
        messages.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"t{number}", "name": "lookup_item", "input": {}}]})
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"t{number}", "content": "ok"}]})
    return messages


def _leases_released(case):
    return all(case["redis"].zcard(key) == 0 for key in _case_keys(case)[:2])


@th.django_unit_test()
def test_stream_settles_actual_usage_only_after_ordered_deltas(opts):
    from mojo.apps.account.models import LLMRequest

    with _guard_case() as case:
        client = _Client(streams=[_events(("Hel", "lo", " there"))])
        answer = _guarded(case, client)
        row = LLMRequest.objects.get(operation=case["marker"])
        assert row.status == "started" and row.operation == case["marker"], \
            f"stream() must return with a started ledger, got {row.status}"
        assert all(case["redis"].zcard(key) == 1 for key in _case_keys(case)[:2]), \
            "a running stream must hold both admission leases"
        events = []
        for event in answer:
            if event["type"] == "text_delta":
                row.refresh_from_db()
                assert row.status == "started", "usage must not settle before the final record"
            events.append(event)
        assert [event.get("text") for event in events[:-1]] == ["Hel", "lo", " there"], \
            f"deltas must reach the caller in provider order: {events}"
        final = events[-1]
        assert final["type"] == "final" and final["response"]["content"] == [
            {"type": "text", "text": "Hello there"}] \
            and final["response"]["stop_reason"] == "end_turn", \
            f"the final record must carry the assembled answer and stop reason: {final}"
        assert answer.response is final["response"], "stream.response must hold the final record"
        row.refresh_from_db()
        assert row.status == "succeeded" and row.provider_request_id == "bedrock-stream-10437" \
            and (row.input_tokens, row.output_tokens, row.cache_read_input_tokens,
                 row.cache_creation_input_tokens) == (7, 3, 2, 1), \
            "the ledger must settle all four usage classes from the stream's final record"
        _assert_budget_total(case, 13)
        assert _leases_released(case), "a finished stream must release both leases"
        assert client.calls[0][0] == "converse_stream", "streaming must use converse_stream"


@th.django_unit_test()
def test_refusals_raise_from_stream_before_anything_streams(opts):
    from mojo.apps.account.models import LLMRequest
    from mojo.apps.account.services import llm_safety

    with _guard_case() as case:
        client = _Client(streams=[_events()])
        cases = [
            ("capability_unsupported", {"tools": TOOLS}),
            ("capability_unsupported", {"reasoning_effort": "high"}),
            ("capability_unsupported", {"messages": _transcript(1)}),
            ("output_too_large", {"max_tokens": 129}),
            ("input_too_large", {"messages": [{"role": "user", "content": "x" * 5000}]}),
        ]
        for code, arguments in cases:
            _assert_denied(code, lambda: _guarded(case, client, **arguments))
        with mock.patch.object(llm_safety, "emergency_stopped", return_value=True):
            _assert_denied("emergency_stopped", lambda: _guarded(case, client))
        with mock.patch.object(llm_safety, "_single_primary_value", return_value="stale"):
            _assert_denied("policy_mixed", lambda: _guarded(case, client))
        assert client.calls == [] and not LLMRequest.objects.filter(
            operation=case["marker"]).exists(), \
            "a refused stream must reach neither ConverseStream nor the ledger"

    with _guard_case(_tool_policy()) as case:
        client = _Client(streams=[_events()])
        _assert_denied("operation_invalid", lambda: _guarded(
            case, client, tools=TOOLS, context={"job_id": case["marker"]}))
        _assert_denied("loop_limit", lambda: _guarded(
            case, client, tools=TOOLS, messages=_transcript(9)))
        _assert_denied("loop_limit", lambda: _guarded(
            case, client, tools=TOOLS, messages=_transcript(3), max_tool_rounds=2))
        _assert_denied("provider_rejected", lambda: _guarded(
            case, client, tools=TOOLS, messages=_transcript(1)[:-1]))
        _assert_denied("provider_rejected", lambda: _guarded(
            case, client, stream=False, tools=[dict(TOOLS[0], cache_control={})]))
        assert client.calls == [] and not LLMRequest.objects.filter(
            operation=case["marker"]).exists(), \
            "tool-loop refusals must happen before dispatch or ledger"

    from mojo.helpers.llm_providers import get_provider
    with _guard_case(_guard_policy("anthropic")) as case:
        _assert_denied("capability_unsupported", lambda: llm_safety.stream_guarded_for_test(
            HELLO, feature=case["feature"], operation=case["marker"], max_tokens=8,
            provider_factory=get_provider, redis=case["redis"], policy_raw=case["raw"]))
        assert not LLMRequest.objects.filter(operation=case["marker"]).exists(), \
            "an Anthropic route must refuse streaming before the ledger"


@th.django_unit_test()
def test_cancel_mid_stream_settles_once_and_keeps_the_reservation(opts):
    from mojo.apps.account.models import LLMCircuitBreaker, LLMRequest

    with _guard_case() as case:
        client = _Client(streams=[_events(("one", " two", " three"))])
        answer = _guarded(case, client)
        assert next(answer) == {"type": "text_delta", "text": "one"}, \
            "the first delta must stream before the cancel"
        answer.close()
        answer.close()
        try:
            next(answer)
            assert False, "a cancelled stream must stop"
        except StopIteration:
            pass
        row = LLMRequest.objects.get(operation=case["marker"])
        assert row.status == "failed" and row.error_code == "cancelled" \
            and row.output_tokens == 0, \
            f"a cancel must finish the ledger as cancelled, got {row.status}/{row.error_code}"
        _assert_budget_total(case, row.reserved_tokens)
        assert _leases_released(case), "a cancel must release both leases"
        breaker = LLMCircuitBreaker.objects.get(
            provider="bedrock", credential_fingerprint=case["fingerprint"])
        assert breaker.state == "closed" and breaker.failure_count == 0, \
            "a caller's cancel must not count against the provider's health"
        assert client.opened[0].closed, "a cancel must release the AWS connection"


@th.django_unit_test()
def test_cancel_from_another_thread_interrupts_a_blocked_read(opts):
    from mojo.apps.account.models import LLMRequest

    with _guard_case() as case:
        delta = {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Partial"}}}
        client = _Client(streams=[[{"messageStart": {"role": "assistant"}}, delta, "block"]])
        answer = _guarded(case, client)
        assert next(answer)["text"] == "Partial", "the first delta must stream"
        took = []

        def cancel():
            time.sleep(0.2)
            started = time.monotonic()
            answer.close()
            took.append(time.monotonic() - started)

        canceller = threading.Thread(target=cancel)
        canceller.start()
        try:
            next(answer)
            assert False, "the blocked read must end when another thread cancels"
        except StopIteration:
            pass
        canceller.join(5)
        assert took and took[0] < 0.5, \
            f"close() from another thread must not wait on the blocked read: {took}"
        assert client.opened[0]._raw_stream.shutdowns == 1, \
            "a cross-thread cancel must shut the socket down"
        row = LLMRequest.objects.get(operation=case["marker"])
        assert row.status == "failed" and row.error_code == "cancelled", \
            f"the reading thread must settle the cancel, got {row.status}/{row.error_code}"
        assert _leases_released(case), "a cross-thread cancel must release both leases"


@th.django_unit_test()
def test_provider_failure_after_a_delta_settles_safely(opts):
    from botocore.exceptions import EventStreamError
    from mojo.apps.account.models import LLMCircuitBreaker, LLMRequest
    from mojo.apps.account.services import llm_safety

    with _guard_case() as case:
        delta = {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Partial"}}}
        failure = EventStreamError(
            {"Error": {"Code": "throttlingException", "Message": "secret"}}, "ConverseStream")
        client = _Client(streams=[[{"messageStart": {"role": "assistant"}}, delta, failure]])
        answer = _guarded(case, client)
        assert next(answer)["text"] == "Partial", "text before the failure must stream"
        try:
            next(answer)
            assert False, "a mid-stream provider failure must raise"
        except llm_safety.LLMSafetyError as err:
            assert err.code == "provider_rate_limited" and "secret" not in str(err), \
                f"mid-stream throttling must be a safe rate-limit code, got {err.code}"
        row = LLMRequest.objects.get(operation=case["marker"])
        assert row.status == "failed" and row.error_code == "provider_rate_limited", \
            f"the ledger must record the safe failure, got {row.status}/{row.error_code}"
        _assert_budget_total(case, row.reserved_tokens)
        assert _leases_released(case), "a failed stream must release both leases"
        breaker = LLMCircuitBreaker.objects.get(
            provider="bedrock", credential_fingerprint=case["fingerprint"])
        assert breaker.failure_count == 1, "mid-stream throttling is a provider health failure"


@th.django_unit_test()
def test_tool_round_trip_through_the_guard_plain_and_streamed(opts):
    from mojo.apps.account.models import LLMRequest

    for streamed in (False, True):
        with _guard_case(_tool_policy()) as case:
            if streamed:
                client = _Client(streams=[
                    _events(("Checking.",), "tool_use", '{"id": 7}'), _events(("Done.",))])
            else:
                client = _Client(replies=[_tool_reply(), _bedrock_reply()])
            context = {"operation_id": uuid.uuid4().hex, "job_id": case["marker"]}

            def run(messages):
                result = _guarded(case, client, stream=streamed, tools=TOOLS,
                                  context=context, messages=messages)
                return list(result)[-1]["response"] if streamed else result

            first = run(HELLO)
            request = {"type": "tool_use", "id": "tooluse_1", "name": "lookup_item",
                       "input": {"id": 7}}
            assert first["stop_reason"] == "tool_use" and first["content"][-1] == request, \
                f"the model's tool request must come back to the caller: {first}"
            transcript = HELLO + [
                {"role": "assistant", "content": first["content"]},
                {"role": "user", "content": [{"type": "tool_result",
                                              "tool_use_id": "tooluse_1", "content": "Item 7"}]}]
            second = run(transcript)
            assert second["stop_reason"] == "end_turn" and second["content"][0]["type"] == "text", \
                f"the answer after the tool result must be text: {second}"
            wire = client.calls[1][1]
            assert wire["toolConfig"]["tools"][0]["toolSpec"]["name"] == "lookup_item" \
                and wire["messages"][2]["content"][0]["toolResult"]["toolUseId"] == "tooluse_1", \
                f"the tool result must reach Bedrock as a toolResult turn: {wire}"
            _assert_denied("loop_limit", lambda: run(transcript))
            assert len(client.calls) == 2, \
                "one operation id must bound the tool loop at the route's max_loop_calls"
            rows = list(LLMRequest.objects.filter(operation=case["marker"]))
            assert len(rows) == 2 and all(row.status == "succeeded" for row in rows), \
                f"each guarded tool round must settle its own ledger row ({streamed})"


@th.django_unit_test()
def test_tool_round_limit_allows_the_limit_and_refuses_beyond(opts):
    with _guard_case(_tool_policy()) as case:
        client = _Client(replies=[_bedrock_reply(), _bedrock_reply()])
        _guarded(case, client, stream=False, tools=TOOLS, messages=_transcript(8))
        _assert_denied("loop_limit", lambda: _guarded(
            case, client, stream=False, tools=TOOLS, messages=_transcript(9)))
        _guarded(case, client, stream=False, tools=TOOLS, messages=_transcript(2),
                 max_tool_rounds=2)
        _assert_denied("loop_limit", lambda: _guarded(
            case, client, stream=False, tools=TOOLS, messages=_transcript(3),
            max_tool_rounds=2))
        assert len(client.calls) == 2, \
            "only transcripts within the round limit may reach Bedrock"


@th.django_unit_test()
def test_facade_stream_raises_stable_execution_errors(opts):
    from botocore.exceptions import EventStreamError
    from mojo.apps.account.services import llm_safety
    from mojo.helpers import llm

    with _guard_case() as case:
        failure = EventStreamError(
            {"Error": {"Code": "internalServerException", "Message": "secret"}},
            "ConverseStream")
        client = _Client(streams=[_events(("Hi",)), [
            {"messageStart": {"role": "assistant"}}, failure]])
        through = lambda *args, **kwargs: _guarded(case, client)
        with mock.patch.object(llm_safety, "invoke_stream", side_effect=through):
            with llm.stream(HELLO, feature="assistant") as answer:
                events = list(answer)
            assert events[-1]["type"] == "final" and answer.response["content"] == [
                {"type": "text", "text": "Hi"}], f"the facade must pass the final record: {events}"
            answer = llm.stream(HELLO, feature="assistant")
            try:
                list(answer)
                assert False, "a mid-stream failure must raise from iteration"
            except llm.LLMExecutionError as err:
                assert err.code == "provider_unavailable" and "secret" not in str(err), \
                    f"iteration must raise a stable code, got {err.code}"
            with mock.patch.object(llm_safety, "emergency_stopped", return_value=True):
                try:
                    llm.stream(HELLO, feature="assistant")
                    assert False, "a refused stream must raise from llm.stream()"
                except llm.LLMExecutionError as err:
                    assert err.code == "emergency_stopped", \
                        f"a refusal must surface as its stable code, got {err.code}"


def _short_policy(seconds=1):
    raw = _guard_policy()
    raw["shared"]["timeout_seconds"] = seconds
    raw["features"]["assistant"]["timeout_seconds"] = seconds
    return raw


@th.django_unit_test()
def test_a_caller_stall_is_not_a_provider_timeout_for_the_circuit(opts):
    from mojo.apps.account.models import LLMCircuitBreaker, LLMRequest
    from mojo.apps.account.services import llm_safety

    delta = {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Partial"}}}
    for blocked in (False, True):
        with _guard_case(_short_policy()) as case:
            tail = ["block"] if blocked else [delta]
            client = _Client(streams=[[{"messageStart": {"role": "assistant"}}, delta] + tail])
            answer = _guarded(case, client)
            next(answer)
            if not blocked:
                time.sleep(1.3)
            try:
                next(answer)
                assert False, "a stream past its deadline must fail"
            except llm_safety.LLMSafetyError as err:
                assert err.code == "provider_timeout", \
                    f"the deadline must surface as provider_timeout, got {err.code}"
            row = LLMRequest.objects.get(operation=case["marker"])
            assert row.status == "failed" and row.error_code == "provider_timeout", \
                f"a timed-out stream must finish its ledger, got {row.status}/{row.error_code}"
            assert _leases_released(case), "a timed-out stream must release both leases"
            breaker = LLMCircuitBreaker.objects.get(
                provider="bedrock", credential_fingerprint=case["fingerprint"])
            assert breaker.failure_count == (1 if blocked else 0), \
                f"only a read AWS left blocked may count against the circuit (blocked={blocked})"


@th.django_unit_test()
def test_a_cancel_before_any_provider_event_leaves_a_half_open_circuit(opts):
    from django.utils import timezone
    from mojo.apps.account.models import LLMCircuitBreaker, LLMRequest

    with _guard_case() as case:
        LLMCircuitBreaker.objects.create(
            provider="bedrock", credential_fingerprint=case["fingerprint"], state="open",
            opened_until=timezone.now() - timezone.timedelta(seconds=1))
        client = _Client(streams=[["block"]])
        answer = _guarded(case, client)
        breaker = LLMCircuitBreaker.objects.get(
            provider="bedrock", credential_fingerprint=case["fingerprint"])
        assert breaker.state == "half_open" and breaker.half_open_owner, \
            "the stream must hold the half-open probe lease"
        threading.Timer(0.2, answer.close).start()
        try:
            next(answer)
            assert False, "the blocked first read must end on cancel"
        except StopIteration:
            pass
        breaker.refresh_from_db()
        assert breaker.state == "half_open", \
            "a cancel with no provider event must not close a half-open circuit"
        row = LLMRequest.objects.get(operation=case["marker"])
        assert row.error_code == "cancelled" and _leases_released(case), \
            "the cancel must still settle its ledger and leases"


@th.django_unit_test()
def test_close_from_an_event_loop_defers_settlement_to_the_reader(opts):
    import asyncio
    from mojo.apps.account.models import LLMRequest

    with _guard_case() as case:
        client = _Client(streams=[_events(("one", " two"))])
        answer = _guarded(case, client)
        next(answer)

        async def cancel():
            answer.close()

        asyncio.run(cancel())
        row = LLMRequest.objects.get(operation=case["marker"])
        assert row.status == "started", \
            "close() on an event loop must not write the ledger from async code"
        try:
            next(answer)
            assert False, "the reader's next step must see the cancel"
        except StopIteration:
            pass
        row.refresh_from_db()
        assert row.status == "failed" and row.error_code == "cancelled", \
            f"the reading thread must settle the deferred cancel, got {row.status}"
        assert _leases_released(case), "the deferred cancel must release both leases"


def _png():
    import io
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (4, 3), (200, 40, 40)).save(out, format="PNG")
    return out.getvalue()


def _image_turn(raw):
    from mojo.helpers.llm_images import image_block

    return [{"role": "user", "content": [image_block(raw, "image/png"),
                                         {"type": "text", "text": "What is broken?"}]}]


@th.django_unit_test()
def test_image_input_streams_through_the_guard(opts):
    import json
    from mojo.apps.account.models import LLMRequest
    from mojo.helpers.llm_images import IMAGE_RESERVED_TOKENS

    raw = _png()
    messages = _image_turn(raw)
    with _guard_case(_tool_policy()) as case:
        client = _Client(streams=[_events()])
        _assert_denied("capability_unsupported", lambda: _guarded(
            case, client, messages=messages))
        assert client.calls == [] and not LLMRequest.objects.filter(
            operation=case["marker"]).exists(), \
            "a stream route without images must refuse an image before dispatch or ledger"

    policy = _guard_policy()
    policy["routes"]["assistant"]["capabilities"] = ["text", "images"]
    with _guard_case(policy) as case:
        client = _Client(streams=[_events(("A red", " box"))])
        events = list(_guarded(case, client, messages=messages))
        assert [event.get("text") for event in events[:-1]] == ["A red", " box"] \
            and events[-1]["type"] == "final", \
            f"an image stream must yield its deltas, then the final record: {events}"
        kind, request = client.calls[0]
        assert kind == "converse_stream" and request["messages"][0]["content"] == [
            {"image": {"format": "png", "source": {"bytes": raw}}},
            {"text": "What is broken?"}], \
            f"the streamed image must reach ConverseStream as raw bytes: {request['messages']}"
        sizing = json.loads(json.dumps(messages))
        sizing[0]["content"][0]["source"]["data"] = ""
        expected = len(json.dumps({"messages": sizing, "system": None, "tools": None},
                                  separators=(",", ":")).encode("utf-8")) \
            + 8 + IMAGE_RESERVED_TOKENS
        row = LLMRequest.objects.get(operation=case["marker"])
        assert row.reserved_tokens == expected, \
            f"a streamed image must reserve blanked bytes + max_tokens + one image, got {row.reserved_tokens} vs {expected}"
        assert row.status == "succeeded" and (row.input_tokens, row.output_tokens) == (7, 3), \
            f"an image stream must settle exact usage on the ledger, got {row.status}"
        _assert_budget_total(case, 13)
        assert _leases_released(case), "a finished image stream must release both leases"


@th.django_unit_test()
def test_image_turn_survives_a_guarded_tool_loop_plain_and_streamed(opts):
    from mojo.apps.account.models import LLMRequest

    raw = _png()
    wire_image = {"image": {"format": "png", "source": {"bytes": raw}}}
    policy = _guard_policy()
    policy["routes"]["assistant"]["capabilities"] = ["text", "tools", "images"]
    for streamed in (False, True):
        with _guard_case(policy) as case:
            if streamed:
                client = _Client(streams=[
                    _events(("Checking.",), "tool_use", '{"id": 7}'), _events(("Done.",))])
            else:
                client = _Client(replies=[_tool_reply(), _bedrock_reply()])
            context = {"operation_id": uuid.uuid4().hex, "job_id": case["marker"]}

            def run(messages):
                result = _guarded(case, client, stream=streamed, tools=TOOLS,
                                  context=context, messages=messages)
                return list(result)[-1]["response"] if streamed else result

            asked = _image_turn(raw)
            first = run(asked)
            assert first["stop_reason"] == "tool_use", \
                f"an image turn must still let the model request a tool: {first}"
            transcript = asked + [
                {"role": "assistant", "content": first["content"]},
                {"role": "user", "content": [{"type": "tool_result",
                                              "tool_use_id": "tooluse_1",
                                              "content": "Item 7 is open"}]}]
            second = run(transcript)
            assert second["stop_reason"] == "end_turn", \
                f"the answer after the tool round must be text: {second}"
            assert len(client.calls) == 2 and all(
                request["messages"][0]["content"][0] == wire_image
                for _kind, request in client.calls), \
                f"every round must resend the image turn as raw bytes ({streamed})"
            rows = list(LLMRequest.objects.filter(operation=case["marker"]))
            assert len(rows) == 2 and all(row.status == "succeeded" for row in rows), \
                f"each image tool round must settle its own ledger row ({streamed})"

    with _guard_case(policy) as case:
        client = _Client(replies=[_bedrock_reply()])
        nested = _transcript(1)
        nested[-1]["content"][0]["content"] = _image_turn(raw)[0]["content"][:1]
        _assert_denied("image_invalid", lambda: _guarded(
            case, client, stream=False, tools=TOOLS, messages=nested))
        assert client.calls == [] and not LLMRequest.objects.filter(
            operation=case["marker"]).exists(), \
            "an image inside a tool result must be refused before dispatch or ledger"


@th.django_unit_test()
def test_image_shaped_tool_arguments_do_not_stall_a_tool_loop(opts):
    from mojo.apps.account.models import LLMRequest

    with _guard_case(_tool_policy()) as case:
        client = _Client(replies=[_bedrock_reply()])
        transcript = HELLO + [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "lookup_item",
                 "input": {"type": "image", "id": 7}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                          "content": "ok"}]}]
        response = _guarded(case, client, stream=False, tools=TOOLS, messages=transcript)
        assert response["stop_reason"] == "end_turn" and len(client.calls) == 1, \
            "tool arguments shaped like an image must not need the images capability"
        assert LLMRequest.objects.get(operation=case["marker"]).status == "succeeded", \
            "the round must settle like any other tool round"
