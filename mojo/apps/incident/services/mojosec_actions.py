"""Bounded MojoSec recommendation lifecycle — the single action owner.

Proposals derive from correlated cases only; targets only ever come from a
case's server-derived ``observed_sources``. Validation fails closed, automatic
execution is a default-off single-IP capability, and every state change and
per-target try is recorded append-only. ``block()`` itself is never modified —
the applied/pre-existing/whitelisted distinction comes from deciding under a
row lock on the GeoLocatedIP row.
"""

import datetime
import hashlib
import ipaddress
import json

from django.db import transaction
from django.db.models import F

from mojo.apps import metrics
from mojo.helpers import dates, logit
from mojo.helpers.settings import settings

from . import mojosec_correlation


logger = logit.get_logger(__name__, "incident.log")

MIN_TTL_SECONDS = 300
MAX_TTL_SECONDS = 604800
PROPOSAL_REASONS = {
    "repeated_impossible_paths": "temporary_block_ip",
    "distributed_campaign": "temporary_block_ip_set",
    "ssh_failure_then_success": "temporary_block_ip",
}
_EXEC_STATES = ("approved", "auto_approved", "executing")


def _digest(*values):
    payload = json.dumps(
        values, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _record_metric(slug, count=1):
    try:
        metrics.record(
            f"mojosec:action:{slug}", count=count, category="mojosec_action",
            account="incident")
    except Exception:
        logger.exception("MojoSec action metric failed for %s", slug)


def auto_execute_enabled():
    return settings.get_static("MOJOSEC_ACTION_AUTO_EXECUTE", False) is True


def max_targets():
    return mojosec_correlation._bounded_static(
        "MOJOSEC_ACTION_MAX_TARGETS", 256, 1, 1024)


def default_ttl_seconds():
    return mojosec_correlation._bounded_static(
        "MOJOSEC_ACTION_DEFAULT_TTL_SECONDS", 86400,
        MIN_TTL_SECONDS, MAX_TTL_SECONDS)


def auto_max_ttl_seconds():
    return mojosec_correlation._bounded_static(
        "MOJOSEC_ACTION_AUTO_MAX_TTL_SECONDS", 86400,
        MIN_TTL_SECONDS, MAX_TTL_SECONDS)


def max_attempts():
    return mojosec_correlation._bounded_static(
        "MOJOSEC_ACTION_MAX_ATTEMPTS", 5, 1, 20)


def proposal_ttl_seconds():
    return mojosec_correlation._bounded_static(
        "MOJOSEC_ACTION_PROPOSAL_TTL_SECONDS", 259200, 3600, MAX_TTL_SECONDS)


def _cidr_list(name):
    """A configured CIDR list, or None when the config is malformed.

    None means "cannot decide" — and because blocking is the dangerous act,
    the caller must fail every target closed, not open.
    """
    value = settings.get_static(name, [], kind="list")
    networks = []
    for row in value:
        if not isinstance(row, str):
            return None
        try:
            networks.append(ipaddress.ip_network(row, strict=False))
        except ValueError:
            return None
    return networks


def validate_target(ip):
    """Canonicalize and safety-check one candidate block target.

    Returns (canonical_ip_or_None, validation_state, reason).
    """
    from mojo.apps.account.models import GeoLocatedIP

    try:
        address = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return None, "invalid", "not_an_ip_address"
    canonical = str(address)
    if (address.is_private or address.is_loopback or address.is_link_local or
            address.is_multicast or address.is_reserved or
            address.is_unspecified or not address.is_global):
        return canonical, "protected", "non_global_address"
    for name in ("MOJOSEC_PROTECTED_CIDRS", "MOJOSEC_TRUSTED_PROXY_CIDRS"):
        networks = _cidr_list(name)
        if networks is None:
            return canonical, "invalid", "protected_config_invalid"
        if any(address in network for network in networks):
            return canonical, "protected", name.lower()
    geo = GeoLocatedIP.objects.filter(ip_address=canonical).first()
    if geo is not None and geo.whitelist_active:
        return canonical, "protected", "whitelisted"
    return canonical, "validated", ""


def _transition(recommendation, transition, reason, from_state, actor=None):
    from mojo.apps.incident.models import MojoSecRecommendationTransition

    MojoSecRecommendationTransition.objects.create(
        recommendation=recommendation, transition=transition,
        reason=reason[:96], from_state=from_state,
        to_state=recommendation.state, actor=actor,
        actor_id_snapshot=actor.pk if actor else 0,
        target_count=recommendation.target_count,
        validated_count=recommendation.validated_count,
        protected_count=recommendation.protected_count,
        executed_count=recommendation.executed_count,
        failed_count=recommendation.failed_count,
        reversed_count=recommendation.reversed_count)


def _attempt(recommendation, target, outcome, detail, started_at):
    from mojo.apps.incident.models import MojoSecExecutionAttempt

    MojoSecExecutionAttempt.objects.create(
        recommendation=recommendation, target=target,
        attempt_number=target.attempts, started_at=started_at,
        finished_at=dates.utcnow(), outcome=outcome, detail=detail[:256])


def _refresh_counts(recommendation):
    targets = list(recommendation.targets.all())
    recommendation.target_count = len(targets)
    recommendation.validated_count = sum(
        1 for t in targets if t.validation_state == "validated")
    recommendation.protected_count = sum(
        1 for t in targets if t.validation_state != "validated")
    recommendation.executed_count = sum(
        1 for t in targets if t.outcome == "applied")
    recommendation.failed_count = sum(
        1 for t in targets if t.outcome == "failed")
    recommendation.reversed_count = sum(
        1 for t in targets if t.outcome == "reversed")


def propose(case, action, reason_code, explanation, confidence,
            candidate_ips, requested_ttl=None, requested_scope="installation"):
    """Idempotently propose one bounded action for one case.

    A second call while a (case, action) recommendation is open refreshes
    evidence and adds any new validated targets up to the cap — it never
    duplicates. Targets must come from the case's observed sources; this
    function trusts its caller only because every caller is the sweep.
    """
    from mojo.apps.incident.models import (
        MojoSecRecommendation, MojoSecRecommendationTarget)

    ttl = requested_ttl or default_ttl_seconds()
    ttl = max(MIN_TTL_SECONDS, min(int(ttl), MAX_TTL_SECONDS))
    if requested_scope not in ("installation", "group"):
        raise ValueError(
            "region/fleet scope requires the authoritative identity contract "
            "from Maestro item 1636")
    now = dates.utcnow()
    with transaction.atomic():
        open_rec = MojoSecRecommendation.objects.select_for_update().filter(
            case=case, action=action,
            state__in=MojoSecRecommendation.OPEN_STATES).first()
        created = open_rec is None
        if created:
            generation = MojoSecRecommendation.objects.filter(
                case=case, action=action).count()
            open_rec = MojoSecRecommendation.objects.create(
                group_id=case.group_id,
                installation_key_id=case.installation_key_id,
                case=case, action=action, reason_code=reason_code[:96],
                explanation=explanation[:512], confidence=confidence,
                urgency=case.urgency, requested_scope=requested_scope,
                requested_ttl_seconds=ttl,
                policy_version=case.policy_version,
                evaluator_version=case.evaluator_version,
                idempotency_key=_digest(
                    "mojosec-rec", case.pk, action, generation),
                expires_at=now + datetime.timedelta(
                    seconds=proposal_ttl_seconds()))
        existing = set(open_rec.targets.values_list("ip", flat=True))
        for candidate in list(candidate_ips)[:max_targets()]:
            canonical, state, reason = validate_target(candidate)
            if canonical is None or canonical in existing:
                continue
            if len(existing) >= max_targets():
                break
            existing.add(canonical)
            MojoSecRecommendationTarget.objects.create(
                recommendation=open_rec, ip=canonical,
                validation_state=state, validation_reason=reason)
        open_rec.urgency = case.urgency
        _refresh_counts(open_rec)
        open_rec.save()
        if created:
            _transition(open_rec, "proposed", reason_code, "")
    if created:
        _record_metric("recommendations_proposed")
    return open_rec, created


def _queue_execution(recommendation):
    from mojo.apps import jobs

    recommendation.execution_rounds = F("execution_rounds") + 1
    recommendation.save(update_fields=["execution_rounds", "modified"])
    recommendation.refresh_from_db()
    jobs.publish(
        "mojo.apps.incident.services.mojosec_actions.execute_recommendation",
        {"recommendation_id": recommendation.pk},
        channel="incident_handlers", max_retries=5, expires_in=86400,
        idempotency_key=(
            f"mojosec-rec:{recommendation.pk}:"
            f"{recommendation.execution_rounds}"))


def approve(recommendation, actor, note=""):
    """Human approval: exactly what was proposed, nothing wider."""
    from mojo.apps.incident.models import MojoSecRecommendation

    with transaction.atomic():
        locked = MojoSecRecommendation.objects.select_for_update().get(
            pk=recommendation.pk)
        if locked.state != MojoSecRecommendation.STATE_PROPOSED:
            raise ValueError(f"recommendation is {locked.state}, not proposed")
        if locked.expires_at <= dates.utcnow():
            raise ValueError("recommendation proposal has expired")
        if locked.validated_count < 1:
            raise ValueError("recommendation has no validated targets")
        from_state = locked.state
        locked.state = MojoSecRecommendation.STATE_APPROVED
        locked.approved_by = actor
        locked.approved_at = dates.utcnow()
        locked.approval_note = note[:256]
        locked.save(update_fields=[
            "state", "approved_by", "approved_at", "approval_note",
            "modified"])
        _transition(locked, "approved", "human_approval", from_state, actor)
    _record_metric("recommendations_approved")
    _queue_execution(locked)
    return locked


def reject(recommendation, actor, note="", transition="rejected"):
    from mojo.apps.incident.models import MojoSecRecommendation

    with transaction.atomic():
        locked = MojoSecRecommendation.objects.select_for_update().get(
            pk=recommendation.pk)
        if locked.state not in (
                MojoSecRecommendation.STATE_PROPOSED,
                MojoSecRecommendation.STATE_APPROVED):
            raise ValueError(f"recommendation is {locked.state}; cannot reject")
        from_state = locked.state
        locked.state = MojoSecRecommendation.STATE_REJECTED
        locked.approval_note = note[:256]
        locked.save(update_fields=["state", "approval_note", "modified"])
        _transition(locked, transition, note or transition, from_state, actor)
    _record_metric("recommendations_rejected")
    return locked


def _maybe_auto_approve(recommendation):
    """Auto-approval: default-off, single validated IP, bounded TTL only."""
    from mojo.apps.incident.models import MojoSecRecommendation

    if (not auto_execute_enabled() or
            recommendation.action != MojoSecRecommendation.ACTION_BLOCK_IP or
            recommendation.state != MojoSecRecommendation.STATE_PROPOSED or
            recommendation.confidence != "high" or
            recommendation.validated_count != 1 or
            recommendation.reason_code == "ssh_failure_then_success" or
            recommendation.requested_ttl_seconds > auto_max_ttl_seconds()):
        return False
    with transaction.atomic():
        locked = MojoSecRecommendation.objects.select_for_update().get(
            pk=recommendation.pk)
        if locked.state != MojoSecRecommendation.STATE_PROPOSED:
            return False
        from_state = locked.state
        locked.state = MojoSecRecommendation.STATE_AUTO_APPROVED
        locked.approved_at = dates.utcnow()
        locked.approval_note = "auto: policy thresholds met"
        locked.save(update_fields=[
            "state", "approved_at", "approval_note", "modified"])
        _transition(locked, "auto_approved", "policy_auto_approval", from_state)
    _record_metric("recommendations_auto_approved")
    _queue_execution(locked)
    return True


def _apply_block(ip, reason, ttl):
    """Decide applied/pre-existing/whitelisted under the GeoLocatedIP lock.

    ``block()`` is untouched — its bare-bool return stays load-bearing for
    legacy callers; the lock is what makes this attribution truthful when a
    legacy handler or a second recommendation races on the same IP.
    """
    from mojo.apps.account.models import GeoLocatedIP

    with transaction.atomic():
        geo = GeoLocatedIP.objects.select_for_update().filter(
            ip_address=ip).first()
        if geo is None:
            GeoLocatedIP.geolocate(ip, auto_refresh=False)
            geo = GeoLocatedIP.objects.select_for_update().get(ip_address=ip)
        if geo.whitelist_active:
            return {"outcome": "whitelisted",
                    "prior_until": geo.whitelisted_until,
                    "prior_reason": geo.whitelisted_reason or ""}
        if geo.block_active:
            return {"outcome": "pre_existing",
                    "prior_until": geo.blocked_until,
                    "prior_reason": geo.blocked_reason or ""}
        applied = geo.block(reason=reason, ttl=ttl)
        if not applied:
            return {"outcome": "whitelisted",
                    "prior_until": geo.whitelisted_until,
                    "prior_reason": geo.whitelisted_reason or ""}
        return {"outcome": "applied", "prior_until": None, "prior_reason": ""}


def _execute(recommendation_id):
    from mojo.apps.incident.models import MojoSecRecommendation

    with transaction.atomic():
        recommendation = (
            MojoSecRecommendation.objects.select_for_update()
            .get(pk=recommendation_id))
        if recommendation.state not in _EXEC_STATES:
            return recommendation
        if recommendation.state != MojoSecRecommendation.STATE_EXECUTING:
            from_state = recommendation.state
            recommendation.state = MojoSecRecommendation.STATE_EXECUTING
            recommendation.save(update_fields=["state", "modified"])
            _transition(
                recommendation, "executing", "execution_started", from_state)
        targets = list(recommendation.targets.select_for_update().filter(
            validation_state="validated",
            outcome__in=("pending", "failed"),
            attempts__lt=max_attempts()))
        ttl = recommendation.requested_ttl_seconds
        for target in targets:
            started = dates.utcnow()
            target.attempts += 1
            canonical, state, reason = validate_target(target.ip)
            if state != "validated":
                target.validation_state = state
                target.validation_reason = reason
                target.outcome = (
                    "whitelisted" if reason == "whitelisted" else "failed")
                target.last_error = f"revalidation: {reason}"[:256]
                target.save()
                _attempt(recommendation, target, target.outcome,
                         target.last_error, started)
                continue
            reason_text = (
                f"mojosec:rec:{recommendation.pk}|case:"
                f"{recommendation.case_id}|{recommendation.reason_code}")[:255]
            try:
                result = _apply_block(target.ip, reason_text, ttl)
            except Exception as err:
                logger.exception(
                    "MojoSec block execution failed for %s", target.ip)
                target.outcome = "failed"
                target.last_error = str(err)[:256]
                target.save()
                _attempt(recommendation, target, "failed",
                         target.last_error, started)
                _record_metric("targets_failed")
                continue
            target.outcome = result["outcome"]
            target.last_error = ""
            if result["outcome"] == "applied":
                target.applied_at = dates.utcnow()
                target.expires_at = target.applied_at + datetime.timedelta(
                    seconds=ttl)
                _record_metric("targets_applied")
            else:
                target.prior_blocked_until = result["prior_until"]
                target.prior_reason = (result["prior_reason"] or "")[:255]
                _record_metric(f"targets_{result['outcome']}")
            target.save()
            _attempt(recommendation, target, result["outcome"],
                     result["prior_reason"] or "", started)
        _refresh_counts(recommendation)
        remaining = recommendation.targets.filter(
            validation_state="validated", outcome__in=("pending", "failed"),
            attempts__lt=max_attempts()).exists()
        if not remaining:
            terminal_ok = recommendation.targets.filter(
                outcome__in=("applied", "pre_existing", "whitelisted")).exists()
            from_state = recommendation.state
            recommendation.state = (
                MojoSecRecommendation.STATE_EXECUTED if terminal_ok
                else MojoSecRecommendation.STATE_FAILED)
            reason = "executed"
            if (recommendation.state == MojoSecRecommendation.STATE_EXECUTED
                    and recommendation.failed_count):
                reason = "partial"
            elif recommendation.state == MojoSecRecommendation.STATE_FAILED:
                reason = "all_targets_failed"
            recommendation.save()
            _transition(recommendation, recommendation.state, reason, from_state)
            if recommendation.state == MojoSecRecommendation.STATE_EXECUTED:
                _record_metric("recommendations_executed")
        else:
            recommendation.save()
    return recommendation


def execute_recommendation(job):
    return _execute(job.payload["recommendation_id"]) is not None


def reverse(recommendation, actor, note=""):
    """Operator rollback: unblock every applied target, audited per try."""
    from mojo.apps.incident.models import MojoSecRecommendation

    with transaction.atomic():
        locked = MojoSecRecommendation.objects.select_for_update().get(
            pk=recommendation.pk)
        if locked.state not in (
                MojoSecRecommendation.STATE_EXECUTED,
                MojoSecRecommendation.STATE_EXPIRED):
            raise ValueError(f"recommendation is {locked.state}; cannot reverse")
        from mojo.apps.account.models import GeoLocatedIP

        for target in locked.targets.select_for_update().filter(
                outcome="applied"):
            started = dates.utcnow()
            target.attempts += 1
            geo = GeoLocatedIP.objects.filter(ip_address=target.ip).first()
            try:
                if geo is not None and geo.block_active:
                    geo.unblock(reason=f"mojosec:rec:{locked.pk}:reversed")
                target.outcome = "reversed"
                target.reversed_at = dates.utcnow()
                target.save()
                _attempt(locked, target, "reverse_applied", note, started)
            except Exception as err:
                logger.exception(
                    "MojoSec block reversal failed for %s", target.ip)
                target.last_error = str(err)[:256]
                target.save()
                _attempt(locked, target, "reverse_failed",
                         target.last_error, started)
        from_state = locked.state
        locked.state = MojoSecRecommendation.STATE_REVERSED
        locked.approval_note = note[:256] or locked.approval_note
        _refresh_counts(locked)
        locked.save()
        _transition(locked, "reversed", note or "operator_reversal",
                    from_state, actor)
    _record_metric("recommendations_reversed")
    return locked


def owns_enforcement(event):
    """True when the recommendation lifecycle owns this event's enforcement.

    Suppression applies only to categories the installation's enrollment
    actually routes — silencing a handler nothing replaces would be a
    regression, so unrouted categories keep their operator rules.
    """
    category = getattr(event, "category", "") or ""
    if not category.startswith("mojosec."):
        return False
    metadata = getattr(event, "metadata", None)
    if not isinstance(metadata, dict):
        return False
    block = metadata.get("mojosec") or metadata.get("mojosec_case") or {}
    installation_id = block.get("installation_key_id") if isinstance(
        block, dict) else None
    if not isinstance(installation_id, int):
        return False
    enrollment = mojosec_correlation.installation_enrollment(installation_id)
    if enrollment is None or enrollment["mode"] != "authoritative":
        return False
    if category == "mojosec.case.promoted":
        return True
    if category.startswith("mojosec.web.") or category == "mojosec.fim.change":
        return True
    if (category.startswith("mojosec.auth.") or
            category == "mojosec.system.service_error"):
        return bool(enrollment.get("include_host"))
    return False


def execute_manual_block(ip, reason, actor, ttl=None):
    """Validated single-IP block for the human-approved ticket path.

    Deliberately NOT a recommendation: recommendations exist so targets only
    ever come from case evidence, and a ticket-supplied IP is not that. The
    same validation and honest outcome semantics apply; the caller reports
    the outcome and must not claim success it did not get.
    """
    ttl = max(MIN_TTL_SECONDS, min(int(ttl or default_ttl_seconds()),
                                   MAX_TTL_SECONDS))
    canonical, state, why = validate_target(ip)
    if state != "validated":
        return {"outcome": "refused", "reason": why, "ip": canonical or str(ip)}
    result = _apply_block(
        canonical, f"mojosec:manual:{actor.pk if actor else 0}|{reason}"[:255],
        ttl)
    result["ip"] = canonical
    result["ttl"] = ttl
    _record_metric(f"manual_{result['outcome']}")
    return result


def _propose_from_cases(now, horizon, limit):
    from mojo.apps.incident.models import MojoSecCase, MojoSecRecommendation

    proposed = 0
    single = MojoSecCase.objects.filter(
        sensor_kind="web", modified__gte=horizon, distinct_source_count=1,
        urgency_reason__in=(
            "trusted_impossible_path", "sustained_trusted_impossible_paths"),
        occurrence_count__gte=mojosec_correlation.block_min_occurrences(),
    ).exclude(observed_sources=[])[:limit]
    for case in single:
        recommendation, created = propose(
            case, MojoSecRecommendation.ACTION_BLOCK_IP,
            "repeated_impossible_paths",
            (f"{case.occurrence_count} impossible-path probes from one "
             f"address on {case.resource_id}"),
            "high", case.observed_sources[:1])
        proposed += 1 if created else 0
        _maybe_auto_approve(recommendation)
    campaigns = MojoSecCase.objects.filter(
        sensor_kind="campaign", modified__gte=horizon,
    ).exclude(observed_sources=[])[:limit]
    for case in campaigns:
        _, created = propose(
            case, MojoSecRecommendation.ACTION_BLOCK_IP_SET,
            "distributed_campaign",
            (f"campaign across {case.distinct_count} networks on "
             f"{case.resource_id}"),
            "high", case.observed_sources, requested_scope="group")
        proposed += 1 if created else 0
    progressions = MojoSecCase.objects.filter(
        sensor_kind="auth", family="ssh", modified__gte=horizon,
        urgency="critical", urgency_reason="ssh_failure_then_success",
    ).exclude(observed_sources=[])[:limit]
    for case in progressions:
        _, created = propose(
            case, MojoSecRecommendation.ACTION_BLOCK_IP,
            "ssh_failure_then_success",
            f"failure burst then success from {case.network}",
            "high", case.observed_sources[:1])
        proposed += 1 if created else 0
        # Never auto: blocking a source that just authenticated could lock
        # out the admin racing the attacker. A human decides.
    return proposed


def action_sweep(job=None, now=None, limit=100, lookback_seconds=900):
    """Propose, expire, retry and settle recommendation state. Cron */5."""
    from mojo.apps.incident.models import (
        MojoSecRecommendation, MojoSecRecommendationTarget)

    now = now or dates.utcnow()
    horizon = now - datetime.timedelta(seconds=lookback_seconds)
    proposed = _propose_from_cases(now, horizon, limit)
    expired = 0
    for recommendation in MojoSecRecommendation.objects.filter(
            state=MojoSecRecommendation.STATE_PROPOSED,
            expires_at__lte=now)[:limit]:
        with transaction.atomic():
            locked = MojoSecRecommendation.objects.select_for_update().get(
                pk=recommendation.pk)
            if (locked.state != MojoSecRecommendation.STATE_PROPOSED or
                    locked.expires_at > now):
                continue
            locked.state = MojoSecRecommendation.STATE_EXPIRED
            locked.save(update_fields=["state", "modified"])
            _transition(locked, "expired", "proposal_expired", "proposed")
        expired += 1
        _record_metric("recommendations_expired")
    retried = 0
    stalled = MojoSecRecommendation.objects.filter(
        state=MojoSecRecommendation.STATE_EXECUTING,
        modified__lt=now - datetime.timedelta(seconds=60))[:limit]
    for recommendation in stalled:
        _execute(recommendation.pk)
        retried += 1
    expired_targets = 0
    for target in MojoSecRecommendationTarget.objects.filter(
            outcome="applied", expires_at__lte=now)[:limit]:
        with transaction.atomic():
            locked_target = (
                MojoSecRecommendationTarget.objects.select_for_update()
                .get(pk=target.pk))
            if (locked_target.outcome != "applied" or
                    locked_target.expires_at is None or
                    locked_target.expires_at > now):
                continue
            locked_target.outcome = "expired"
            locked_target.save(update_fields=["outcome", "modified"])
            _attempt(locked_target.recommendation, locked_target,
                     "expire_confirmed",
                     "ttl elapsed; central sweep owns the unblock", now)
        expired_targets += 1
    # Recommendations whose applied targets have all expired settle to
    # expired themselves.
    for recommendation in MojoSecRecommendation.objects.filter(
            state=MojoSecRecommendation.STATE_EXECUTED)[:limit]:
        targets = recommendation.targets.all()
        if not any(t.outcome == "applied" for t in targets):
            if any(t.outcome == "expired" for t in targets):
                with transaction.atomic():
                    locked = (
                        MojoSecRecommendation.objects.select_for_update()
                        .get(pk=recommendation.pk))
                    if locked.state != MojoSecRecommendation.STATE_EXECUTED:
                        continue
                    locked.state = MojoSecRecommendation.STATE_EXPIRED
                    locked.save(update_fields=["state", "modified"])
                    _transition(locked, "expired", "all_targets_expired",
                                MojoSecRecommendation.STATE_EXECUTED)
    if job is not None and hasattr(job, "add_log"):
        job.add_log(
            f"Proposed {proposed}; expired {expired} proposal(s) and "
            f"{expired_targets} target(s); retried {retried} execution(s)")
    return {"proposed": proposed, "expired": expired,
            "expired_targets": expired_targets, "retried": retried}
