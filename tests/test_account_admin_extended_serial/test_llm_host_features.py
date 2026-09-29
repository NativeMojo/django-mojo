"""LLM_HOST_FEATURES tests that override django.conf.settings in-process.

These mutate process-global settings and the protected policy-hash Setting
row, so they live in this serial package, not the default test_helpers one.
"""

import uuid
from contextlib import contextmanager
from unittest import mock

from testit import helpers as th


@contextmanager
def _override_setting(name, value):
    """In-process Django settings override (th.server_settings only affects the
    separate server process; override_settings is banned by testing rules)."""
    import django.conf
    sentinel = object()
    original = getattr(django.conf.settings, name, sentinel)
    setattr(django.conf.settings, name, value)
    try:
        yield
    finally:
        if original is sentinel:
            delattr(django.conf.settings, name)
        else:
            setattr(django.conf.settings, name, original)


@contextmanager
def _expected_policy_hash(value):
    """Write the activated hash the way activate_policy does, then restore it."""
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import llm_safety
    key = llm_safety.EXPECTED_POLICY_HASH_KEY
    originals = [(row.pk, row.value) for row in
                 Setting.objects.filter(key=key, group=None).order_by("pk")]
    Setting.objects.filter(key=key, group=None).delete()
    row = Setting(key=key, group=None, is_secret=False)
    row.set_value(value)
    row.save(_protected_writer=key, _skip_cache=True)
    try:
        yield
    finally:
        Setting.objects.filter(key=key, group=None).delete()
        for pk, raw in originals:
            restored = Setting(pk=pk, key=key, group=None, is_secret=False, value=raw)
            restored.save(_protected_writer=key, _skip_cache=True)


def _limits(**overrides):
    value = {
        "requests_minute": 50, "requests_hour": 50, "requests_day": 50,
        "tokens_minute": 100000, "tokens_hour": 100000, "tokens_day": 100000,
        "concurrency": 2, "max_input_bytes": 4096, "max_output_tokens": 128,
        "timeout_seconds": 10, "max_loop_calls": 4,
    }
    value.update(overrides)
    return value


def _policy():
    return {
        "version": 1,
        "routes": {
            "support_test": {
                "provider": "anthropic", "model": "claude-test",
                "credential": "handler",
                "capabilities": ["text", "prompt_cache"],
            },
        },
        "shared": _limits(),
        "features": {"support_test": _limits(
            requests_minute=2, requests_hour=2, requests_day=2)},
        "breaker": {
            "auth_failures": 2, "rate_failures": 2,
            "server_failures": 2, "open_seconds": 60,
        },
    }


class _FakeAdapter:
    def supports(self, capability):
        return True

    def call(self, **kwargs):
        return {"id": "fake-host-feature", "content": [],
                "usage": {"input_tokens": 3, "output_tokens": 1}}


@th.django_unit_test()
def test_listed_host_feature_is_metered_and_limited(opts):
    from mojo.apps.account.models import LLMCircuitBreaker, LLMRequest
    from mojo.apps.account.services import llm_safety
    from mojo.helpers.redis import get_connection

    raw = _policy()
    credential = f"host-feature-test-{uuid.uuid4().hex}"
    fingerprint = llm_safety.credential_fingerprint("anthropic", credential)
    redis = get_connection()
    root = f"mojo:llm:anthropic:{fingerprint}"
    messages = [{"role": "user", "content": "hello"}]
    with _override_setting("LLM_HOST_FEATURES", ["support_test"]), \
            _override_setting("LLM_EMERGENCY_STOP", False), \
            mock.patch.object(llm_safety, "_credential", return_value=credential):
        try:
            policy = llm_safety.parse_policy(raw)
            assert set(policy["routes"]) == {"support_test"}, \
                f"a listed host feature's route must be accepted, got {policy['routes']}"
            with _expected_policy_hash(policy["hash"]):
                for attempt in range(2):
                    response = llm_safety.execute_guarded_for_test(
                        messages, feature="support_test",
                        provider_factory=lambda name, api_key: _FakeAdapter(),
                        redis=redis, policy_raw=raw)
                    assert response.get("usage"), \
                        f"call {attempt + 1} of 2 must succeed, got {response}"
                rows = LLMRequest.objects.filter(credential_fingerprint=fingerprint)
                assert rows.count() == 2 and \
                    set(rows.values_list("feature", flat=True)) == {"support_test"}, \
                    f"each call must write a support_test ledger row, got {list(rows.values('feature', 'status'))}"
                shared_requests = sum(
                    int(redis.get(key) or 0)
                    for key in redis.keys(f"{root}:shared:requests:day:*"))
                assert shared_requests == 2, \
                    f"host calls must also charge the shared envelope, got {shared_requests}"
                try:
                    llm_safety.execute_guarded_for_test(
                        messages, feature="support_test",
                        provider_factory=lambda name, api_key: _FakeAdapter(),
                        redis=redis, policy_raw=raw)
                    assert False, "a third call must exceed the host feature's limit of 2"
                except llm_safety.LLMSafetyError as err:
                    assert err.code == "budget_exhausted", \
                        f"the host feature's own limit must refuse with budget_exhausted, got {err.code}"
        finally:
            LLMRequest.objects.filter(credential_fingerprint=fingerprint).delete()
            LLMCircuitBreaker.objects.filter(credential_fingerprint=fingerprint).delete()
            keys = redis.keys(f"{root}:*")
            if keys:
                redis.delete(*keys)


@th.django_unit_test()
def test_bad_host_feature_setting_stops_startup(opts):
    from django.apps import apps
    from django.core.exceptions import ImproperlyConfigured

    with _override_setting("LLM_HOST_FEATURES", ["assistant"]):
        try:
            apps.get_app_config("account").ready()
            assert False, "a framework name in LLM_HOST_FEATURES must stop startup"
        except ImproperlyConfigured as err:
            assert "assistant" in str(err), \
                f"the startup error must name the offending entry, got {err}"
    with _override_setting("LLM_HOST_FEATURES", ["support_test"]):
        apps.get_app_config("account").ready()
