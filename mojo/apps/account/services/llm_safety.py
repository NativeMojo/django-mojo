"""Fail-closed provider policy, permit, ledger, and circuit boundary."""

import hashlib
import json
import re
import secrets
import time

from django.db import models, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from mojo.helpers.redis import get_connection
from mojo.helpers.settings import settings


POLICY_KEYS = frozenset({"version", "routes", "shared", "features", "breaker"})
ROUTE_KEYS = frozenset({"provider", "model", "credential", "capabilities"})
BEDROCK_ROUTE_KEYS = ROUTE_KEYS | {"region", "reasoning_efforts"}
BEDROCK_MODELS = frozenset(
    f"{prefix}.amazon.nova-2-lite-v1:0" for prefix in ("us", "eu", "jp", "global"))
AWS_REGION_PATTERN = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-[1-9][0-9]*$")
REASONING_EFFORTS = frozenset({"off", "low", "medium"})
BEDROCK_CREDENTIAL_IDENTITY = "installation-aws-chain"
BEDROCK_HEALTH_FAILURES = frozenset({
    "provider_authentication", "provider_rate_limited", "provider_timeout",
    "provider_unavailable",
})
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
    "safety_unavailable", "triage_incomplete", "credential_mismatch",
    "catalogue_unavailable", "catalogue_incomplete", "no_compatible_model",
    "refresh_in_progress", "refresh_throttled",
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
    from mojo.helpers.llm import feature_names
    names = feature_names()
    for feature, route in routes.items():
        if feature not in names or not isinstance(route, dict):
            _deny("policy_invalid")
        provider = route.get("provider")
        route_keys = BEDROCK_ROUTE_KEYS if provider == "bedrock" else ROUTE_KEYS
        if set(route) != route_keys:
            _deny("policy_invalid")
        if provider not in ("anthropic", "bedrock"):
            _deny("provider_unsupported")
        if not isinstance(route["model"], str) or not route["model"].strip():
            _deny("policy_invalid")
        if route["model"].startswith("auto:") and not (
                route["model"] == "auto:sonnet" and feature == "assistant"
                and provider == "anthropic"):
            _deny("policy_invalid")
        capabilities = route["capabilities"]
        if not isinstance(capabilities, list) or not capabilities \
                or any(not isinstance(item, str) for item in capabilities) \
                or len(capabilities) != len(set(capabilities)) \
                or any(item not in {"text", "tools", "images", "prompt_cache", "models"}
                       for item in capabilities):
            _deny("policy_invalid")
        if provider == "bedrock":
            efforts = route["reasoning_efforts"]
            if route["credential"] != "aws" or capabilities != ["text"] \
                    or route["model"] not in BEDROCK_MODELS \
                    or not isinstance(route["region"], str) \
                    or not AWS_REGION_PATTERN.fullmatch(route["region"]) \
                    or not isinstance(efforts, list) or not efforts \
                    or any(not isinstance(item, str) or item not in REASONING_EFFORTS
                           for item in efforts) \
                    or len(efforts) != len(set(efforts)):
                _deny("policy_invalid")
        elif route["credential"] not in ("admin", "handler"):
            _deny("policy_invalid")
    for feature, limits in features.items():
        if feature not in names:
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
    if isinstance(value, int) and not isinstance(value, bool) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    _deny("control_state_unknown")


def emergency_stopped():
    return emergency_stop_static() or emergency_stop_database()


def emergency_stop_static():
    return _as_bool(settings.get_static("LLM_EMERGENCY_STOP", False))


def emergency_stop_database():
    """Return only the authoritative database stop half, failing closed."""
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
    return database_stop


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


def delegates_model(route):
    return route.get("provider") == "anthropic" and route.get("model") == "auto:sonnet"


def assistant_pin():
    """Authoritative primary pin read; malformed/duplicate active controls deny."""
    from mojo.apps.account.models import Setting
    rows = list(Setting.objects.using("default").filter(
        key="LLM_ADMIN_MODEL", group=None).order_by("pk")[:2])
    if len(rows) > 1:
        _deny("control_state_unknown")
    stored = rows[0].get_value() if rows else None
    deployed = settings.get_static("LLM_ADMIN_MODEL", None)
    for value, source in ((stored, "admin"), (deployed, "deployment")):
        if value in (None, ""):
            continue
        from mojo.helpers.llm_catalogue import model_id
        if not model_id(value) or value.startswith("auto:"):
            _deny("control_state_unknown")
        return value, source
    return "", "automatic"


