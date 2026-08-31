"""Fail-closed provider policy, permit, ledger, and circuit boundary."""

import hashlib
import json
import secrets
import time

from django.db import models, transaction
from django.utils import timezone

from mojo.helpers.redis import get_connection
from mojo.helpers.settings import settings


POLICY_KEYS = frozenset({"version", "routes", "shared", "features", "breaker"})
ROUTE_KEYS = frozenset({"provider", "model", "credential", "capabilities"})
LIMIT_KEYS = frozenset({
    "requests_minute", "requests_hour", "requests_day", "tokens_minute",
    "tokens_hour", "tokens_day", "concurrency", "max_input_bytes",
    "max_output_tokens", "timeout_seconds", "max_loop_calls",
})
BREAKER_KEYS = frozenset({
    "auth_failures", "rate_failures", "server_failures", "open_seconds",
})
WINDOWS = (("minute", 60), ("hour", 3600), ("day", 86400))


class LLMSafetyError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _deny(code):
    raise LLMSafetyError(code)


def _record_metrics(provider, feature, status, tokens=0):
    """Bounded dimensions only: never fingerprint, model, request id, or error text."""
    try:
        from mojo.apps import metrics
        metrics.record(
            "llm:requests", account=provider, category=f"{feature}:{status}",
            min_granularity="minutes")
        if tokens:
            metrics.record(
                "llm:tokens", count=max(0, int(tokens)), account=provider,
                category=feature, min_granularity="minutes")
    except Exception:
        pass


def _canonical_hash(value):
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def credential_fingerprint(provider, credential):
    value = f"{provider}\0{credential}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _validate_limits(value, label):
    if not isinstance(value, dict) or set(value) != LIMIT_KEYS:
        _deny("policy_invalid")
    for key in LIMIT_KEYS:
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            _deny("policy_invalid")
    if not (value["requests_minute"] <= value["requests_hour"] <= value["requests_day"]):
        _deny("policy_invalid")
    if not (value["tokens_minute"] <= value["tokens_hour"] <= value["tokens_day"]):
        _deny("policy_invalid")
    return value


def parse_policy(raw=None):
    if raw is None:
        raw = settings.get_static("LLM_SAFETY_POLICY", None)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            _deny("policy_invalid")
    if not isinstance(raw, dict) or set(raw) != POLICY_KEYS or raw.get("version") != 1:
        _deny("policy_invalid")
    routes = raw.get("routes")
    features = raw.get("features")
    breaker = raw.get("breaker")
    if not isinstance(routes, dict) or not isinstance(features, dict):
        _deny("policy_invalid")
    if not isinstance(breaker, dict) or set(breaker) != BREAKER_KEYS:
        _deny("policy_invalid")
    for value in breaker.values():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            _deny("policy_invalid")
    _validate_limits(raw["shared"], "shared")
    from mojo.helpers.llm import FEATURES
    for feature, route in routes.items():
        if feature not in FEATURES or not isinstance(route, dict) or set(route) != ROUTE_KEYS:
            _deny("policy_invalid")
        if route["provider"] not in {"anthropic"}:
            _deny("provider_unsupported")
        if route["credential"] not in {"admin", "handler"}:
            _deny("policy_invalid")
        if not isinstance(route["model"], str) or not route["model"].strip():
            _deny("policy_invalid")
        if not isinstance(route["capabilities"], list) or any(
                not isinstance(item, str) for item in route["capabilities"]):
            _deny("policy_invalid")
    for feature, limits in features.items():
        if feature not in FEATURES:
            _deny("policy_invalid")
        _validate_limits(limits, feature)
    if set(routes) - set(features):
        _deny("policy_invalid")
    policy = dict(raw)
    policy["hash"] = _canonical_hash(raw)
    return policy


