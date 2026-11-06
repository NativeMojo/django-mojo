import json

from testit import helpers as th


def _limits(**overrides):
    value = {
        "requests_minute": 5, "requests_hour": 10, "requests_day": 20,
        "tokens_minute": 1000, "tokens_hour": 5000, "tokens_day": 10000,
        "concurrency": 2, "max_input_bytes": 4096, "max_output_tokens": 128,
        "timeout_seconds": 10, "max_loop_calls": 2,
    }
    value.update(overrides)
    return value


class _CountingAdapter:
    """Local stand-in for a provider adapter: returns or raises a fixed count."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def count_input_tokens(self, messages, model, system=None, tools=None,
                           timeout=None):
        self.calls.append({"model": model, "system": system, "tools": tools,
                           "timeout": timeout})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _serialized(size):
    payload = json.dumps({"messages": [{"role": "user", "content": "x"}]}).encode()
    return payload + b" " * (size - len(payload))


def _estimate(adapter, provider="anthropic", messages=None, size=4000):
    from mojo.apps.account.services import llm_safety

    messages = messages or [{"role": "user", "content": "hello"}]
    return llm_safety._input_estimate(
        adapter, provider, messages, "system text", [{"name": "tool"}],
        "claude-test", _serialized(size))


@th.django_unit_test()
def test_count_used_with_margin(opts):
    from mojo.apps.account.services import llm_safety

    adapter = _CountingAdapter(1000)
    reserved, estimated, source = _estimate(adapter, size=4000)
    assert (reserved, estimated, source) == (1164, 1000, "count"), \
        f"count 1000 must reserve ceil(1000 x 1.10) + 64 = 1164, got {(reserved, estimated, source)}"
    assert adapter.calls and adapter.calls[0]["model"] == "claude-test", \
        "the count must be asked for the resolved model"
    assert adapter.calls[0]["timeout"] == llm_safety.COUNT_TIMEOUT_SECONDS, \
        "the count must run under its own short timeout"
    assert adapter.calls[0]["system"] == "system text" \
        and adapter.calls[0]["tools"] == [{"name": "tool"}], \
        "the count must cover the exact system prompt and tools of the request"


@th.django_unit_test()
def test_count_failure_falls_back_to_bytes(opts):
    from mojo.helpers.llm_providers.base import ProviderError

    for result in (ProviderError("provider_timeout"), RuntimeError("boom"), 0, -5,
                   "x", True, None, 12.5):
        reserved, estimated, source = _estimate(_CountingAdapter(result), size=4000)
        assert (reserved, estimated, source) == (4000, 0, "bytes"), \
            f"count result {result!r} must reserve exactly the byte length, got {(reserved, estimated, source)}"


@th.django_unit_test()
def test_count_never_raises_reservation(opts):
    reserved, estimated, source = _estimate(_CountingAdapter(5000), size=4000)
    assert reserved == 4000 and estimated == 5000 and source == "count", \
        f"a count above the byte length must reserve no more than the bytes, got {(reserved, estimated, source)}"


@th.django_unit_test()
def test_image_and_bedrock_skip_count(opts):
    adapter = _CountingAdapter(10)
    image = [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "data": "AA=="}}]}]
    assert _estimate(adapter, messages=image) == (4000, 0, "bytes"), \
        "an image request must keep the byte reservation"
    assert _estimate(adapter, provider="bedrock") == (4000, 0, "bytes"), \
        "a Bedrock request must keep the byte reservation"
    assert adapter.calls == [], "image and Bedrock requests must never call the count"


@th.django_unit_test()
def test_overage_is_charged(opts):
    from mojo.apps.account.services import llm_safety
    from mojo.helpers.redis import get_connection

    redis = get_connection()
    marker = __import__("uuid").uuid4().hex
    shared, feature = _limits(tokens_minute=2000), _limits(tokens_minute=2000)
    keys = llm_safety._permit_keys("anthropic", marker, "unattributed", 120)
    token_keys = [key for key in keys if ":tokens:" in key]
    try:
        permit = llm_safety.acquire_permit(
            redis, "anthropic", marker, "unattributed", shared, feature, 1000,
            owner="first", now=120)
        assert llm_safety.release_permit(redis, permit, actual_tokens=1300) is True, \
            "the owner's release must succeed"
        for key in token_keys:
            assert int(redis.get(key)) == 1300, \
                f"usage above the reservation must be charged to {key}, got {redis.get(key)}"
        try:
            llm_safety.acquire_permit(
                redis, "anthropic", marker, "unattributed", shared, feature, 1000,
                owner="second", now=121)
            assert False, "a call that fit before the overage must now be refused"
        except llm_safety.LLMSafetyError as err:
            assert err.code == "budget_exhausted", \
                f"the charged overage must exhaust the minute budget, got {err.code}"
        permit = llm_safety.acquire_permit(
            redis, "anthropic", marker, "unattributed", shared, feature, 700,
            owner="third", now=122)
        assert llm_safety.release_permit(redis, permit, actual_tokens=200) is True, \
            "a release below the reservation must still succeed"
        for key in token_keys:
            assert int(redis.get(key)) == 1500, \
                f"the refund path must be unchanged for {key}, got {redis.get(key)}"
    finally:
        redis.delete(*keys)


@th.django_unit_test()
def test_overage_skips_expired_key(opts):
    from mojo.apps.account.services import llm_safety
    from mojo.helpers.redis import get_connection

    redis = get_connection()
    marker = __import__("uuid").uuid4().hex
    keys = llm_safety._permit_keys("anthropic", marker, "unattributed", 120)
    minute_keys = [key for key in keys if ":tokens:minute:" in key]
    try:
        permit = llm_safety.acquire_permit(
            redis, "anthropic", marker, "unattributed", _limits(), _limits(), 500,
            owner="late", now=120)
        redis.delete(*minute_keys)
        assert llm_safety.release_permit(redis, permit, actual_tokens=800) is True, \
            "the owner's release must succeed"
        for key in minute_keys:
            assert not redis.exists(key), \
                f"an expired window must not be recreated (without a TTL) by overage: {key}"
        for key in keys:
            if ":tokens:hour:" in key or ":tokens:day:" in key:
                assert int(redis.get(key)) == 800 and redis.ttl(key) > 0, \
                    f"live windows must take the overage and keep their TTL: {key}"
    finally:
        redis.delete(*keys)


@th.django_unit_test()
def test_budget_retry_after_first_window(opts):
    from mojo.apps.account.services import llm_safety
    from mojo.helpers.redis import get_connection

    redis = get_connection()
    marker = __import__("uuid").uuid4().hex
    used = set()
    try:
        minute = _limits()
        first = llm_safety.acquire_permit(
            redis, "anthropic", marker, "unattributed", minute, minute, 1000,
            owner="minute-1", now=100)
        used.update(first["keys"])
        used.update(llm_safety._permit_keys("anthropic", marker, "unattributed", 110))
        try:
            llm_safety.acquire_permit(
                redis, "anthropic", marker, "unattributed", minute, minute, 1,
                owner="minute-2", now=110)
            assert False, "a full minute window must refuse the next call"
        except llm_safety.LLMSafetyError as err:
            assert err.code == "budget_exhausted" and err.retry_after == 10, \
                f"minute full at second 50 must say retry in 10, got {err.code}/{err.retry_after}"

        other = __import__("uuid").uuid4().hex
        hour = _limits(tokens_hour=1000)
        first = llm_safety.acquire_permit(
            redis, "anthropic", other, "unattributed", hour, hour, 600,
            owner="hour-1", now=3440)
        used.update(first["keys"])
        used.update(llm_safety._permit_keys("anthropic", other, "unattributed", 3500))
        try:
            llm_safety.acquire_permit(
                redis, "anthropic", other, "unattributed", hour, hour, 600,
                owner="hour-2", now=3500)
            assert False, "a full hour window must refuse the next call"
        except llm_safety.LLMSafetyError as err:
            assert err.code == "budget_exhausted" and err.retry_after == 100, \
                f"hour full at second 3500 must say retry in 100, got {err.code}/{err.retry_after}"
    finally:
        redis.delete(*used)
