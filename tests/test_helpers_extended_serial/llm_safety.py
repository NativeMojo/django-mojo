from unittest import mock
from contextlib import contextmanager
import uuid

from testit import helpers as th


def _guard_policy(provider="bedrock", feature="assistant"):
    limits = {
        "requests_minute": 60, "requests_hour": 120, "requests_day": 240,
        "tokens_minute": 1000000, "tokens_hour": 2000000, "tokens_day": 4000000,
        "concurrency": 1, "max_input_bytes": 4096, "max_output_tokens": 128,
        "timeout_seconds": 10, "max_loop_calls": 2,
    }
    route = {
        "provider": provider, "model": "us.amazon.nova-2-lite-v1:0",
        "credential": "aws", "capabilities": ["text"],
        "region": "us-east-1", "reasoning_efforts": ["off", "low", "medium"],
    }
    if provider == "anthropic":
        route = {
            "provider": provider, "model": "claude-test", "credential": "admin",
            "capabilities": ["text", "prompt_cache"],
        }
    return {
        "version": 1, "routes": {feature: route}, "shared": dict(limits),
        "features": {feature: dict(limits)},
        "breaker": {"auth_failures": 2, "rate_failures": 2,
                    "server_failures": 2, "open_seconds": 60},
    }


@contextmanager
def _guard_case(raw=None):
    """Own the fixed installation identity only in this serial test package."""
    from mojo.apps.account.models import LLMCircuitBreaker, LLMRequest
    from mojo.apps.account.services import llm_safety
    from mojo.helpers.redis import get_connection

    raw = raw or _guard_policy()
    policy = llm_safety.parse_policy(raw)
    feature, route = next(iter(raw["routes"].items()))
    provider = route["provider"]
    marker = f"llm8533-{uuid.uuid4().hex}"
    credential = llm_safety.BEDROCK_CREDENTIAL_IDENTITY if provider == "bedrock" \
        else marker
    fingerprint = llm_safety.credential_fingerprint(provider, credential)
    redis = get_connection()
    key_pattern = f"mojo:llm:{provider}:{fingerprint}:*"

    def clear():
        keys = list(redis.scan_iter(match=key_pattern, count=1000))
        if keys:
            redis.delete(*keys)
        LLMRequest.objects.filter(operation=marker).delete()
        LLMCircuitBreaker.objects.filter(
            provider=provider, credential_fingerprint=fingerprint).delete()

    def configured(key, default=None, **kwargs):
        if key in {"LLM_ADMIN_API_KEY", "LLM_HANDLER_API_KEY"}:
            assert provider == "anthropic", \
                f"Bedrock must never read the Anthropic key setting {key}"
            return credential
        if key == "LLM_ADMIN_PROMPT_CACHE_ENABLED":
            return True
        return default

    clear()
    try:
        with mock.patch.object(llm_safety.settings, "get", side_effect=configured), \
                mock.patch.object(llm_safety, "_single_primary_value",
                                  return_value=policy["hash"]), \
                mock.patch.object(llm_safety, "emergency_stopped", return_value=False), \
                mock.patch.object(llm_safety, "_record_metrics"), \
                mock.patch.object(llm_safety, "_record_burn"):
            yield {
                "raw": raw, "policy": policy, "feature": feature,
                "provider": provider, "fingerprint": fingerprint,
                "redis": redis, "marker": marker,
            }
    finally:
        clear()


def _run_guard(case, provider_factory, **kwargs):
    from mojo.apps.account.services import llm_safety

    values = dict(
        feature=case["feature"], operation=case["marker"],
        context={"operation_id": uuid.uuid4().hex, "job_id": case["marker"]},
        max_tokens=8, provider_factory=provider_factory, redis=case["redis"],
        policy_raw=case["raw"])
    messages = kwargs.pop("messages", [{"role": "user", "content": "hello"}])
    values.update(kwargs)
    return llm_safety.execute_guarded_for_test(messages, **values)


def _assert_denied(code, run):
    from mojo.apps.account.services import llm_safety

    try:
        run()
        assert False, f"guarded request should have failed with {code}"
    except llm_safety.LLMSafetyError as err:
        assert err.code == code, f"expected safe code {code}, got {err.code}"


def _bedrock_reply(stop="end_turn", content=None):
    return {
        "ResponseMetadata": {"RequestId": "bedrock-8533-request"},
        "output": {"message": {"role": "assistant", "content":
                               [{"text": "final answer"}] if content is None else content}},
        "stopReason": stop,
        "usage": {"inputTokens": 7, "outputTokens": 3,
                  "cacheReadInputTokens": 2, "cacheWriteInputTokens": 1},
    }


def _case_keys(case):
    from mojo.apps.account.services import llm_safety

    return llm_safety._permit_keys(
        case["provider"], case["fingerprint"], case["feature"],
        __import__("time").time())


def _assert_budget_total(case, total):
    root = f"mojo:llm:{case['provider']}:{case['fingerprint']}"
    for scope in ("shared", case["feature"]):
        for window in ("minute", "hour", "day"):
            keys = list(case["redis"].scan_iter(
                match=f"{root}:{scope}:tokens:{window}:*", count=1000))
            actual = sum(int(case["redis"].get(key) or 0) for key in keys)
            assert actual == total, \
                f"{scope} {window} token budget must hold {total} billed/reserved tokens, got {actual}"


