"""Pending managed-service upgrades, and applying one of them.

The read half is ``VersionDriftScanner`` (which stays read-only and untouched)
plus live resource status, cached briefly so opening the Maintenance page does
not re-scan AWS on every render.

The write half exists because the alternative is an operator doing the same
upgrade from the AWS console with no record of who asked for it. It is
deliberately narrow:

* Only a version the SERVER re-derived from its own scan can be applied.
  ``offered_target`` is called on the apply path for exactly this reason — a
  client-supplied ``target_version`` is a request, never an instruction.
* One apply per resource at a time, enforced by a short single-flight key. On a
  cache outage the answer is 503, not "go ahead" — a second concurrent
  major-version upgrade against a live database is the failure this guards.
* Success means the ENGINE VERSION CHANGED. A resource returning to
  "available" proves only that AWS finished doing something.

Raw provider text never reaches a caller: every failure is re-raised as
``MaintenanceError`` carrying ``ProviderCallError.detail()``.
"""

from django.core.cache import cache

from mojo.helpers import infrastructure
from mojo.helpers import logit
from mojo.helpers.aws import elasticache as elasticache_helper
from mojo.helpers.aws import rds as rds_helper
from mojo.helpers.aws.provider_call import ProviderCallError
from mojo.helpers.settings import settings


logger = logit.get_logger("aws_maintenance", "aws.log")

SCHEMA_VERSION = 1
CACHE_PREFIX = "mojo:aws:maintenance"
REPORT_TTL = 600
# Long enough to cover a slow modify round trip, short enough that a crashed
# request cannot wedge a resource for the rest of the day.
APPLY_LOCK_TTL = 120

KIND_RDS_INSTANCE = "rds-instance"
KIND_RDS_CLUSTER = "rds-cluster"
KIND_ELASTICACHE = "elasticache"
KINDS = (KIND_RDS_INSTANCE, KIND_RDS_CLUSTER, KIND_ELASTICACHE)


class MaintenanceError(Exception):
    """A wire-safe refusal or provider failure, with the status it should get."""

    def __init__(self, message, error_code, status=400, data=None):
        self.message = str(message)
        self.error_code = str(error_code)
        self.status = int(status)
        self.data = dict(data or {})
        super().__init__(self.message)


def _setting(name, default=None, kind=None):
    try:
        return settings.get_static(name, default, kind=kind) if kind \
            else settings.get_static(name, default)
    except Exception:
        return default


def _region():
    return _setting("AWS_REGION", "us-east-1")


def _report_key(region):
    return f"{CACHE_PREFIX}:{region}"


def _apply_key(kind, resource_id):
    return f"{CACHE_PREFIX}:apply:{kind}:{resource_id}"


def _provider_error(err, message):
    """The single translation from a provider failure to a wire answer."""
    if err.denied:
        status, code = 403, "provider_denied"
    elif err.retryable:
        status, code = 503, "provider_unavailable"
    else:
        status, code = 502, "provider_error"
    return MaintenanceError(message, code, status, {"failure": err.detail()})


# ── report ──────────────────────────────────────────────────────────────────

def _statuses(findings, rds_client=None, elasticache_client=None):
    """Live status for exactly the services the findings mention.

    A denied describe degrades to "status unknown" on those rows and is named
    in ``warnings`` — a Maintenance page that silently hid the RDS rows because
    one describe was refused would look identical to one with nothing to do.
    """
    kinds = {finding.get("kind") for finding in findings}
    statuses = {kind: {} for kind in KINDS}
    warnings = []

    def read(kind, iam_action, loader):
        try:
            statuses[kind] = loader() or {}
        except ProviderCallError as err:
            detail = err.detail()
            warnings.append({
                "iam_action": detail.get("iam_action") or iam_action,
                "aws_code": detail.get("provider_code"),
                "message": (f"{iam_action} was denied; live status for these "
                            f"resources was not read"),
            })
            logger.warning("maintenance status read failed %s", detail)

    if KIND_RDS_INSTANCE in kinds:
        read(KIND_RDS_INSTANCE, "rds:DescribeDBInstances",
             lambda: rds_helper.instance_statuses(client=rds_client))
    if KIND_RDS_CLUSTER in kinds:
        read(KIND_RDS_CLUSTER, "rds:DescribeDBClusters",
             lambda: rds_helper.cluster_statuses(client=rds_client))
    if KIND_ELASTICACHE in kinds:
        read(KIND_ELASTICACHE, "elasticache:DescribeCacheClusters",
             lambda: elasticache_helper.group_statuses(client=elasticache_client))
    return statuses, warnings


