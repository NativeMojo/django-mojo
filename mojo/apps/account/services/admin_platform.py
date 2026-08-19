"""Permission-separated, bounded evidence for Admin Platform/Advanced."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError, wait
from contextlib import contextmanager
from datetime import timedelta

from django.db import connection, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from mojo import errors as merrors
from mojo.apps.account.services import system_settings
from mojo.helpers.settings import settings


SCHEMA_VERSION = 1
# Dashboard alone. Its source roster, availability contract, and envelope keys
# changed; Platform/Advanced still speak SCHEMA_VERSION 1.
DASHBOARD_SCHEMA_VERSION = 2
COLLECTOR_TIMEOUT = 3.0
STALE_SECONDS = 600
ROW_LIMIT = 100
WEBAPP_LIMIT = 24
WEBAPP_WORKERS = 4
WEBAPP_PROBE_TIMEOUT = 1.5
WEBAPP_COLLECTOR_DEADLINE = 2.5
INCIDENT_TERMINAL_STATUSES = ("ignored", "resolved", "closed")
# A drift finding older than this is not evidence about the engine running now.
VERSION_DRIFT_MAX_AGE = timedelta(days=3)
# A job that failed inside this window is happening now. The all-time failed
# count is a ledger, not a symptom — it never colours the Dashboard row.
JOBS_FAILURE_WINDOW = timedelta(hours=1)
LOAD_BALANCER_CACHE_KEY = "mojo:admin:dashboard:elbv2"
LOAD_BALANCER_CACHE_TTL = 60

_ENGINE_DISPLAY = {
    "postgresql": "Postgres",
    "mysql": "MySQL",
    "sqlite": "SQLite",
    "oracle": "Oracle",
    "microsoft": "SQL Server",
}


def _bounded_redis():
    from mojo.helpers.redis import get_bounded_connection
    return get_bounded_connection(timeout=min(1.0, COLLECTOR_TIMEOUT / 2.0))


@contextmanager
def _redis_client():
    client = _bounded_redis()
    try:
        yield client
    finally:
        client.close()


def _envelope(status, data=None, reason=None, reason_detail=None):
    observed = timezone.now()
    value = {
        "status": status, "observed_at": observed.isoformat(),
        "stale_after": (observed + timedelta(seconds=STALE_SECONDS)).isoformat(),
        "data": data if data is not None else {},
    }
    if reason:
        value["reason"] = reason
    if reason_detail:
        value["reason_detail"] = reason_detail
    return value


def _collect(func):
    # Future cancellation does not stop an already-running provider call.
    # Therefore this wrapper is only a response budget: every external
    # collector below must also use its own SDK/RPC timeout and row/page cap.
    from django.db import close_old_connections
    def bounded():
        close_old_connections()
        try:
            return func()
        finally:
            close_old_connections()
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(bounded)
    try:
        value = future.result(timeout=COLLECTOR_TIMEOUT)
        status = "healthy"
        reason = None
        reason_detail = None
        if isinstance(value, dict):
            value = dict(value)
            status = value.pop("_collector_status", status)
            reason = value.pop("_collector_reason", None)
            reason_detail = value.pop("_collector_reason_detail", None)
        return _envelope(status, value, reason=reason, reason_detail=reason_detail)
    except TimeoutError:
        future.cancel()
        return _envelope("timeout", reason="collector_timeout")
    except Exception:
        return _envelope("unavailable", reason="collector_unavailable")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _permitted(request, *perms):
    return bool(request.user.is_superuser or request.user.has_permission(list(perms)))


def _guarded(request, perms, collector):
    if not _permitted(request, *perms):
        return _envelope("unauthorized", reason="permission_required")
    return _collect(collector)


def _section_map(request, specs):
    """Run permitted sections concurrently under their individual budgets."""
    output = {}
    permitted = {}
    for name, (perms, collector) in specs.items():
        if _permitted(request, *perms):
            permitted[name] = collector
        else:
            output[name] = _envelope("unauthorized", reason="permission_required")
    if not permitted:
        return output
    # One wave for both of today's rosters — Platform's ten sections and the
    # Dashboard's ten platform-tier sources. Each _collect response is bounded;
    # its provider work is independently bounded by SDK/RPC timeouts.
    with ThreadPoolExecutor(max_workers=min(12, len(permitted))) as pool:
        futures = {name: pool.submit(_collect, collector)
                   for name, collector in permitted.items()}
        for name, future in futures.items():
            output[name] = future.result()
    return output


def _database():
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        ok = cursor.fetchone() == (1,)
    return {"reachable": ok, "vendor": connection.vendor}


def _redis():
    with _redis_client() as redis:
        return {"reachable": bool(redis.ping())}


def _api():
    import mojo
    from mojo.apps.account.services import system_readiness
    origin = system_settings.get_value(system_settings.BASE_URL)
    # What THIS node would answer /api/version with. The probe reports what the
    # public origin actually served, so the pair is the only way to see a node
    # serving an older build than the one the operator is looking at.
    node_version = str(settings.get_static("VERSION", "") or "")
    if not origin:
        return {"_collector_status": "unconfigured",
                "django_mojo_version": mojo.__version__,
                "node_version": node_version, "public_origin": None,
                "configured": False}
    proof = system_readiness.probe_public_api_details(origin, timeout=2.0)
    return {"_collector_status": "healthy" if proof["ok"] else "unhealthy",
            "django_mojo_version": mojo.__version__,
            "node_version": node_version, "public_origin": origin,
            "configured": True, "probe": proof}


def _fleet():
    from mojo.apps import jobs
    runners = jobs.get_runners_bounded(
        "edge", limit=ROW_LIMIT,
        timeout=min(1.0, COLLECTOR_TIMEOUT / 2.0))
    rows = [{
        "runner": str(row.get("runner_id") or "")[:64],
        "channels": sorted({str(item)[:64]
                            for item in (row.get("channels") or [])})[:32],
        "last_heartbeat": str(row.get("last_heartbeat") or "")[:64],
    } for row in runners]
    return {"_collector_status": "healthy" if rows else "unhealthy",
            "channel": "edge", "runners": rows, "truncated": False}


def _jobs():
    from mojo.apps.jobs.keys import JobKeys
    from mojo.apps.jobs.models import Job
    with _redis_client() as redis:
        scheduler = redis.get(JobKeys().scheduler_lock())
    counts = {
        "pending": Job.objects.filter(status="pending").count(),
        "running": Job.objects.filter(status="running").count(),
        "failed": Job.objects.filter(status="failed").count(),
    }
    return {"_collector_status": "healthy" if scheduler else "unhealthy",
            "scheduler_active": bool(scheduler), "jobs": counts}


def _sanity(redis_client=None):
    from mojo.apps.account.services import system_readiness
    from mojo.apps.edge.services import sanity
    local_target = system_readiness.trusted_local_api_target()
    options = {
        "url": local_target["url"],
        "timeout": 1.0, "retries": 1, "delay": 0,
    }
    # The checks otherwise reach for the process-wide Redis client and its
    # 60-second socket timeout. Callers on a response budget pass a bounded
    # one; Platform keeps today's behaviour by passing nothing.
    if redis_client is not None:
        options["redis_client"] = redis_client
    results = sanity.run(options)
    rows = [{"name": row.get("name"), "ok": bool(row.get("ok"))}
            for row in results[:16]]
    return {"_collector_status": "healthy" if rows and all(
                row["ok"] for row in rows) else "unhealthy",
            "checks": rows, "local_target_source": local_target["source"],
            "migration_check": next((
                row["ok"] for row in rows if row["name"] == "migrations"), False)}


def _deployments(include_stderr=False):
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import deploy, platform_deploy
    with _redis_client() as redis:
        pipe = redis.pipeline(transaction=False)
        try:
            pipe.get(deploy.TARGET_KEY)
            pipe.get(deploy.STATUS_KEY)
            target_raw, status_raw = pipe.execute()
        finally:
            pipe.reset()
    target = deploy._loads(target_raw)
    coordination = deploy._loads(status_raw)
    rows = PlatformDeployment.objects.select_related("retry_of").all()[:50]
    status = _platform_deployment_status(rows[0]) if rows else "unconfigured"
    # The operator's framework hold, and what it currently resolves to. A junk
    # row would refuse the next DEPLOY loudly; it must not take the read-only
    # overview down with it, so this reads defensively and shows the raw mode.
    from mojo.apps.account.models import Setting
    from mojo.apps.edge.settings_validators import FRAMEWORK_VERSION_KEY
    pin = Setting.get_from_db(FRAMEWORK_VERSION_KEY)[0]
    pin = pin.strip() if isinstance(pin, str) else ""
    if pin == deploy.FRAMEWORK_HOLD:
        mode, resolved = "hold", platform_deploy.last_converged_framework()
    elif pin:
        mode, resolved = "pinned", pin
    else:
        mode, resolved = "latest", None
    return {
        "_collector_status": status,
        "items": [platform_deploy.serialize(
            row, desired_commit=(target or {}).get("sha"),
            include_stderr=include_stderr) for row in rows],
        "limit": 50,
        "desired_commit": (target or {}).get("sha"),
        "desired_deployment": (target or {}).get("deployment"),
        "framework_pin": {"configured": bool(pin), "value": pin or None,
                          "mode": mode, "resolved": resolved},
        "coordination": {
            "state": (coordination or {}).get("state"),
            "deployment": (coordination or {}).get("deployment"),
        },
    }


def _current_certificate_rows(rows):
    """Newest lifecycle row for each managed certificate name set."""
    current = []
    seen = set()
    for row in rows:
        names = row.get("sans") or [row.get("common_name")]
        identity = (
            row.get("domain_id"),
            tuple(sorted(str(name).strip().lower().rstrip(".") for name in names)))
        if identity in seen:
            continue
        seen.add(identity)
        current.append(row)
    return current


def _certificate_counts(rows):
    counts = {}
    for row in rows:
        status = row.get("status")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _certificates():
    from mojo.apps.dnsman.models import Certificate
    rows = _current_certificate_rows(Certificate.objects.order_by(
        "-created", "-pk").values(
            "id", "domain_id", "common_name", "sans", "status", "not_after"))
    counts = _certificate_counts(rows)
    cutoff = timezone.now() + timedelta(days=30)
    expiring = sum(
        1 for row in rows
        if row.get("not_after") is not None and row["not_after"] <= cutoff)
    return {"counts": counts, "expiring_within_30_days": expiring}


def _security():
    keys = (
        "SECURE_SSL_REDIRECT", "SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE",
        "SECURE_HSTS_SECONDS", "KMS_KEY_ID", "AWS_KMS_KEY_ID")
    from django.utils.dateparse import parse_datetime
    from mojo.apps.aws.models import CloudWatchAlarmTransition
    from mojo.apps.incident.models import Incident
    from mojo.helpers.cron import get_cron_heartbeats
    values = {key: {"configured": bool(settings.get_static(key, None))} for key in keys}
    beats = get_cron_heartbeats(limit=1)
    beat = beats[0] if beats else None
    observed = parse_datetime((beat or {}).get("completed_at") or "")
    age = int((timezone.now() - observed).total_seconds()) if observed else None
    probe = CloudWatchAlarmTransition.objects.filter(
        is_delivery_probe=True).order_by("-created").values(
            "created", "dispatch_status", "new_state").first()
    probe_age = int((timezone.now() - probe["created"]).total_seconds()) if probe else None
    open_incidents = _open_incidents()
    open_rows = open_incidents.order_by(
        "-priority", "-created").values("id", "priority", "status", "category")[:25]
    secure_posture = _secure_posture()
    values.update({
        "cron_heartbeat": {"present": bool(beat), "state": (beat or {}).get("state"),
                           "age_seconds": age},
        "monitoring_delivery": {"present": bool(probe),
                                "observed_at": probe["created"].isoformat() if probe else None,
                                "age_seconds": probe_age,
                                "status": probe["dispatch_status"] if probe else None,
                                "state": probe["new_state"] if probe else None},
        "open_incidents": {"count": open_incidents.count(), "items": list(open_rows)},
        "secure_posture": secure_posture,
    })
    secure_keys = ("SECURE_SSL_REDIRECT", "SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE")
    posture_ok = all(values[key]["configured"] for key in secure_keys)
    posture_ok = posture_ok and bool(settings.get_static("SECURE_HSTS_SECONDS", 0))
    delivery_ok = bool(
        probe and probe_age is not None and probe_age <= STALE_SECONDS
        and probe["dispatch_status"] == "complete")
    healthy = bool(
        beat and (beat or {}).get("state") == "completed"
        and age is not None and age <= 180 and delivery_ok and posture_ok)
    return {"_collector_status": "healthy" if healthy else "unhealthy", **values}


def _open_incidents():
    from mojo.apps.incident.models import Incident
    return Incident.objects.exclude(status__in=INCIDENT_TERMINAL_STATUSES)


def _secure_posture():
    controls = {
        "https_redirect": bool(settings.get_static("SECURE_SSL_REDIRECT", False)),
        "session_cookie_secure": bool(settings.get_static("SESSION_COOKIE_SECURE", False)),
        "csrf_cookie_secure": bool(settings.get_static("CSRF_COOKIE_SECURE", False)),
        "hsts": bool(settings.get_static("SECURE_HSTS_SECONDS", 0)),
    }
    return {"controls": controls,
            "disabled": [name for name, enabled in controls.items() if not enabled]}


def _probe_webapp(summary):
    from mojo.apps.edge.services import public_probe
    observed = timezone.now()
    origin = (summary.get("address") or {}).get("https_origin")
    base = {
        "observed_at": observed.isoformat(),
        "stale_after": (observed + timedelta(seconds=STALE_SECONDS)).isoformat(),
    }
    if not origin:
        return {**base, "status": "not_configured", "reason": "origin_missing"}
    try:
        proof = public_probe.probe_https_root(
            origin, timeout=WEBAPP_PROBE_TIMEOUT, max_body=65536)
    except public_probe.UnsafePublicProbe:
        return {**base, "status": "unknown", "reason": "unsafe_destination"}
    except Exception:
        return {**base, "status": "unknown", "reason": "probe_unavailable"}
    if proof.get("ok"):
        return {**base, "status": "healthy", "http_status": proof.get("status")}
    if proof.get("status") is not None:
        return {**base, "status": "unhealthy", "reason": "http_failure",
                "http_status": proof.get("status")}
    return {**base, "status": "unknown", "reason": "probe_unavailable"}


def _webapp_collector_status(summaries, truncated=False):
    if not summaries:
        return "unconfigured"
    statuses = [item["current_health"]["status"] for item in summaries]
    if "unhealthy" in statuses:
        return "unhealthy"
    if "unknown" in statuses or "not_configured" in statuses:
        return "degraded"
    return "degraded" if truncated else "healthy"


def _webapp_evidence():
    from mojo.apps.edge.models import WebApp, WebAppOnboardingOperation
    from mojo.apps.edge.services import webapp_onboarding
    # vhost__certificate/vhost__domain ride along because summary_for reads
    # them per app (item 2158's additive address.certificate) — without the
    # join this budgeted collector would pay +1 query per app.
    rows = list(WebApp.objects.select_related(
        "vhost", "vhost__certificate", "vhost__domain",
        "api_key").all()[:WEBAPP_LIMIT + 1])
    truncated = len(rows) > WEBAPP_LIMIT
    rows = rows[:WEBAPP_LIMIT]
    operations = WebAppOnboardingOperation.objects.exclude(
        registrar_provider="").values(
            "registrar_provider", "dns_provider", "status")[:ROW_LIMIT]
    provider_evidence = [{
        "registrar": row["registrar_provider"], "dns": row["dns_provider"],
        "matches": row["registrar_provider"] == row["dns_provider"],
        "status": row["status"],
    } for row in operations]
    summaries = [webapp_onboarding.summary_for(row) for row in rows]
    pool = ThreadPoolExecutor(max_workers=WEBAPP_WORKERS)
    futures = {pool.submit(_probe_webapp, item): item for item in summaries}
    done, pending = wait(futures, timeout=WEBAPP_COLLECTOR_DEADLINE)
    for future in done:
        futures[future]["current_health"] = future.result()
    observed = timezone.now()
    for future in pending:
        future.cancel()
        futures[future]["current_health"] = {
            "status": "unknown", "reason": "collector_deadline",
            "observed_at": observed.isoformat(),
            "stale_after": (observed + timedelta(seconds=STALE_SECONDS)).isoformat(),
        }
    pool.shutdown(wait=False, cancel_futures=True)

    health = {}
    onboarding = {}
    active_keys = 0
    configured_origins = 0
    for item in summaries:
        health_status = item["current_health"]["status"]
        health[health_status] = health.get(health_status, 0) + 1
        onboarding_status = item["onboarding"]["status"]
        onboarding[onboarding_status] = onboarding.get(onboarding_status, 0) + 1
        active_keys += int(item["deployment_key"]["active"])
        configured_origins += int(bool(item["address"].get("https_origin")))
    if truncated:
        health["not_probed"] = 1
    status = _webapp_collector_status(summaries, truncated=truncated)
    return {
        "_collector_status": status,
        "summary_contract": 1,
        "items": summaries,
        "rollup": {
            "count": len(summaries), "configured_origins": configured_origins,
            "not_probed": 1 if truncated else 0,
            "current_health": health, "onboarding": onboarding,
            "deployment_keys": {"active": active_keys,
                                "inactive": len(summaries) - active_keys},
        },
        "registrar_vs_dns": provider_evidence,
        "truncated": truncated,
        "limits": {"items": WEBAPP_LIMIT, "workers": WEBAPP_WORKERS,
                   "per_probe_seconds": WEBAPP_PROBE_TIMEOUT,
                   "collector_seconds": WEBAPP_COLLECTOR_DEADLINE},
    }


def _webapps():
    return _webapp_evidence()


def _platform_deployment_status(row):
    if row.status == "failed":
        return "unhealthy"
    if row.status in ("requested", "canary", "fleet", "partial"):
        return "degraded"
    if row.status == "unknown":
        return "unknown"
    if row.status == "superseded":
        return "stale"
    if row.status in ("verified", "converged"):
        return "healthy"
    return "unknown"


def _dashboard_deployment():
    """One durable attempt, projected to the six facts the row shows.

    Deliberately NOT the full serializer: node evidence carries a deploy stderr
    tail that can survive redaction with a credential in it, and the Dashboard
    row renders a sha, an outcome, and a time. Not projecting it here is what
    makes the tail unreachable from this endpoint for every role.
    """
    from mojo.apps.edge.models import PlatformDeployment
    row = PlatformDeployment.objects.first()
    if row is None:
        return {"_collector_status": "unconfigured", "items": []}
    return {"_collector_status": _platform_deployment_status(row),
            "items": [{
                "id": str(row.pk), "sha": row.sha, "status": row.status,
                "created": row.created.isoformat(),
                "finished": row.finished.isoformat() if row.finished else None,
                "actor": row.actor,
            }]}


def _dashboard_database():
    """Reachability first; engine facts are enrichment that must never mask it."""
    try:
        base = _database()
    except Exception:
        # A raise-through would become "unavailable" and then "unknown" — a
        # database nobody can reach is a proven failure, not an unknown one.
        return {"_collector_status": "unhealthy",
                "_collector_reason": "database_unreachable",
                "reachable": False, "vendor": connection.vendor}
    if not base.get("reachable"):
        return {"_collector_status": "unhealthy",
                "_collector_reason": "database_unreachable", **base}
    data = {"_collector_status": "healthy", "engine": None, "version": None,
            "drift": None, **base}
    try:
        data["engine"] = _ENGINE_DISPLAY.get(
            connection.vendor, str(connection.vendor or "").title() or None)
    except Exception:
        pass
    try:
        version = connection.get_database_version()
        data["version"] = ".".join(str(part) for part in version) \
            if isinstance(version, (tuple, list)) else str(version)
    except Exception:
        pass
    try:
        data["drift"] = _version_drift_finding(
            ("rds", "aurora"), data.get("version"))
    except Exception:
        pass
    return data


def _dashboard_cache():
    """Redis down MUST redden the page — never degrade into 'unknown'."""
    try:
        base = _redis()
    except Exception:
        return {"_collector_status": "unhealthy",
                "_collector_reason": "cache_unreachable", "reachable": False}
    if not base.get("reachable"):
        return {"_collector_status": "unhealthy",
                "_collector_reason": "cache_unreachable", **base}
    data = {"_collector_status": "healthy", "engine": "Redis", "version": None,
            "memory_used": None, **base}
    try:
        # Plain .info(): on RedisCluster redis-py routes a section-less INFO to
        # the default node, while a named section fans out to every node.
        with _redis_client() as redis:
            info = redis.info()
        data["version"] = info.get("redis_version")
        data["memory_used"] = info.get("used_memory_human")
    except Exception:
        pass
    return data


def _dashboard_certificates():
    """Zero managed certificates means 'not managed here', never a failure."""
    from mojo.apps.dnsman.models import Certificate
    base = _certificates()
    total = sum(base["counts"].values())
    data = {"total": total, "active": base["counts"].get("active", 0),
            "failing": base["counts"].get("failed", 0)
            + base["counts"].get("revoked", 0),
            "soonest_renew": None, "soonest_renew_name": None,
            "soonest_expiry": None, **base}
    if not total:
        return {"_collector_status": "unconfigured", **data}
    active = Certificate.objects.filter(status="active")
    renew = active.filter(renew_after__isnull=False).order_by(
        "renew_after").values("common_name", "renew_after").first()
    expiry = active.filter(not_after__isnull=False).order_by(
        "not_after").values("not_after").first()
    if renew:
        data["soonest_renew"] = renew["renew_after"].isoformat()
        data["soonest_renew_name"] = renew["common_name"]
    if expiry:
        data["soonest_expiry"] = expiry["not_after"].isoformat()
    return {"_collector_status": "unhealthy" if data["failing"] else "healthy",
            "_collector_reason": "certificate_failed" if data["failing"] else None,
            **data}


def _recent_job_failures():
    """Failures inside the window. A seam, so the row's logic is testable."""
    from mojo.apps.jobs.models import Job
    # status and modified are both indexed, so this is one bounded count.
    return Job.objects.filter(
        status="failed",
        modified__gte=timezone.now() - JOBS_FAILURE_WINDOW).count()