@th.django_unit_test()
def test_bedrock_breaker_persistence_failure_after_dispatch_keeps_provider_error(opts):
    from mojo.apps.account.models import LLMRequest
    from mojo.apps.account.services import llm_safety
    from mojo.helpers import llm
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    with _guard_case() as case:
        client = mock.Mock()
        client.converse.return_value = _bedrock_reply()
        factory = lambda name, **kwargs: BedrockProvider(client=client, **kwargs)
        with mock.patch.object(llm_safety, "_breaker_success",
                               side_effect=RuntimeError("private database detail")), \
                mock.patch.object(llm_safety, "invoke", side_effect=lambda *args, **kwargs:
                                  _run_guard(case, factory)):
            try:
                llm.call([{"role": "user", "content": "hello"}], feature="assistant")
                assert False, "a lost post-dispatch breaker update cannot report a successful completion"
            except llm.LLMExecutionError as error:
                assert error.code == "provider_failed" and str(error) == "provider_failed", \
                    f"post-dispatch persistence must retain safe provider provenance, got {error.code}"
        assert client.converse.call_count == 1, "post-dispatch persistence failure must never replay inference"
        row = LLMRequest.objects.get(operation=case["marker"])
        assert row.status == "started", "an unfinalized ledger must retain uncertainty instead of invented success"
        _assert_budget_total(case, 13)
        assert all(case["redis"].zcard(key) == 0 for key in _case_keys(case)[:2]), \
            "post-dispatch persistence failure must still release concurrency after reconciling observed tokens"


@th.django_unit_test()
def test_bedrock_readiness_declares_chain_without_sdk_or_anthropic_keys(opts):
    from mojo.apps.account.services import llm_safety

    with _guard_case() as case, \
            mock.patch.object(llm_safety, "parse_policy", return_value=case["policy"]), \
            mock.patch("mojo.helpers.llm_providers.get_provider") as factory, \
            mock.patch("mojo.helpers.aws.client.get_client") as aws_client:
        state = llm_safety.route_state(case["feature"])
        identity = llm_safety._credential(case["raw"]["routes"][case["feature"]])
    assert state["provider"] == "bedrock" and state["credential"] == "aws" \
        and state["credential_configured"] is True and state["ready"] is True, \
        f"Bedrock declarative chain readiness is incorrect: {state}"
    assert identity == llm_safety.BEDROCK_CREDENTIAL_IDENTITY, \
        "role refresh must keep a fixed installation budget identity"
    assert factory.call_count == aws_client.call_count == 0, \
        "readiness must not construct a provider, AWS client, or resolve AWS credentials"


@th.django_unit_test()
def test_bedrock_guard_is_lazy_uncached_and_reconciles_actual_usage(opts):
    from mojo.apps.account.models import LLMRequest
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    with _guard_case() as case:
        client = mock.Mock()
        client.converse.return_value = _bedrock_reply()
        created = []

        def client_factory(service, **kwargs):
            started = LLMRequest.objects.get(operation=case["marker"])
            assert started.status == "started", \
                "AWS client construction must occur after the durable started ledger"
            assert all(case["redis"].zcard(key) == 1 for key in _case_keys(case)[:2]), \
                "AWS client construction must occur after both admission leases"
            created.append((service, kwargs))
            return client

        def factory(name, **kwargs):
            assert name == "bedrock" and kwargs == {"region": "us-east-1"}, \
                f"Bedrock construction must pass only its policy region, got {name}/{kwargs}"
            assert not created, "constructing the Bedrock adapter must not construct the SDK client"
            return BedrockProvider(client_factory=client_factory, **kwargs)

        response = _run_guard(case, factory, reasoning_effort="low", system="Be brief")
        row = LLMRequest.objects.get(operation=case["marker"])
        assert response["provider"] == "bedrock" and response["content"] == [
            {"type": "text", "text": "final answer"}], \
            f"the guarded caller did not receive normalized final text: {response}"
        assert row.status == "succeeded" and row.provider_request_id == "bedrock-8533-request", \
            f"the success ledger lost status/request identity: {row.status}/{row.provider_request_id}"
        assert (row.input_tokens, row.output_tokens, row.cache_read_input_tokens,
                row.cache_creation_input_tokens) == (7, 3, 2, 1), \
            "the ledger must persist all four separate billed usage classes"
        _assert_budget_total(case, 13)
        assert all(case["redis"].zcard(key) == 0 for key in _case_keys(case)[:2]), \
            "successful Bedrock calls must release both concurrency leases"
        wire = client.converse.call_args.kwargs
        assert "cachePoint" not in str(wire) and "cache_control" not in str(wire), \
            f"Anthropic's enabled cache setting leaked explicit cache controls to Bedrock: {wire}"
        assert wire["additionalModelRequestFields"]["reasoningConfig"]["maxReasoningEffort"] == "low", \
            f"validated reasoning effort did not reach the Bedrock wire request: {wire}"
        assert len(created) == 1 and created[0][0] == "bedrock-runtime", \
            f"one guarded request must create exactly one Bedrock runtime client: {created}"