def model_selection_state(policy=None, *, refresh=False, credential=None, redis=None,
                          loader=None):
    """Passive by default. This is the same selector the invocation snapshots."""
    from mojo.helpers import llm
    from mojo.helpers.llm_catalogue import select_sonnet
    result = {
        "selected": "", "effective": "", "source": "policy", "choices": [],
        "mode": "fixed", "editable": False, "policy_model": "",
        "restriction": "policy_unavailable",
        "discovery": {"status": "not_applicable", "fetched_at": None,
                      "attempted_at": None, "expires_at": None,
                      "max_stale_at": None, "error_code": ""},
        "refresh": {"attempted": False, "ok": None, "code": ""},
    }
    # Preserve the legacy echo even when policy is temporarily unavailable.
    # This value has no runtime authority on any nondelegating route.
    try:
        from mojo.apps.account.services.assistant_setup import _stored_value
        legacy = _stored_value("LLM_ADMIN_MODEL")
        if not isinstance(legacy, str) or not legacy.strip():
            legacy = settings.get_static("LLM_ADMIN_MODEL", None)
        result["selected"] = legacy.strip() if isinstance(legacy, str) else ""
    except Exception:
        pass
    try:
        policy = policy or parse_policy()
        route = policy["routes"].get("assistant")
        if route is None:
            _deny("route_missing")
        result.update(policy_model=route["model"], effective=route["model"],
                      restriction="fixed_policy" if route["provider"] == "anthropic"
                      else "provider_unsupported")
        if delegates_model(route):
            result.update(selected="", effective="", editable=True, mode="auto",
                          source="automatic", restriction="")
            pin, source = assistant_pin()
            if pin:
                result.update(selected=pin, effective=pin, mode="pinned", source=source,
                              restriction="deployment_pin"
                              if settings.get_static("LLM_ADMIN_MODEL", None) else "")
            else:
                catalogue = llm.catalogue_state(
                    policy=policy, credential=credential or _credential(route),
                    redis=redis, refresh=refresh, force=False, loader=loader)
                result.update(discovery=catalogue["discovery"], refresh=catalogue["refresh"])
                result["effective"] = select_sonnet(
                    catalogue["models"], min(policy["shared"]["max_output_tokens"],
                        policy["features"]["assistant"]["max_output_tokens"]),
                    images="images" in route["capabilities"])
                if not result["effective"] and not result["discovery"]["error_code"]:
                    result["discovery"]["error_code"] = (
                        "no_compatible_model" if result["discovery"]["status"] in {"fresh", "stale"}
                        else "catalogue_unavailable")
                result["choices"] = [{"id": row["id"],
                                      "label": row["display_name"] or row["id"]}
                                     for row in catalogue["models"][:40]]
        return result
    except LLMSafetyError as error:
        result["discovery"]["error_code"] = error.code
        if result["editable"]:
            result["discovery"]["status"] = "unavailable"
        return result
    except Exception:
        result["effective"] = "" if result["editable"] else result["effective"]
        result["discovery"]["error_code"] = "control_state_unknown"
        if result["editable"]:
            result["discovery"]["status"] = "unavailable"
        return result


def route_state(feature, selection=None):
    """Passive resolved readiness and admission to the guarded invocation path.

    An automatic route can admit a turn while its catalogue is cold/expired.
    Only the invocation guard may refresh and prove a usable runtime model.
    """
    result = {"feature": feature, "provider": "", "model": "", "credential": "",
              "credential_configured": False, "ready": False, "can_invoke": False,
              "error": ""}
    try:
        policy = parse_policy()
        route = policy["routes"].get(feature)
        if route is None:
            _deny("route_missing")
        effective = route["model"]
        if feature == "assistant":
            selection = selection or model_selection_state(policy)
            effective = selection["effective"]
        result.update(provider=route["provider"], model=effective,
                      credential=route["credential"])
        _policy_agreement(policy)
        result["credential_configured"] = bool(_credential(route))
        if emergency_stopped():
            _deny("emergency_stopped")
        # Invocation admission deliberately precedes resolved-model readiness:
        # a cold automatic route must reach _execute to populate its catalogue.
        # No discovery, permit or provider request happens on this passive read.
        result["can_invoke"] = result["credential_configured"]
        if not effective:
            code = (selection or {}).get("discovery", {}).get("error_code")
            _deny(code or "catalogue_unavailable")
        result["ready"] = result["credential_configured"]
    except LLMSafetyError as err:
        result["error"] = err.code
    except Exception:
        result["error"] = "safety_unavailable"
    return result