def _enrich(finding, statuses):
    live = (statuses.get(finding.get("kind")) or {}).get(finding.get("resource_id"))
    row = dict(finding)
    if live is None:
        row.update({"status": None, "pending_version": None, "settled": None})
        return row
    if finding.get("kind") == KIND_ELASTICACHE:
        row.update({
            "status": live.get("status"),
            "pending_version": None,
            "settled": live.get("status") == elasticache_helper.SETTLED,
            "members": len(live.get("members") or []),
            "engine_versions": list(live.get("engine_versions") or []),
        })
        return row
    row.update({
        "status": live.get("status"),
        "pending_version": live.get("pending_version"),
        "settled": live.get("status") == rds_helper.SETTLED,
    })
    return row


def _build(scanner=None, rds_client=None, elasticache_client=None):
    from mojo.apps.aws.services.version_drift import VersionDriftScanner

    scan = (scanner or VersionDriftScanner()).scan()
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": scan.get("generated_at"),
        "region": scan.get("region"),
        "status": scan.get("status"),
        "level": scan.get("level"),
        "findings": [],
        "warnings": list(scan.get("warnings") or []),
        # An operator looking at an empty page deserves to know whether nothing
        # is out of date or nothing has been LOOKED at since the last page load.
        "scheduled": bool(_setting("AWS_VERSION_DRIFT_ENABLED", False, kind="bool")),
    }
    if scan.get("reason"):
        envelope["reason"] = scan["reason"]
    if scan.get("status") != "ok":
        return envelope
    findings = scan.get("findings") or []
    statuses, warnings = _statuses(findings, rds_client, elasticache_client)
    envelope["findings"] = [_enrich(finding, statuses) for finding in findings]
    envelope["warnings"].extend(warnings)
    return envelope


def report(refresh=False, scanner=None, rds_client=None, elasticache_client=None):
    """The Maintenance page's whole read. Cached for ten minutes per region."""
    key = _report_key(_region())
    if not refresh:
        try:
            cached = cache.get(key)
        except Exception:
            # A cache outage must not take the page down — it only costs a scan.
            cached = None
        if isinstance(cached, dict) and cached.get("schema_version") == SCHEMA_VERSION:
            return cached
    envelope = _build(scanner, rds_client, elasticache_client)
    try:
        cache.set(key, envelope, REPORT_TTL)
    except Exception:
        logger.warning("maintenance report could not be cached")
    return envelope


def invalidate():
    try:
        cache.delete(_report_key(_region()))
    except Exception:
        logger.warning("maintenance report cache could not be invalidated")


def offered_target(kind, resource_id, current):
    """The version the SERVER is offering for this resource, or None.

    The apply path re-derives the target through this function instead of
    trusting the request body, so a stale tab or a hand-made request can never
    move a database to a version this installation never offered.
    """
    for finding in (current or {}).get("findings") or []:
        if finding.get("kind") == kind and finding.get("resource_id") == resource_id:
            return finding.get("available_major")
    return None


def finding(kind, resource_id, current):
    for row in (current or {}).get("findings") or []:
        if row.get("kind") == kind and row.get("resource_id") == resource_id:
            return row
    return None


# ── apply ───────────────────────────────────────────────────────────────────

def _claim(kind, resource_id, actor_pk):
    """Single-flight. A cache that cannot answer is a refusal, never a go-ahead."""
    key = _apply_key(kind, resource_id)
    try:
        acquired = cache.add(key, actor_pk, APPLY_LOCK_TTL)
    except Exception:
        raise MaintenanceError(
            "The coordination cache is unavailable, so a second concurrent "
            "upgrade cannot be ruled out. Try again shortly.",
            "cache_unavailable", 503) from None
    if acquired:
        return
    # `add` returning False means either "somebody holds it" or "the backend
    # silently did nothing". Reading the key back is what tells those apart,
    # and they get different answers.
    try:
        holder = cache.get(key)
    except Exception:
        holder = None
    if holder:
        raise MaintenanceError(
            "An upgrade for this resource is already in progress.",
            "upgrade_in_progress", 409)
    raise MaintenanceError(
        "The coordination cache is unavailable, so a second concurrent "
        "upgrade cannot be ruled out. Try again shortly.",
        "cache_unavailable", 503)


def _dispatch(kind, resource_id, target_version, apply_immediately,
              rds_client=None, elasticache_client=None):
    if kind == KIND_RDS_INSTANCE:
        rds_helper.modify_instance_engine_version(
            resource_id, target_version, apply_immediately, client=rds_client)
        return {"operation": "rds.modify_db_instance"}
    if kind == KIND_RDS_CLUSTER:
        rds_helper.modify_cluster_engine_version(
            resource_id, target_version, apply_immediately, client=rds_client)
        return {"operation": "rds.modify_db_cluster"}
    # ElastiCache: which modify operation applies is a property of the resource,
    # resolved live rather than read off a report that may be minutes old.
    shape = elasticache_helper.resolve_kind(resource_id, client=elasticache_client)
    if shape == elasticache_helper.KIND_REPLICATION_GROUP:
        elasticache_helper.modify_replication_group_engine_version(
            resource_id, target_version, apply_immediately, client=elasticache_client)
        return {"operation": "elasticache.modify_replication_group"}
    elasticache_helper.modify_cache_cluster_engine_version(
        resource_id, target_version, apply_immediately, client=elasticache_client)
    return {"operation": "elasticache.modify_cache_cluster"}