def _dashboard_jobs():
    """Queue depth, plus failures inside the last hour — never an outage.

    ``_jobs`` reports a stalled scheduler as unhealthy, which is right for the
    Platform evidence card. Here it is deliberately downgraded to amber: work
    piling up is not proof that customers cannot use the system, and this row
    is not an availability source.

    The all-time ``failed`` count stays in the payload for the drill-in but
    never colours the row — on a long-lived queue it is permanently large.
    """
    data = dict(_jobs())
    data["failed_recent"] = _recent_job_failures()
    if not data.get("scheduler_active"):
        status, reason = "degraded", "scheduler_inactive"
    elif data["failed_recent"]:
        status, reason = "degraded", "recent_failures"
    else:
        status, reason = "healthy", None
    data["_collector_status"] = status
    data["_collector_reason"] = reason
    return data


def _dashboard_sanity():
    """The core node checks this node can actually prove about itself.

    Two deliberate narrowings versus ``_sanity``:

    * ``local request`` is dropped unless the operator configured the local
      target explicitly. An inferred target that does not answer is evidence
      about the inference, not about this node — the same suppression the
      Setup report already applies (``operatorChecks``).
    * A failing check is amber, never red. These checks are cheap local
      liveness probes, and the Dashboard's red is reserved for proven
      customer-facing failure.
    """
    with _redis_client() as redis:
        data = dict(_sanity(redis_client=redis))
    if data.get("local_target_source") != "configured_static":
        data["checks"] = [row for row in data.get("checks") or []
                          if row.get("name") != "local request"]
    checks = data.get("checks") or []
    healthy = bool(checks) and all(row["ok"] for row in checks)
    data["_collector_status"] = "healthy" if healthy else "degraded"
    data["_collector_reason"] = None if healthy else "sanity_check_failed"
    return data