def _credential(route):
    if route.get("provider") == "bedrock":
        # Declarative installation identity, not an SDK credential lookup.
        # Role refresh must not reset the installation's budgets or circuit.
        return BEDROCK_CREDENTIAL_IDENTITY
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


def _candidate_envelopes(policy):
    """Return hard single-flight envelopes independent of policy concurrency."""
    shared = dict(policy["shared"])
    limits = dict(policy["features"]["configuration"])
    shared["concurrency"] = 1
    limits["concurrency"] = 1
    return shared, limits


def _execute(messages, system, tools, model, max_tokens, feature, operation,
             context, reasoning_effort="off", provider_factory=None, redis=None,
             policy_raw=None):
    from mojo.apps.account.models import LLMRequest
    from mojo.helpers.llm_providers import get_provider
    from mojo.helpers.llm_providers.base import ProviderError, validated_usage

    policy = parse_policy(policy_raw)
    route = policy["routes"].get(feature)
    limits = policy["features"].get(feature)
    if route is None or limits is None:
        _blocked("", feature, "route_missing")
    provider_name = route["provider"]
    try:
        _policy_agreement(policy)
        if emergency_stopped():
            _deny("emergency_stopped")
        if not delegates_model(route) and model is not None and model != route["model"]:
            _deny("model_mismatch")
        if not isinstance(reasoning_effort, str) \
                or reasoning_effort not in REASONING_EFFORTS \
                or (provider_name == "anthropic" and reasoning_effort != "off") \
                or (provider_name == "bedrock" and
                    reasoning_effort not in route["reasoning_efforts"]):
            _deny("capability_unsupported")
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
        if provider_name == "bedrock":
            output_cap = min(output_cap, 65000)
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) \
                or max_tokens <= 0 or max_tokens > output_cap:
            _deny("output_too_large")
        credential = _credential(route)
        if not credential:
            _deny("credential_missing")
        fingerprint = credential_fingerprint(provider_name, credential)
        factory = provider_factory or get_provider
        if provider_name == "bedrock":
            adapter = factory(provider_name, region=route["region"])
        else:
            adapter = factory(provider_name, api_key=credential)
        if adapter is None:
            _deny("provider_unsupported")
        cache_enabled = provider_name != "bedrock" and bool(settings.get(
            "LLM_ADMIN_PROMPT_CACHE_ENABLED", True, kind="bool"))
        required = _required_capabilities(messages, tools, cache_enabled)
        if not required.issubset(set(route["capabilities"])) \
                or any(not adapter.supports(item) for item in required):
            _deny("capability_unsupported")
        redis = redis or get_connection()
        resolved_model = route["model"]
        if delegates_model(route):
            selection = model_selection_state(
                policy, refresh=True, credential=credential, redis=redis,
                loader=lambda: discover_models(
                    expected_policy_hash=policy["hash"],
                    expected_fingerprint=fingerprint,
                    provider_factory=factory, redis=redis, policy_raw=policy_raw))
            resolved_model = selection["effective"]
            if not resolved_model:
                _deny(selection["discovery"]["error_code"] or "catalogue_unavailable")
            # Discovery may span several requests; prove current controls again
            # before the actual inference permit and durable message ledger.
            _policy_agreement(policy)
            if emergency_stopped():
                _deny("emergency_stopped")
            if _credential(route) != credential:
                _deny("credential_mismatch")
        if model is not None and model != resolved_model:
            _deny("model_mismatch")
        operation_id = context.get("operation_id") or secrets.token_hex(16)
        _count_operation(
            redis, provider_name, fingerprint, feature, operation_id,
            policy["shared"], limits)
        generation, breaker_owner = _breaker_lease(
            provider_name, fingerprint, policy)
        reserved_tokens = len(serialized) + max_tokens
        permit = acquire_permit(
            redis, provider_name, fingerprint, feature, policy["shared"],
            limits, reserved_tokens)
        _record_burn(
            provider_name, feature, reserved_tokens, policy["shared"], limits)
    except LLMSafetyError as err:
        _blocked(provider_name, feature, err.code, retry_after=err.retry_after)

    started = time.monotonic()
    try:
        ledger = LLMRequest.objects.create(
            feature=feature, operation=str(operation)[:64], provider=provider_name,
            model=resolved_model, credential_fingerprint=fingerprint,
            policy_hash=policy["hash"], reserved_tokens=reserved_tokens,
            **scalar_context)
    except Exception:
        release_permit(redis, permit)
        _blocked(provider_name, feature, "ledger_unavailable")
    try:
        call_kwargs = dict(
            messages=messages, system=system, tools=tools, model=resolved_model,
            max_tokens=max_tokens, cache_enabled=cache_enabled,
            timeout=min(policy["shared"]["timeout_seconds"], limits["timeout_seconds"]))
        if provider_name == "bedrock":
            call_kwargs["reasoning_effort"] = reasoning_effort
        response = adapter.call(**call_kwargs)
        if provider_name == "bedrock" and (
                not isinstance(response, dict) or
                validated_usage(response.get("usage")) is None):
            raise ProviderError("provider_rejected")
    except ProviderError as err:
        elapsed = int((time.monotonic() - started) * 1000)
        if provider_name == "bedrock":
            code = err.code
            if not isinstance(code, str) or code not in SAFE_CODES \
                    or not code.startswith("provider_"):
                code = "provider_failed"
            failed_usage = validated_usage(getattr(err, "usage", None))
            actual_tokens = sum(failed_usage.values()) if failed_usage is not None \
                else reserved_tokens
            fields = dict(
                status="failed", error_code=code,
                provider_request_id=str(err.request_id or "")[:128], duration_ms=elapsed,
                finished_at=timezone.now())
            if failed_usage is not None:
                fields.update(failed_usage)
            try:
                if code in BEDROCK_HEALTH_FAILURES:
                    _breaker_failure(
                        provider_name, fingerprint, generation, breaker_owner, code, policy)
                else:
                    _breaker_success(provider_name, fingerprint, generation, breaker_owner)
                LLMRequest.objects.filter(pk=ledger.pk).update(**fields)
            except Exception:
                _deny("provider_failed")
            finally:
                release_permit(redis, permit, actual_tokens=actual_tokens)
            _record_metrics(provider_name, feature, "failed",
                            sum(failed_usage.values()) if failed_usage is not None else 0)
            _deny(code, retry_after=getattr(err, "retry_after", None))
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
        if provider_name == "bedrock":
            try:
                LLMRequest.objects.filter(pk=ledger.pk).update(
                    status="failed", error_code="provider_failed", finished_at=timezone.now())
                _breaker_success(provider_name, fingerprint, generation, breaker_owner)
            except Exception:
                _deny("provider_failed")
            finally:
                release_permit(redis, permit, actual_tokens=reserved_tokens)
        else:
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
    try:
        _breaker_success(provider_name, fingerprint, generation, breaker_owner)
    except Exception:
        if provider_name == "bedrock":
            _deny("provider_failed")
        raise
    elapsed = int((time.monotonic() - started) * 1000)
    try:
        updated = LLMRequest.objects.filter(pk=ledger.pk, status="started").update(
            status="succeeded", finished_at=timezone.now(), duration_ms=elapsed,
            provider_request_id=str(response.get("id") or "")[:128],
            input_tokens=max(0, int(usage.get("input_tokens", 0) or 0)),
            output_tokens=max(0, int(usage.get("output_tokens", 0) or 0)),
            cache_read_input_tokens=max(0, int(usage.get("cache_read_input_tokens", 0) or 0)),
            cache_creation_input_tokens=max(
                0, int(usage.get("cache_creation_input_tokens", 0) or 0)))
    except Exception:
        if provider_name == "bedrock":
            _deny("provider_failed")
        raise
    if updated != 1:
        _blocked(provider_name, feature, "provider_failed" if provider_name == "bedrock"
                 else "ledger_persistence_unknown")
    _record_metrics(provider_name, feature, "succeeded", actual_tokens)
    response["model"] = resolved_model
    return response