@th.django_unit_test()
def test_bedrock_guards_deny_before_sdk_or_ledger(opts):
    from mojo.apps.account.models import LLMRequest
    from mojo.apps.account.services import llm_safety
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    with _guard_case() as case:
        sdk = mock.Mock(side_effect=AssertionError("a denied request reached AWS"))
        factory = lambda name, **kwargs: BedrockProvider(client_factory=sdk, **kwargs)
        cases = [
            ("route_missing", {"feature": "memory"}),
            ("capability_unsupported", {"reasoning_effort": "high"}),
            ("capability_unsupported", {"reasoning_effort": None}),
            ("model_mismatch", {"model": "global.amazon.nova-2-lite-v1:0"}),
            ("output_too_large", {"max_tokens": 129}),
            ("input_too_large", {"messages": [{"role": "user", "content": "x" * 5000}]}),
            ("context_invalid", {"context": {"unexpected": 1}}),
            ("operation_invalid", {"context": {"operation_id": 123}}),
            ("capability_unsupported", {"tools": [{"name": "unsupported"}]}),
            ("capability_unsupported", {"messages": [{"role": "user", "content": [
                {"type": "image", "source": "unsupported"}]}]}),
        ]
        for code, arguments in cases:
            _assert_denied(code, lambda: _run_guard(case, factory, **arguments))
        with mock.patch.object(llm_safety, "_single_primary_value", return_value="stale"):
            _assert_denied("policy_mixed", lambda: _run_guard(case, factory))
        with mock.patch.object(llm_safety, "emergency_stopped", return_value=True):
            _assert_denied("emergency_stopped", lambda: _run_guard(case, factory))
        assert sdk.call_count == 0 and not LLMRequest.objects.filter(
            operation=case["marker"]).exists(), \
            "pre-network denials must not reach the SDK or create a started request ledger"

    raw = _guard_policy()
    raw["routes"]["assistant"]["reasoning_efforts"] = ["off"]
    with _guard_case(raw) as case:
        factory = mock.Mock()
        _assert_denied("capability_unsupported", lambda: _run_guard(
            case, factory, reasoning_effort="low"))
        assert factory.call_count == 0, "unapproved effort must deny before adapter construction"

    raw = _guard_policy()
    raw["shared"]["max_output_tokens"] = 100000
    raw["features"]["assistant"]["max_output_tokens"] = 100000
    with _guard_case(raw) as case:
        factory = mock.Mock()
        _assert_denied("output_too_large", lambda: _run_guard(case, factory, max_tokens=65001))
        assert factory.call_count == 0, "the Nova 65000-token ceiling must deny before construction"


@th.django_unit_test()
def test_bedrock_output_rejections_keep_usage_and_do_not_trip_breaker(opts):
    from django.utils import timezone
    from mojo.apps.account.models import LLMCircuitBreaker, LLMRequest
    from mojo.apps.account.services import llm_safety
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    with _guard_case() as case:
        client = mock.Mock()
        factory = lambda name, **kwargs: BedrockProvider(client=client, **kwargs)
        replies = [
            _bedrock_reply(stop="content_filtered"),
            _bedrock_reply(content=[{"reasoningContent": {"redactedContent": b"private"}}]),
            _bedrock_reply(content=[{"text": "   "}]),
        ]
        with mock.patch.object(llm_safety, "_breaker_failure") as breaker_failure:
            for reply in replies:
                client.converse.return_value = reply
                _assert_denied("provider_rejected", lambda: _run_guard(case, factory))
            breaker = LLMCircuitBreaker.objects.get(
                provider="bedrock", credential_fingerprint=case["fingerprint"])
            assert breaker.state == "closed" and breaker.failure_count == 0, \
                f"consecutive prompt-dependent failures opened the circuit: {breaker.state}"
            breaker.state = "open"
            breaker.opened_until = timezone.now() - timezone.timedelta(seconds=1)
            breaker.save(update_fields=["state", "opened_until"])
            _assert_denied("provider_rejected", lambda: _run_guard(case, factory))
            breaker.refresh_from_db()
            assert breaker.state == "closed" and not breaker.half_open_owner \
                and breaker.failure_count == 0, \
                "a rejected answer must release the half-open health lease as success"
            assert breaker_failure.call_count == 0, \
                "Bedrock output validation must never call the health failure path"
        rows = list(LLMRequest.objects.filter(operation=case["marker"]))
        assert len(rows) == 4 and all(row.status == "failed" and
            row.error_code == "provider_rejected" and row.input_tokens == 7 and
            row.output_tokens == 3 and row.cache_read_input_tokens == 2 and
            row.cache_creation_input_tokens == 1 for row in rows), \
            "billed-but-rejected final answers must retain validated usage in each failed ledger"
        _assert_budget_total(case, 52)
        assert all(case["redis"].zcard(key) == 0 for key in _case_keys(case)[:2]), \
            "output rejections must release concurrency"


@th.django_unit_test()
def test_bedrock_unknown_spend_retains_reservation_and_only_health_opens(opts):
    from mojo.apps.account.models import LLMCircuitBreaker, LLMRequest
    from mojo.helpers.llm_providers.base import ProviderError

    for failure in (ProviderError("provider_timeout"), RuntimeError("private SDK detail")):
        with _guard_case() as case:
            adapter = mock.Mock()
            adapter.supports.return_value = True
            adapter.call.side_effect = failure
            code = "provider_timeout" if isinstance(failure, ProviderError) else "provider_failed"
            _assert_denied(code, lambda: _run_guard(case, lambda *args, **kwargs: adapter))
            row = LLMRequest.objects.get(operation=case["marker"])
            assert row.status == "failed" and row.error_code == code, \
                f"ambiguous provider outcome lost its safe failed ledger: {row.status}/{row.error_code}"
            _assert_budget_total(case, row.reserved_tokens)
            assert all(case["redis"].zcard(key) == 0 for key in _case_keys(case)[:2]), \
                "unknown spend must release concurrency while retaining token budget"
            breaker = LLMCircuitBreaker.objects.get(
                provider="bedrock", credential_fingerprint=case["fingerprint"])
            assert breaker.failure_count == (1 if code == "provider_timeout" else 0), \
                f"only provider health failures may affect Bedrock circuit state: {code}"


@th.django_unit_test()
def test_bedrock_health_errors_open_the_shared_circuit_at_threshold(opts):
    from mojo.apps.account.models import LLMCircuitBreaker
    from mojo.helpers.llm_providers.base import ProviderError

    for code in ("provider_authentication", "provider_rate_limited",
                 "provider_timeout", "provider_unavailable"):
        with _guard_case() as case:
            adapter = mock.Mock()
            adapter.supports.return_value = True
            adapter.call.side_effect = ProviderError(code)
            factory = lambda *args, **kwargs: adapter
            for _ in range(2):
                _assert_denied(code, lambda: _run_guard(case, factory))
            breaker = LLMCircuitBreaker.objects.get(
                provider="bedrock", credential_fingerprint=case["fingerprint"])
            assert breaker.state == "open" and breaker.failure_count == 2 \
                and breaker.error_code == code, \
                f"Bedrock health failure {code} must open its circuit at the policy threshold"
            _assert_denied("breaker_open", lambda: _run_guard(case, factory))
            assert adapter.call.call_count == 2, \
                f"the open circuit dispatched another Bedrock request after {code}"