def _newest_drift_event():
    """The newest managed-engine drift event still inside the freshness window."""
    from mojo.apps.aws.services import version_drift
    from mojo.apps.incident.models import Event
    return Event.objects.filter(
        category=version_drift.CATEGORY,
        created__gte=timezone.now() - VERSION_DRIFT_MAX_AGE).order_by(
            "-created").values("metadata").first()


def _version_drift_finding(kinds, current_version=None):
    """The newest still-relevant managed-engine upgrade finding, or None.

    Two suppressions, both deliberate: a finding older than
    ``VERSION_DRIFT_MAX_AGE`` is not evidence about today's engine, and a
    finding whose ``current_version`` no longer matches the version the engine
    actually reports is about a database that has already been upgraded.
    """
    row = _newest_drift_event()
    findings = (row or {}).get("metadata")
    findings = findings.get("findings") if isinstance(findings, dict) else None
    if not isinstance(findings, list):
        return None
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("kind") not in kinds:
            continue
        observed = str(finding.get("current_version") or "")
        if current_version and observed and observed != str(current_version):
            return None
        return {
            "available_major": finding.get("available_major"),
            "deadline": finding.get("deadline"),
            "note": finding.get("note"),
        }
    return None


def _cached_frontend(refresh=False):
    """One shared, short-lived ELBv2 read behind the load balancer and EC2 rows.

    Both collectors run in the same wave, so the cache is what keeps the pair
    to one set of provider calls; a cold race costs at most one extra bounded
    read, which is cheaper than serializing them onto the request thread.
    """
    from django.core.cache import cache
    if not refresh:
        value = cache.get(LOAD_BALANCER_CACHE_KEY)
        if isinstance(value, dict):
            return value
    from mojo.helpers.aws.elbv2 import LoadBalancerHelper
    value = LoadBalancerHelper().frontend()
    cache.set(LOAD_BALANCER_CACHE_KEY, value, LOAD_BALANCER_CACHE_TTL)
    return value