def invoke(messages, system=None, tools=None, model=None, max_tokens=4096, *,
           feature, operation, context=None, reasoning_effort="off"):
    """The only production message execution entry point."""
    return _execute(
        messages, system, tools, model, max_tokens, feature, operation, context,
        reasoning_effort=reasoning_effort)


def execute_guarded_for_test(messages, *, feature, operation="test", context=None,
                             system=None, tools=None, model=None, max_tokens=4,
                             reasoning_effort="off", provider_factory, redis,
                             policy_raw):
    """Explicit test-only injection; every production guard still executes."""
    return _execute(
        messages, system, tools, model, max_tokens, feature, operation, context,
        reasoning_effort=reasoning_effort, provider_factory=provider_factory,
        redis=redis, policy_raw=policy_raw)


def _fixed_configuration_probe_core(actor, credential, identity, operation):
    """Run the module-owned text-only four-token configuration probe.

    This is deliberately not parameterized with messages, tools, system,
    model, capabilities, or output size. The sole stopped-state exception is
    selected by the private candidate wrapper below.
    """
    from mojo.apps.account.models import LLMRequest
    from mojo.helpers.llm_providers import get_provider
    from mojo.helpers.llm_providers.base import ProviderError

    recovery_probe = identity == "candidate-installation"
    if recovery_probe:
        # Authority is proved before policy, Redis, ledger, or provider work.
        # Thus even a malformed installation cannot expose the stopped-state
        # exception to an unauthorised caller.
        from mojo.apps.account.services import system_settings
        system_settings.require_system_admin(actor)
    policy = parse_policy()
    feature = "configuration"
    route = policy["routes"].get(feature)
    limits = policy["features"].get(feature)
    provider = route["provider"] if route else ""
    try:
        if route is None or limits is None:
            _deny("route_missing")
        if provider != "anthropic":
            _deny("provider_unsupported")
        _policy_agreement(policy)
        if not recovery_probe and emergency_stopped():
            _deny("emergency_stopped")
        if not isinstance(credential, str) or not credential.strip():
            _deny("credential_missing")
        fingerprint = credential_fingerprint(provider, credential)
        adapter = get_provider(provider, api_key=credential)
        if adapter is None or "text" not in route["capabilities"] \
                or not adapter.supports("text"):
            _deny("capability_unsupported")
        redis = get_connection()
        shared = dict(policy["shared"])
        feature_limits = dict(limits)
        if recovery_probe:
            # Candidate recovery is a single installation-wide lane even
            # when the configured shared/feature concurrency is larger.
            shared, feature_limits = _candidate_envelopes(policy)
        operation_id = secrets.token_hex(16)
        _count_operation(redis, provider, identity, feature, operation_id,
                         shared, feature_limits)
        generation, breaker_owner = _breaker_lease(provider, fingerprint, policy)
        prompt = [{"role": "user", "content": "Reply OK"}]
        serialized = json.dumps(
            {"messages": prompt, "system": None, "tools": None},
            separators=(",", ":")).encode("utf-8")
        if len(serialized) > min(shared["max_input_bytes"],
                                 feature_limits["max_input_bytes"]):
            _deny("input_too_large")
        if 4 > min(shared["max_output_tokens"],
                   feature_limits["max_output_tokens"]):
            _deny("output_too_large")
        reserved_tokens = len(serialized) + 4
        permit = acquire_permit(
            redis, provider, identity, feature, shared, feature_limits,
            reserved_tokens)
    except LLMSafetyError as err:
        _blocked(provider, feature, err.code, retry_after=err.retry_after)

    try:
        ledger = LLMRequest.objects.create(
            feature=feature, operation=operation, provider=provider,
            model=route["model"], credential_fingerprint=fingerprint,
            policy_hash=policy["hash"], reserved_tokens=reserved_tokens)
    except Exception:
        release_permit(redis, permit)
        _blocked(provider, feature, "ledger_unavailable")
    started = time.monotonic()
    try:
        response = adapter.call(
            messages=prompt, system=None, tools=None, model=route["model"],
            max_tokens=4, cache_enabled=False,
            timeout=min(shared["timeout_seconds"],
                        feature_limits["timeout_seconds"]))
    except ProviderError as err:
        _breaker_failure(
            provider, fingerprint, generation, breaker_owner, err.code, policy)
        LLMRequest.objects.filter(pk=ledger.pk).update(
            status="failed", error_code=err.code,
            provider_request_id=err.request_id[:128], finished_at=timezone.now())
        release_permit(redis, permit)
        _deny(err.code, retry_after=getattr(err, "retry_after", None))
    except Exception:
        _breaker_failure(
            provider, fingerprint, generation, breaker_owner,
            "provider_failed", policy)
        LLMRequest.objects.filter(pk=ledger.pk).update(
            status="failed", error_code="provider_failed", finished_at=timezone.now())
        release_permit(redis, permit)
        _deny("provider_failed")
    usage = response.get("usage") or {}
    actual_tokens = sum(max(0, int(usage.get(key, 0) or 0)) for key in (
        "input_tokens", "output_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens"))
    release_permit(redis, permit, actual_tokens=actual_tokens)
    _breaker_success(provider, fingerprint, generation, breaker_owner)
    updated = LLMRequest.objects.filter(pk=ledger.pk, status="started").update(
        status="succeeded", finished_at=timezone.now(),
        duration_ms=int((time.monotonic() - started) * 1000),
        input_tokens=max(0, int(usage.get("input_tokens", 0) or 0)),
        output_tokens=max(0, int(usage.get("output_tokens", 0) or 0)))
    if updated != 1:
        _blocked(provider, feature, "ledger_persistence_unknown")
    return True


