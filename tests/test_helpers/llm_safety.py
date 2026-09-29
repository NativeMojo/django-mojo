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
