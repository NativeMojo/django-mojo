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
EVALUATOR_VERSION = 3
MAX_SAMPLES = 8
MAX_BREAKDOWN_OPERATIONS = 16
MAX_BREAKDOWN_TIERS = 8
MAX_CASE_SOURCES = 64
MAX_CORROBORATING_CASES = 8
DEFAULT_FUTURE_SKEW_SECONDS = 300
MAX_FUTURE_SKEW_SECONDS = 3600
DEFAULT_DEPLOY_QUIET_SECONDS = 60
MIN_DEPLOY_QUIET_SECONDS = 10
MAX_DEPLOY_QUIET_SECONDS = 900
# Auth/privilege and service/host evidence the sensor already ships; these
# are the only non-web/fim kinds enrollment may bind. session_open is
# local-only in practice and carries no correlation value.
HOST_KINDS = frozenset((
    "auth.ssh_failure", "auth.ssh_login", "auth.sudo_command",
    "auth.sudo_failure", "system.oom", "system.service_error"))
# Digest tier under authoritative routing; oom (critical) and sudo_failure
# (weak evidence) keep their immediate per-receipt Events and only contribute.
ROUTABLE_HOST_KINDS = frozenset((
    "auth.ssh_failure", "auth.ssh_login", "auth.sudo_command",
    "system.service_error"))
# Reasons that qualify a high/critical case to look for corroborating
# evidence from other sensor kinds on the same node.
CORROBORATION_REASONS = frozenset((
    "unexplained_protected_change", "ssh_failure_then_success",
    "service_failure_burst", "fim_collector_overflow",
    "sustained_trusted_impossible_paths"))
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


def _bounded_static(name, default, low, high):
    """Static-only integer setting clamped to its documented bounds."""
    value = settings.get_static(name, default)
    if (not isinstance(value, int) or isinstance(value, bool) or
            not low <= value <= high):
        return default
    return value


def ssh_promote_min_failures():
    return _bounded_static("MOJOSEC_SSH_PROMOTE_MIN_FAILURES", 5, 1, 1000)


def auth_elevate_occurrences():
    return _bounded_static("MOJOSEC_AUTH_ELEVATE_OCCURRENCES", 50, 5, 100000)


def host_elevate_occurrences():
    return _bounded_static("MOJOSEC_HOST_ELEVATE_OCCURRENCES", 10, 2, 100000)


def corroboration_window_seconds():
    return _bounded_static(
        "MOJOSEC_CORROBORATION_WINDOW_SECONDS", 3600, 300, 14400)


def campaign_min_sources():
    return _bounded_static("MOJOSEC_CAMPAIGN_MIN_SOURCES", 10, 3, 256)


def block_min_occurrences():
    return _bounded_static("MOJOSEC_BLOCK_MIN_OCCURRENCES", 12, 3, 10000)


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


def _deployment_registered(installation_key_id, deployment_id, observed):
    from mojo.apps.incident.models import MojoSecDeployment

    return MojoSecDeployment.objects.filter(
        installation_key_id=installation_key_id,
        deployment_id=deployment_id,
        expires_at__gte=observed).exists()