def _fixed_configuration_probe(actor, credential, identity, operation):
    """Fixed probe; candidate recovery re-proves actor authority in the core."""
    return _fixed_configuration_probe_core(actor, credential, identity, operation)


def _candidate_audit_requested(actor):
    """Durably record authority before the stopped-state exception can spend."""
    try:
        row = actor.log(
            "LLM candidate recovery probe outcome=requested",
            "llm:candidate_probe")
    except Exception:
        _deny("safety_unavailable")
    if row is None or not getattr(row, "pk", None):
        _deny("safety_unavailable")
    return row


def _candidate_audit_outcome(row, outcome):
    """Finish the one actor-linked audit row with a bounded safe outcome."""
    outcome = "accepted" if outcome == "accepted" else "rejected"
    try:
        updated = type(row).objects.filter(pk=row.pk).update(
            log=f"LLM candidate recovery probe outcome={outcome}")
    except Exception:
        _deny("safety_unavailable")
    if updated != 1:
        _deny("safety_unavailable")


def verify_candidate(actor, candidate):
    """Owner-authorized fixed recovery probe; the only stopped exception."""
    from mojo.apps.account.services import system_settings
    system_settings.require_system_admin(actor)
    if not isinstance(candidate, str) or not candidate.strip() \
            or candidate != candidate.strip() or len(candidate) > 4096 \
            or any(char.isspace() for char in candidate):
        _deny("credential_missing")
    audit = _candidate_audit_requested(actor)
    try:
        result = _fixed_configuration_probe(
            actor, candidate, "candidate-installation", "candidate_key_probe")
    except Exception:
        _candidate_audit_outcome(audit, "rejected")
        raise
    _candidate_audit_outcome(audit, "accepted")
    return result