def _policy_agreement(policy, redis):
    now = int(time.time())
    key = "mojo:llm:policy_hashes"
    pipe = redis.pipeline(transaction=True)
    pipe.zremrangebyscore(key, 0, now - 120)
    pipe.zadd(key, {policy["hash"]: now})
    pipe.expire(key, 180)
    pipe.zcard(key)
    result = pipe.execute()
    if int(result[-1]) != 1:
        _deny("policy_mixed")


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def emergency_stopped():
    if _as_bool(settings.get_static("LLM_EMERGENCY_STOP", False)):
        return True
    try:
        from mojo.apps.account.models import Setting
        rows = list(Setting.objects.using("default").filter(
            key="LLM_EMERGENCY_STOP", group=None).order_by("pk")[:2])
        if len(rows) > 1:
            _deny("control_state_unknown")
        return bool(rows and _as_bool(rows[0].get_value()))
    except LLMSafetyError:
        raise
    except Exception:
        _deny("control_state_unknown")


def _credential(route, candidate=None):
    if candidate:
        return candidate
    key = "LLM_ADMIN_API_KEY" if route["credential"] == "admin" else "LLM_HANDLER_API_KEY"
    credential = settings.get(key, None)
    if not credential and route["credential"] == "admin":
        credential = settings.get("LLM_HANDLER_API_KEY", None)
    if not credential:
        _deny("credential_missing")
    return credential


ACQUIRE_LUA = r"""
local concurrency = tonumber(redis.call('GET', KEYS[1]) or '0')
if concurrency >= tonumber(ARGV[1]) then return {0, 'concurrency'} end
for i=2,#KEYS do
  local current = tonumber(redis.call('GET', KEYS[i]) or '0')
  local amount = tonumber(ARGV[(i-2)*3+2])
  local limit = tonumber(ARGV[(i-2)*3+3])
  if current + amount > limit then return {0, 'budget'} end
end
redis.call('INCR', KEYS[1]); redis.call('EXPIRE', KEYS[1], tonumber(ARGV[#ARGV]))
for i=2,#KEYS do
  local amount = tonumber(ARGV[(i-2)*3+2])
  local ttl = tonumber(ARGV[(i-2)*3+4])
  redis.call('INCRBY', KEYS[i], amount); redis.call('EXPIRE', KEYS[i], ttl)
end
return {1, 'ok'}
"""


def _permit_keys(provider, fingerprint, feature):
    root = f"mojo:llm:{provider}:{fingerprint}"
    keys = [f"{root}:concurrency"]
    for scope in ("shared", feature):
        for kind in ("requests", "tokens"):
            for window, _ in WINDOWS:
                keys.append(f"{root}:{scope}:{kind}:{window}")
    return keys


def acquire_permit(redis, provider, fingerprint, feature, shared, limits,
                   reserved_tokens):
    keys = _permit_keys(provider, fingerprint, feature)
    args = [str(min(shared["concurrency"], limits["concurrency"]))]
    for source in (shared, limits):
        for kind, amount in (("requests", 1), ("tokens", reserved_tokens)):
            for window, ttl in WINDOWS:
                args.extend((str(amount), str(source[f"{kind}_{window}"]), str(ttl)))
    args.append(str(max(shared["timeout_seconds"], limits["timeout_seconds"]) + 30))
    try:
        result = redis.eval(ACQUIRE_LUA, len(keys), *(keys + args))
    except Exception:
        _deny("permit_unavailable")
    code = result[1].decode("utf-8") if isinstance(result[1], bytes) else result[1]
    if int(result[0]) != 1:
        _deny("budget_exhausted" if code == "budget" else "concurrency_exhausted")
    return {"keys": keys, "reserved_tokens": reserved_tokens}


