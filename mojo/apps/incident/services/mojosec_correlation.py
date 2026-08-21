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
EVALUATOR_VERSION = 2
MAX_SAMPLES = 8
MAX_BREAKDOWN_OPERATIONS = 16
MAX_BREAKDOWN_TIERS = 8
DEFAULT_FUTURE_SKEW_SECONDS = 300
MAX_FUTURE_SKEW_SECONDS = 3600
DEFAULT_DEPLOY_QUIET_SECONDS = 60
MIN_DEPLOY_QUIET_SECONDS = 10
MAX_DEPLOY_QUIET_SECONDS = 900
# Mirrors mojo/deploy/mojosec_changes.MAX_TTL_SECONDS: no journal operation
# may promise a longer annotation lifetime, so a wider claim is not trusted.
MAX_EXPECTED_TTL_SECONDS = 900
PROMOTED_CATEGORY = "mojosec.case.promoted"
_URGENCY_RANK = {"": -1, "info": 0, "warning": 1, "high": 2, "critical": 3}
_TOKEN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
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


def future_skew_seconds():
    """Return the bounded static-only clock-skew allowance."""
    value = settings.get_static(
        "MOJOSEC_CASE_FUTURE_SKEW_SECONDS", DEFAULT_FUTURE_SKEW_SECONDS)
    if (not isinstance(value, int) or isinstance(value, bool) or
            not 0 <= value <= MAX_FUTURE_SKEW_SECONDS):
        return 0
    return value


def quiet_seconds():
    """Bounded quiet window after which a deployment case settles."""
    value = settings.get_static(
        "MOJOSEC_DEPLOY_QUIET_SECONDS", DEFAULT_DEPLOY_QUIET_SECONDS)
    if (not isinstance(value, int) or isinstance(value, bool) or
            not MIN_DEPLOY_QUIET_SECONDS <= value <= MAX_DEPLOY_QUIET_SECONDS):
        return DEFAULT_DEPLOY_QUIET_SECONDS
    return value


def _bounded_observed(observed, receipt_time):
    if not isinstance(receipt_time, datetime.datetime) or receipt_time.tzinfo is None:
        receipt_time = dates.utcnow()
    receipt_time = receipt_time.astimezone(datetime.timezone.utc)
    if observed > receipt_time + datetime.timedelta(seconds=future_skew_seconds()):
        return receipt_time
    return observed


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


def _safe_expected(attributes, observed):
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
    ttl_bound = datetime.timedelta(seconds=MAX_EXPECTED_TTL_SECONDS)
    # An annotation that had already expired when the change was observed is
    # not trusted evidence. The journal prunes expired manifest entries, so a
    # well-behaved sensor can never emit one — rejecting is fail-closed with
    # no legitimate loss, and under authoritative routing it is the difference
    # between a quiet deployment case and total central silence.
    if expires < observed:
        return None
    if set(value) == v2_fields:
        if (not isinstance(value.get("operation_id"), str) or
                not _TOKEN.fullmatch(value["operation_id"])):
            return None
        completed = _observed(value.get("completed_at"))
        if completed is None or completed > expires:
            return None
        if expires > completed + ttl_bound:
            return None
    elif expires > observed + ttl_bound:
        return None
    return {"deployment_id": deployment, "operation_kind": operation}