def apply_upgrade(actor, kind, resource_id, target_version, apply_immediately,
                  scanner=None, rds_client=None, elasticache_client=None):
    """Request ONE engine-version upgrade the server itself is offering."""
    # Backstop for NON-REST callers (a shell, a job, a future importer). The
    # REST gate already answered HTTP for every ordinary caller, so reaching
    # this line means something bypassed it — which is exactly when a
    # deliberate, logged refusal is worth the duplication.
    if infrastructure.is_external():
        logger.error(
            "maintenance apply refused: %s is %s on this installation",
            infrastructure.SETTING, infrastructure.EXTERNAL)
        raise MaintenanceError(
            infrastructure.refusal_message("Applying an engine-version upgrade"),
            infrastructure.ERROR_CODE, 403)
    if kind not in KINDS:
        raise MaintenanceError(f"Unknown maintenance kind '{kind}'", "invalid_request")
    if not resource_id:
        raise MaintenanceError("A resource identifier is required", "invalid_request")

    current = report(scanner=scanner, rds_client=rds_client,
                     elasticache_client=elasticache_client)
    offered = offered_target(kind, resource_id, current)
    if not offered:
        raise MaintenanceError(
            "No upgrade is currently offered for this resource.",
            "upgrade_not_offered", 409)
    if target_version != offered:
        raise MaintenanceError(
            f"This installation is offering {offered} for this resource, "
            f"not {target_version}. Reload Maintenance and try again.",
            "upgrade_not_offered", 409)

    row = finding(kind, resource_id, current) or {}
    _claim(kind, resource_id, getattr(actor, "pk", None))
    try:
        dispatched = _dispatch(kind, resource_id, offered, apply_immediately,
                               rds_client, elasticache_client)
    except ProviderCallError as err:
        # The single-flight key is deliberately NOT released here: a failed
        # mutation is "attempted" or "unknown", never proof nothing happened,
        # and a retry racing an in-flight change is worse than waiting 120s.
        raise _provider_error(
            err, "AWS refused or could not complete the upgrade request.") from None
    invalidate()
    logger.info(
        "maintenance upgrade requested kind=%s resource=%s target=%s immediate=%s actor=%s",
        kind, resource_id, offered, bool(apply_immediately), getattr(actor, "pk", None))
    return {
        "schema_version": SCHEMA_VERSION,
        "requested": True,
        "resource": resource_id,
        "kind": kind,
        "engine": row.get("engine"),
        "from_version": row.get("current_version"),
        "target_version": offered,
        "apply_immediately": bool(apply_immediately),
        "release_notes_url": row.get("release_notes_url"),
        **dispatched,
    }


# ── poll ────────────────────────────────────────────────────────────────────

def resource_status(kind, resource_id, target, rds_client=None, elasticache_client=None):
    """Live progress for one resource against the version it is moving to.

    ``upgraded`` is the only success signal. ``settled`` says AWS finished; it
    does NOT say the engine moved, and an upgrade that quietly did not take
    effect leaves the resource available on its old version.
    """
    if kind not in KINDS:
        raise MaintenanceError(f"Unknown maintenance kind '{kind}'", "invalid_request")
    try:
        if kind == KIND_RDS_INSTANCE:
            live = rds_helper.instance_status(resource_id, client=rds_client)
        elif kind == KIND_RDS_CLUSTER:
            live = rds_helper.cluster_status(resource_id, client=rds_client)
        else:
            live = elasticache_helper.group_status(resource_id, client=elasticache_client)
    except ProviderCallError as err:
        raise _provider_error(err, "AWS did not report this resource's status.") from None

    row = {"schema_version": SCHEMA_VERSION, "kind": kind, "resource": resource_id,
           "target_version": target or None, "found": live is not None,
           "status": None, "engine_version": None, "engine_versions": [],
           "pending_version": None, "settled": False, "upgraded": False}
    if live is None:
        return row

    if kind == KIND_ELASTICACHE:
        members = live.get("members") or []
        versions = [member.get("engine_version") for member in members]
        row.update({
            "status": live.get("status"),
            "engine_versions": list(live.get("engine_versions") or []),
            "engine_version": versions[0] if versions else None,
            # Every member, not any member: a group whose replica is still on
            # the old engine has not been upgraded.
            "settled": bool(members) and all(
                member.get("status") == elasticache_helper.SETTLED for member in members),
            "upgraded": bool(target) and bool(members) and all(
                version == target for version in versions),
        })
        return row

    version = live.get("engine_version")
    row.update({
        "status": live.get("status"),
        "engine_version": version,
        "engine_versions": [version] if version else [],
        "pending_version": live.get("pending_version"),
        "settled": live.get("status") == rds_helper.SETTLED,
        "upgraded": bool(target) and version == target,
    })
    return row