def verify_stored_key(target="admin"):
    """Verify exactly one stored target, never falling back or bypassing stop."""
    if target not in {"admin", "handler"}:
        _deny("operation_invalid")
    key = "LLM_ADMIN_API_KEY" if target == "admin" else "LLM_HANDLER_API_KEY"
    credential = settings.get(key, None)
    if not credential:
        _deny("credential_missing")
    fingerprint = credential_fingerprint("anthropic", credential)
    return _fixed_configuration_probe(
        None, credential, fingerprint, f"stored_{target}_key_probe")


def _discover_models_page(*, after_id, operation_id, deadline,
                          expected_policy_hash=None, expected_fingerprint=None,
                          provider_factory=None, redis=None, policy_raw=None):
    """Each page independently proves policy, stop, credential, budget and ledger."""
    from mojo.apps.account.models import LLMRequest
    from mojo.helpers.llm_providers import get_provider
    from mojo.helpers.llm_providers.base import ProviderError

    policy = parse_policy(policy_raw)
    feature = "model_discovery"
    route = policy["routes"].get(feature)
    limits = policy["features"].get(feature)
    provider = route["provider"] if route else ""
    try:
        if route is None or limits is None:
            _deny("route_missing")
        if provider != "anthropic":
            _deny("provider_unsupported")
        if expected_policy_hash is not None and policy["hash"] != expected_policy_hash:
            _deny("policy_mixed")
        _policy_agreement(policy)
        if emergency_stopped():
            _deny("emergency_stopped")
        credential = _credential(route)
        fingerprint = credential_fingerprint(provider, credential)
        if expected_fingerprint is not None and fingerprint != expected_fingerprint:
            _deny("credential_mismatch")
        adapter = (provider_factory or get_provider)(provider, api_key=credential)
        if adapter is None or "models" not in route["capabilities"] \
                or not adapter.supports("models"):
            _deny("capability_unsupported")
        redis = redis or get_connection()
        _count_operation(
            redis, provider, fingerprint, feature, operation_id,
            policy["shared"], limits)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _deny("provider_timeout")
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
            policy_hash=policy["hash"], reserved_tokens=1, job_id=operation_id)
    except Exception:
        release_permit(redis, permit)
        _blocked(provider, feature, "ledger_unavailable")
    started = time.monotonic()
    try:
        models = adapter.list_models_page(after_id=after_id, timeout=min(
            deadline - time.monotonic(), policy["shared"]["timeout_seconds"],
            limits["timeout_seconds"]))
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
    updated = LLMRequest.objects.filter(pk=ledger.pk, status="started").update(
        status="succeeded", finished_at=timezone.now(),
        duration_ms=int((time.monotonic() - started) * 1000))
    if updated != 1:
        _deny("ledger_persistence_unknown")
    _record_metrics(provider, feature, "succeeded")
    return models