def _provider_unavailable(frontend):
    """No balancer and no evidence: say which, without inventing a failure."""
    if frontend.get("denied"):
        return {"_collector_status": "unknown",
                "_collector_reason": "provider_denied",
                "_collector_reason_detail": {"iam_action": frontend["denied"][0]}}
    if frontend.get("failures"):
        return {"_collector_status": "unknown",
                "_collector_reason": "provider_unavailable"}
    return {"_collector_status": "unconfigured",
            "_collector_reason": "no_load_balancer"}


def _load_balancer(refresh=False):
    """Serving-tier health from the balancer's own registered targets."""
    frontend = _cached_frontend(refresh)
    balancer = frontend.get("balancer") or {}
    serving = [row for row in frontend.get("groups") or [] if row["registered"]]
    data = {
        "configured": frontend.get("configured", False),
        "balancer": balancer or None,
        "groups": frontend.get("groups") or [],
        "registered": frontend.get("registered", 0),
        "healthy": frontend.get("healthy", 0),
        "denied": frontend.get("denied") or [],
        # An NLB with no allocation id has no address that survives its own
        # replacement. Worth saying out loud; not by itself an outage.
        "elastic_ip_missing": bool(
            balancer and not balancer.get("elastic_ips")),
    }
    if not frontend.get("configured"):
        return {**_provider_unavailable(frontend), **data}
    if frontend.get("denied"):
        return {"_collector_status": "unknown",
                "_collector_reason": "provider_denied",
                "_collector_reason_detail": {"iam_action": frontend["denied"][0]},
                **data}
    if any(row["healthy"] == 0 for row in serving):
        return {"_collector_status": "unhealthy",
                "_collector_reason": "no_healthy_target", **data}
    if serving and not balancer.get("addresses"):
        return {"_collector_status": "unhealthy",
                "_collector_reason": "no_balancer_address", **data}
    if not serving:
        return {"_collector_status": "unconfigured",
                "_collector_reason": "no_registered_target", **data}
    if data["healthy"] < data["registered"]:
        return {"_collector_status": "degraded",
                "_collector_reason": "target_unhealthy", **data}
    return {"_collector_status": "healthy", **data}