def _case_input(receipt, sensor_event, web_binding=None):
    kind = sensor_event.get("kind")
    attributes = sensor_event.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {}
    observed = _observed(sensor_event.get("last_seen"))
    if observed is None:
        return None
    observed = _bounded_observed(observed, receipt.created)
    count = sensor_event.get("count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        return None

    if kind in ("web.probe", "web.denied", "web.error"):
        if web_binding is None:
            return None
        projection = web_binding["projection"]
        projected = projection["evidence"]
        resource_id = web_binding["resource_id"]
        response_class = projected["response_class"]
        policy_version = web_binding["policy"]["version"]
        family = _web_family(projected.get("path"))
        network = _source_network(projection.get("source_ip"))
        start, end = _window(observed, 60)
        is_redirect = (
            projected.get("scheme") == "http" and projected.get("status") == 301)
        if response_class == "impossible_path":
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
            "correlation_key": _digest(
                "web", family, network, resource_id, policy_version),
            "window_key": _digest(start.isoformat(), end.isoformat()),
        }

    if kind in ("fim.change", "fim.overflow", "fim.expected_change_error"):
        tier = _fim_tier(attributes.get("path"))
        expected = (
            _safe_expected(attributes, observed)
            if kind == "fim.change" else None)
        if expected:
            # Trusted, provenance-backed deployment evidence: one case per
            # sensor + deployment identity per UTC day. The quiet window
            # drives the settled state, never the key, so redelivery and late
            # evidence stay deterministic even for constant-id drivers.
            operation = expected["operation_kind"]
            deployment = expected["deployment_id"]
            return {
                "sensor_kind": "fim", "family": "deployment", "network": "",
                "resource_id": f"installation:{receipt.api_key_id}",
                "policy_version": 1,
                "observed": observed, "window_start": observed,
                "window_end": observed,
                "occurrences": count, "projected_events": 1,
                "urgency": "info", "urgency_reason": "trusted_deployment_change",
                "deployment_id": deployment,
                "breakdown": {"operation": operation, "tier": tier, "count": count},
                "sample": {"operation": operation, "tier": tier},
                "sample_key": _digest("deploy", operation, tier),
                "correlation_key": _digest(
                    "fim.deploy", receipt.sensor_id, deployment),
                "window_key": _digest(
                    "fim.deploy", deployment, observed.date().isoformat()),
            }
        start, end = _window(observed, 15)
        if kind == "fim.overflow":
            family = "overflow"
            urgency, reason = "critical", "fim_collector_overflow"
        else:
            family = tier
            urgency, reason = "high", "unexplained_protected_change"
        sample = {"family": family, "protected_tier": tier, "operation": "unknown"}
        return {
            "sensor_kind": "fim", "family": family, "network": "",
            "resource_id": f"installation:{receipt.api_key_id}", "policy_version": 1,
            "observed": observed, "window_start": start, "window_end": end,
            "occurrences": count, "projected_events": 1,
            "urgency": urgency, "urgency_reason": reason, "sample": sample,
            "sample_key": _digest(tier, family, "untrusted"),
            "correlation_key": _digest("fim", tier, family, "untrusted"),
            "window_key": _digest(start.isoformat(), end.isoformat()),
        }
    return None


def _shadow_targets():
    value = settings.get_static("MOJOSEC_CASE_SHADOW_TARGETS", [], kind="list")
    if len(value) > 32:
        return []
    targets = []
    for row in value:
        if not isinstance(row, dict) or not set(row).issubset(
                {"installation_key_id", "vhost_ids", "include_fim", "mode"}):
            return []
        key_id = row.get("installation_key_id")
        vhost_ids = row.get("vhost_ids", [])
        include_fim = row.get("include_fim", False)
        mode = row.get("mode", "shadow")
        if (not isinstance(key_id, int) or isinstance(key_id, bool) or key_id < 1 or
                not isinstance(vhost_ids, list) or len(vhost_ids) > 32 or
                any(not isinstance(item, int) or isinstance(item, bool) or item < 1
                    for item in vhost_ids) or
                not isinstance(include_fim, bool) or
                mode not in ("shadow", "authoritative")):
            return []
        targets.append({
            "installation_key_id": key_id,
            "vhost_ids": set(vhost_ids),
            "include_fim": include_fim,
            "mode": mode,
        })
    return targets


def installation_mode(installation_key_id):
    """The enrollment mode for one installation, or None when un-enrolled."""
    for target in _shadow_targets():
        if target["installation_key_id"] == installation_key_id:
            return target["mode"]
    return None