def release_permit(redis, permit, actual_tokens=0):
    pipe = redis.pipeline(transaction=True)
    pipe.decr(permit["keys"][0])
    excess = max(0, permit["reserved_tokens"] - max(0, actual_tokens))
    if excess:
        for index, key in enumerate(permit["keys"][1:], start=1):
            if ((index - 1) // 3) % 2 == 1:
                pipe.decrby(key, excess)
    try:
        pipe.execute()
    except Exception:
        pass


def _breaker_lease(provider, fingerprint, policy):
    from mojo.apps.account.models import LLMCircuitBreaker
    now = timezone.now()
    owner = secrets.token_hex(16)
    with transaction.atomic():
        row, _ = LLMCircuitBreaker.objects.select_for_update().get_or_create(
            provider=provider, credential_fingerprint=fingerprint)
        if row.state == "open" and row.opened_until and row.opened_until > now:
            _deny("breaker_open")
        if row.state == "open":
            row.state = "half_open"
            row.half_open_owner = owner
            row.half_open_expires_at = now + timezone.timedelta(
                seconds=policy["breaker"]["open_seconds"])
            row.save(update_fields=[
                "state", "half_open_owner", "half_open_expires_at", "modified"])
        elif row.state == "half_open":
            if row.half_open_expires_at and row.half_open_expires_at > now:
                _deny("breaker_half_open")
            row.half_open_owner = owner
            row.half_open_expires_at = now + timezone.timedelta(
                seconds=policy["breaker"]["open_seconds"])
            row.save(update_fields=["half_open_owner", "half_open_expires_at", "modified"])
        else:
            owner = ""
        return row.generation, owner


def _breaker_success(provider, fingerprint, generation, owner):
    from mojo.apps.account.models import LLMCircuitBreaker
    query = LLMCircuitBreaker.objects.filter(
        provider=provider, credential_fingerprint=fingerprint, generation=generation)
    if owner:
        query = query.filter(state="half_open", half_open_owner=owner)
    query.update(
        state="closed", failure_count=0, error_code="", opened_until=None,
        half_open_owner="", half_open_expires_at=None)


def _breaker_failure(provider, fingerprint, generation, owner, code, policy):
    from mojo.apps.account.models import LLMCircuitBreaker
    with transaction.atomic():
        row = LLMCircuitBreaker.objects.select_for_update().filter(
            provider=provider, credential_fingerprint=fingerprint,
            generation=generation).first()
        if row is None or (owner and row.half_open_owner != owner):
            return
        row.failure_count += 1
        immediate = code == "provider_billing_exhausted"
        threshold = policy["breaker"]["server_failures"]
        if code == "provider_authentication":
            threshold = policy["breaker"]["auth_failures"]
        elif code == "provider_rate_limited":
            threshold = policy["breaker"]["rate_failures"]
        if immediate or row.failure_count >= threshold or owner:
            row.state = "open"
            row.generation += 1
            row.opened_until = timezone.now() + timezone.timedelta(
                seconds=policy["breaker"]["open_seconds"])
            row.half_open_owner = ""
            row.half_open_expires_at = None
        row.error_code = code
        row.save()


def _context_fields(context):
    allowed = {"job_id", "incident_id", "conversation_id", "file_id", "loop_call"}
    if not isinstance(context, dict) or set(context) - allowed:
        _deny("context_invalid")
    result = {}
    for key in allowed - {"loop_call"}:
        value = context.get(key)
        if value is not None and not isinstance(value, (str, int)):
            _deny("context_invalid")
        if value is not None:
            result[key] = value
    return result


def invoke(messages, system=None, tools=None, model=None, max_tokens=4096, *,
           feature, operation, context=None, policy_raw=None, candidate=None,
           allow_stopped=False, redis=None):
    from mojo.apps.account.models import LLMRequest
    from mojo.helpers.llm_providers import get_provider
    from mojo.helpers.llm_providers.base import ProviderError

    policy = parse_policy(policy_raw)
    route = policy["routes"].get(feature)
    limits = policy["features"].get(feature)
    if route is None or limits is None:
        _deny("route_missing")
    if emergency_stopped() and not allow_stopped:
        _deny("emergency_stopped")
    context = context or {}
    scalar_context = _context_fields(context)
    loop_call = context.get("loop_call", 1)
    if isinstance(loop_call, bool) or not isinstance(loop_call, int) \
            or loop_call < 1 or loop_call > limits["max_loop_calls"]:
        _deny("loop_limit")
    serialized = json.dumps(
        {"messages": messages, "system": system, "tools": tools},
        separators=(",", ":"), default=str).encode("utf-8")
    if len(serialized) > limits["max_input_bytes"]:
        _deny("input_too_large")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) \
            or max_tokens <= 0 or max_tokens > limits["max_output_tokens"]:
        _deny("output_too_large")
    provider_name = route["provider"]
    credential = _credential(route, candidate=candidate)
    fingerprint = credential_fingerprint(provider_name, credential)
    adapter = get_provider(provider_name, api_key=credential)
    if adapter is None:
        _deny("provider_unsupported")
    for capability in route["capabilities"]:
        if not adapter.supports(capability):
            _deny("capability_unsupported")
    redis = redis or get_connection()
    _policy_agreement(policy, redis)
    generation, breaker_owner = _breaker_lease(provider_name, fingerprint, policy)
    reserved_tokens = len(serialized) + max_tokens
    permit = acquire_permit(
        redis, provider_name, fingerprint, feature, policy["shared"], limits,
        reserved_tokens)
    started = time.monotonic()
    try:
        ledger = LLMRequest.objects.create(
            feature=feature, operation=str(operation)[:64], provider=provider_name,
            model=model or route["model"], credential_fingerprint=fingerprint,
            policy_hash=policy["hash"], reserved_tokens=reserved_tokens,
            **scalar_context)
    except Exception:
        release_permit(redis, permit)
        _deny("ledger_unavailable")
    try:
        response = adapter.call(
            messages=messages, system=system, tools=tools,
            model=model or route["model"], max_tokens=max_tokens,
            cache_enabled=settings.get(
                "LLM_ADMIN_PROMPT_CACHE_ENABLED", True, kind="bool"),
            timeout=min(policy["shared"]["timeout_seconds"], limits["timeout_seconds"]))
    except ProviderError as err:
        elapsed = int((time.monotonic() - started) * 1000)
        _breaker_failure(
            provider_name, fingerprint, generation, breaker_owner, err.code, policy)
        LLMRequest.objects.filter(pk=ledger.pk).update(
            status="failed", error_code=err.code,
            provider_request_id=err.request_id[:128], duration_ms=elapsed,
            finished_at=timezone.now())
        release_permit(redis, permit)
        _record_metrics(provider_name, feature, "failed")
        _deny(err.code)
    usage = response.get("usage") or {}
    actual_tokens = sum(max(0, int(usage.get(key, 0) or 0)) for key in (
        "input_tokens", "output_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens"))
    release_permit(redis, permit, actual_tokens=actual_tokens)
    _breaker_success(provider_name, fingerprint, generation, breaker_owner)
    elapsed = int((time.monotonic() - started) * 1000)
    updated = LLMRequest.objects.filter(pk=ledger.pk, status="started").update(
        status="succeeded", finished_at=timezone.now(), duration_ms=elapsed,
        provider_request_id=str(response.get("id") or "")[:128],
        input_tokens=max(0, int(usage.get("input_tokens", 0) or 0)),
        output_tokens=max(0, int(usage.get("output_tokens", 0) or 0)),
        cache_read_input_tokens=max(0, int(usage.get("cache_read_input_tokens", 0) or 0)),
        cache_creation_input_tokens=max(
            0, int(usage.get("cache_creation_input_tokens", 0) or 0)))
    if updated != 1:
        _deny("ledger_persistence_unknown")
    _record_metrics(provider_name, feature, "succeeded", actual_tokens)
    return response


