"""Fail-closed provider policy, permit, ledger, and circuit boundary."""

import hashlib
import json
import secrets
import time

from django.db import models, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

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
EXPECTED_POLICY_HASH_KEY = "LLM_SAFETY_POLICY_EXPECTED_HASH"
AUTONOMOUS_TRIAGE_KEY = "LLM_AUTONOMOUS_INCIDENT_TRIAGE_ENABLED"
AUTONOMOUS_TRIAGE_WATERMARK_KEY = "LLM_AUTONOMOUS_INCIDENT_TRIAGE_ACTIVATED_AT"
SAFE_CODES = frozenset({
    "breaker_half_open", "breaker_open", "budget_exhausted",
    "capability_unsupported", "concurrency_exhausted", "context_invalid",
    "control_state_unknown", "credential_missing", "emergency_stopped",
    "input_too_large", "ledger_persistence_unknown", "ledger_unavailable",
    "loop_limit", "model_mismatch", "operation_invalid", "output_too_large",
    "permit_unavailable", "policy_invalid", "policy_mixed",
    "provider_authentication", "provider_billing_exhausted", "provider_failed",
    "provider_rate_limited", "provider_rejected", "provider_timeout",
    "provider_unavailable", "provider_unsupported", "route_missing",
    "safety_unavailable",
})


class LLMSafetyError(Exception):
    def __init__(self, code, retry_after=None):
        self.code = code if code in SAFE_CODES else "provider_failed"
        self.retry_after = retry_after
        super().__init__(self.code)


def _deny(code, retry_after=None):
    raise LLMSafetyError(code, retry_after=retry_after)


def _record_metrics(provider, feature, status, tokens=0):
    """Use bounded dimensions only: no fingerprint, model, id, or raw error."""
    try:
        from mojo.apps import metrics
        metrics.record(
            "llm:requests", account=provider or "unknown",
            category=f"{feature or 'unknown'}:{status}", min_granularity="minutes")
        if tokens:
            metrics.record(
                "llm:tokens", count=max(0, int(tokens)),
                account=provider or "unknown", category=feature or "unknown",
                min_granularity="minutes")
    except Exception:
        pass


def _record_burn(provider, feature, reserved_tokens, shared, limits):
    """Bounded reservation burn-rate signals for shared and feature envelopes."""
    try:
        from mojo.apps import metrics
        for scope, source in (("shared", shared), ("feature", limits)):
            percent = min(100, int(
                max(0, reserved_tokens) * 100 / source["tokens_minute"]))
            metrics.record(
                "llm:burn_rate", count=percent, account=provider,
                category=f"{feature}:{scope}", min_granularity="minutes")
    except Exception:
        pass


def _signal(provider, feature, code):
    if code not in {
            "breaker_open", "breaker_half_open", "budget_exhausted",
            "concurrency_exhausted", "policy_mixed", "permit_unavailable"}:
        return
    try:
        from mojo.apps.incident import report_event_suppressed
        report_event_suppressed(
            f"LLM guard blocked provider={provider or 'unknown'} "
            f"feature={feature or 'unknown'} code={code}",
            title="LLM safety guard blocked a request", category="llm:safety",
            level=7, key=f"llm-safety:{provider or 'unknown'}:"
                         f"{feature or 'unknown'}:{code}", window=300, budget=50)
    except Exception:
        pass


def _blocked(provider, feature, code, retry_after=None):
    _record_metrics(provider, feature, "blocked")
    _signal(provider, feature, code)
    _deny(code, retry_after=retry_after)


def _canonical_hash(value):
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def credential_fingerprint(provider, credential):
    return hashlib.sha256(f"{provider}\0{credential}".encode("utf-8")).hexdigest()