def _case_input(receipt, sensor_event, web_binding=None, fim_binding=None):
    from . import mojosec_evidence

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
            "source_ip": projection.get("source_ip"),
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
        if (expected and fim_binding is not None and
                fim_binding.get("require_registered_deployments") and
                not _deployment_registered(
                    receipt.api_key_id, expected["deployment_id"], observed)):
            # The annotation is well-formed but its deployment identity was
            # never pre-registered centrally. Under this opt-in trust bound
            # the sensor's root assertion alone is not enough — the change is
            # unexplained, immediate evidence, never a quiet deployment.
            expected = None
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

    if kind in HOST_KINDS:
        # Bounded, scrubbed fields only — the same projection the per-receipt
        # Event path uses. Missing fields degrade to "unknown"; a routed
        # receipt must never fail contribution on a field gap.
        projection = mojosec_evidence.project(
            kind, attributes, count, sensor_event.get("last_seen"))
        evidence = projection["evidence"]
        source_ip = projection["source_ip"]
        start, end = _window(observed, 60)
        window_key = _digest(start.isoformat(), end.isoformat())
        base = {
            "policy_version": 1, "observed": observed,
            "window_start": start, "window_end": end,
            "occurrences": count, "projected_events": 1,
            "window_key": window_key, "wire_kind": kind,
        }
        if kind in ("auth.ssh_failure", "auth.ssh_login"):
            user = evidence.get("user") or "unknown"
            ip_key = source_ip or "unknown"
            if kind == "auth.ssh_failure":
                urgency, reason = "info", "ssh_failures_observed"
            else:
                urgency, reason = "info", "ssh_login"
            base.update({
                "sensor_kind": "auth", "family": "ssh",
                # Exact IP in the key; the /24 bucket is display only. A
                # same-subnet admin must never share a case with an attacker.
                "network": _source_network(ip_key),
                "resource_id": f"user:{user}"[:96],
                "urgency": urgency, "urgency_reason": reason,
                "source_ip": source_ip,
                "breakdown": {"operation": kind, "tier": "auth", "count": count},
                "sample": {"kind": kind, "user": user,
                           "network": _source_network(ip_key)},
                "sample_key": _digest(kind, ip_key, user),
                "correlation_key": _digest(
                    "auth.ssh", receipt.sensor_id, ip_key, user),
            })
            return base
        if kind == "auth.sudo_command":
            actor = evidence.get("actor") or "unknown"
            target_user = evidence.get("target_user") or "unknown"
            sample = {"kind": kind, "actor": actor, "target_user": target_user}
            # Bounded identity only — never the exact command text, which
            # stays on the receipt/Event for security-admin drill-down.
            for field in ("command_path", "command_sha256"):
                if evidence.get(field):
                    sample[field] = evidence[field]
            base.update({
                "sensor_kind": "auth", "family": "sudo",
                "network": "", "resource_id": f"user:{actor}"[:96],
                "urgency": "info", "urgency_reason": "sudo_session",
                "source_ip": source_ip,
                "breakdown": {"operation": kind, "tier": "auth", "count": count},
                "sample": sample,
                "sample_key": _digest(
                    kind, actor, target_user,
                    evidence.get("command_sha256", "")),
                "correlation_key": _digest(
                    "auth.sudo", receipt.sensor_id, actor, target_user),
            })
            return base
        if kind == "auth.sudo_failure":
            # The sensor never populates actor/source on this kind (its
            # fingerprint is bounded message text) — per-sensor keying is the
            # only honest correlation available.
            base.update({
                "sensor_kind": "auth", "family": "sudo_failure",
                "network": "",
                "resource_id": f"installation:{receipt.api_key_id}",
                "urgency": "warning", "urgency_reason": "sudo_failures_observed",
                "breakdown": {"operation": kind, "tier": "auth", "count": count},
                "sample": {"kind": kind},
                "sample_key": _digest(kind, receipt.sensor_id),
                "correlation_key": _digest(
                    "auth.sudo_failure", receipt.sensor_id),
            })
            return base
        unit = evidence.get("unit") or "unknown"
        if kind == "system.service_error":
            failure_kind = evidence.get("failure_kind") or "unknown"
            base.update({
                "sensor_kind": "host", "family": "service",
                "network": "",
                "resource_id": f"installation:{receipt.api_key_id}",
                "urgency": "warning", "urgency_reason": "service_failure",
                "breakdown": {"operation": unit[:96], "tier": failure_kind[:64],
                              "count": count},
                "sample": {"kind": kind, "unit": unit,
                           "failure_kind": failure_kind},
                "sample_key": _digest(kind, unit, failure_kind),
                "correlation_key": _digest(
                    "host.service", receipt.sensor_id, unit, failure_kind),
            })
            return base
        # system.oom — kernel-transport only; always an immediate
        # per-receipt Event as well, so this only accumulates case state.
        base.update({
            "sensor_kind": "host", "family": "oom",
            "network": "",
            "resource_id": f"installation:{receipt.api_key_id}",
            "urgency": "critical", "urgency_reason": "oom_kill",
            "breakdown": {"operation": unit[:96], "tier": "oom", "count": count},
            "sample": {"kind": kind, "unit": unit},
            "sample_key": _digest(kind, unit),
            "correlation_key": _digest("host.oom", receipt.sensor_id),
        })
        return base
    return None


def _shadow_targets():
    value = settings.get_static("MOJOSEC_CASE_SHADOW_TARGETS", [], kind="list")
    if len(value) > 32:
        return []
    targets = []
    for row in value:
        if not isinstance(row, dict) or not set(row).issubset(
                {"installation_key_id", "vhost_ids", "include_fim", "mode",
                 "include_host", "require_registered_deployments"}):
            return []
        key_id = row.get("installation_key_id")
        vhost_ids = row.get("vhost_ids", [])
        include_fim = row.get("include_fim", False)
        include_host = row.get("include_host", False)
        require_registered = row.get("require_registered_deployments", False)
        mode = row.get("mode", "shadow")
        if (not isinstance(key_id, int) or isinstance(key_id, bool) or key_id < 1 or
                not isinstance(vhost_ids, list) or len(vhost_ids) > 32 or
                any(not isinstance(item, int) or isinstance(item, bool) or item < 1
                    for item in vhost_ids) or
                not isinstance(include_fim, bool) or
                not isinstance(include_host, bool) or
                not isinstance(require_registered, bool) or
                mode not in ("shadow", "authoritative")):
            return []
        targets.append({
            "installation_key_id": key_id,
            "vhost_ids": set(vhost_ids),
            "include_fim": include_fim,
            "include_host": include_host,
            "require_registered_deployments": require_registered,
            "mode": mode,
        })
    return targets


