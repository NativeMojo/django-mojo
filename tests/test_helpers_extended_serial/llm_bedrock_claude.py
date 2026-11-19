"""Guarded Bedrock calls on a Claude route (#10757).

Serial and opt-in for the same reason as llm_safety.py: the guard reads the
settings singleton and the protected policy-hash row, which _guard_case
patches for the fixed installation identity.
"""

from contextlib import contextmanager
from unittest import mock
import hashlib
import json
import uuid

from testit import helpers as th

from .llm_safety import (
    _assert_budget_total, _assert_denied, _bedrock_reply, _case_keys, _guard_case,
    _guard_policy, _image_messages, _png, _run_guard,
)
from .llm_stream import TOOLS, _Client, _events, _guarded, _leases_released, _transcript


CLAUDE = "us.anthropic.claude-sonnet-5-5"


def _claude_policy(capabilities=("text",), model=CLAUDE):
    raw = _guard_policy()
    raw["routes"]["assistant"]["model"] = model
    raw["routes"]["assistant"]["capabilities"] = list(capabilities)
    return raw


def _claude_circuit():
    from mojo.apps.account.services import llm_safety
    return llm_safety.credential_fingerprint(
        "bedrock", llm_safety.BEDROCK_CREDENTIAL_IDENTITY + ":claude")


@contextmanager
def _claude_case(raw=None):
    """_guard_case for a Claude route, also owning the Claude circuit row."""
    from mojo.apps.account.models import LLMCircuitBreaker

    def clear():
        LLMCircuitBreaker.objects.filter(
            provider="bedrock", credential_fingerprint=_claude_circuit()).delete()

    clear()
    try:
        with _guard_case(raw or _claude_policy()) as case:
            case["circuit"] = _claude_circuit()
            yield case
    finally:
        clear()


@contextmanager
def _as_policy(raw):
    """Run another route's policy inside an open case, same Redis and ledger."""
    from mojo.apps.account.services import llm_safety
    with mock.patch.object(llm_safety, "_single_primary_value",
                           return_value=llm_safety.parse_policy(raw)["hash"]):
        yield


def _breaker(fingerprint):
    from mojo.apps.account.models import LLMCircuitBreaker
    return LLMCircuitBreaker.objects.filter(
        provider="bedrock", credential_fingerprint=fingerprint).first()


def _failing(code):
    from mojo.helpers.llm_providers.base import ProviderError

    adapter = mock.Mock()
    adapter.supports.return_value = True
    adapter.call.side_effect = ProviderError(code)
    return adapter


