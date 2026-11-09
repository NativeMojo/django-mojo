from testit import helpers as th


def _limits(**overrides):
    value = {
        "requests_minute": 2, "requests_hour": 10, "requests_day": 20,
        "tokens_minute": 1000, "tokens_hour": 5000, "tokens_day": 10000,
        "concurrency": 1, "max_input_bytes": 4096, "max_output_tokens": 128,
        "timeout_seconds": 10, "max_loop_calls": 2,
    }
    value.update(overrides)
    return value


def _policy():
    return {
        "version": 1,
        "routes": {
            "unattributed": {
                "provider": "anthropic", "model": "claude-test",
                "credential": "handler", "capabilities": ["text"],
            },
        },
        "shared": _limits(),
        "features": {"unattributed": _limits()},
        "breaker": {
            "auth_failures": 2, "rate_failures": 2,
            "server_failures": 2, "open_seconds": 60,
        },
    }


def _bedrock_policy():
    policy = _policy()
    policy["routes"]["unattributed"] = {
        "provider": "bedrock", "model": "us.amazon.nova-2-lite-v1:0",
        "credential": "aws", "capabilities": ["text"],
        "region": "us-east-1", "reasoning_efforts": ["off", "low", "medium"],
    }
    return policy


@th.django_unit_test()
def test_bedrock_policy_binds_profile_region_and_efforts(opts):
    from mojo.apps.account.services import llm_safety

    original = llm_safety.parse_policy(_bedrock_policy())
    for prefix in ("us", "eu", "jp", "global"):
        raw = _bedrock_policy()
        raw["routes"]["unattributed"]["model"] = f"{prefix}.amazon.nova-2-lite-v1:0"
        parsed = llm_safety.parse_policy(raw)
        assert parsed["routes"] == raw["routes"], \
            f"the explicit Nova profile must survive parsing: {raw['routes']}"
    for field, value in (("region", "us-west-2"),
                         ("reasoning_efforts", ["off", "low"])):
        raw = _bedrock_policy()
        raw["routes"]["unattributed"][field] = value
        assert llm_safety.parse_policy(raw)["hash"] != original["hash"], \
            f"changing Bedrock {field} must require fresh policy agreement"


@th.django_unit_test()
def test_bedrock_policy_rejects_unapproved_routes(opts):
    from mojo.apps.account.services import llm_safety

    mutations = [
        ("credential", "admin"), ("credential", "handler"),
        ("model", "amazon.nova-2-lite-v1:0"),
        ("model", "us.amazon.nova-lite-v1:0"),
        ("model", "arn:aws:bedrock:us-east-1:123456789012:inference-profile/example"),
        ("capabilities", ["text", "prompt_cache"]), ("capabilities", ["tools"]),
        ("capabilities", ["text", "text"]), ("capabilities", [["text"]]),
        ("region", ""), ("region", " us-east-1"), ("region", "US-EAST-1"),
        ("region", "https://bedrock.example"), ("region", "us-east-1\n"),
        ("reasoning_efforts", []), ("reasoning_efforts", ["off", "off"]),
        ("reasoning_efforts", ["high"]), ("reasoning_efforts", [True]),
        ("reasoning_efforts", "low"),
    ]
    for field, value in mutations:
        raw = _bedrock_policy()
        raw["routes"]["unattributed"][field] = value
        try:
            llm_safety.parse_policy(raw)
            assert False, f"unapproved Bedrock {field}={value!r} was accepted"
        except llm_safety.LLMSafetyError as err:
            assert err.code == "policy_invalid", \
                f"invalid Bedrock {field} must use policy_invalid, got {err.code}"