def _compute(refresh=False):
    """EC2 scoped to what the balancer actually serves, when there is one."""
    from mojo.helpers.aws.provider_call import ProviderCallError
    frontend = _cached_frontend(refresh)
    ids = list(frontend.get("instance_ids") or [])
    if ids:
        healthy = [value for value in frontend.get("healthy_instance_ids") or []]
        names = {}
        try:
            from mojo.helpers.aws.elbv2 import LoadBalancerHelper
            names = LoadBalancerHelper().instance_names(ids)
        except Exception:
            names = {}
        status = "healthy" if len(healthy) == len(ids) else "degraded"
        if not healthy:
            status = "unhealthy"
        return {"_collector_status": status, "source": "target_group",
                "total": len(ids), "up": len(healthy),
                "instances": [{"id": value, "name": names.get(value, value),
                               "healthy": value in healthy} for value in ids]}
    if frontend.get("denied"):
        return {"_collector_status": "unknown",
                "_collector_reason": "provider_denied",
                "_collector_reason_detail": {"iam_action": frontend["denied"][0]},
                "source": "target_group", "total": 0, "up": 0, "instances": []}
    # No balancer to scope by. A raw instance count proves nothing about
    # whether traffic is being served, so this path NEVER reports unhealthy.
    try:
        from mojo.helpers.aws.elbv2 import LoadBalancerHelper
        page = LoadBalancerHelper().ec2.describe_instances(
            Filters=[{"Name": "instance-state-name", "Values": ["running"]}],
            MaxResults=100)
    except ProviderCallError as err:
        detail = err.detail()
        value = {"_collector_status": "unknown", "source": "ec2",
                 "_collector_reason": "provider_denied" if err.denied
                 else "provider_unavailable",
                 "total": 0, "up": 0, "instances": []}
        if detail.get("iam_action"):
            value["_collector_reason_detail"] = {"iam_action": detail["iam_action"]}
        return value
    except Exception:
        return {"_collector_status": "unknown", "source": "ec2",
                "_collector_reason": "provider_unavailable",
                "total": 0, "up": 0, "instances": []}
    running = sum(len(row.get("Instances") or [])
                  for row in page.get("Reservations") or [])
    return {"_collector_status": "healthy" if running else "unconfigured",
            "source": "ec2", "total": running, "up": running, "instances": []}


