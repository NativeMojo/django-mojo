"""Permission-separated, bounded evidence for Admin Platform/Advanced."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import timedelta

from django.db import connection, transaction
from django.utils import timezone

from mojo.apps.account.services import system_settings
from mojo.helpers.settings import settings


SCHEMA_VERSION = 1
COLLECTOR_TIMEOUT = 3.0
STALE_SECONDS = 600
ROW_LIMIT = 100


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
    from mojo.helpers.redis import get_client
    return {"reachable": bool(get_client().ping())}


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
    import json
    from mojo.apps.jobs.keys import JobKeys
    from mojo.helpers.redis import get_connection
    redis = get_connection()
    keys = JobKeys()
    rows = []
    # Exactly one bounded SCAN page. scan_iter can traverse the entire Redis
    # keyspace in a worker that outlives the response timeout.
    cursor, page = redis.scan(
        cursor=0, match=keys.runner_hb("*"), count=ROW_LIMIT)
    truncated = bool(cursor) or len(page) > ROW_LIMIT
    for key in list(page)[:ROW_LIMIT]:
        raw = redis.get(key)
        try:
            row = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except (TypeError, ValueError):
            continue
        if "edge" not in (row.get("channels") or []):
            continue
        rows.append({
            "runner": str(row.get("runner_id") or "")[:64],
            "channels": sorted({str(item)[:64]
                                for item in (row.get("channels") or [])})[:32],
            "last_heartbeat": str(row.get("last_heartbeat") or "")[:64],
        })
    return {"_collector_status": "healthy" if rows else "unhealthy",
            "channel": "edge", "runners": rows, "truncated": truncated}


def _jobs():
    from mojo.apps.jobs.keys import JobKeys
    from mojo.apps.jobs.models import Job
    from mojo.helpers.redis import get_connection
    redis = get_connection()
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
    results = sanity.run({
        "url": system_readiness.trusted_local_api_url(),
        "timeout": 1.0, "retries": 1, "delay": 0,
    })
    rows = [{"name": row.get("name"), "ok": bool(row.get("ok"))}
            for row in results[:16]]
    return {"_collector_status": "healthy" if rows and all(
                row["ok"] for row in rows) else "unhealthy",
            "checks": rows, "migration_check": next((
                row["ok"] for row in rows if row["name"] == "migrations"), False)}


def _deployments():
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import deploy, platform_deploy
    target = deploy.get_target()
    coordination = deploy.get_status()
    rows = PlatformDeployment.objects.select_related("retry_of").all()[:50]
    return {
        "items": [platform_deploy.serialize(row) for row in rows], "limit": 50,
        "desired_commit": (target or {}).get("sha"),
        "desired_deployment": (target or {}).get("deployment"),
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
    open_rows = Incident.objects.exclude(status__in=("resolved", "closed")).order_by(
        "-priority", "-created").values("id", "priority", "status", "category")[:25]
    values.update({
        "cron_heartbeat": {"present": bool(beat), "state": (beat or {}).get("state"),
                           "age_seconds": age},
        "monitoring_delivery": {"present": bool(probe),
                                "observed_at": probe["created"].isoformat() if probe else None,
                                "age_seconds": probe_age,
                                "status": probe["dispatch_status"] if probe else None,
                                "state": probe["new_state"] if probe else None},
        "open_incidents": {"count": Incident.objects.exclude(
            status__in=("resolved", "closed")).count(), "items": list(open_rows)},
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


def _webapps():
    from mojo.apps.edge.models import WebApp, WebAppOnboardingOperation
    from mojo.apps.edge.services import webapp_onboarding
    rows = list(WebApp.objects.select_related("vhost", "api_key").all()[:50])
    operations = WebAppOnboardingOperation.objects.exclude(
        registrar_provider="").values(
            "registrar_provider", "dns_provider", "status")[:ROW_LIMIT]
    provider_evidence = [{
        "registrar": row["registrar_provider"], "dns": row["dns_provider"],
        "matches": row["registrar_provider"] == row["dns_provider"],
        "status": row["status"],
    } for row in operations]
    return {
        "summary_contract": 1,
        "items": [webapp_onboarding.summary_for(row) for row in rows],
        "registrar_vs_dns": provider_evidence,
        "truncated": WebApp.objects.count() > len(rows),
    }


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
    return {
        "schema_version": SCHEMA_VERSION,
        "sections": _section_map(request, {
            "api": (("view_platform", "manage_platform", "admin"), _api),
            "fleet": (("view_platform", "manage_platform", "admin"), _fleet),
            "jobs": (("view_platform", "manage_platform", "admin"), _jobs),
            "sanity": (("view_platform", "manage_platform", "admin"), _sanity),
            "database": (("view_platform", "manage_platform", "admin"), _database),
            "redis": (("view_platform", "manage_platform", "admin"), _redis),
            "deployments": (("view_platform", "manage_platform", "admin"), _deployments),
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
            "settings": (("view_advanced_settings", "manage_advanced", "admin"), _settings),
        }),
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