def _shadow_binding(receipt, sensor_event):
    return binding_for(receipt.api_key_id, sensor_event)


def binding_for(installation_key_id, sensor_event):
    """Resolve the enrollment binding for one installation's sensor event."""
    from mojo import errors as merrors
    from mojo.apps.edge.models import Vhost
    from mojo.apps.edge import validators
    from . import mojosec_evidence

    kind = sensor_event.get("kind")
    attributes = sensor_event.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {}
    for target in _shadow_targets():
        if target["installation_key_id"] != installation_key_id:
            continue
        if kind and kind.startswith("fim.") and target["include_fim"]:
            return {"kind": "fim", "mode": target["mode"]}
        if not kind or not kind.startswith("web."):
            continue
        projection = mojosec_evidence.project(
            kind, attributes, sensor_event.get("count"),
            sensor_event.get("last_seen"))
        evidence = projection["evidence"]
        resource_id = evidence.get("resource_id", "")
        match = re.fullmatch(r"vhost:(?P<id>[1-9][0-9]{0,19})", resource_id)
        vhost_id = int(match.group("id")) if match else None
        if vhost_id not in target["vhost_ids"]:
            continue
        vhost = Vhost.objects.select_related("domain").filter(
            pk=vhost_id, is_enabled=True,
            domain__group__api_keys__pk=installation_key_id).first()
        if vhost is None:
            return None
        try:
            policy = validators.validate_mojosec_policy(vhost.mojosec_policy)
        except (merrors.ValueException, TypeError, ValueError):
            return None
        family = _web_family(evidence.get("path"))
        response_class = evidence.get("response_class")
        response_matches = (
            response_class == policy.get("response_class") or
            (response_class == "impossible_path" and
             family in policy.get("impossible_path_families", ())))
        if (not policy or evidence.get("edge_policy_version") != policy["version"] or
                not response_matches):
            return None
        return {
            "kind": "web", "mode": target["mode"], "policy": policy,
            "projection": projection, "resource_id": f"vhost:{vhost.pk}",
        }
    return None


def shadow_enabled(receipt, sensor_event):
    return _shadow_binding(receipt, sensor_event) is not None


def _record_metric(slug, count=1):
    try:
        metrics.record(
            f"mojosec:shadow:{slug}", count=count, category="mojosec_shadow",
            account="incident")
    except Exception:
        logger.exception("MojoSec shadow metric failed for %s", slug)


def _apply_breakdown(case, info):
    """Fold one bounded operation/tier contribution into the deployment case."""
    breakdown = case.breakdown if isinstance(case.breakdown, dict) else {}
    operations = breakdown.get("operations")
    if not isinstance(operations, dict):
        operations = {}
    key = info["operation"]
    if key not in operations and len(operations) >= MAX_BREAKDOWN_OPERATIONS:
        key = "_other"
    operations[key] = operations.get(key, 0) + info["count"]
    tiers = breakdown.get("tiers")
    if not isinstance(tiers, dict):
        tiers = {}
    tier_key = info["tier"]
    if tier_key not in tiers and len(tiers) >= MAX_BREAKDOWN_TIERS:
        tier_key = "_other"
    tiers[tier_key] = tiers.get(tier_key, 0) + info["count"]
    case.breakdown = {"operations": operations, "tiers": tiers}


