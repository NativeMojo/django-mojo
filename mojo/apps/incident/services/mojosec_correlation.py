"""Concurrency-safe, bounded MojoSec shadow correlation."""

import datetime
import hashlib
import ipaddress
import json
import re

from django.db import transaction

from mojo.apps import metrics
from mojo.helpers import dates, logit
from mojo.helpers.settings import settings


logger = logit.get_logger(__name__, "incident.log")
EVALUATOR_VERSION = 1
MAX_SAMPLES = 8
_TOKEN = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
_WORDPRESS = re.compile(r"^/(?:wp-admin|wp-login\.php|wp-content|xmlrpc\.php)(?:/|$)")
_PHP = re.compile(r"\.(?:php[3-8]?|phtml)(?:/|$)")
_SECRET = re.compile(r"^/(?:\.env(?:\.|$)|\.git(?:/|$))")
_ADMIN = re.compile(r"^/(?:phpmyadmin|server-status|solr|jenkins)(?:/|$)")


def _digest(*values):
    payload = json.dumps(
        values, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _observed(value):
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(datetime.timezone.utc)


def _window(value, minutes):
    minute = value.minute - (value.minute % minutes)
    start = value.replace(minute=minute, second=0, microsecond=0)
    return start, start + datetime.timedelta(minutes=minutes)


def _source_network(value):
    try:
        address = ipaddress.ip_address(str(value))
    except ValueError:
        return "unknown"
    prefix = 24 if address.version == 4 else 64
    return str(ipaddress.ip_network(f"{address}/{prefix}", strict=False))


def _web_family(path):
    value = str(path or "").lower()[:512]
    if _WORDPRESS.search(value):
        return "wordpress"
    if _SECRET.search(value):
        return "secret_files"
    if _ADMIN.search(value):
        return "admin_tools"
    if _PHP.search(value):
        return "php_runtime"
    return "other_probe"


def _fim_tier(path):
    value = str(path or "")[:512]
    for prefix, tier in (
            ("/etc/", "system_config"), ("/usr/", "system_binary"),
            ("/opt/", "application"), ("/var/www/", "web_content"),
            ("/var/lib/", "service_state")):
        if value.startswith(prefix):
            return tier
    return "other_protected"


def _safe_expected(attributes):
    value = attributes.get("expected_change")
    if not isinstance(value, dict):
        return None
    v1_fields = {"deployment_id", "expires_at"}
    v2_fields = v1_fields | {"operation_id", "operation_kind", "completed_at"}
    if set(value) not in (v1_fields, v2_fields):
        return None
    deployment = value.get("deployment_id")
    operation = value.get("operation_kind", "deployment")
    if (not isinstance(deployment, str) or not _TOKEN.fullmatch(deployment) or
            not isinstance(operation, str) or not _TOKEN.fullmatch(operation)):
        return None
    expires = _observed(value.get("expires_at"))
    if expires is None:
        return None
    if set(value) == v2_fields:
        if (not isinstance(value.get("operation_id"), str) or
                not _TOKEN.fullmatch(value["operation_id"])):
            return None
        completed = _observed(value.get("completed_at"))
        if completed is None or completed > expires:
            return None
    return {"deployment_id": deployment, "operation_kind": operation}


def _case_input(receipt, sensor_event):
    from . import mojosec_evidence

    kind = sensor_event.get("kind")
    attributes = sensor_event.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {}
    observed = _observed(sensor_event.get("last_seen"))
    if observed is None:
        return None
    count = sensor_event.get("count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        return None

    if kind in ("web.probe", "web.denied", "web.error"):
        projection = mojosec_evidence.project(
            kind, attributes, count, sensor_event.get("last_seen"))
        projected = projection["evidence"]
        resource_id = projected.get("resource_id", "")
        response_class = projected.get("response_class", "")
        policy_version = projected.get("edge_policy_version")
        trusted = bool(
            re.fullmatch(r"vhost:(?:0|[1-9][0-9]{0,19})", resource_id) and
            response_class in (
                "impossible_path", "redirect", "reverse_proxy", "site_api",
                "spa_fallback", "static_site") and
            isinstance(policy_version, int))
        family = _web_family(projected.get("path"))
        network = _source_network(projection.get("source_ip"))
        start, end = _window(observed, 60)
        is_redirect = (
            projected.get("scheme") == "http" and projected.get("status") == 301)
        if not trusted:
            urgency, reason = "info", "unknown_edge_evidence"
        elif response_class == "impossible_path":
            urgency, reason = "warning", "trusted_impossible_path"
        else:
            urgency, reason = "info", "trusted_bounded_response"
        sample = {
            "family": family,
            "network": network,
            "resource_id": resource_id or "unknown",
            "response_class": response_class or "unknown",
            "path_shape_sha256": _digest(projected.get("path", "/")),
        }
        return {
            "sensor_kind": "web", "family": family, "network": network,
            "resource_id": resource_id or "unknown", "policy_version": policy_version or 1,
            "observed": observed, "window_start": start, "window_end": end,
            "occurrences": count, "projected_events": 0 if is_redirect else 1,
            "urgency": urgency, "urgency_reason": reason, "sample": sample,
            "sample_key": _digest(family, network, resource_id, projected.get("path", "/")),
            "correlation_key": _digest("web", family, network, resource_id or "unknown"),
        }

    if kind in ("fim.change", "fim.overflow", "fim.expected_change_error"):
        start, end = _window(observed, 15)
        tier = _fim_tier(attributes.get("path"))
        expected = _safe_expected(attributes)
        if kind == "fim.overflow":
            family = "overflow"
            urgency, reason = "critical", "fim_collector_overflow"
        elif expected:
            family = f"trusted_{expected['operation_kind']}"[:64]
            urgency, reason = "info", "trusted_deployment_change"
        else:
            family = tier
            urgency, reason = "high", "unexplained_protected_change"
        deployment = expected["deployment_id"] if expected else "untrusted"
        sample = {
            "family": family, "protected_tier": tier,
            "operation": expected["operation_kind"] if expected else "unknown",
        }
        return {
            "sensor_kind": "fim", "family": family, "network": "",
            "resource_id": f"installation:{receipt.api_key_id}", "policy_version": 1,
            "observed": observed, "window_start": start, "window_end": end,
            "occurrences": count, "projected_events": 1,
            "urgency": urgency, "urgency_reason": reason, "sample": sample,
            "sample_key": _digest(tier, family, deployment),
            "correlation_key": _digest("fim", tier, family, deployment),
        }
    return None


def _shadow_targets():
    value = settings.get_static("MOJOSEC_CASE_SHADOW_TARGETS", [], kind="list")
    targets = []
    for row in value:
        if not isinstance(row, dict) or not set(row).issubset(
                {"installation_key_id", "vhost_ids", "include_fim"}):
            return []
        key_id = row.get("installation_key_id")
        vhost_ids = row.get("vhost_ids", [])
        include_fim = row.get("include_fim", False)
        if (not isinstance(key_id, int) or isinstance(key_id, bool) or key_id < 1 or
                not isinstance(vhost_ids, list) or len(vhost_ids) > 32 or
                any(not isinstance(item, int) or isinstance(item, bool) or item < 1
                    for item in vhost_ids) or
                not isinstance(include_fim, bool)):
            return []
        targets.append({
            "installation_key_id": key_id,
            "vhost_ids": set(vhost_ids),
            "include_fim": include_fim,
        })
    return targets


def shadow_enabled(receipt, sensor_event):
    kind = sensor_event.get("kind")
    resource = ""
    attributes = sensor_event.get("attributes")
    if isinstance(attributes, dict):
        resource = attributes.get("resource_id", "")
    match = re.fullmatch(r"vhost:(?P<id>[1-9][0-9]{0,19})", str(resource))
    vhost_id = int(match.group("id")) if match else None
    for target in _shadow_targets():
        if target["installation_key_id"] != receipt.api_key_id:
            continue
        if kind and kind.startswith("fim.") and target["include_fim"]:
            return True
        if kind and kind.startswith("web.") and vhost_id in target["vhost_ids"]:
            return True
    return False


def _record_metric(slug, count=1):
    try:
        metrics.record(
            f"mojosec:shadow:{slug}", count=count, category="mojosec_shadow",
            account="incident")
    except Exception:
        logger.exception("MojoSec shadow metric failed for %s", slug)


def contribute(receipt, sensor_event):
    """Contribute one receipt at most once; shadow failures never own ingestion."""
    from mojo.apps.incident.models import (
        MojoSecCase, MojoSecCaseTransition, MojoSecReceipt)

    if not shadow_enabled(receipt, sensor_event):
        return None, False
    normalized = _case_input(receipt, sensor_event)
    if normalized is None:
        _record_metric("failures")
        return None, False
    window_key = _digest(
        normalized["window_start"].isoformat(), normalized["window_end"].isoformat())
    with transaction.atomic():
        locked_receipt = MojoSecReceipt.objects.select_for_update().get(pk=receipt.pk)
        if locked_receipt.case_contributed_at is not None:
            return locked_receipt.mojosec_case, False
        case, created = MojoSecCase.objects.get_or_create(
            installation_key_id=locked_receipt.api_key_id,
            correlation_key=normalized["correlation_key"],
            window_key=window_key,
            defaults={
                "group_id": locked_receipt.api_key.group_id,
                "sensor_id": locked_receipt.sensor_id,
                "sensor_kind": normalized["sensor_kind"],
                "resource_id": normalized["resource_id"],
                "family": normalized["family"],
                "network": normalized["network"],
                "window_start": normalized["window_start"],
                "window_end": normalized["window_end"],
                "first_seen": normalized["observed"],
                "last_seen": normalized["observed"],
                "policy_version": normalized["policy_version"],
                "evaluator_version": EVALUATOR_VERSION,
                "urgency": normalized["urgency"],
                "urgency_reason": normalized["urgency_reason"],
            })
        case = MojoSecCase.objects.select_for_update().get(pk=case.pk)
        from_state = case.state
        from_urgency = case.urgency
        is_distinct = not MojoSecReceipt.objects.filter(
            mojosec_case=case, case_sample_key=normalized["sample_key"]).exists()
        samples = list(case.samples) if isinstance(case.samples, list) else []
        if is_distinct and len(samples) < MAX_SAMPLES:
            samples.append(normalized["sample"])
            case.sample_count = len(samples)
        elif is_distinct:
            case.overflow_count += 1
        case.samples = samples[:MAX_SAMPLES]
        case.occurrence_count += normalized["occurrences"]
        case.receipt_count += 1
        case.projected_event_count += normalized["projected_events"]
        case.distinct_count += 1 if is_distinct else 0
        case.first_seen = min(case.first_seen, normalized["observed"])
        case.last_seen = max(case.last_seen, normalized["observed"])
        urgency_rank = {"info": 0, "warning": 1, "high": 2, "critical": 3}
        if urgency_rank[normalized["urgency"]] > urgency_rank[case.urgency]:
            case.urgency = normalized["urgency"]
            case.urgency_reason = normalized["urgency_reason"]
        if normalized["urgency"] in ("high", "critical"):
            case.state = MojoSecCase.STATE_ELEVATED
            case.state_reason = normalized["urgency_reason"]
        elif case.occurrence_count >= 1000 and normalized["urgency"] == "warning":
            case.state = MojoSecCase.STATE_ELEVATED
            case.state_reason = "sustained_trusted_impossible_paths"
            case.urgency = "high"
            case.urgency_reason = "sustained_trusted_impossible_paths"
        case.save()
        locked_receipt.mojosec_case = case
        locked_receipt.case_sample_key = normalized["sample_key"]
        locked_receipt.case_contributed_at = dates.utcnow()
        locked_receipt.save(update_fields=[
            "mojosec_case", "case_sample_key", "case_contributed_at", "modified"])
        transition = "opened" if created else "updated"
        if from_state != case.state or from_urgency != case.urgency:
            transition = "promoted"
        MojoSecCaseTransition.objects.create(
            case=case, receipt=locked_receipt,
            receipt_id_snapshot=locked_receipt.pk, transition=transition,
            reason=case.urgency_reason, from_state="" if created else from_state,
            to_state=case.state, from_urgency="" if created else from_urgency,
            to_urgency=case.urgency, occurrence_count=case.occurrence_count,
            receipt_count=case.receipt_count,
            projected_event_count=case.projected_event_count,
            distinct_count=case.distinct_count, sample_count=case.sample_count,
            overflow_count=case.overflow_count, policy_version=case.policy_version,
            evaluator_version=case.evaluator_version)
    _record_metric("receipts")
    _record_metric("occurrences", normalized["occurrences"])
    _record_metric(
        "compressed_occurrences",
        max(0, normalized["occurrences"] - normalized["projected_events"]))
    _record_metric("cases_opened" if created else "cases_updated")
    _record_metric(f"urgency:{case.urgency}")
    if transition == "promoted":
        _record_metric("promotions")
    if case.overflow_count:
        _record_metric("overflow")
    return case, True