@th.django_unit_test()
def test_bedrock_route_may_admit_images_explicitly(opts):
    from mojo.apps.account.services import llm_safety

    text_only = llm_safety.parse_policy(_bedrock_policy())
    hashes = set()
    admitted = (["text", "images"], ["images", "text"], ["text", "tools", "images"],
                ["images", "tools", "text"])
    for capabilities in admitted:
        raw = _bedrock_policy()
        raw["routes"]["unattributed"]["capabilities"] = capabilities
        parsed = llm_safety.parse_policy(raw)
        assert parsed["routes"]["unattributed"]["capabilities"] == capabilities, \
            f"an explicit Bedrock image route must parse unchanged, got {parsed['routes']}"
        assert parsed["hash"] != text_only["hash"], \
            "admitting images must change the policy hash so the operator re-agrees"
        hashes.add(parsed["hash"])
    assert len(hashes) == len(admitted), "capability order is part of the exact policy text"
    assert llm_safety.parse_policy(_bedrock_policy())["hash"] == text_only["hash"], \
        "an existing text-only Bedrock policy must hash exactly as before"
    for capabilities in (["images"], ["tools", "images"], ["text", "images", "images"],
                         ["text", "images", "prompt_cache"], ["text", "images", "stream"]):
        raw = _bedrock_policy()
        raw["routes"]["unattributed"]["capabilities"] = capabilities
        try:
            llm_safety.parse_policy(raw)
            assert False, f"Bedrock accepted capabilities {capabilities}"
        except llm_safety.LLMSafetyError as err:
            assert err.code == "policy_invalid", \
                f"Bedrock capabilities {capabilities} must be policy_invalid, got {err.code}"
    assert "image_invalid" in llm_safety.SAFE_CODES, \
        "image_invalid must cross the guard as a stable safe code"


@th.django_unit_test()
def test_provider_route_shapes_remain_exact(opts):
    from mojo.apps.account.services import llm_safety

    for provider, extra in (("anthropic", "region"), ("bedrock", "endpoint_url")):
        raw = _policy() if provider == "anthropic" else _bedrock_policy()
        raw["routes"]["unattributed"][extra] = "us-east-1"
        try:
            llm_safety.parse_policy(raw)
            assert False, f"{provider} accepted the unknown route key {extra}"
        except llm_safety.LLMSafetyError as err:
            assert err.code == "policy_invalid", \
                f"unknown {provider} route keys must be policy_invalid, got {err.code}"
    for required in ("region", "reasoning_efforts"):
        raw = _bedrock_policy()
        del raw["routes"]["unattributed"][required]
        try:
            llm_safety.parse_policy(raw)
            assert False, f"Bedrock accepted missing route key {required}"
        except llm_safety.LLMSafetyError as err:
            assert err.code == "policy_invalid", \
                f"missing Bedrock {required} must be policy_invalid, got {err.code}"


@th.django_unit_test()
def test_policy_is_exact_and_provider_explicit(opts):
    from mojo.apps.account.services import llm_safety

    policy = llm_safety.parse_policy(_policy())
    assert policy["routes"]["unattributed"]["provider"] == "anthropic", \
        f"the route must retain its explicit provider, got {policy['routes']}"
    bad = _policy()
    bad["unexpected"] = True
    try:
        llm_safety.parse_policy(bad)
        assert False, "an unknown policy key must be refused"
    except llm_safety.LLMSafetyError as err:
        assert err.code == "policy_invalid", \
            f"unknown policy keys must use policy_invalid, got {err.code}"


@th.django_unit_test()
def test_missing_policy_denies_before_route_or_provider(opts):
    from mojo.apps.account.services import llm_safety

    try:
        llm_safety.parse_policy(None)
        assert False, "missing deployment policy must deny external LLM work"
    except llm_safety.LLMSafetyError as err:
        assert err.code == "policy_invalid", \
            f"missing policy must fail with policy_invalid, got {err.code}"


@th.django_unit_test()
def test_policy_rejects_window_and_feature_mistakes(opts):
    from mojo.apps.account.services import llm_safety

    bad_window = _policy()
    bad_window["features"]["unattributed"]["requests_minute"] = 11
    try:
        llm_safety.parse_policy(bad_window)
        assert False, "minute requests above the hour ceiling must be refused"
    except llm_safety.LLMSafetyError as err:
        assert err.code == "policy_invalid", \
            f"invalid window relationships must use policy_invalid, got {err.code}"

    unknown = _policy()
    unknown["routes"]["mystery"] = unknown["routes"].pop("unattributed")
    unknown["features"]["mystery"] = unknown["features"].pop("unattributed")
    try:
        llm_safety.parse_policy(unknown)
        assert False, "unknown features must be refused"
    except llm_safety.LLMSafetyError as err:
        assert err.code == "policy_invalid", \
            f"unknown features must use policy_invalid, got {err.code}"