@th.django_unit_test()
def test_bedrock_keeps_operation_permit_and_circuit_limits_authoritative(opts):
    from django.utils import timezone
    from mojo.apps.account.models import LLMCircuitBreaker, LLMRequest
    from mojo.apps.account.services import llm_safety
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    with _guard_case() as case:
        client = mock.Mock()
        client.converse.return_value = _bedrock_reply()
        factory = lambda name, **kwargs: BedrockProvider(client=client, **kwargs)
        shared = case["raw"]["shared"]
        limits = case["raw"]["features"][case["feature"]]
        permit = llm_safety.acquire_permit(
            case["redis"], "bedrock", case["fingerprint"], case["feature"],
            shared, limits, 5)
        try:
            _assert_denied("concurrency_exhausted", lambda: _run_guard(case, factory))
        finally:
            llm_safety.release_permit(case["redis"], permit, actual_tokens=0)
        token_key = next(key for key in _case_keys(case) if ":shared:tokens:day:" in key)
        case["redis"].set(token_key, shared["tokens_day"], keepttl=True)
        try:
            _assert_denied("budget_exhausted", lambda: _run_guard(case, factory))
        finally:
            case["redis"].set(token_key, 0, keepttl=True)
        breaker = LLMCircuitBreaker.objects.get(
            provider="bedrock", credential_fingerprint=case["fingerprint"])
        breaker.state = "open"
        breaker.opened_until = timezone.now() + timezone.timedelta(seconds=30)
        breaker.save(update_fields=["state", "opened_until"])
        _assert_denied("breaker_open", lambda: _run_guard(case, factory))
        assert client.converse.call_count == 0 and not LLMRequest.objects.filter(
            operation=case["marker"]).exists(), \
            "permit and circuit denials must occur before dispatch or started ledger"
        breaker.state = "closed"
        breaker.save(update_fields=["state"])
        context = {"operation_id": "same-durable-job"}
        _run_guard(case, factory, context=context)
        _run_guard(case, factory, context=context)
        _assert_denied("loop_limit", lambda: _run_guard(case, factory, context=context))
        assert client.converse.call_count == 2, \
            "one stable operation ID must not dispatch more than its approved loop budget"


@th.django_unit_test()
def test_anthropic_guard_keeps_default_kwargs_and_failure_refund(opts):
    from mojo.apps.account.models import LLMCircuitBreaker, LLMRequest
    from mojo.helpers.llm_providers.base import ProviderError

    with _guard_case(_guard_policy("anthropic")) as case:
        adapter = mock.Mock()
        adapter.supports.return_value = True
        adapter.call.side_effect = ProviderError("provider_rejected", usage={
            "input_tokens": 7, "output_tokens": 3, "cache_read_input_tokens": 2,
            "cache_creation_input_tokens": 1})
        factory = mock.Mock(return_value=adapter)
        _assert_denied("capability_unsupported", lambda: _run_guard(
            case, factory, reasoning_effort="low"))
        assert factory.call_count == 0, "Anthropic non-off effort must deny before construction"
        _assert_denied("provider_rejected", lambda: _run_guard(case, factory))
        assert factory.call_args.kwargs == {"api_key": case["marker"]}, \
            f"Anthropic factory kwargs changed: {factory.call_args}"
        assert "reasoning_effort" not in adapter.call.call_args.kwargs \
            and adapter.call.call_args.kwargs["cache_enabled"] is True, \
            "Anthropic must retain its existing call kwargs and cache setting"
        row = LLMRequest.objects.get(operation=case["marker"])
        assert row.input_tokens == row.output_tokens == 0, \
            "Anthropic exception usage semantics must remain unchanged"
        _assert_budget_total(case, 0)
        breaker = LLMCircuitBreaker.objects.get(
            provider="anthropic", credential_fingerprint=case["fingerprint"])
        assert breaker.failure_count == 1, \
            "Anthropic rejected responses must retain the existing breaker failure semantics"


@th.django_unit_test()
def test_bedrock_first_does_not_consume_anthropic_cache_warning(opts):
    from mojo.helpers import llm
    from mojo.apps.account.services import llm_safety

    bedrock = {"provider": "bedrock", "content": [{"type": "text", "text": "answer"}],
               "usage": {"input_tokens": 2, "output_tokens": 1}}
    anthropic = {"content": [{"type": "text", "text": "answer"}],
                 "usage": {"input_tokens": 2, "output_tokens": 1}}
    with mock.patch.object(llm, "_zero_cache_warned", False), \
            mock.patch.object(llm.settings, "get", return_value=True), \
            mock.patch.object(llm.logger, "warning") as warning, \
            mock.patch.object(llm_safety, "invoke", side_effect=[bedrock, anthropic]) as invoke:
        answer = llm.ask("hello", feature="assistant", reasoning_effort="low")
        assert answer == "answer" and llm._zero_cache_warned is False \
            and warning.call_count == 0, \
            "a Bedrock response must not warn or consume Anthropic's once-per-worker cache warning"
        assert invoke.call_args.kwargs["reasoning_effort"] == "low", \
            "ask must forward internal reasoning effort through call to the guard"
        llm.call([{"role": "user", "content": "hello"}], feature="assistant")
        assert llm._zero_cache_warned is True and warning.call_count == 1, \
            "the first later Anthropic zero-cache response must still emit its warning"
        assert invoke.call_args.kwargs["reasoning_effort"] == "off", \
            "the existing default Anthropic facade call must use off effort"