def _framework(refresh=False):
    """Installed django-mojo, and whether an unpinned fleet is behind PyPI."""
    from mojo.apps.edge.services import framework_version
    if refresh:
        framework_version.refresh()
    value = framework_version.status()
    update = bool(value.get("update_available"))
    return {"_collector_status": "degraded" if update else "healthy",
            "_collector_reason": "update_available" if update else None,
            **value}


def _hosting():
    from django.db.models import Count
    from mojo.apps.dnsman.models import Certificate, Domain
    from mojo.apps.edge.models import Upstream, Vhost, VhostRoute
    domain_counts = {row["status"]: row["count"] for row in
                     Domain.objects.values("status").annotate(count=Count("id"))}
    return {
        "domains": domain_counts, "certificates": Certificate.objects.count(),
        "vhosts": Vhost.objects.count(), "upstreams": Upstream.objects.count(),
        "routes": VhostRoute.objects.count(),
    }


def _aws_inventory():
    if not settings.get_static("ADMIN_AWS_INVENTORY_ENABLED", False, kind="bool"):
        return {"configured": False, "resources": {}}
    from mojo.helpers.aws.cloudwatch import CloudWatchHelper
    helper = CloudWatchHelper(timeout=2)
    ec2_page = helper.ec2.describe_instances(MaxResults=100)
    rds_page = helper.rds.describe_db_instances(MaxRecords=100)
    redis_page = helper.elasticache.describe_cache_clusters(MaxRecords=100)
    ec2 = []
    for reservation in ec2_page.get("Reservations", [])[:ROW_LIMIT]:
        for row in reservation.get("Instances", [])[:ROW_LIMIT - len(ec2)]:
            ec2.append({"id": row.get("InstanceId"),
                        "state": (row.get("State") or {}).get("Name"),
                        "type": row.get("InstanceType")})
    return {
        "configured": True,
        "resources": {
            "ec2": ec2[:ROW_LIMIT],
            "rds": [{"id": row.get("DBInstanceIdentifier"),
                     "engine": row.get("Engine"), "status": row.get("DBInstanceStatus"),
                     "class": row.get("DBInstanceClass")}
                    for row in rds_page.get("DBInstances", [])[:ROW_LIMIT]],
            "redis": [{"id": row.get("CacheClusterId"), "engine": row.get("Engine"),
                       "status": row.get("CacheClusterStatus"),
                       "nodes": row.get("NumCacheNodes")}
                      for row in redis_page.get("CacheClusters", [])[:ROW_LIMIT]],
        },
    }


def _settings():
    from mojo.apps.account.services import auth_config
    raw = system_settings.get_value(system_settings.AUTH_CONFIG, {})
    public = auth_config.public_auth_config(
        auth_config.resolve_auth_config())
    return {
        "auth": public,
        "editable": sorted(system_settings.AUTH_SAFE_PATHS),
        "edge_topology": system_settings.get_value(
            system_settings.EXPECTED_EDGE_TOPOLOGY, {"nodes": [], "pools": []}),
        "stored_auth_object": isinstance(raw, dict),
    }