@th.django_unit_test()
def test_claude_guarded_call_settles_usage_on_the_shared_budget(opts):
    from mojo.apps.account.models import LLMRequest
    from mojo.apps.account.services import llm_safety

    with _claude_case() as case:
        client = _Client(replies=[_bedrock_reply()])
        response = _guarded(case, client, stream=False, reasoning_effort="low")
        row = LLMRequest.objects.get(operation=case["marker"])
        assert response["provider"] == "bedrock" and response["model"] == CLAUDE \
            and response["content"] == [{"type": "text", "text": "final answer"}], \
            f"a guarded Claude call must return the normalised answer: {response}"
        assert row.status == "succeeded" and row.model == CLAUDE \
            and (row.input_tokens, row.output_tokens, row.cache_read_input_tokens,
                 row.cache_creation_input_tokens) == (7, 3, 2, 1), \
            f"the Claude ledger must settle all four usage classes: {row.status}"
        shared = llm_safety.credential_fingerprint(
            "bedrock", llm_safety.BEDROCK_CREDENTIAL_IDENTITY)
        assert row.credential_fingerprint == shared == case["fingerprint"], \
            "the Claude ledger row must name the shared installation fingerprint"
        _assert_budget_total(case, 13)
        assert _leases_released(case), "a settled Claude call must release both leases"
        wire = client.calls[0][1]
        assert wire["additionalModelRequestFields"] == {
            "thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}}, \
            f"the guard must pass the effort to the Claude request: {wire}"
        claude = _breaker(case["circuit"])
        assert claude is not None and claude.state == "closed", \
            "a Claude call must lease the Claude circuit row"
        assert _breaker(case["fingerprint"]) is None, \
            "a Claude call must not touch the Nova circuit row"

        # Nova on the same installation spends from the same token budget.
        nova = _guard_policy()
        with _as_policy(nova):
            _guarded(case, _Client(replies=[_bedrock_reply()]), stream=False,
                     policy_raw=nova)
        _assert_budget_total(case, 26)


@th.django_unit_test()
def test_claude_route_honours_the_tool_round_and_loop_limits(opts):
    from mojo.apps.account.models import LLMRequest

    with _claude_case(_claude_policy(("text", "tools"))) as case:
        client = _Client(replies=[_bedrock_reply(), _bedrock_reply()])
        _guarded(case, client, stream=False, tools=TOOLS, messages=_transcript(2),
                 max_tool_rounds=2)
        _assert_denied("loop_limit", lambda: _guarded(
            case, client, stream=False, tools=TOOLS, messages=_transcript(3),
            max_tool_rounds=2))
        assert len(client.calls) == 1, "a transcript past max_tool_rounds reached Bedrock"

        context = {"operation_id": uuid.uuid4().hex, "job_id": case["marker"]}
        _guarded(case, client, stream=False, tools=TOOLS, context=context,
                 messages=_transcript(1))
        client.replies.append(_bedrock_reply())
        _guarded(case, client, stream=False, tools=TOOLS, context=context,
                 messages=_transcript(1))
        _assert_denied("loop_limit", lambda: _guarded(
            case, client, stream=False, tools=TOOLS, context=context,
            messages=_transcript(1)))
        assert len(client.calls) == 3, \
            "one operation id must bound a Claude tool loop at max_loop_calls"
        assert LLMRequest.objects.filter(operation=case["marker"], status="succeeded") \
            .count() == 3, "each Claude tool round must settle its own ledger row"


@th.django_unit_test()
def test_claude_stream_settles_once(opts):
    from mojo.apps.account.models import LLMRequest

    with _claude_case() as case:
        client = _Client(streams=[_events(("Hel", "lo"))])
        answer = _guarded(case, client)
        events = list(answer)
        assert [event["type"] for event in events] == ["text_delta", "text_delta", "final"], \
            f"a guarded Claude stream must yield deltas then one final: {events}"
        assert list(answer) == [], "a settled stream must yield nothing more"
        rows = list(LLMRequest.objects.filter(operation=case["marker"]))
        assert len(rows) == 1 and rows[0].status == "succeeded" \
            and rows[0].output_tokens == 3, \
            f"a Claude stream must settle exactly one ledger row: {[r.status for r in rows]}"
        _assert_budget_total(case, 13)
        assert _leases_released(case), "a finished Claude stream must release both leases"
        assert client.calls[0][0] == "converse_stream", "streaming must use converse_stream"


@th.django_unit_test()
def test_claude_refusals_happen_before_the_adapter(opts):
    from mojo.apps.account.models import LLMRequest
    from mojo.apps.account.services import llm_safety

    raw = _claude_policy()
    raw["routes"]["assistant"]["reasoning_efforts"] = ["off"]
    with _claude_case(raw) as case:
        factory = mock.Mock()
        _assert_denied("capability_unsupported", lambda: _run_guard(
            case, factory, reasoning_effort="low"))
        _assert_denied("capability_unsupported", lambda: _run_guard(
            case, factory, reasoning_effort="high"))
        with mock.patch.object(llm_safety, "emergency_stopped", return_value=True):
            _assert_denied("emergency_stopped", lambda: _run_guard(case, factory))
        assert factory.call_count == 0 and not LLMRequest.objects.filter(
            operation=case["marker"]).exists(), \
            "an unlisted effort or the emergency stop must refuse before adapter or ledger"


@th.django_unit_test()
def test_claude_and_nova_circuits_open_independently(opts):
    for code in ("provider_authentication", "provider_rate_limited",
                 "provider_timeout", "provider_unavailable"):
        nova = _guard_policy()
        with _claude_case() as case:
            adapter = _failing(code)
            for _ in range(2):
                _assert_denied(code, lambda: _run_guard(case, lambda *a, **k: adapter))
            claude = _breaker(case["circuit"])
            assert claude.state == "open" and claude.failure_count == 2 \
                and claude.error_code == code, \
                f"{code} on Claude must open the Claude circuit at the policy threshold"
            _assert_denied("breaker_open", lambda: _run_guard(case, lambda *a, **k: adapter))
            assert adapter.call.call_count == 2, "the open Claude circuit dispatched again"
            with _as_policy(nova):
                _guarded(case, _Client(replies=[_bedrock_reply()]), stream=False,
                         policy_raw=nova)
            assert _breaker(case["fingerprint"]).state == "closed", \
                f"a Claude {code} must leave the Nova circuit closed"

        with _claude_case(nova) as case:
            adapter = _failing(code)
            for _ in range(2):
                _assert_denied(code, lambda: _run_guard(case, lambda *a, **k: adapter))
            assert _breaker(case["fingerprint"]).state == "open", \
                f"{code} on Nova must open the Nova circuit"
            claude_raw = _claude_policy()
            with _as_policy(claude_raw):
                _guarded(case, _Client(replies=[_bedrock_reply()]), stream=False,
                         policy_raw=claude_raw)
            assert _breaker(case["circuit"]).state == "closed", \
                f"a Nova {code} must leave the Claude circuit closed"
            assert _breaker(case["fingerprint"]).state == "open", \
                "a Claude success must not close the Nova circuit"


@th.django_unit_test()
def test_reset_breakers_closes_both_bedrock_circuits(opts):
    from mojo.apps.account.models import LLMCircuitBreaker, User
    from mojo.apps.account.services import llm_safety

    marker = uuid.uuid4().hex
    owner = User.objects.create_user(
        username=f"claude-reset-{marker}", email=f"claude-reset-{marker}@test.com",
        password="Claude_reset_test_99")
    owner.is_active = owner.is_superuser = True
    owner.save()
    try:
        with _claude_case() as case:
            for fingerprint in (case["fingerprint"], case["circuit"]):
                LLMCircuitBreaker.objects.create(
                    provider="bedrock", credential_fingerprint=fingerprint,
                    state="open", failure_count=3, error_code="provider_timeout")
            assert llm_safety.reset_breakers(owner, provider="bedrock") == 2, \
                "a Bedrock reset must select both the Nova and the Claude circuit"
            for fingerprint in (case["fingerprint"], case["circuit"]):
                row = _breaker(fingerprint)
                assert row.state == "closed" and row.failure_count == 0 \
                    and not row.error_code, \
                    f"a Bedrock reset must close every Bedrock circuit, got {row.state}"
            state = llm_safety.aggregate_state()
            closed = [item for item in state["breakers"]
                      if item["provider"] == "bedrock" and item["state"] == "closed"]
            assert closed and closed[0]["count"] >= 2, \
                f"owner diagnostics must count both Bedrock circuits: {state['breakers']}"
    finally:
        owner.delete()


@th.django_unit_test()
def test_claude_reply_that_spent_max_tokens_thinking_is_charged_and_explained(opts):
    from mojo.apps.account.models import LLMRequest

    reply = _bedrock_reply(stop="max_tokens", content=[
        {"reasoningContent": {"reasoningText": {"text": "", "signature": "sig"}}}])
    with _claude_case() as case:
        _assert_denied("provider_rejected", lambda: _guarded(
            case, _Client(replies=[reply]), stream=False))
        row = LLMRequest.objects.get(operation=case["marker"])
        assert row.status == "failed" and row.error_code == "provider_rejected" \
            and (row.input_tokens, row.output_tokens) == (7, 3) \
            and row.error_detail == "max_tokens reached before any answer text", \
            f"a reply cut off mid-thinking must keep usage and say why: {row.error_detail!r}"
        _assert_budget_total(case, 13)
        assert _breaker(case["circuit"]).failure_count == 0, \
            "an answer cut at max_tokens is not a provider health failure"


@th.django_unit_test()
def test_claude_image_reserves_4800_and_oversized_is_refused_first(opts):
    from mojo.apps.account.models import LLMRequest
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    messages = _image_messages(_png(), count=2)
    with _claude_case(_claude_policy(("text", "images"))) as case:
        client = mock.Mock()
        client.converse.return_value = _bedrock_reply()
        reserved = []

        def client_factory(service, **kwargs):
            reserved.append(LLMRequest.objects.get(operation=case["marker"]).reserved_tokens)
            return client

        factory = lambda name, **kwargs: BedrockProvider(client_factory=client_factory, **kwargs)
        _run_guard(case, factory, messages=messages)
        sizing = json.loads(json.dumps(messages))
        for block in sizing[0]["content"][:2]:
            block["source"]["data"] = ""
        expected = len(json.dumps({"messages": sizing, "system": None, "tools": None},
                                  separators=(",", ":")).encode("utf-8")) + 8 + 2 * 4800
        assert reserved == [expected], \
            f"a Claude route must reserve 4800 tokens per image, got {reserved} vs {expected}"
        _assert_budget_total(case, 13)
        LLMRequest.objects.filter(operation=case["marker"]).delete()

        big = _png(size=(1150, 1120), noise=True)
        assert 3750000 < len(big) <= 5 * 1024 * 1024, \
            f"the fixture must sit between the Claude and the general cap, got {len(big)}"
        operation_id = uuid.uuid4().hex
        _assert_denied("provider_rejected", lambda: _run_guard(
            case, factory, messages=_image_messages(big),
            context={"operation_id": operation_id, "job_id": case["marker"]}))
        _assert_budget_total(case, 13)
        digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        operation_key = (f"mojo:llm:bedrock:{case['fingerprint']}:{case['feature']}"
                         f":operation:{digest}")
        counted = list(case["redis"].scan_iter(
            match=operation_key.rsplit(":", 1)[0] + ":*", count=1000))
        assert len(counted) == 1, \
            f"only the admitted image call may hold an operation counter, got {len(counted)}"
        assert not case["redis"].exists(operation_key), \
            "an oversized Claude image must be refused before the operation counter"
        assert client.converse.call_count == 1 and not LLMRequest.objects.filter(
            operation=case["marker"]).exists(), \
            "an oversized Claude image must be refused before any ledger row or dispatch"


@th.django_unit_test()
def test_claude_rejection_detail_is_bounded_like_nova(opts):
    from botocore import exceptions as be
    from mojo.apps.account.models import LLMRequest
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    response = {"Error": {"Code": "ValidationException", "Message": "z" * 900},
                "ResponseMetadata": {"RequestId": "bedrock-claude-10757",
                                     "HTTPStatusCode": 400}}
    client = mock.Mock()
    client.converse.side_effect = be.ClientError(response, "Converse")
    with _claude_case() as case:
        factory = lambda name, **kwargs: BedrockProvider(client=client, **kwargs)
        _assert_denied("provider_rejected", lambda: _run_guard(
            case, factory, messages=[{"role": "user", "content": "private prompt"}]))
        row = LLMRequest.objects.get(operation=case["marker"])
        assert row.error_detail == ("ValidationException: " + "z" * 900)[:512] \
            and "private prompt" not in row.error_detail, \
            f"a Claude rejection must store AWS's reason cut to 512, got {len(row.error_detail)}"
        assert row.provider_request_id == "bedrock-claude-10757", \
            "a Claude rejection must keep the AWS request id"
        assert all(case["redis"].zcard(key) == 0 for key in _case_keys(case)[:2]), \
            "a Claude rejection must release concurrency"


def _probe_ready(fingerprint):
    """An open circuit whose wait has passed: the next call is the half-open probe."""
    from django.utils import timezone
    from mojo.apps.account.models import LLMCircuitBreaker

    LLMCircuitBreaker.objects.update_or_create(
        provider="bedrock", credential_fingerprint=fingerprint,
        defaults={"state": "open", "failure_count": 2, "error_code": "provider_timeout",
                  "opened_until": timezone.now() - timezone.timedelta(seconds=1),
                  "half_open_owner": "", "half_open_expires_at": None})


def _nova_parked(fingerprint):
    from django.utils import timezone
    from mojo.apps.account.models import LLMCircuitBreaker

    LLMCircuitBreaker.objects.update_or_create(
        provider="bedrock", credential_fingerprint=fingerprint,
        defaults={"state": "open", "failure_count": 3, "error_code": "provider_unavailable",
                  "opened_until": timezone.now() + timezone.timedelta(seconds=300),
                  "half_open_owner": "", "half_open_expires_at": None})
    row = _breaker(fingerprint)
    return (row.state, row.failure_count, row.error_code, row.generation,
            row.opened_until, row.half_open_owner)


@th.django_unit_test()
def test_claude_probe_settlements_close_the_claude_circuit_only(opts):
    def plain(case):
        _guarded(case, _Client(replies=[_bedrock_reply()]), stream=False)

    def drained(case):
        list(_guarded(case, _Client(streams=[_events()])))

    def rejected(case):
        _assert_denied("provider_rejected", lambda: _guarded(
            case, _Client(replies=[_bedrock_reply(stop="content_filtered")]), stream=False))

    def cancelled(case):
        answer = _guarded(case, _Client(streams=[_events(("Hel", "lo", " there"))]))
        assert next(answer)["type"] == "text_delta", "the stream must deliver an event first"
        answer.close()

    with _claude_case() as case:
        nova_before = _nova_parked(case["fingerprint"])
        for name, run in (("plain call", plain), ("drained stream", drained),
                          ("rejected output", rejected), ("cancelled stream", cancelled)):
            _probe_ready(case["circuit"])
            run(case)
            claude = _breaker(case["circuit"])
            assert claude.state == "closed" and claude.half_open_owner == "" \
                and claude.failure_count == 0, \
                f"a {name} probe must close the Claude circuit, got {claude.state}/{claude.half_open_owner!r}"
            nova = _breaker(case["fingerprint"])
            assert (nova.state, nova.failure_count, nova.error_code, nova.generation,
                    nova.opened_until, nova.half_open_owner) == nova_before, \
                f"a Claude {name} must not touch the Nova circuit"


def _refused_probe_gives_its_turn_back(raw, circuit_key):
    from mojo.apps.account.services import llm_safety

    with _claude_case(raw) as case:
        circuit = case[circuit_key]
        _probe_ready(circuit)
        shared = case["raw"]["shared"]
        limits = case["raw"]["features"][case["feature"]]
        permit = llm_safety.acquire_permit(
            case["redis"], "bedrock", case["fingerprint"], case["feature"],
            shared, limits, 5)
        try:
            _assert_denied("concurrency_exhausted", lambda: _guarded(
                case, _Client(replies=[_bedrock_reply()]), stream=False))
        finally:
            llm_safety.release_permit(case["redis"], permit, actual_tokens=0)
        row = _breaker(circuit)
        assert row.state == "open" and row.half_open_owner == "" \
            and row.half_open_expires_at is None, \
            f"a probe refused by the permit must give its turn back, got {row.state}/{row.half_open_owner!r}"
        client = _Client(replies=[_bedrock_reply()])
        _guarded(case, client, stream=False)
        row = _breaker(circuit)
        assert len(client.calls) == 1 and row.state == "closed" and row.half_open_owner == "", \
            f"the next call with room must be the probe and close the circuit, got {row.state}"


@th.django_unit_test()
def test_claude_probe_refused_by_the_permit_is_released(opts):
    _refused_probe_gives_its_turn_back(_claude_policy(), "circuit")


@th.django_unit_test()
def test_nova_probe_refused_by_the_permit_is_released(opts):
    _refused_probe_gives_its_turn_back(_guard_policy(), "fingerprint")