@th.django_unit_test()
def test_unknown_explicit_feature_never_calls_adapter(opts):
    from mojo.helpers import llm

    try:
        llm.call(
            [{"role": "user", "content": "hello"}], model="claude-test",
            feature="mystery")
        assert False, "unknown explicit feature must be refused"
    except ValueError as err:
        assert str(err) == "Unknown LLM feature", \
            f"unknown feature error must be stable, got {err}"


@th.django_unit_test()
def test_fingerprint_is_provider_scoped_and_secret_free(opts):
    from mojo.apps.account.services import llm_safety

    first = llm_safety.credential_fingerprint("anthropic", "secret-value")
    second = llm_safety.credential_fingerprint("future-provider", "secret-value")
    assert first != second, "the same credential under different providers needs different state"
    assert "secret-value" not in first, "the fingerprint must not contain credential material"
    assert len(first) == 64, f"the fingerprint must be sha256 hex, got {len(first)} chars"


@th.django_unit_test()
def test_non_owner_and_late_release_cannot_free_new_lease(opts):
    from mojo.apps.account.services import llm_safety
    from mojo.helpers.redis import get_connection

    redis = get_connection()
    marker = __import__("uuid").uuid4().hex
    shared = _limits(concurrency=2)
    feature = _limits(concurrency=2)
    first = llm_safety.acquire_permit(
        redis, "anthropic", marker, "unattributed", shared, feature, 20,
        owner="first-owner", now=120)
    keys = first["keys"]
    try:
        forged = dict(first, owner="not-the-owner")
        assert llm_safety.release_permit(redis, forged, actual_tokens=1) is False, \
            "a non-owner release must be refused"
        assert redis.zcard(keys[0]) == 1, \
            "a non-owner release must not free the shared concurrency lease"

        assert llm_safety.release_permit(redis, first, actual_tokens=5) is True, \
            "the real owner must be able to release its own lease"
        second = llm_safety.acquire_permit(
            redis, "anthropic", marker, "unattributed", shared, feature, 30,
            owner="second-owner", now=181)
        new_token_key = [key for key in second["keys"] if ":tokens:minute:" in key][0]
        before = int(redis.get(new_token_key))
        assert llm_safety.release_permit(redis, first, actual_tokens=0) is False, \
            "a late release from the old owner must be refused"
        assert redis.zcard(second["keys"][0]) == 1, \
            "the old owner must not free the newer concurrency lease"
        assert int(redis.get(new_token_key)) == before, \
            "old-epoch reconciliation must not decrement the newer token epoch"
        llm_safety.release_permit(redis, second, actual_tokens=30)
    finally:
        redis.delete(*(set(keys) | set(locals().get("second", {}).get("keys", []))))


@th.django_unit_test()
def test_public_call_has_no_adapter_bypass_parameters(opts):
    import inspect
    from mojo.helpers import llm
    from mojo.apps.account.services import llm_safety

    call_parameters = inspect.signature(llm.call).parameters
    invoke_parameters = inspect.signature(llm_safety.invoke).parameters
    assert "client" not in call_parameters, \
        f"public call must not accept a provider client, got {tuple(call_parameters)}"
    assert "candidate" not in invoke_parameters and "allow_stopped" not in invoke_parameters, \
        f"production invoke must expose no stopped-state bypass, got {tuple(invoke_parameters)}"
    for function in (llm.call, llm.ask, llm.stream, llm_safety.invoke,
                     llm_safety.invoke_stream):
        effort = inspect.signature(function).parameters["reasoning_effort"]
        assert effort.default == "off" and effort.kind == inspect.Parameter.KEYWORD_ONLY, \
            f"{function.__name__} reasoning effort must be keyword-only with default off"
    for function in (llm.call, llm.stream, llm_safety.invoke, llm_safety.invoke_stream):
        parameters = inspect.signature(function).parameters
        rounds = parameters["max_tool_rounds"]
        assert rounds.default is None and rounds.kind == inspect.Parameter.KEYWORD_ONLY, \
            f"{function.__name__} max_tool_rounds must be keyword-only with default None"
        assert "client" not in parameters and "provider_factory" not in parameters, \
            f"{function.__name__} must not accept a provider client or factory"