def platform_overview(request):
    # The deploy stderr tail rides inside node_evidence but belongs to the
    # security tier. Decide once, on the request thread, and close it into the
    # collector — _section_map submits zero-arg callables to a pool.
    stderr = _permitted(request, "view_platform_security", "manage_platform", "admin")
    specs = {
        "api": (("view_platform", "manage_platform", "admin"), _api),
        "fleet": (("view_platform", "manage_platform", "admin"), _fleet),
        "jobs": (("view_platform", "manage_platform", "admin"), _jobs),
        "sanity": (("view_platform", "manage_platform", "admin"), _sanity),
        "database": (("view_platform", "manage_platform", "admin"), _database),
        "redis": (("view_platform", "manage_platform", "admin"), _redis),
        "deployments": (("view_platform", "manage_platform", "admin"),
                        lambda: _deployments(include_stderr=stderr)),
        "certificates": (("view_platform", "manage_platform", "admin"), _certificates),
        "security": (("view_platform_security", "manage_platform", "admin"), _security),
        "webapps": (("view_platform", "manage_platform", "admin"), _webapps),
    }
    # `?sections=` names an allowlist of sections to collect (item 2158): the
    # merged Deployments page reads only deployments+api, and paying the full
    # roster there would fan out per-app summary_for + HTTPS probes on every
    # visit. The filter narrows WORK, never authority — each named section
    # keeps its own permission tuple. Unknown names are ignored; a bare or
    # empty parameter keeps today's full roster. A parameter naming no known
    # section collects nothing: the caller asked to narrow, and widening a
    # typo into the full fan-out is the exact cost this filter removes.
    # Non-string values (service callers pass bare mock/service requests)
    # read as absent.
    wanted = request.DATA.get("sections") if hasattr(request, "DATA") else None
    if isinstance(wanted, str) and wanted.strip():
        names = {name.strip() for name in wanted.split(",") if name.strip()}
        specs = {name: spec for name, spec in specs.items() if name in names}
    return {
        "schema_version": SCHEMA_VERSION,
        "sections": _section_map(request, specs),
    }


def advanced_overview(request):
    return {
        "schema_version": SCHEMA_VERSION,
        "sections": _section_map(request, {
            "hosting": (("view_advanced", "manage_advanced", "admin"), _hosting),
            "aws_inventory": (("view_advanced_inventory", "manage_advanced", "admin"), _aws_inventory),
            "network_security": (("view_advanced_security", "manage_advanced", "admin"), _security),
        }),
    }


_DASHBOARD_STATUS_ORDER = {
    "healthy": 0,
    "unconfigured": 1,
    "stale": 2,
    "degraded": 3,
    "unhealthy": 4,
}


def _dashboard_envelope(value, *, empty_is_unconfigured=False):
    """Normalize one collector without inventing health from missing evidence."""
    value = dict(value or {})
    status = value.get("status", "unavailable")
    if status == "unauthorized":
        status = "permission_denied"
    elif status == "unavailable":
        status = "unknown"
    elif status == "timeout":
        status = "degraded"
    elif status not in _DASHBOARD_STATUS_ORDER:
        status = "unknown"
    stale_after = parse_datetime(str(value.get("stale_after") or ""))
    if status == "healthy" and stale_after and stale_after < timezone.now():
        status = "stale"
    data = value.get("data") if isinstance(value.get("data"), dict) else {}
    if empty_is_unconfigured and status == "healthy" and not data.get("items"):
        status = "unconfigured"
    return {
        "status": status,
        "observed_at": value.get("observed_at"),
        "stale_after": value.get("stale_after"),
        "reason": value.get("reason"),
        "reason_detail": value.get("reason_detail"),
        "data": data,
    }


def _attention(request, model_name, permissions):
    """Count one attention source only after its own permission succeeds."""
    if not _permitted(request, *permissions):
        return _envelope("unauthorized", reason="permission_required")
    def collect():
        if model_name == "incidents":
            rows = _open_incidents()
        else:
            from mojo.apps.incident.models import Ticket
            rows = Ticket.objects.exclude(status__in=("resolved", "closed"))
        count = rows.count()
        oldest = rows.order_by("created").values_list(
            "created", flat=True).first()
        return {"_collector_status": "healthy" if count == 0 else "degraded",
                "_collector_reason": "open_attention" if count else None,
                "open": count,
                "oldest_created": oldest.isoformat() if oldest else None,
                "oldest_age_days": (timezone.now() - oldest).days if oldest else None}
    return _collect(collect)


# The sources that can prove customers cannot use the system. A backlog of
# open incidents is work, not an outage, so it is deliberately not here.
AVAILABILITY_SOURCES = (
    "load_balancer", "compute", "database", "cache", "certificates", "public_api")

# Verbatim row names, so the headline names the same thing the row does.
SOURCE_LABELS = {
    "load_balancer": "Load balancer",
    "compute": "EC2",
    "database": "RDS",
    "cache": "Elasticache",
    "certificates": "SSL certs",
    "public_api": "Public API",
}


def _availability(sources):
    """Green/red from proven failures only — never from attention or denial."""
    down = [SOURCE_LABELS[name] for name in AVAILABILITY_SOURCES
            if (sources.get(name) or {}).get("status") == "unhealthy"]
    if down:
        message = f"{down[0]} is down" if len(down) == 1 else \
            f"{len(down)} things are down: {', '.join(down)}"
        return {"state": "down", "message": message, "down": down}
    reporting = any((sources.get(name) or {}).get("status") == "healthy"
                    for name in AVAILABILITY_SOURCES)
    if not reporting:
        return {"state": "unknown",
                "message": "Status unknown — no source is reporting.", "down": []}
    return {"state": "ok", "message": "Everything is running", "down": []}


def _attention_message(sources, down):
    """The muted sub-line. Open work is never allowed to sound like an outage."""
    incidents = sources.get("incidents") or {}
    if incidents.get("status") == "permission_denied":
        return None
    count = (incidents.get("data") or {}).get("open") or 0
    if not count:
        return None
    noun = "incident needs" if count == 1 else "incidents need"
    if down:
        return f"{count} {noun} review."
    return f"{count} {noun} review — nothing is down."