def discover_models(*, expected_policy_hash=None, expected_fingerprint=None,
                    provider_factory=None, redis=None, policy_raw=None):
    """Complete catalogue, at most five separately guarded pages in 20 seconds."""
    policy = parse_policy(policy_raw)
    limits = policy["features"].get("model_discovery")
    route = policy["routes"].get("model_discovery")
    if limits is None or route is None:
        _deny("route_missing")
    if route["provider"] != "anthropic":
        _deny("provider_unsupported")
    if expected_fingerprint is None:
        expected_fingerprint = credential_fingerprint("anthropic", _credential(route))
    deadline = time.monotonic() + min(
        20, policy["shared"]["timeout_seconds"], limits["timeout_seconds"])
    operation_id = secrets.token_hex(16)
    rows, seen, after_id = [], set(), None
    from mojo.helpers.llm_catalogue import model_id
    for page_number in range(5):
        page = _discover_models_page(
            after_id=after_id, operation_id=operation_id, deadline=deadline,
            expected_policy_hash=expected_policy_hash or policy["hash"],
            expected_fingerprint=expected_fingerprint, provider_factory=provider_factory,
            redis=redis, policy_raw=policy_raw)
        if time.monotonic() > deadline:
            _deny("provider_timeout")
        if not isinstance(page, dict) or not isinstance(page.get("data"), list) \
                or len(page["data"]) > 100 or type(page.get("has_more")) is not bool:
            _deny("catalogue_incomplete")
        rows.extend(page["data"])
        if not page["has_more"]:
            return rows
        cursor = page.get("last_id")
        if not page["data"] or not model_id(cursor) or cursor in seen:
            _deny("catalogue_incomplete")
        seen.add(cursor)
        after_id = cursor
    _deny("catalogue_incomplete")


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
        if provider not in {"anthropic", "bedrock"}:
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