@th.django_unit_test()
def test_safety_records_have_no_generic_row_rest_surface(opts):
    from mojo.apps.account.models import LLMCircuitBreaker, LLMRequest
    from mojo.apps.incident.models import IncidentLLMAttempt

    for model in (LLMRequest, LLMCircuitBreaker, IncidentLLMAttempt):
        assert not hasattr(model, "RestMeta"), \
            f"{model.__name__} must be visible only through aggregate services"


@th.django_unit_test()
def test_candidate_permits_are_single_flight_across_fingerprints(opts):
    from mojo.apps.account.services import llm_safety
    from mojo.helpers.redis import get_connection

    policy = _policy()
    configuration = dict(policy["routes"]["unattributed"])
    policy["routes"] = {"configuration": configuration}
    policy["features"] = {"configuration": _limits(concurrency=7)}
    policy["shared"] = _limits(concurrency=9)
    shared, limits = llm_safety._candidate_envelopes(policy)
    assert shared["concurrency"] == limits["concurrency"] == 1, \
        f"candidate envelopes must hard-cap concurrency at one, got {shared} / {limits}"

    redis = get_connection()
    identity = f"candidate-installation-{__import__('uuid').uuid4().hex}"
    first = llm_safety.acquire_permit(
        redis, "anthropic", identity, "configuration", shared, limits, 4,
        owner="candidate-one")
    try:
        try:
            llm_safety.acquire_permit(
                redis, "anthropic", identity, "configuration", shared, limits, 4,
                owner="candidate-two")
            assert False, "a second candidate probe permit must be denied while one is active"
        except llm_safety.LLMSafetyError as err:
            assert err.code == "concurrency_exhausted", \
                f"second candidate denial used the wrong safe code: {err.code}"
    finally:
        llm_safety.release_permit(redis, first, actual_tokens=4)


# The Anthropic constructor-patch test lives in
# tests/test_helpers_extended_serial/llm_safety.py. The SDK constructor is a
# process-global symbol shared by every LLM test module.


@th.django_unit_test()
def test_unlisted_host_feature_is_refused(opts):
    from mojo.apps.account.services import llm_safety
    from mojo.helpers import llm

    try:
        llm.call(
            [{"role": "user", "content": "hello"}], model="claude-test",
            feature="support_test")
        assert False, "a host feature missing from LLM_HOST_FEATURES must be refused"
    except ValueError as err:
        assert str(err) == "Unknown LLM feature", \
            f"an unlisted host feature must get the stable error, got {err}"
    policy = _policy()
    policy["routes"] = {"support_test": policy["routes"]["unattributed"]}
    policy["features"] = {"support_test": _limits()}
    try:
        llm_safety.parse_policy(policy)
        assert False, "a route for an unlisted host feature must be refused"
    except llm_safety.LLMSafetyError as err:
        assert err.code == "policy_invalid", \
            f"an unlisted host route must use policy_invalid, got {err.code}"


@th.django_unit_test()
def test_host_feature_names_are_validated(opts):
    from django.core.exceptions import ImproperlyConfigured
    from mojo.helpers import llm

    assert llm.host_features(["support_test", "support_eval"]) == \
        frozenset({"support_test", "support_eval"}), \
        "valid host slugs must come back as a frozenset"
    assert llm.host_features([]) == frozenset(), "an empty list must add no names"
    assert llm.host_features(("a" * 32,)) == frozenset({"a" * 32}), \
        "a 32-character slug fits LLMRequest.feature and must be accepted"
    bad_values = {
        "a core name": ["assistant"],
        "reserved shared": ["shared"],
        "reserved breaker": ["breaker"],
        "reserved unknown": ["unknown"],
        "upper case": ["Support"],
        "a hyphen": ["support-test"],
        "a leading digit": ["1support"],
        "an empty name": [""],
        "33 characters": ["a" * 33],
        "a trailing newline": ["support_test\n"],
        "32 characters and a trailing newline": ["a" * 32 + "\n"],
        "a duplicate": ["support_test", "support_test"],
        "a non-string entry": [7],
        "a bare string": "support_test",
        "a dict": {"support_test": True},
        "None": None,
    }
    for label, value in bad_values.items():
        try:
            llm.host_features(value)
            assert False, f"LLM_HOST_FEATURES with {label} must be refused"
        except ImproperlyConfigured as err:
            assert "LLM_HOST_FEATURES" in str(err), \
                f"the error for {label} must name the setting, got {err}"