def contribute(receipt, sensor_event):
    """Contribute one receipt at most once.

    In shadow mode failures never own ingestion; for a case-routed receipt the
    caller treats this as the authoritative publication act and acks from it.
    """
    from mojo.apps.incident.models import (
        MojoSecCase, MojoSecCaseTransition, MojoSecReceipt)

    binding = _shadow_binding(receipt, sensor_event)
    if binding is None:
        return None, False
    normalized = _case_input(
        receipt, sensor_event,
        web_binding=binding if binding["kind"] == "web" else None)
    if normalized is None:
        _record_metric("failures")
        return None, False
    is_deployment = normalized["family"] == "deployment"
    with transaction.atomic():
        locked_receipt = MojoSecReceipt.objects.select_for_update().get(pk=receipt.pk)
        if locked_receipt.case_contributed_at is not None:
            return locked_receipt.mojosec_case, False
        # A case-routed receipt projects no per-receipt Event, so its
        # contribution never counts one; the legacy path still projects and
        # keeps the counter meaning "Events actually projected".
        projected_events = (
            0 if locked_receipt.case_routed else normalized["projected_events"])
        case, created = MojoSecCase.objects.get_or_create(
            installation_key_id=locked_receipt.api_key_id,
            correlation_key=normalized["correlation_key"],
            window_key=normalized["window_key"],
            defaults={
                "group_id": locked_receipt.api_key.group_id,
                "sensor_id": locked_receipt.sensor_id,
                "sensor_kind": normalized["sensor_kind"],
                "resource_id": normalized["resource_id"],
                "family": normalized["family"],
                "network": normalized["network"],
                "deployment_id": normalized.get("deployment_id", ""),
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
        case.projected_event_count += projected_events
        case.distinct_count += 1 if is_distinct else 0
        case.first_seen = min(case.first_seen, normalized["observed"])
        case.last_seen = max(case.last_seen, normalized["observed"])
        reopened = False
        if is_deployment:
            _apply_breakdown(case, normalized["breakdown"])
            case.window_start = min(case.window_start, normalized["observed"])
            case.window_end = max(case.window_end, normalized["observed"])
            if case.state == MojoSecCase.STATE_SETTLED:
                # A genuinely new receipt after the quiet window reopens the
                # deployment summary; redeliveries short-circuited above.
                case.state = MojoSecCase.STATE_OBSERVING
                case.state_reason = "deployment_reopened"
                case.settled_at = None
                reopened = True
        if _URGENCY_RANK[normalized["urgency"]] > _URGENCY_RANK[case.urgency]:
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
            transition = "reopened" if reopened else "promoted"
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
        if (binding.get("mode") == "authoritative" and
                _URGENCY_RANK[case.urgency] > _URGENCY_RANK.get(
                    case.projected_urgency, -1) and
                case.urgency in ("high", "critical")):
            case_id = case.pk
            transaction.on_commit(lambda: _project_case(case_id))
    _record_metric("receipts")
    _record_metric("occurrences", normalized["occurrences"])
    _record_metric(
        "compressed_occurrences",
        max(0, normalized["occurrences"] - projected_events))
    _record_metric("cases_opened" if created else "cases_updated")
    if is_deployment and created:
        _record_metric("deploy_cases_opened")
    _record_metric(f"urgency:{case.urgency}")
    if transition == "promoted":
        _record_metric("promotions")
    if case.overflow_count:
        _record_metric("overflow")
    return case, True


def _projection_prefix(case_id, urgency):
    return f"mojosec-case:{case_id}:{urgency}"


def _dispatch_projection(case, event):
    """Idempotently queue the promoted Event's RuleSet handlers, at most once.

    Mirrors the receipt outbox discipline: strict dispatch with a stable
    idempotency prefix, and `projection_dispatched_at` records durable
    queueing so the sweep can retry without double-dispatching.
    """
    from mojo.apps.incident.models import MojoSecCase

    dispatched = False
    try:
        incident = event.incident if event.incident_id else None
        rule_set = incident.rule_set if incident is not None else None
        if rule_set is None or not rule_set.handler:
            dispatched = True
        else:
            dispatched = rule_set.run_handler(
                event, incident,
                idempotency_prefix=_projection_prefix(case.pk, case.urgency),
                strict=True)
    except Exception:
        logger.exception(
            "MojoSec case projection dispatch failed for case %s", case.pk)
    if dispatched:
        MojoSecCase.objects.filter(pk=case.pk).update(
            projection_dispatched_at=dates.utcnow(), modified=dates.utcnow())
    return dispatched


def _project_case(case_id):
    """Project one deliberate case-level Event for a high/critical promotion.

    At most one Event per upward urgency step, enforced by the
    `projected_urgency` ratchet under the case row lock; safe to call from
    both the post-commit hook and the sweep. Shadow-mode installations never
    project.
    """
    from mojo.apps.incident.models import Event, MojoSecCase, MojoSecCaseTransition

    try:
        with transaction.atomic():
            case = MojoSecCase.objects.select_for_update().get(pk=case_id)
            if case.urgency not in ("high", "critical"):
                return None
            if (_URGENCY_RANK[case.urgency] <=
                    _URGENCY_RANK.get(case.projected_urgency, -1)):
                return None
            if installation_mode(case.installation_key_id) != "authoritative":
                return None
            level = 12 if case.urgency == "critical" else 8
            event = Event(
                category=PROMOTED_CATEGORY,
                scope="mojosec",
                level=level,
                source_ip=None,
                title=(
                    f"MojoSec case promoted to {case.urgency}: "
                    f"{case.family}")[:256],
                details=(
                    f"MojoSec case {case.pk} ({case.sensor_kind}/{case.family}) "
                    f"was promoted to {case.urgency}: {case.urgency_reason}. "
                    f"{case.occurrence_count} occurrence(s) across "
                    f"{case.receipt_count} receipt(s)."
                ),
                model_name="mojosec_case",
                model_id=case.pk,
                metadata={
                    "mojosec_case": {
                        "case_id": case.pk,
                        "sensor_kind": case.sensor_kind,
                        "family": case.family,
                        "network": case.network,
                        "resource_id": case.resource_id,
                        "urgency": case.urgency,
                        "reason": case.urgency_reason,
                        "occurrence_count": case.occurrence_count,
                        "receipt_count": case.receipt_count,
                    },
                },
            )
            event.sync_metadata()
            event.save()
            publication = event.publish(
                use_catchall=False,
                dispatch_handlers=False,
                allow_default_llm=False,
                exact_category=True,
            )
            from_projected = case.projected_urgency
            case.projected_urgency = case.urgency
            case.projected_event_count += 1
            # Null until dispatch is durably queued; None also when there is
            # nothing to dispatch — resolved right after this transaction.
            case.projection_dispatched_at = None
            case.save(update_fields=[
                "projected_urgency", "projected_event_count",
                "projection_dispatched_at", "modified"])
            MojoSecCaseTransition.objects.create(
                case=case, receipt=None, receipt_id_snapshot=0,
                transition="projection", reason=case.urgency_reason,
                from_state=case.state, to_state=case.state,
                from_urgency=from_projected, to_urgency=case.urgency,
                occurrence_count=case.occurrence_count,
                receipt_count=case.receipt_count,
                projected_event_count=case.projected_event_count,
                distinct_count=case.distinct_count,
                sample_count=case.sample_count,
                overflow_count=case.overflow_count,
                policy_version=case.policy_version,
                evaluator_version=case.evaluator_version,
                projected_event=event,
                projected_event_id_snapshot=event.pk)
    except Exception:
        logger.exception("MojoSec case projection failed for case %s", case_id)
        _record_metric("projection_failures")
        return None
    _record_metric("case_events_projected")
    if publication["should_dispatch"]:
        _dispatch_projection(case, event)
    else:
        from mojo.apps.incident.models import MojoSecCase as CaseModel
        CaseModel.objects.filter(pk=case.pk).update(
            projection_dispatched_at=dates.utcnow(), modified=dates.utcnow())
    return event


def settle_sweep(job=None, now=None, limit=500):
    """Settle quiet deployment cases and heal projection after a crash.

    Runs every few minutes from cron. Settling is a system transition and
    never an Event — real deploys stay invisible to operators.
    """
    from django.db.models import F
    from mojo.apps.incident.models import MojoSecCase, MojoSecCaseTransition

    now = now or dates.utcnow()
    cutoff = now - datetime.timedelta(seconds=quiet_seconds())
    settled = 0
    candidates = list(MojoSecCase.objects.filter(
        sensor_kind="fim", family="deployment",
        state=MojoSecCase.STATE_OBSERVING,
        last_seen__lt=cutoff).values_list("pk", flat=True)[:limit])
    for case_id in candidates:
        with transaction.atomic():
            case = MojoSecCase.objects.select_for_update().get(pk=case_id)
            if (case.state != MojoSecCase.STATE_OBSERVING or
                    case.last_seen >= cutoff):
                continue
            case.state = MojoSecCase.STATE_SETTLED
            case.state_reason = "deployment_quiet_window"
            case.settled_at = now
            case.save(update_fields=[
                "state", "state_reason", "settled_at", "modified"])
            MojoSecCaseTransition.objects.create(
                case=case, receipt=None, receipt_id_snapshot=0,
                transition="settled", reason="deployment_quiet_window",
                from_state=MojoSecCase.STATE_OBSERVING,
                to_state=MojoSecCase.STATE_SETTLED,
                from_urgency=case.urgency, to_urgency=case.urgency,
                occurrence_count=case.occurrence_count,
                receipt_count=case.receipt_count,
                projected_event_count=case.projected_event_count,
                distinct_count=case.distinct_count,
                sample_count=case.sample_count,
                overflow_count=case.overflow_count,
                policy_version=case.policy_version,
                evaluator_version=case.evaluator_version)
        settled += 1
        _record_metric("deploy_cases_settled")
    # Catch-up work only exists for authoritative installations; scoping the
    # query keeps every shadow-mode elevated case out of the batch instead of
    # re-resolving the enrollment per case, forever.
    authoritative_keys = [
        target["installation_key_id"] for target in _shadow_targets()
        if target["mode"] == "authoritative"]
    projected = 0
    catchup = list(MojoSecCase.objects.filter(
        installation_key_id__in=authoritative_keys,
        state=MojoSecCase.STATE_ELEVATED,
        urgency__in=("high", "critical")).exclude(
        projected_urgency=F("urgency")).values_list(
        "pk", flat=True)[:limit]) if authoritative_keys else []
    for case_id in catchup:
        if _project_case(case_id) is not None:
            projected += 1
    redispatched = 0
    stale_dispatch = MojoSecCase.objects.filter(
        installation_key_id__in=authoritative_keys,
        state=MojoSecCase.STATE_ELEVATED,
        urgency=F("projected_urgency"),
        projection_dispatched_at__isnull=True)[:limit] if authoritative_keys else []
    for case in stale_dispatch:
        transition = MojoSecCaseTransition.objects.filter(
            case=case, transition="projection").order_by(
            "-created", "-id").select_related("projected_event").first()
        if transition is None:
            continue
        event = transition.projected_event
        if event is None:
            # The projected Event was pruned before dispatch could be
            # confirmed; there is nothing left to hand the handlers.
            MojoSecCase.objects.filter(pk=case.pk).update(
                projection_dispatched_at=dates.utcnow(), modified=dates.utcnow())
            continue
        if _dispatch_projection(case, event):
            redispatched += 1
    from . import mojosec as mojosec_service
    retried = mojosec_service.retry_case_routed(now=now, limit=limit)
    if job is not None and hasattr(job, "add_log"):
        job.add_log(
            f"Settled {settled} deployment case(s); projected {projected}; "
            f"re-dispatched {redispatched}; retried {retried} case-routed "
            "receipt(s)")
    return {
        "settled": settled, "projected": projected,
        "redispatched": redispatched, "retried": retried,
    }