def _validate_limits(value, label):
    if not isinstance(value, dict) or set(value) != LIMIT_KEYS:
        _deny("policy_invalid")
    for key in LIMIT_KEYS:
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            _deny("policy_invalid")
    if not (value["requests_minute"] <= value["requests_hour"] <=
            value["requests_day"]):
        _deny("policy_invalid")
    if not (value["tokens_minute"] <= value["tokens_hour"] <=
            value["tokens_day"]):
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
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
           for value in breaker.values()):
        _deny("policy_invalid")
    _validate_limits(raw["shared"], "shared")
    from mojo.helpers.llm import FEATURES
    for feature, route in routes.items():
        if feature not in FEATURES or not isinstance(route, dict) \
                or set(route) != ROUTE_KEYS:
            _deny("policy_invalid")
        if route["provider"] != "anthropic":
            _deny("provider_unsupported")
        if route["credential"] not in {"admin", "handler"}:
            _deny("policy_invalid")
        if not isinstance(route["model"], str) or not route["model"].strip():
            _deny("policy_invalid")
        capabilities = route["capabilities"]
        if not isinstance(capabilities, list) or not capabilities \
                or len(capabilities) != len(set(capabilities)) \
                or any(item not in {"text", "tools", "images", "prompt_cache", "models"}
                       for item in capabilities):
            _deny("policy_invalid")
    for feature, limits in features.items():
        if feature not in FEATURES:
            _deny("policy_invalid")
        _validate_limits(limits, feature)
    if set(routes) != set(features):
        _deny("policy_invalid")
    policy = dict(raw)
    policy["hash"] = _canonical_hash(raw)
    return policy


def _single_primary_value(key):
    from mojo.apps.account.models import Setting
    rows = list(Setting.objects.using("default").filter(
        key=key, group=None).order_by("pk")[:2])
    if len(rows) != 1:
        _deny("control_state_unknown")
    return rows[0].get_value()


def _policy_agreement(policy):
    try:
        expected = str(_single_primary_value(EXPECTED_POLICY_HASH_KEY) or "").strip()
    except Exception:
        _deny("policy_mixed")
    if not expected or not secrets.compare_digest(expected, policy["hash"]):
        _deny("policy_mixed")


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def emergency_stopped():
    static_stop = _as_bool(settings.get_static("LLM_EMERGENCY_STOP", False))
    try:
        from mojo.apps.account.models import Setting
        rows = list(Setting.objects.using("default").filter(
            key="LLM_EMERGENCY_STOP", group=None).order_by("pk")[:2])
        if len(rows) > 1:
            _deny("control_state_unknown")
        database_stop = bool(rows and _as_bool(rows[0].get_value()))
    except LLMSafetyError:
        raise
    except Exception:
        _deny("control_state_unknown")
    return static_stop or database_stop


def autonomous_triage_state():
    """Return authoritative primary-DB activation state, failing closed."""
    try:
        from mojo.apps.account.models import Setting
        rows = list(Setting.objects.using("default").filter(
            key__in=(AUTONOMOUS_TRIAGE_KEY, AUTONOMOUS_TRIAGE_WATERMARK_KEY),
            group=None).order_by("key", "pk"))
        grouped = {}
        for row in rows:
            grouped.setdefault(row.key, []).append(row)
        enabled_rows = grouped.get(AUTONOMOUS_TRIAGE_KEY, [])
        if len(enabled_rows) != 1 or not _as_bool(enabled_rows[0].get_value()):
            return False, None
        watermark_rows = grouped.get(AUTONOMOUS_TRIAGE_WATERMARK_KEY, [])
        if len(watermark_rows) != 1:
            return False, None
        watermark = parse_datetime(str(watermark_rows[0].get_value() or ""))
        if watermark is None:
            return False, None
        return True, watermark
    except Exception:
        return False, None


def _credential(route):
    key = "LLM_ADMIN_API_KEY" if route["credential"] == "admin" \
        else "LLM_HANDLER_API_KEY"
    credential = settings.get(key, None)
    if not credential:
        _deny("credential_missing")
    return credential