@th.django_unit_test()
def test_bedrock_routes_cannot_become_key_probes_or_model_catalogues(opts):
    from mojo.apps.account.models import LLMRequest, User
    from mojo.apps.account.services import llm_safety

    marker = uuid.uuid4().hex
    owner = User.objects.create_user(
        username=f"bedrock-probe-{marker}", email=f"bedrock-probe-{marker}@test.com",
        password="Bedrock_probe_test_99")
    owner.is_active = owner.is_superuser = True
    owner.save()
    try:
        with _guard_case(_guard_policy(feature="configuration")) as case, \
                mock.patch.object(llm_safety, "parse_policy", return_value=case["policy"]), \
                mock.patch.object(llm_safety, "emergency_stopped", return_value=True), \
                mock.patch("mojo.helpers.llm_providers.get_provider") as factory, \
                mock.patch("mojo.helpers.aws.client.get_client") as aws_client:
            _assert_denied("provider_unsupported", lambda: llm_safety.verify_candidate(
                owner, "candidate-key-is-anthropic-only"))
            # Stored-key semantics remain exact-target Anthropic lookup; the
            # configuration core must refuse Bedrock before any probe client.
            with mock.patch.object(llm_safety.settings, "get", return_value="stored-key"):
                _assert_denied("provider_unsupported", lambda: llm_safety.verify_stored_key("admin"))
            assert factory.call_count == aws_client.call_count == 0, \
                "neither candidate recovery nor a stored-key probe may construct Bedrock"
            assert not LLMRequest.objects.filter(provider="bedrock").exists(), \
                "a refused Anthropic key probe must create no Bedrock request ledger"
        with _guard_case(_guard_policy(feature="model_discovery")) as case, \
                mock.patch.object(llm_safety, "parse_policy", return_value=case["policy"]), \
                mock.patch("mojo.helpers.llm_providers.get_provider") as factory, \
                mock.patch("mojo.helpers.aws.client.get_client") as aws_client:
            _assert_denied("provider_unsupported", llm_safety.discover_models)
            assert factory.call_count == aws_client.call_count == 0, \
                "an Anthropic model catalogue must reject Bedrock before provider construction"
    finally:
        owner.delete()


@th.django_unit_test()
def test_owner_can_reset_only_the_explicit_bedrock_circuit(opts):
    from mojo.apps.account.models import LLMCircuitBreaker, User
    from mojo.apps.account.services import llm_safety

    marker = uuid.uuid4().hex
    owner = User.objects.create_user(
        username=f"bedrock-reset-{marker}", email=f"bedrock-reset-{marker}@test.com",
        password="Bedrock_reset_test_99")
    owner.is_active = owner.is_superuser = True
    owner.save()
    other = LLMCircuitBreaker.objects.create(
        provider="anthropic", credential_fingerprint=marker, state="open", failure_count=3)
    try:
        with _guard_case() as case:
            breaker = LLMCircuitBreaker.objects.create(
                provider="bedrock", credential_fingerprint=case["fingerprint"],
                state="half_open", failure_count=3, half_open_owner="test-owner")
            generation = breaker.generation
            assert llm_safety.reset_breakers(owner, provider="bedrock") == 1, \
                "an owner must be able to select and reset the Bedrock provider"
            breaker.refresh_from_db()
            other.refresh_from_db()
            assert breaker.state == "closed" and breaker.failure_count == 0 \
                and not breaker.half_open_owner and breaker.generation == generation + 1, \
                "a Bedrock reset must clear health state and invalidate earlier leases"
            assert other.state == "open" and other.failure_count == 3, \
                "an explicit Bedrock reset must not reset an Anthropic circuit"
    finally:
        other.delete()
        owner.delete()


@th.django_unit_test()
def test_anthropic_provider_disables_sdk_retries(opts):
    """The adapter must disable retries so the governed caller owns them."""
    from mojo.helpers.llm_providers.anthropic import AnthropicProvider

    with mock.patch("anthropic.Anthropic") as constructor:
        adapter = AnthropicProvider(api_key="candidate")
        adapter._client()
    constructor.assert_called_once_with(api_key="candidate", max_retries=0)


@th.django_unit_test()
def test_explicit_admin_credential_never_falls_back_to_handler(opts):
    from mojo.apps.account.services import llm_safety

    def configured(key, default=None):
        if key == "LLM_HANDLER_API_KEY":
            return "handler-secret"
        return default

    route = {"credential": "admin"}
    with mock.patch.object(llm_safety.settings, "get", side_effect=configured):
        try:
            llm_safety._credential(route)
            assert False, "an explicit admin route must deny when the admin key is absent"
        except llm_safety.LLMSafetyError as err:
            assert err.code == "credential_missing", \
                f"explicit admin denial must use credential_missing, got {err.code}"