def verify_candidate(candidate, policy_raw=None):
    response = invoke(
        [{"role": "user", "content": "Reply OK"}], max_tokens=4,
        feature="configuration", operation="candidate_key_probe", context={},
        policy_raw=policy_raw, candidate=candidate, allow_stopped=True)
    return bool(response)


def discover_models(policy_raw=None):
    from mojo.apps.account.models import LLMRequest
    from mojo.helpers.llm_providers import get_provider
    from mojo.helpers.llm_providers.base import ProviderError

    policy = parse_policy(policy_raw)
    feature = "model_discovery"
    route = policy["routes"].get(feature)
    limits = policy["features"].get(feature)
    if route is None or limits is None:
        _deny("route_missing")
    if emergency_stopped():
        _deny("emergency_stopped")
    credential = _credential(route)
    fingerprint = credential_fingerprint(route["provider"], credential)
    adapter = get_provider(route["provider"], api_key=credential)
    if adapter is None or not adapter.supports("models"):
        _deny("capability_unsupported")
    redis = get_connection()
    _policy_agreement(policy, redis)
    generation, owner = _breaker_lease(route["provider"], fingerprint, policy)
    permit = acquire_permit(
        redis, route["provider"], fingerprint, feature, policy["shared"], limits, 1)
    try:
        ledger = LLMRequest.objects.create(
            feature=feature, operation="model_discovery", provider=route["provider"],
            model=route["model"], credential_fingerprint=fingerprint,
            policy_hash=policy["hash"], reserved_tokens=1)
    except Exception:
        release_permit(redis, permit)
        _deny("ledger_unavailable")
    started = time.monotonic()
    try:
        models = adapter.list_models()
    except ProviderError as err:
        _breaker_failure(
            route["provider"], fingerprint, generation, owner, err.code, policy)
        LLMRequest.objects.filter(pk=ledger.pk).update(
            status="failed", error_code=err.code, finished_at=timezone.now())
        release_permit(redis, permit)
        _deny(err.code)
    release_permit(redis, permit)
    _breaker_success(route["provider"], fingerprint, generation, owner)
    LLMRequest.objects.filter(pk=ledger.pk).update(
        status="succeeded", finished_at=timezone.now(),
        duration_ms=int((time.monotonic() - started) * 1000))
    return models


