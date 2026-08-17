"""Permission-separated, bounded evidence for Admin Platform/Advanced."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError, wait
from contextlib import contextmanager
from datetime import timedelta

from django.db import connection, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from mojo.apps.account.services import system_settings
from mojo.helpers.settings import settings


SCHEMA_VERSION = 1
COLLECTOR_TIMEOUT = 3.0
STALE_SECONDS = 600
ROW_LIMIT = 100
WEBAPP_LIMIT = 24
WEBAPP_WORKERS = 4
WEBAPP_PROBE_TIMEOUT = 1.5
WEBAPP_COLLECTOR_DEADLINE = 2.5
INCIDENT_TERMINAL_STATUSES = ("ignored", "resolved", "closed")


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


def _envelope(status, data=None, reason=None):
    observed = timezone.now()
    value = {
        "status": status, "observed_at": observed.isoformat(),
        "stale_after": (observed + timedelta(seconds=STALE_SECONDS)).isoformat(),
        "data": data if data is not None else {},
    }
    if reason:
        value["reason"] = reason
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
        if isinstance(value, dict):
            value = dict(value)
            status = value.pop("_collector_status", status)
            reason = value.pop("_collector_reason", None)
        return _envelope(status, value, reason=reason)
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
    # At most two waves for today's Platform roster. Each _collect response is
    # bounded; its provider work is independently bounded by SDK/RPC timeouts.
    with ThreadPoolExecutor(max_workers=min(8, len(permitted))) as pool:
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
    if not origin:
        return {"_collector_status": "unconfigured",
                "django_mojo_version": mojo.__version__, "public_origin": None,
                "configured": False}
    proof = system_readiness.probe_public_api_details(origin, timeout=2.0)
    return {"_collector_status": "healthy" if proof["ok"] else "unhealthy",
            "django_mojo_version": mojo.__version__, "public_origin": origin,
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


def _sanity():
    from mojo.apps.account.services import system_readiness
    from mojo.apps.edge.services import sanity
    local_target = system_readiness.trusted_local_api_target()
    results = sanity.run({
        "url": local_target["url"],
        "timeout": 1.0, "retries": 1, "delay": 0,
    })
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


def _certificates():
    from django.db.models import Count
    from mojo.apps.dnsman.models import Certificate
    counts = {row["status"]: row["count"] for row in
              Certificate.objects.values("status").annotate(count=Count("id"))}
    expiring = Certificate.objects.filter(
        not_after__isnull=False,
        not_after__lte=timezone.now() + timedelta(days=30)).count()
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
    rows = list(WebApp.objects.select_related("vhost", "api_key").all()[:WEBAPP_LIMIT + 1])
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


def _dashboard_webapps():
    """Small WebApp rollup; Dashboard does not serialize every application."""
    evidence = _webapp_evidence()
    rollup = dict(evidence["rollup"])
    rollup["_collector_status"] = evidence["_collector_status"]
    rollup["truncated"] = evidence["truncated"]
    return rollup


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


def _dashboard_deployment(include_stderr=False):
    """Return one durable attempt without loading coordination or history."""
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import platform_deploy
    row = PlatformDeployment.objects.select_related("retry_of").first()
    if row is None:
        return {"_collector_status": "unconfigured", "items": []}
    from mojo.apps.edge.services import deploy
    with _redis_client() as redis:
        target = deploy._loads(redis.get(deploy.TARGET_KEY))
    return {"_collector_status": _platform_deployment_status(row),
            "items": [platform_deploy.serialize(
                row, desired_commit=(target or {}).get("sha"),
                include_stderr=include_stderr)]}


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
    return {
        "schema_version": SCHEMA_VERSION,
        "sections": _section_map(request, {
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
        }),
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
        "data": data,
    }


def _attention(request, model_name, permissions):
    """Count one attention source only after its own permission succeeds."""
    if not _permitted(request, *permissions):
        return _envelope("unauthorized", reason="permission_required")
    def collect():
        if model_name == "incidents":
            count = _open_incidents().count()
        else:
            from mojo.apps.incident.models import Ticket
            count = Ticket.objects.exclude(status__in=("resolved", "closed")).count()
        return {"_collector_status": "healthy" if count == 0 else "degraded",
                "_collector_reason": "open_attention" if count else None,
                "open": count}
    return _collect(collect)


def dashboard_overview(request):
    """Return the small, independently permissioned Admin landing matrix.

    This intentionally never calls System Setup readiness. Setup is a
    superuser-only Platform workflow, not an implicit Dashboard dependency.
    """
    stderr = _permitted(request, "view_platform_security", "manage_platform", "admin")
    raw = _section_map(request, {
        "public_api": (("view_platform", "manage_platform", "admin"), _api),
        "fleet": (("view_platform", "manage_platform", "admin"), _fleet),
        "webapps": (("view_dns", "manage_dns", "security", "admin"), _dashboard_webapps),
        "security": (("view_platform_security", "manage_platform", "admin"), _security),
        "last_deployment": (("view_platform", "manage_platform", "admin"),
                            lambda: _dashboard_deployment(include_stderr=stderr)),
    })
    raw["incidents"] = _attention(
        request, "incidents", ("view_security", "manage_security", "security", "admin"))
    raw["tickets"] = _attention(
        request, "tickets", ("view_security", "manage_security", "security", "admin"))
    sources = {
        name: _dashboard_envelope(
            value, empty_is_unconfigured=name == "last_deployment")
        for name, value in raw.items()
    }
    observable = [
        item["status"] for item in sources.values()
        if item["status"] in _DASHBOARD_STATUS_ORDER
    ]
    overall = max(
        observable, key=lambda status: _DASHBOARD_STATUS_ORDER[status]) \
        if observable else "unknown"
    return {
        "schema_version": SCHEMA_VERSION,
        "overall": overall,
        "observable_sources": len(observable),
        "sources": sources,
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
