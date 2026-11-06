from testit import helpers as th


def _limits(**overrides):
    value = {
        "requests_minute": 2, "requests_hour": 10, "requests_day": 20,
        "tokens_minute": 1000, "tokens_hour": 5000, "tokens_day": 10000,
        "concurrency": 2, "max_input_bytes": 4096, "max_output_tokens": 128,
        "timeout_seconds": 10, "max_loop_calls": 2,
    }
    value.update(overrides)
    return value


@th.django_unit_test()
def test_permit_refuses_request_larger_than_minute_budget(opts):
    from mojo.apps.account.services import llm_safety
    from mojo.helpers.redis import get_connection

    redis = get_connection()
    marker = __import__("uuid").uuid4().hex
    shared = _limits(tokens_minute=4000)
    feature = _limits()
    keys = llm_safety._permit_keys("anthropic", marker, "unattributed", 120)
    permit = None
    try:
        try:
            llm_safety.acquire_permit(
                redis, "anthropic", marker, "unattributed", shared, feature, 1001,
                owner="too-big", now=120)
            assert False, "a reservation above the smallest minute budget must be refused"
        except llm_safety.LLMSafetyError as err:
            assert err.code == "request_exceeds_budget", \
                f"a call that can never fit must be refused permanently, got {err.code}"
            assert err.retry_after is None, \
                "a call that can never fit must not carry a retry hint"
        assert not any(redis.exists(key) for key in keys), \
            "a refused oversized call must write no lease or budget key"

        permit = llm_safety.acquire_permit(
            redis, "anthropic", marker, "unattributed", shared, feature, 1000,
            owner="fits", now=120)
        assert permit["reserved_tokens"] == 1000, \
            "a reservation equal to the minute budget must still be admitted"
    finally:
        if permit:
            llm_safety.release_permit(redis, permit, actual_tokens=1000)
        redis.delete(*keys)