def dashboard_overview(request, refresh=False):
    """Return the small, independently permissioned Admin landing matrix.

    This intentionally never calls System Setup readiness. Setup is a
    superuser-only workflow, not an implicit Dashboard dependency;
    ``_dashboard_sanity`` adds only the bounded node checks (one local HTTP
    probe with a 1s budget, plus a bounded Redis client), never the readiness
    report's provider fan-out.
    """
    platform = ("view_platform", "manage_platform", "admin")
    security = ("view_security", "manage_security", "security", "admin")
    raw = _section_map(request, {
        "load_balancer": (platform, lambda: _load_balancer(refresh)),
        "compute": (platform, lambda: _compute(refresh)),
        "database": (platform, _dashboard_database),
        "cache": (platform, _dashboard_cache),
        "certificates": (platform, _dashboard_certificates),
        "public_api": (platform, _api),
        "framework": (platform, lambda: _framework(refresh)),
        "last_deployment": (platform, _dashboard_deployment),
        "jobs": (platform, _dashboard_jobs),
        "sanity": (platform, _dashboard_sanity),
    })
    raw["incidents"] = _attention(request, "incidents", security)
    raw["tickets"] = _attention(request, "tickets", security)
    sources = {
        name: _dashboard_envelope(
            value, empty_is_unconfigured=name == "last_deployment")
        for name, value in raw.items()
    }
    availability = _availability(sources)
    return {
        "schema_version": DASHBOARD_SCHEMA_VERSION,
        "observed_at": timezone.now().isoformat(),
        "availability": availability,
        "attention": {
            "message": _attention_message(sources, availability["down"])},
        "sources": sources,
    }


def framework_overview(request, refresh=False):
    """Which django-mojo runs here, and whether this caller could update it.

    Entirely derived from ``framework_version.status()`` — there is exactly one
    PyPI check in this codebase and this is not a second one.

    ``blocked_reason`` names the ONE thing standing in the way, so the portal
    can disable the control and say why instead of offering an action that
    fails:

    * ``update_unavailable`` — nothing newer is published, or PyPI is not
      answering.
    * ``requires_superuser`` — a pin or hold is set. Clearing it is a protected
      settings write, and only a literal superuser may make it.
    * ``no_converged_deployment`` — nothing has ever been proven to run on this
      fleet, so there is no commit to redeploy. Checked last because it is the
      only reason that costs a query.
    """
    from mojo.apps.edge.services import framework_version, platform_deploy
    if refresh:
        framework_version.refresh()
    value = framework_version.status()
    pin = value.get("pin") or {"mode": "latest", "value": None}
    pinned = pin.get("mode") != "latest"
    superuser = bool(getattr(request.user, "is_superuser", False))

    blocked = None
    if not value.get("latest") or not (value.get("update_available") or pinned):
        blocked = "update_unavailable"
    elif pinned and not superuser:
        blocked = "requires_superuser"
    elif platform_deploy.last_converged_deployment() is None:
        blocked = "no_converged_deployment"
    return {
        "schema_version": SCHEMA_VERSION,
        "installed": value.get("installed"),
        "latest": value.get("latest"),
        "checked_at": value.get("checked_at"),
        "source": value.get("source"),
        "update_available": bool(value.get("update_available")),
        "pin": pin,
        "can_update": blocked is None,
        "blocked_reason": blocked,
    }


def apply_framework_update(request, version):
    """Put the fleet back on the newest published django-mojo.

    Two steps, in this order, and NEITHER of them writes a version into the pin:

    1. If a pin or hold is set, CLEAR it. The pin is what would otherwise make
       step 2 reinstall the version the operator is trying to leave. Writing
       ``version`` into it instead would silently freeze the fleet at today's
       release — the opposite of "update", and invisible until the next one.
       The writer enforces the literal-superuser rule itself.
    2. Redeploy the last converged commit. The framework version is resolved at
       install time, so re-running the proven commit is what actually installs
       the new release; there is no separate "install framework" path.
    """
    from mojo.apps.edge.services import deploy, platform_deploy
    from mojo.apps.edge.settings_validators import FRAMEWORK_VERSION_KEY

    pin = None
    try:
        pin = deploy.framework_version_pin()
    except Exception:
        pin = None
    if pin:
        # Target records the transition, matching the framework_pin writer's
        # convention: "who cleared what" must survive in the audit trail.
        system_settings.set_value(request.user, FRAMEWORK_VERSION_KEY, "")
        audit_after_commit(request.user, "framework_pin", f"{pin}->latest")

    row = platform_deploy.last_converged_deployment()
    if row is None:
        raise merrors.ValueException(
            "No deployment has ever converged on this fleet, so there is no "
            "proven commit to redeploy.", code=409, status=409)
    try:
        deployment = platform_deploy.same_sha_retry(
            row, actor=f"framework-update:{request.user.pk}",
            created_by=request.user,
            idempotency_key=request.META.get("HTTP_IDEMPOTENCY_KEY"))
    except deploy.DeploymentCoordinationError:
        raise merrors.ValueException(
            "Deploy coordination unavailable", code=503, status=503) from None
    audit_after_commit(request.user, "framework_update",
                       f"{version}:{deployment.pk}")
    return {
        "schema_version": SCHEMA_VERSION,
        "requested": True,
        "version": version,
        "cleared_pin": pin or None,
        "deployment": platform_deploy.serialize(deployment, include_stderr=True),
    }


def audit_after_commit(actor, action, target=""):
    actor_id = getattr(actor, "pk", None)
    def write():
        from mojo.apps.incident import report_event_suppressed
        report_event_suppressed(
            f"Admin platform action={action} actor={actor_id} target={str(target)[:80]}",
            title="Admin platform control used", category="admin_platform", level=5,
            key=f"admin-platform:{action}:{actor_id}:{str(target)[:80]}")
    transaction.on_commit(write)