@th.django_unit_test()
def test_stored_probe_uses_exact_target_and_candidate_requires_owner(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import User
    from mojo.apps.account.services import llm_safety

    captured = []

    def fixed(actor, credential, identity, operation):
        captured.append((credential, identity, operation))
        return True

    def configured(key, default=None, **kwargs):
        return {
            "LLM_ADMIN_API_KEY": "admin-exact-secret",
            "LLM_HANDLER_API_KEY": "handler-exact-secret",
        }.get(key, default)

    with mock.patch.object(llm_safety.settings, "get", side_effect=configured), \
            mock.patch.object(llm_safety, "_fixed_configuration_probe", side_effect=fixed):
        assert llm_safety.verify_stored_key("admin") is True, \
            "the exact stored admin target did not verify"
        assert llm_safety.verify_stored_key("handler") is True, \
            "the exact stored handler target did not verify"
    assert [row[0] for row in captured] == [
        "admin-exact-secret", "handler-exact-secret"], \
        f"stored verification discarded or fell back from its target: {captured}"
    assert all(row[1] != "candidate-installation" for row in captured), \
        f"stored probes must never receive the candidate recovery identity: {captured}"

    with mock.patch.object(
            llm_safety.settings, "get",
            side_effect=lambda key, default=None, **kwargs:
            "handler-only" if key == "LLM_HANDLER_API_KEY" else default):
        try:
            llm_safety.verify_stored_key("admin")
            assert False, "missing exact admin target must not fall back to handler"
        except llm_safety.LLMSafetyError as err:
            assert err.code == "credential_missing", \
                f"missing exact target used wrong safe code: {err.code}"

    regular = User.objects.create_user(
        username=f"candidate-non-owner-{uuid.uuid4().hex}",
        email=f"candidate-non-owner-{uuid.uuid4().hex}@test.com",
        password="Candidate_owner_test_99")
    regular.is_active = True
    regular.save()
    try:
        with th.assert_raises(merrors.PermissionDeniedException):
            llm_safety.verify_candidate(regular, "candidate-secret")
        regular.is_superuser = True
        regular.save(update_fields=["is_superuser"])
        for invalid in (None, "", "   ", " candidate-secret "):
            try:
                llm_safety.verify_candidate(regular, invalid)
                assert False, f"invalid candidate input was accepted: {invalid!r}"
            except llm_safety.LLMSafetyError as err:
                assert err.code == "credential_missing", \
                    f"invalid candidate used wrong safe code: {err.code}"
    finally:
        regular.delete()


@th.django_unit_test()
def test_candidate_boundary_audits_outcomes_and_fails_before_provider(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import llm_safety
    from mojo.apps.logit.models import Log

    owner = User.objects.create_user(
        username=f"candidate-audit-{uuid.uuid4().hex}",
        email=f"candidate-audit-{uuid.uuid4().hex}@test.com",
        password="Candidate_audit_test_99")
    owner.is_active = True
    owner.is_superuser = True
    owner.save()
    Log.objects.filter(
        kind="llm:candidate_probe", model_name="account.User",
        model_id=owner.pk).delete()
    try:
        with mock.patch.object(
                llm_safety, "_fixed_configuration_probe", return_value=True):
            assert llm_safety.verify_candidate(owner, "accepted-candidate") is True, \
                "an accepted direct candidate probe did not return success"
        accepted = Log.objects.filter(
            kind="llm:candidate_probe", model_name="account.User",
            model_id=owner.pk).order_by("created").last()
        assert accepted and accepted.log == \
            "LLM candidate recovery probe outcome=accepted", \
            f"accepted direct probe lacks its bounded actor audit: {accepted}"

        with mock.patch.object(
                llm_safety, "_fixed_configuration_probe",
                side_effect=llm_safety.LLMSafetyError(
                    "provider_authentication")):
            try:
                llm_safety.verify_candidate(owner, "rejected-candidate")
                assert False, "a provider-rejected candidate returned success"
            except llm_safety.LLMSafetyError as err:
                assert err.code == "provider_authentication", \
                    f"provider rejection lost its safe code: {err.code}"
        rejected = Log.objects.filter(
            kind="llm:candidate_probe", model_name="account.User",
            model_id=owner.pk).order_by("created").last()
        assert rejected and rejected.log == \
            "LLM candidate recovery probe outcome=rejected", \
            f"rejected direct probe lacks its bounded actor audit: {rejected}"

        provider = mock.Mock()
        with mock.patch.object(
                User, "log", side_effect=RuntimeError("audit unavailable")), \
                mock.patch.object(
                    llm_safety, "_fixed_configuration_probe", provider):
            try:
                llm_safety.verify_candidate(owner, "audit-failed-candidate")
                assert False, "an unaudited candidate probe was allowed"
            except llm_safety.LLMSafetyError as err:
                assert err.code == "safety_unavailable", \
                    f"audit failure used the wrong safe code: {err.code}"
        assert provider.call_count == 0, \
            f"audit failure reached the provider path {provider.call_count} time(s)"
    finally:
        Log.objects.filter(
            kind="llm:candidate_probe", model_name="account.User",
            model_id=owner.pk).delete()
        owner.delete()


@th.django_unit_test()
def test_fixed_probe_is_text_only_uncached_and_stored_stop_is_enforced(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import llm_safety

    calls = []

    class Adapter:
        def supports(self, capability):
            return capability == "text"

        def call(self, **kwargs):
            calls.append(kwargs)
            return {"id": "probe-one", "usage": {
                "input_tokens": 2, "output_tokens": 1}}

    policy = {
        "version": 1,
        "routes": {"configuration": {
            "provider": "anthropic", "model": "claude-config-exact",
            "credential": "admin", "capabilities": ["text"],
        }},
        "shared": {
            "requests_minute": 2, "requests_hour": 10, "requests_day": 20,
            "tokens_minute": 1000, "tokens_hour": 5000, "tokens_day": 10000,
            "concurrency": 4, "max_input_bytes": 4096, "max_output_tokens": 128,
            "timeout_seconds": 10, "max_loop_calls": 2,
        },
        "features": {},
        "breaker": {"auth_failures": 2, "rate_failures": 2,
                    "server_failures": 2, "open_seconds": 60},
    }
    policy["features"]["configuration"] = dict(policy["shared"])
    policy = llm_safety.parse_policy(policy)
    owner = User.objects.create_user(
        username=f"candidate-owner-{uuid.uuid4().hex}",
        email=f"candidate-owner-{uuid.uuid4().hex}@test.com",
        password="Candidate_owner_test_99")
    owner.is_active = True
    owner.is_superuser = True
    owner.save()
    candidate = f"candidate-{uuid.uuid4().hex}"
    try:
        with mock.patch.object(llm_safety, "parse_policy", return_value=policy), \
                mock.patch.object(llm_safety, "_policy_agreement", lambda value: None), \
                mock.patch.object(llm_safety, "emergency_stopped", return_value=True), \
                mock.patch("mojo.helpers.llm_providers.get_provider",
                           side_effect=lambda name, api_key: Adapter()):
            assert llm_safety.verify_candidate(owner, candidate) is True, \
                "the actor-bound fixed candidate probe did not complete while stopped"
    finally:
        owner.delete()
    assert len(calls) == 1, f"one probe must equal one adapter call, got {len(calls)}"
    assert calls[0]["messages"] == [{"role": "user", "content": "Reply OK"}] \
        and calls[0]["system"] is None and calls[0]["tools"] is None, \
        f"configuration probe shape is not fixed and text-only: {calls[0]}"
    assert calls[0]["model"] == "claude-config-exact" \
        and calls[0]["max_tokens"] == 4 and calls[0]["cache_enabled"] is False, \
        f"configuration probe did not bind route model/4 tokens/no cache: {calls[0]}"

    with mock.patch.object(llm_safety, "parse_policy", return_value=policy), \
            mock.patch.object(llm_safety, "_policy_agreement", lambda value: None), \
            mock.patch.object(llm_safety, "emergency_stopped", return_value=True), \
            mock.patch.object(llm_safety.settings, "get", return_value="stored-secret"), \
            mock.patch("mojo.helpers.llm_providers.get_provider",
                       side_effect=lambda name, api_key: Adapter()):
        try:
            llm_safety.verify_stored_key("admin")
            assert False, "the stored probe bypassed the emergency stop"
        except llm_safety.LLMSafetyError as err:
            assert err.code == "emergency_stopped", \
                f"stored stop denial used wrong code: {err.code}"


@th.django_unit_test()
def test_malformed_emergency_controls_fail_closed_before_provider(opts):
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import llm_safety

    limits = {
        "requests_minute": 2, "requests_hour": 10, "requests_day": 20,
        "tokens_minute": 1000, "tokens_hour": 5000, "tokens_day": 10000,
        "concurrency": 2, "max_input_bytes": 4096, "max_output_tokens": 128,
        "timeout_seconds": 10, "max_loop_calls": 2,
    }
    raw = {
        "version": 1,
        "routes": {"assistant": {
            "provider": "anthropic", "model": "claude-exact",
            "credential": "admin", "capabilities": ["text"],
        }},
        "shared": dict(limits), "features": {"assistant": dict(limits)},
        "breaker": {"auth_failures": 2, "rate_failures": 2,
                    "server_failures": 2, "open_seconds": 60},
    }
    policy = llm_safety.parse_policy(raw)
    provider = mock.Mock()
    with mock.patch.object(llm_safety, "parse_policy", return_value=policy), \
            mock.patch.object(llm_safety, "_policy_agreement", lambda value: None), \
            mock.patch.object(llm_safety.settings, "get_static", return_value="maybe"), \
            mock.patch.object(llm_safety.settings, "get", return_value="admin-secret"), \
            mock.patch("mojo.helpers.llm_providers.get_provider", provider):
        state = llm_safety.route_state("assistant")
    assert state["ready"] is False and state["error"] == "control_state_unknown", \
        f"malformed static stop did not fail closed: {state}"
    assert provider.call_count == 0, \
        "malformed static stop reached a provider adapter"

    Setting.objects.filter(key="LLM_EMERGENCY_STOP", group=None).delete()
    Setting.objects.bulk_create([
        Setting(key="LLM_EMERGENCY_STOP", group=None, value="maybe")])
    try:
        with mock.patch.object(llm_safety, "parse_policy", return_value=policy), \
                mock.patch.object(llm_safety, "_policy_agreement", lambda value: None), \
                mock.patch.object(llm_safety.settings, "get_static", return_value=False), \
                mock.patch.object(llm_safety.settings, "get", return_value="admin-secret"), \
                mock.patch("mojo.helpers.llm_providers.get_provider", provider):
            state = llm_safety.route_state("assistant")
        assert state["ready"] is False and state["error"] == "control_state_unknown", \
            f"malformed database stop did not fail closed: {state}"
        assert provider.call_count == 0, \
            "malformed database stop reached a provider adapter"
    finally:
        Setting.objects.filter(key="LLM_EMERGENCY_STOP", group=None).delete()


def _image_policy():
    raw = _guard_policy()
    raw["routes"]["assistant"]["capabilities"] = ["text", "images"]
    return raw


def _png(size=(4, 3), noise=False):
    import io
    import os
    from PIL import Image

    out = io.BytesIO()
    if noise:
        image = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    else:
        image = Image.new("RGB", size, (30, 160, 90))
    image.save(out, format="PNG")
    return out.getvalue()


def _image_messages(raw, count=1):
    from mojo.helpers.llm_images import image_block

    block = image_block(raw, "image/png")
    return [{"role": "user", "content": [block] * count + [
        {"type": "text", "text": "Describe this screenshot."}]}]


@th.django_unit_test()
def test_text_only_route_refuses_images_before_adapter_or_ledger(opts):
    from mojo.apps.account.models import LLMRequest

    with _guard_case() as case:
        factory = mock.Mock(side_effect=AssertionError("an image reached a text-only adapter"))
        _assert_denied("capability_unsupported", lambda: _run_guard(
            case, factory, messages=_image_messages(_png())))
        assert factory.call_count == 0 and not LLMRequest.objects.filter(
            operation=case["marker"]).exists(), \
            "a text-only route must refuse an image before any adapter or ledger row"


@th.django_unit_test()
def test_image_route_dispatches_bytes_and_records_exact_usage(opts):
    import json
    from mojo.apps.account.models import LLMRequest
    from mojo.helpers.llm_images import IMAGE_RESERVED_TOKENS
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    raw = _png(size=(64, 64), noise=True)
    messages = _image_messages(raw, count=2)
    assert len(json.dumps(messages)) > 4096, \
        "fixture must exceed the policy byte cap so blanked sizing is exercised"
    with _guard_case(_image_policy()) as case:
        client = mock.Mock()
        client.converse.return_value = _bedrock_reply()
        reserved = []

        def client_factory(service, **kwargs):
            row = LLMRequest.objects.get(operation=case["marker"])
            reserved.append(row.reserved_tokens)
            return client

        factory = lambda name, **kwargs: BedrockProvider(client_factory=client_factory, **kwargs)
        response = _run_guard(case, factory, messages=messages)
        sizing = json.loads(json.dumps(messages))
        for block in sizing[0]["content"][:2]:
            block["source"]["data"] = ""
        expected = len(json.dumps(
            {"messages": sizing, "system": None, "tools": None},
            separators=(",", ":")).encode("utf-8")) + 8 + 2 * IMAGE_RESERVED_TOKENS
        assert reserved == [expected], \
            f"the permit must reserve blanked bytes + max_tokens + {IMAGE_RESERVED_TOKENS} per image, got {reserved} vs {expected}"
        wire = client.converse.call_args.kwargs["messages"][0]["content"]
        assert wire[:2] == [{"image": {"format": "png", "source": {"bytes": raw}}}] * 2 \
            and wire[2] == {"text": "Describe this screenshot."}, \
            "both images must reach Converse as validated raw bytes, then the text"
        row = LLMRequest.objects.get(operation=case["marker"])
        assert response["provider"] == "bedrock" and row.status == "succeeded" \
            and (row.input_tokens, row.output_tokens, row.cache_read_input_tokens,
                 row.cache_creation_input_tokens) == (7, 3, 2, 1), \
            f"an image call must keep the exact succeeded ledger usage, got {row.status}"
        _assert_budget_total(case, 13)


@th.django_unit_test()
def test_image_provider_failure_keeps_returned_usage(opts):
    from mojo.apps.account.models import LLMRequest
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    with _guard_case(_image_policy()) as case:
        client = mock.Mock()
        client.converse.return_value = _bedrock_reply(stop="content_filtered")
        factory = lambda name, **kwargs: BedrockProvider(client=client, **kwargs)
        _assert_denied("provider_rejected", lambda: _run_guard(
            case, factory, messages=_image_messages(_png())))
        row = LLMRequest.objects.get(operation=case["marker"])
        assert row.status == "failed" and row.error_code == "provider_rejected" \
            and (row.input_tokens, row.output_tokens) == (7, 3), \
            f"a billed image failure must keep its usage on the failed ledger, got {row.status}"
        _assert_budget_total(case, 13)


@th.django_unit_test()
def test_invalid_images_deny_before_adapter_or_ledger(opts):
    from mojo.apps.account.models import LLMRequest
    from mojo.helpers.llm_images import MAX_IMAGES

    raw = _png()
    forged = _image_messages(raw)
    forged[0]["content"][0]["source"]["media_type"] = "image/jpeg"
    misplaced = [{"role": "assistant", "content": _image_messages(raw)[0]["content"]},
                 {"role": "user", "content": "And now?"}]
    with _guard_case(_image_policy()) as case:
        factory = mock.Mock(side_effect=AssertionError("an invalid image reached the adapter"))
        cases = [
            ("image_invalid", {"messages": forged}),
            ("image_invalid", {"messages": misplaced}),
            ("image_invalid", {"messages": [{"role": "user", "content": [
                {"type": "image", "source": "unsupported"}]}]}),
            ("image_invalid", {"system": _image_messages(raw)[0]["content"][:1]}),
            ("input_too_large", {"messages": _image_messages(raw, count=MAX_IMAGES + 1)}),
        ]
        for code, arguments in cases:
            _assert_denied(code, lambda: _run_guard(case, factory, **arguments))
        assert factory.call_count == 0 and not LLMRequest.objects.filter(
            operation=case["marker"]).exists(), \
            "image refusals must happen before any adapter or ledger row"


@th.django_unit_test()
def test_anthropic_image_route_validates_before_adapter_and_passes_base64(opts):
    from mojo.apps.account.models import LLMRequest

    raw = _guard_policy("anthropic")
    raw["routes"]["assistant"]["capabilities"] = ["text", "images", "prompt_cache"]
    with _guard_case(raw) as case:
        adapter = mock.Mock()
        adapter.supports.return_value = True
        adapter.call.return_value = {
            "id": "anthropic-image-request", "provider": "anthropic",
            "content": [{"type": "text", "text": "a red square"}],
            "usage": {"input_tokens": 5, "output_tokens": 2}}
        factory = mock.Mock(return_value=adapter)
        forged = _image_messages(_png())
        forged[0]["content"][0]["source"]["media_type"] = "image/webp"
        _assert_denied("image_invalid", lambda: _run_guard(case, factory, messages=forged))
        assert factory.call_count == 0 and adapter.call.call_count == 0 \
            and not LLMRequest.objects.filter(operation=case["marker"]).exists(), \
            "an invalid Anthropic image must be refused before the adapter or the ledger"

        messages = _image_messages(_png())
        expected = __import__("copy").deepcopy(messages)
        _run_guard(case, factory, messages=messages)
        sent = adapter.call.call_args.kwargs["messages"]
        assert sent == expected and sent[0]["content"][0]["source"]["type"] == "base64", \
            "a valid image must reach the Anthropic adapter unchanged, as its base64 block"
        row = LLMRequest.objects.get(operation=case["marker"])
        assert row.status == "succeeded", \
            f"a valid Anthropic image call must record a succeeded ledger, got {row.status}"