@th.django_unit_test()
def test_auto_sonnet_delegation_is_exactly_anthropic_assistant(opts):
    from mojo.apps.account.services import llm_safety
    for feature in ('assistant', 'configuration', 'model_discovery', 'unattributed'):
        raw = _policy()
        route = raw['routes'].pop('unattributed')
        route['model'] = 'auto:sonnet'
        raw['routes'][feature] = route
        raw['features'] = {feature: _limits()}
        if feature == 'assistant':
            assert llm_safety.parse_policy(raw)['routes'][feature]['model'] == 'auto:sonnet', \
                'explicit Anthropic Assistant delegation must parse without widening route keys'
        else:
            try:
                llm_safety.parse_policy(raw)
                assert False, f'{feature} must reject automatic Assistant delegation'
            except llm_safety.LLMSafetyError as error:
                assert error.code == 'policy_invalid', f'invalid delegation must fail safely: {error.code}'
    for value in ('auto:opus', 'auto:', 'auto:sonnet-new'):
        raw = _policy()
        raw['routes']['unattributed']['model'] = value
        try:
            llm_safety.parse_policy(raw)
            assert False, f'unsupported delegation {value} must fail closed'
        except llm_safety.LLMSafetyError as error:
            assert error.code == 'policy_invalid', f'bad token used wrong safe code: {error.code}'


@th.django_unit_test()
def test_bedrock_routes_may_declare_tools(opts):
    from mojo.apps.account.services import llm_safety

    text_only = llm_safety.parse_policy(_bedrock_policy())
    for capabilities in (["text", "tools"], ["tools", "text"]):
        raw = _bedrock_policy()
        raw["routes"]["unattributed"]["capabilities"] = capabilities
        parsed = llm_safety.parse_policy(raw)
        assert parsed["routes"]["unattributed"]["capabilities"] == capabilities, \
            f"a Bedrock tool route must parse unchanged: {parsed['routes']}"
        assert parsed["hash"] != text_only["hash"], \
            "adding tools to a route must require fresh policy activation"
    for capabilities in (["tools"], ["text", "tools", "tools"],
                         ["text", "tools", "prompt_cache"], ["text", "stream"]):
        raw = _bedrock_policy()
        raw["routes"]["unattributed"]["capabilities"] = capabilities
        try:
            llm_safety.parse_policy(raw)
            assert False, f"Bedrock capabilities {capabilities} must be refused"
        except llm_safety.LLMSafetyError as err:
            assert err.code == "policy_invalid", \
                f"invalid Bedrock capabilities must be policy_invalid, got {err.code}"


@th.django_unit_test()
def test_tool_round_arguments_are_validated_before_the_guard(opts):
    from mojo.helpers import llm

    for value in (0, 33, True, 2.0, "4"):
        for function in (llm.call, llm.stream):
            try:
                function([{"role": "user", "content": "hello"}], feature="unattributed",
                         max_tool_rounds=value)
                assert False, f"{function.__name__} accepted max_tool_rounds={value!r}"
            except ValueError as err:
                assert "max_tool_rounds" in str(err), \
                    f"the error for max_tool_rounds={value!r} must name it, got {err}"


@th.django_unit_test()
def test_tool_rounds_count_answered_tool_turns_only(opts):
    from mojo.apps.account.services import llm_safety

    answer = {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}
    ask = {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": "lookup", "input": {}}]}
    transcript = [{"role": "user", "content": "hi"}, ask, answer, ask, answer,
                  {"role": "assistant", "content": "plain"}]
    assert llm_safety._tool_rounds(transcript) == 2, \
        "each user turn answering a tool request is one completed round"
    assert llm_safety._tool_rounds("not a list") == 0, "malformed input counts no rounds"
    assert llm_safety._tool_round_limit("bedrock", None) == llm_safety.BEDROCK_MAX_TOOL_ROUNDS \
        and llm_safety._tool_round_limit("anthropic", None) is None \
        and llm_safety._tool_round_limit("anthropic", 3) == 3, \
        "Bedrock always has a round limit; Anthropic only when the caller sets one"
    for value in (0, 33, True):
        try:
            llm_safety._tool_round_limit("bedrock", value)
            assert False, f"an invalid round limit {value!r} must be refused"
        except llm_safety.LLMSafetyError as err:
            assert err.code == "loop_limit", f"invalid round limit used {err.code}"