def repair_started(max_age_seconds=300, limit=100):
    from mojo.apps.account.models import LLMRequest
    cutoff = timezone.now() - timezone.timedelta(seconds=max_age_seconds)
    return LLMRequest.objects.filter(
        status="started", created__lt=cutoff).order_by("created")[:limit].update(
            status="unknown", error_code="persistence_unknown",
            finished_at=timezone.now())


def repair_started_job(job):
    repaired = repair_started()
    job.add_log(f"Marked {repaired} uncertain LLM request(s) for inspection")


def aggregate_state(hours=24):
    from django.db.models import Count, Sum
    from mojo.apps.account.models import LLMCircuitBreaker, LLMRequest
    cutoff = timezone.now() - timezone.timedelta(hours=min(max(int(hours), 1), 168))
    requests = list(LLMRequest.objects.filter(created__gte=cutoff).values(
        "provider", "feature", "status").annotate(
            requests=Count("id"), input_tokens=Sum("input_tokens"),
            output_tokens=Sum("output_tokens")).order_by(
                "provider", "feature", "status")[:200])
    breakers = list(LLMCircuitBreaker.objects.values(
        "provider", "state", "error_code").annotate(count=Count("id")).order_by(
            "provider", "state", "error_code")[:100])
    return {"hours": min(max(int(hours), 1), 168), "requests": requests,
            "breakers": breakers}


def reset_breakers(actor, provider=None):
    from mojo.apps.account.models import LLMCircuitBreaker
    from mojo.apps.account.services import system_settings
    system_settings.require_system_admin(actor)
    rows = LLMCircuitBreaker.objects.all()
    if provider is not None:
        if provider not in {"anthropic"}:
            raise ValueError("Unsupported provider")
        rows = rows.filter(provider=provider)
    count = rows.update(
        state="closed", failure_count=0, error_code="", opened_until=None,
        half_open_owner="", half_open_expires_at=None,
        generation=models.F("generation") + 1)
    try:
        actor.log(
            f"LLM circuit reset provider={provider or 'all'} count={count}",
            "llm:circuit_reset")
    except Exception:
        pass
    return count
