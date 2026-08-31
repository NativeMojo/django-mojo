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

    first = llm_safety.credential_fingerprint("anthropic", "candidate-one")
    second = llm_safety.credential_fingerprint("anthropic", "candidate-two")
    assert first != second, "the regression needs two distinct candidate fingerprints"
    assert llm_safety._permit_identity(first, candidate_probe=True) == \
        llm_safety._permit_identity(second, candidate_probe=True), \
        "candidate verification must share one installation-wide permit identity"
    assert llm_safety._permit_identity(first) != llm_safety._permit_identity(second), \
        "ordinary stored credentials must retain independent accounting identities"