def installation_enrollment(installation_key_id):
    """The full enrollment row for one installation, or None."""
    for target in _shadow_targets():
        if target["installation_key_id"] == installation_key_id:
            return target
    return None


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
            return {
                "kind": "fim", "mode": target["mode"],
                "require_registered_deployments":
                    target["require_registered_deployments"],
            }
        if kind in HOST_KINDS and target["include_host"]:
            return {"kind": "host", "mode": target["mode"]}
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


def _breakdown_failures(case_or_breakdown):
    breakdown = getattr(case_or_breakdown, "breakdown", case_or_breakdown)
    if not isinstance(breakdown, dict):
        return 0
    operations = breakdown.get("operations")
    if not isinstance(operations, dict):
        return 0
    value = operations.get("auth.ssh_failure", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _apply_ssh_login_promotion(case, normalized):
    """Promotion rules for one successful-login contribution, in-row.

    Exact progression — this case's own IP (or its previous hourly window
    twin) already failed >= threshold times — is critical and the only
    block-eligible signal. Account-level progression (same account, other
    IPs on this node) pages at high and never proposes enforcement: the
    success may be the legitimate admin arriving mid-attack.
    """
    from mojo.apps.incident.models import MojoSecCase

    threshold = ssh_promote_min_failures()
    own_failures = _breakdown_failures(case)
    if own_failures < threshold:
        prev_start = normalized["window_start"] - datetime.timedelta(hours=1)
        prev_key = _digest(
            prev_start.isoformat(), normalized["window_start"].isoformat())
        twin = MojoSecCase.objects.filter(
            installation_key_id=case.installation_key_id,
            correlation_key=case.correlation_key,
            window_key=prev_key).first()
        if twin is not None:
            own_failures += _breakdown_failures(twin)
    if own_failures >= threshold:
        if _URGENCY_RANK[case.urgency] < _URGENCY_RANK["critical"]:
            case.urgency = "critical"
            case.urgency_reason = "ssh_failure_then_success"
        return
    horizon = normalized["observed"] - datetime.timedelta(hours=2)
    others = MojoSecCase.objects.filter(
        installation_key_id=case.installation_key_id,
        sensor_id=case.sensor_id, sensor_kind="auth", family="ssh",
        resource_id=case.resource_id,
        last_seen__gte=horizon).exclude(pk=case.pk)[:32]
    account_failures = sum(_breakdown_failures(row) for row in others)
    if (account_failures >= threshold and
            _URGENCY_RANK[case.urgency] < _URGENCY_RANK["high"]):
        case.urgency = "high"
        case.urgency_reason = "ssh_login_during_failure_burst"


def _corroborate_case(case_id):
    """Cross-kind promotion: bounded, indexed, at most once per case.

    Runs post-commit after a qualifying high/critical contribution. Promotes
    the triggering case to critical when >= 2 distinct sensor kinds show
    warning+ evidence on the same node inside the corroboration window and
    an fim-untrusted or auth participant anchors it.
    """
    from mojo.apps.incident.models import MojoSecCase, MojoSecCaseTransition

    promoted = False
    try:
        with transaction.atomic():
            case = MojoSecCase.objects.select_for_update().get(pk=case_id)
            breakdown = case.breakdown if isinstance(case.breakdown, dict) else {}
            if (case.urgency not in ("high", "critical") or
                    case.urgency_reason == "corroborated_compromise" or
                    "corroborated_with" in breakdown or
                    case.projected_urgency == "critical" or
                    case.sensor_kind == "campaign"):
                return False
            window = datetime.timedelta(seconds=corroboration_window_seconds())
            # Only high/critical evidence corroborates. Warning-level noise
            # (background scanner cases above all) is near-constant on any
            # exposed node; letting it anchor would escalate every
            # unannotated change to critical. The window is two-sided so
            # late-arriving evidence never pairs across distant hours.
            neighbors = list(
                MojoSecCase.objects.filter(
                    installation_key_id=case.installation_key_id,
                    sensor_id=case.sensor_id,
                    last_seen__gte=case.last_seen - window,
                    last_seen__lte=case.last_seen + window,
                    urgency__in=("high", "critical"),
                ).exclude(pk=case.pk).exclude(sensor_kind="campaign")
                .exclude(family="deployment")[:32])
            participants = [case] + neighbors
            if len({p.sensor_kind for p in participants}) < 2:
                return False
            anchored = any(
                p.sensor_kind == "auth" or
                (p.sensor_kind == "fim" and p.family != "deployment")
                for p in participants)
            if not anchored:
                return False
            from_state = case.state
            from_urgency = case.urgency
            case.urgency = "critical"
            case.urgency_reason = "corroborated_compromise"
            case.state = MojoSecCase.STATE_ELEVATED
            case.state_reason = "corroborated_compromise"
            breakdown["corroborated_with"] = [
                row.pk for row in neighbors][:MAX_CORROBORATING_CASES]
            case.breakdown = breakdown
            case.save(update_fields=[
                "urgency", "urgency_reason", "state", "state_reason",
                "breakdown", "modified"])
            MojoSecCaseTransition.objects.create(
                case=case, receipt=None, receipt_id_snapshot=0,
                transition="promoted", reason="corroborated_compromise",
                from_state=from_state, to_state=case.state,
                from_urgency=from_urgency, to_urgency=case.urgency,
                occurrence_count=case.occurrence_count,
                receipt_count=case.receipt_count,
                projected_event_count=case.projected_event_count,
                distinct_count=case.distinct_count,
                sample_count=case.sample_count,
                overflow_count=case.overflow_count,
                policy_version=case.policy_version,
                evaluator_version=case.evaluator_version)
            promoted = True
    except MojoSecCase.DoesNotExist:
        return False
    except Exception:
        logger.exception("MojoSec corroboration failed for case %s", case_id)
        _record_metric("failures")
        return False
    if promoted:
        _record_metric("corroborations")
        _project_case(case_id)
    return promoted


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
        web_binding=binding if binding["kind"] == "web" else None,
        fim_binding=binding if binding["kind"] == "fim" else None)
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
        source = normalized.get("source_ip")
        if source:
            sources = list(case.observed_sources) if isinstance(
                case.observed_sources, list) else []
            if source not in sources:
                # Beyond the cap the counter is an upper bound: spilled IPs
                # are not retained, so a spilled repeat counts again. The
                # single-source proposal check (== 1) is far below the cap.
                case.distinct_source_count += 1
                if len(sources) < MAX_CASE_SOURCES:
                    sources.append(source)
                    case.observed_sources = sources
        if isinstance(normalized.get("breakdown"), dict):
            _apply_breakdown(case, normalized["breakdown"])
        reopened = False
        if is_deployment:
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
        wire_kind = normalized.get("wire_kind", "")
        operations = (
            case.breakdown.get("operations", {})
            if isinstance(case.breakdown, dict) else {})
        if wire_kind == "auth.ssh_login":
            _apply_ssh_login_promotion(case, normalized)
        elif (wire_kind == "auth.ssh_failure" and
                operations.get("auth.ssh_failure", 0) >=
                auth_elevate_occurrences() and
                _URGENCY_RANK[case.urgency] < _URGENCY_RANK["warning"]):
            case.urgency = "warning"
            case.urgency_reason = "ssh_failure_burst"
        elif (wire_kind == "system.service_error" and
                case.occurrence_count >= host_elevate_occurrences() and
                _URGENCY_RANK[case.urgency] < _URGENCY_RANK["high"]):
            case.urgency = "high"
            case.urgency_reason = "service_failure_burst"
        if (case.urgency in ("high", "critical") and
                case.state != MojoSecCase.STATE_ELEVATED):
            case.state = MojoSecCase.STATE_ELEVATED
            case.state_reason = case.urgency_reason
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
        if (case.urgency in ("high", "critical") and
                case.urgency_reason in CORROBORATION_REASONS and
                (not isinstance(case.breakdown, dict) or
                 "corroborated_with" not in case.breakdown)):
            corroborate_id = case.pk
            transaction.on_commit(lambda: _corroborate_case(corroborate_id))
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
            if case.sensor_kind == "campaign":
                # A campaign spans installations in one Group; it surfaces
                # when ANY member is authoritative — the stamped row is only
                # a deterministic placeholder until item 1636's identity.
                member_keys = set(case.members.values_list(
                    "installation_key_id", flat=True)[:64])
                if not any(installation_mode(key) == "authoritative"
                           for key in member_keys):
                    return None
            elif installation_mode(case.installation_key_id) != "authoritative":
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
                        "installation_key_id": case.installation_key_id,
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
            corroborated = (
                case.breakdown.get("corroborated_with")
                if isinstance(case.breakdown, dict) else None)
            if corroborated:
                event.metadata["mojosec_case"]["corroborating_case_ids"] = (
                    corroborated[:MAX_CORROBORATING_CASES])
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
