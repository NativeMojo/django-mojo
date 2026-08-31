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

    class Messages:
        calls = 0

        def create(self, **kwargs):
            self.calls += 1
            raise AssertionError("unknown features must fail before provider I/O")

    class Client:
        messages = Messages()

    try:
        llm.call(
            [{"role": "user", "content": "hello"}], model="claude-test",
            client=Client(), feature="mystery")
        assert False, "unknown explicit feature must be refused"
    except ValueError as err:
        assert str(err) == "Unknown LLM feature", \
            f"unknown feature error must be stable, got {err}"
    assert Client.messages.calls == 0, \
        f"provider call count must stay zero, got {Client.messages.calls}"


@th.django_unit_test()
def test_fingerprint_is_provider_scoped_and_secret_free(opts):
    from mojo.apps.account.services import llm_safety

    first = llm_safety.credential_fingerprint("anthropic", "secret-value")
    second = llm_safety.credential_fingerprint("future-provider", "secret-value")
    assert first != second, "the same credential under different providers needs different state"
    assert "secret-value" not in first, "the fingerprint must not contain credential material"
    assert len(first) == 64, f"the fingerprint must be sha256 hex, got {len(first)} chars"