ACQUIRE_LUA = r"""
local now = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now)
redis.call('ZREMRANGEBYSCORE', KEYS[2], 0, now)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[4]) then return {0, 'concurrency'} end
if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[5]) then return {0, 'concurrency'} end
for i=3,#KEYS do
  local current = tonumber(redis.call('GET', KEYS[i]) or '0')
  local offset = 6 + ((i - 3) * 3)
  if current + tonumber(ARGV[offset]) > tonumber(ARGV[offset + 1]) then
    return {0, 'budget'}
  end
end
redis.call('ZADD', KEYS[1], tonumber(ARGV[3]), ARGV[1])
redis.call('ZADD', KEYS[2], tonumber(ARGV[3]), ARGV[1])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[#ARGV]))
redis.call('EXPIRE', KEYS[2], tonumber(ARGV[#ARGV]))
for i=3,#KEYS do
  local offset = 6 + ((i - 3) * 3)
  redis.call('INCRBY', KEYS[i], tonumber(ARGV[offset]))
  redis.call('EXPIRE', KEYS[i], tonumber(ARGV[offset + 2]))
end
return {1, 'ok'}
"""

RELEASE_LUA = r"""
if not redis.call('ZSCORE', KEYS[1], ARGV[1]) or
   not redis.call('ZSCORE', KEYS[2], ARGV[1]) then return 0 end
redis.call('ZREM', KEYS[1], ARGV[1]); redis.call('ZREM', KEYS[2], ARGV[1])
local excess = tonumber(ARGV[2]) - tonumber(ARGV[3])
if excess > 0 then
  for i=3,#KEYS do
    if string.find(KEYS[i], ':tokens:') then
      local current = tonumber(redis.call('GET', KEYS[i]) or '0')
      redis.call('SET', KEYS[i], math.max(0, current - excess), 'KEEPTTL')
    end
  end
end
return 1
"""

LOOP_LUA = r"""
local value = redis.call('INCR', KEYS[1])
if value == 1 then redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2])) end
if value > tonumber(ARGV[1]) then return 0 end
return value
"""


def _permit_keys(provider, fingerprint, feature, now):
    root = f"mojo:llm:{provider}:{fingerprint}"
    keys = [f"{root}:shared:leases", f"{root}:{feature}:leases"]
    for scope in ("shared", feature):
        for kind in ("requests", "tokens"):
            for window, ttl in WINDOWS:
                epoch = int(now) // ttl
                keys.append(f"{root}:{scope}:{kind}:{window}:{epoch}")
    return keys


def acquire_permit(redis, provider, fingerprint, feature, shared, limits,
                   reserved_tokens, owner=None, now=None):
    now = int(now or time.time())
    owner = owner or secrets.token_hex(16)
    timeout = min(shared["timeout_seconds"], limits["timeout_seconds"])
    keys = _permit_keys(provider, fingerprint, feature, now)
    args = [owner, str(now), str(now + timeout + 30),
            str(shared["concurrency"]), str(limits["concurrency"])]
    for source in (shared, limits):
        for kind, amount in (("requests", 1), ("tokens", reserved_tokens)):
            for window, ttl in WINDOWS:
                remaining = ttl - (now % ttl) + 30
                args.extend((str(amount), str(source[f"{kind}_{window}"]), str(remaining)))
    args.append(str(timeout + 60))
    try:
        result = redis.eval(ACQUIRE_LUA, len(keys), *(keys + args))
    except Exception:
        _deny("permit_unavailable")
    code = result[1].decode("utf-8") if isinstance(result[1], bytes) else result[1]
    if int(result[0]) != 1:
        _deny("budget_exhausted" if code == "budget" else "concurrency_exhausted")
    return {"keys": keys, "owner": owner, "reserved_tokens": reserved_tokens}


def release_permit(redis, permit, actual_tokens=0):
    try:
        return bool(redis.eval(
            RELEASE_LUA, len(permit["keys"]),
            *(permit["keys"] + [permit["owner"], str(permit["reserved_tokens"]),
                                str(max(0, int(actual_tokens or 0)))])))
    except Exception:
        return False


def _count_operation(redis, provider, fingerprint, feature, operation_id,
                     shared, limits):
    if not isinstance(operation_id, str) or not operation_id \
            or len(operation_id) > 128:
        _deny("operation_invalid")
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    key = f"mojo:llm:{provider}:{fingerprint}:{feature}:operation:{digest}"
    maximum = min(shared["max_loop_calls"], limits["max_loop_calls"])
    ttl = min(shared["timeout_seconds"], limits["timeout_seconds"]) * maximum + 60
    try:
        allowed = redis.eval(LOOP_LUA, 1, key, str(maximum), str(ttl))
    except Exception:
        _deny("permit_unavailable")
    if int(allowed) == 0:
        _deny("loop_limit")


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
    if row.state == "open":
        _record_metrics(provider, "breaker", "open")
        _signal(provider, "breaker", "breaker_open")


def _context_fields(context):
    allowed = {"job_id", "incident_id", "conversation_id", "file_id", "operation_id"}
    if not isinstance(context, dict) or set(context) - allowed:
        _deny("context_invalid")
    result = {}
    for key in allowed - {"operation_id"}:
        value = context.get(key)
        if value is not None and not isinstance(value, (str, int)):
            _deny("context_invalid")
        if value is not None:
            result[key] = value
    return result


def _contains_image(value):
    if isinstance(value, dict):
        if value.get("type") == "image":
            return True
        return any(_contains_image(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_image(item) for item in value)
    return False


def _required_capabilities(messages, tools, cache_enabled):
    required = {"text"}
    if tools:
        required.add("tools")
    if _contains_image(messages):
        required.add("images")
    if cache_enabled:
        required.add("prompt_cache")
    return required


def _permit_identity(fingerprint, candidate_probe=False):
    """Candidate verification shares one installation/provider permit identity."""
    return "candidate-installation" if candidate_probe else fingerprint


def _execute(messages, system, tools, model, max_tokens, feature, operation,
             context, candidate=None, candidate_probe=False, provider_factory=None,
             redis=None, policy_raw=None):
    from mojo.apps.account.models import LLMRequest
    from mojo.helpers.llm_providers import get_provider
    from mojo.helpers.llm_providers.base import ProviderError

    policy = parse_policy(policy_raw)
    route = policy["routes"].get(feature)
    limits = policy["features"].get(feature)
    if route is None or limits is None:
        _blocked("", feature, "route_missing")
    provider_name = route["provider"]
    try:
        _policy_agreement(policy)
        if emergency_stopped() and not candidate_probe:
            _deny("emergency_stopped")
        if candidate_probe and (feature != "configuration" or system is not None
                                or tools is not None or model is not None
                                or messages != [{"role": "user", "content": "Reply OK"}]
                                or max_tokens != 4):
            _deny("context_invalid")
        if model is not None and model != route["model"]:
            _deny("model_mismatch")
        context = context or {}
        scalar_context = _context_fields(context)
        serialized = json.dumps(
            {"messages": messages, "system": system, "tools": tools},
            separators=(",", ":"), default=str).encode("utf-8")
        max_input = min(policy["shared"]["max_input_bytes"], limits["max_input_bytes"])
        if len(serialized) > max_input:
            _deny("input_too_large")
        output_cap = min(policy["shared"]["max_output_tokens"],
                         limits["max_output_tokens"])
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) \
                or max_tokens <= 0 or max_tokens > output_cap:
            _deny("output_too_large")
        credential = candidate if candidate_probe else _credential(route)
        if not credential:
            _deny("credential_missing")
        fingerprint = credential_fingerprint(provider_name, credential)
        factory = provider_factory or get_provider
        adapter = factory(provider_name, api_key=credential)
        if adapter is None:
            _deny("provider_unsupported")
        cache_enabled = bool(settings.get(
            "LLM_ADMIN_PROMPT_CACHE_ENABLED", True, kind="bool"))
        required = _required_capabilities(messages, tools, cache_enabled)
        if candidate_probe:
            required = {"text"}
            cache_enabled = False
        if not required.issubset(set(route["capabilities"])) \
                or any(not adapter.supports(item) for item in required):
            _deny("capability_unsupported")
        redis = redis or get_connection()
        operation_id = context.get("operation_id") or secrets.token_hex(16)
        counter_fingerprint = _permit_identity(
            fingerprint, candidate_probe=candidate_probe)
        _count_operation(
            redis, provider_name, counter_fingerprint, feature, operation_id,
            policy["shared"], limits)
        generation, breaker_owner = _breaker_lease(
            provider_name, fingerprint, policy)
        reserved_tokens = len(serialized) + max_tokens
        permit = acquire_permit(
            redis, provider_name, counter_fingerprint, feature, policy["shared"],
            limits, reserved_tokens)
        _record_burn(
            provider_name, feature, reserved_tokens, policy["shared"], limits)
    except LLMSafetyError as err:
        _blocked(provider_name, feature, err.code, retry_after=err.retry_after)

    started = time.monotonic()
    try:
        ledger = LLMRequest.objects.create(
            feature=feature, operation=str(operation)[:64], provider=provider_name,
            model=route["model"], credential_fingerprint=fingerprint,
            policy_hash=policy["hash"], reserved_tokens=reserved_tokens,
            **scalar_context)
    except Exception:
        release_permit(redis, permit)
        _blocked(provider_name, feature, "ledger_unavailable")
    try:
        response = adapter.call(
            messages=messages, system=system, tools=tools, model=route["model"],
            max_tokens=max_tokens, cache_enabled=cache_enabled,
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
        _deny(err.code, retry_after=getattr(err, "retry_after", None))
    except Exception:
        LLMRequest.objects.filter(pk=ledger.pk).update(
            status="failed", error_code="provider_failed", finished_at=timezone.now())
        release_permit(redis, permit)
        _breaker_failure(
            provider_name, fingerprint, generation, breaker_owner,
            "provider_failed", policy)
        _record_metrics(provider_name, feature, "failed")
        _deny("provider_failed")
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
        _blocked(provider_name, feature, "ledger_persistence_unknown")
    _record_metrics(provider_name, feature, "succeeded", actual_tokens)
    return response


def invoke(messages, system=None, tools=None, model=None, max_tokens=4096, *,
           feature, operation, context=None):
    """The only production message execution entry point."""
    return _execute(
        messages, system, tools, model, max_tokens, feature, operation, context)


def execute_guarded_for_test(messages, *, feature, operation="test", context=None,
                             system=None, tools=None, model=None, max_tokens=4,
                             provider_factory, redis, policy_raw):
    """Explicit test-only injection; every production guard still executes."""
    return _execute(
        messages, system, tools, model, max_tokens, feature, operation, context,
        provider_factory=provider_factory, redis=redis, policy_raw=policy_raw)


def verify_candidate(candidate):
    """Fixed candidate probe: one request, no caller-selected prompt or model."""
    return bool(_execute(
        [{"role": "user", "content": "Reply OK"}], None, None, None, 4,
        "configuration", "candidate_key_probe", {}, candidate=candidate,
        candidate_probe=True))


def verify_stored_key():
    """Stored credentials never receive the stopped-state exception."""
    return bool(_execute(
        [{"role": "user", "content": "Reply OK"}], None, None, None, 4,
        "configuration", "stored_key_probe", {}))


def discover_models():
    """One guarded, bounded provider catalogue request."""
    from mojo.apps.account.models import LLMRequest
    from mojo.helpers.llm_providers import get_provider
    from mojo.helpers.llm_providers.base import ProviderError

    policy = parse_policy()
    feature = "model_discovery"
    route = policy["routes"].get(feature)
    limits = policy["features"].get(feature)
    provider = route["provider"] if route else ""
    try:
        if route is None or limits is None:
            _deny("route_missing")
        _policy_agreement(policy)
        if emergency_stopped():
            _deny("emergency_stopped")
        credential = _credential(route)
        fingerprint = credential_fingerprint(provider, credential)
        adapter = get_provider(provider, api_key=credential)
        if adapter is None or "models" not in route["capabilities"] \
                or not adapter.supports("models"):
            _deny("capability_unsupported")
        redis = get_connection()
        _count_operation(
            redis, provider, fingerprint, feature, secrets.token_hex(16),
            policy["shared"], limits)
        generation, owner = _breaker_lease(provider, fingerprint, policy)
        permit = acquire_permit(
            redis, provider, fingerprint, feature, policy["shared"], limits, 1)
        _record_burn(provider, feature, 1, policy["shared"], limits)
    except LLMSafetyError as err:
        _blocked(provider, feature, err.code, retry_after=err.retry_after)
    try:
        ledger = LLMRequest.objects.create(
            feature=feature, operation="model_discovery", provider=provider,
            model=route["model"], credential_fingerprint=fingerprint,
            policy_hash=policy["hash"], reserved_tokens=1)
    except Exception:
        release_permit(redis, permit)
        _blocked(provider, feature, "ledger_unavailable")
    started = time.monotonic()
    try:
        models = adapter.list_models(timeout=min(
            policy["shared"]["timeout_seconds"], limits["timeout_seconds"]))
    except ProviderError as err:
        _breaker_failure(provider, fingerprint, generation, owner, err.code, policy)
        LLMRequest.objects.filter(pk=ledger.pk).update(
            status="failed", error_code=err.code, finished_at=timezone.now())
        release_permit(redis, permit)
        _deny(err.code, retry_after=getattr(err, "retry_after", None))
    except Exception:
        _breaker_failure(
            provider, fingerprint, generation, owner, "provider_failed", policy)
        LLMRequest.objects.filter(pk=ledger.pk).update(
            status="failed", error_code="provider_failed", finished_at=timezone.now())
        release_permit(redis, permit)
        _deny("provider_failed")
    release_permit(redis, permit)
    _breaker_success(provider, fingerprint, generation, owner)
    LLMRequest.objects.filter(pk=ledger.pk).update(
        status="succeeded", finished_at=timezone.now(),
        duration_ms=int((time.monotonic() - started) * 1000))
    _record_metrics(provider, feature, "succeeded")
    return models


def repair_started(max_age_seconds=300, limit=100):
    from mojo.apps.account.models import LLMRequest
    cutoff = timezone.now() - timezone.timedelta(seconds=max_age_seconds)
    ids = list(LLMRequest.objects.filter(
        status="started", created__lt=cutoff).order_by("created").values_list(
            "pk", flat=True)[:max(1, min(int(limit), 500))])
    if not ids:
        return 0
    return LLMRequest.objects.filter(pk__in=ids, status="started").update(
        status="unknown", error_code="persistence_unknown", finished_at=timezone.now())


def repair_started_job(job):
    repaired = repair_started()
    job.add_log(f"Marked {repaired} uncertain LLM request(s) for inspection")


def aggregate_state(hours=24):
    from django.db.models import Count, Sum
    from mojo.apps.account.models import LLMCircuitBreaker, LLMRequest
    bounded_hours = min(max(int(hours), 1), 168)
    cutoff = timezone.now() - timezone.timedelta(hours=bounded_hours)
    requests = list(LLMRequest.objects.filter(created__gte=cutoff).values(
        "provider", "feature", "status").annotate(
            requests=Count("id"), input_tokens=Sum("input_tokens"),
            output_tokens=Sum("output_tokens")).order_by(
                "provider", "feature", "status")[:200])
    breakers = list(LLMCircuitBreaker.objects.values(
        "provider", "state", "error_code").annotate(count=Count("id")).order_by(
            "provider", "state", "error_code")[:100])
    return {"hours": bounded_hours, "requests": requests, "breakers": breakers}


def activate_policy(actor):
    """Owner acknowledgment for a deployed policy hash."""
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import system_settings
    system_settings.require_system_admin(actor)
    policy = parse_policy()
    with transaction.atomic():
        rows = list(Setting.objects.select_for_update().filter(
            key=EXPECTED_POLICY_HASH_KEY, group=None).order_by("pk")[:2])
        row = rows[0] if rows else Setting(
            key=EXPECTED_POLICY_HASH_KEY, group=None, is_secret=False)
        row.is_secret = False
        row.set_value(policy["hash"])
        row.save(_protected_writer=EXPECTED_POLICY_HASH_KEY, _skip_cache=True)
        if len(rows) > 1:
            Setting.objects.filter(pk__in=[item.pk for item in rows[1:]]).delete()
        transaction.on_commit(row.push_to_cache)
    try:
        actor.log("LLM safety policy hash activated", "llm:policy_activated")
    except Exception:
        pass
    return True


def reset_breakers(actor, provider=None):
    from mojo.apps.account.models import LLMCircuitBreaker
    from mojo.apps.account.services import system_settings
    system_settings.require_system_admin(actor)
    rows = LLMCircuitBreaker.objects.all()
    if provider is not None:
        if provider != "anthropic":
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
