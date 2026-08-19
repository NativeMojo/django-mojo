"""
AWS CloudWatch REST Endpoints

Two endpoints, mirroring the metrics app pattern exactly:

    GET /aws/cloudwatch/resources  - list EC2, RDS, and ElastiCache resources with friendly names
    GET /aws/cloudwatch/fetch      - time-series metric data (mirrors metrics/fetch)

Parameters for fetch:
    account     - resource type: "ec2", "rds", "redis"
    category    - metric shortname: "cpu", "conns", "free_storage", etc.
    slugs       - friendly names or AWS IDs (optional; all instances returned when omitted)
                  EC2: use the Name tag value (e.g. "web-server-1") or the instance ID
                  RDS / ElastiCache: the identifier is already human-friendly
    dr_start    - start of range as Unix timestamp (optional, default: 24 h ago)
    dr_end      - end of range as Unix timestamp (optional, default: now)
    dt_start    - alias for dr_start, accepts ISO datetime string
    dt_end      - alias for dr_end, accepts ISO datetime string
    granularity - "minutes", "hours" (default), or "days"
    stat        - "avg" (default), "max", "min", or "sum"

All endpoints require the manage_aws permission.

Both endpoints degrade instead of failing: an installation with no AWS
credentials, a refused IAM identity, or an unreachable endpoint answers 200
with ``available: false`` and a ``reason`` code, so the Admin Metrics page can
explain the situation rather than render a stack trace. Reason codes come from
``mojo.helpers.aws.provider_call``: ``credentials_unavailable``, ``denied``,
``network_unavailable``, ``service_error``. Anything that is not a provider
failure still raises and still returns 500.
"""

import datetime
import functools

from botocore.exceptions import BotoCoreError, ClientError

from mojo import decorators as md
from mojo.helpers import dates as mdates
from mojo.helpers import logit
from mojo.helpers.response import JsonResponse
from mojo.helpers.aws.client import get_client
from mojo.helpers.aws.cloudwatch import CloudWatchHelper, resolve_metric, resolve_namespace, CATEGORY_METRIC, ACCOUNT_NAMESPACE
from mojo.helpers.aws.provider_call import ProviderCallError, map_error
import mojo.errors


logger = logit.get_logger("aws_cloudwatch", "aws.log")

# Most actionable cause first: when several services fail for different
# reasons they share one session, so the strongest signal is the real one.
_REASON_PRIORITY = (
    "credentials_unavailable", "denied", "network_unavailable", "service_error")


def _get_helper():
    # One attempt per call. botocore's default of three turns an unreachable
    # or unauthorized endpoint into a multi-second wait, and a page whose whole
    # job is to say "AWS is not answering" must say it quickly.
    return CloudWatchHelper(
        client_factory=functools.partial(get_client, max_attempts=1))


def _degraded_reason(exc, operation):
    """
    Return the wire reason code for a provider failure, or None when the
    exception is not a provider failure at all (those keep the 500 path).
    """
    if not isinstance(exc, (ProviderCallError, ClientError, BotoCoreError)):
        return None
    error = map_error(exc, operation)
    # detail() is the only provider-exception shape that is safe to record:
    # raw botocore text can carry credentials, signed URLs and parameters.
    logger.error("CloudWatch call degraded %s", error.detail())
    if error.provider_code in ("credentials_unavailable", "network_unavailable"):
        return error.provider_code
    if error.denied:
        return "denied"
    return "service_error"


def _single_cause(reasons):
    """Collapse per-service reasons to the one cause worth explaining."""
    for reason in _REASON_PRIORITY:
        if reason in reasons:
            return reason
    return "service_error"


@md.GET("cloudwatch/resources")
@md.requires_global_perms("manage_aws")
def on_cloudwatch_resources(request):
    """
    List all EC2 instances, RDS DB instances, and ElastiCache clusters.

    Each resource includes a `slug` field — the friendly name used in chart
    labels and accepted as input by the fetch endpoint:
      - EC2: the Name tag value, or the instance ID when no Name tag is set.
      - RDS / ElastiCache: the identifier (already a human-readable name).

    Use the `slug` value (not the raw `id`) when targeting a specific instance
    via the fetch endpoint's `slugs` parameter.

    Each of the three services is read independently, so one refused or
    unreachable service leaves the other two listed. `degraded` maps the failed
    service names to their reason codes; `available` is false only when every
    service failed, and `reason` then names the single cause.
    """
    cw = _get_helper()
    degraded = {}

    def listing(name, operation, load, slug_of):
        try:
            rows = load()
        except Exception as exc:
            reason = _degraded_reason(exc, operation)
            if reason is None:
                raise
            degraded[name] = reason
            return []
        for row in rows:
            row["slug"] = slug_of(row)
        return rows

    # EC2 has no human name of its own — the Name tag is the friendly slug,
    # and the instance id is the fallback when the tag is unset.
    ec2_instances = listing(
        "ec2", "ec2.describe_instances", cw.list_ec2_instances,
        lambda row: row["name"] if row.get("name") else row["id"])
    rds_instances = listing(
        "rds", "rds.describe_db_instances", cw.list_rds_instances,
        lambda row: row["id"])
    redis_clusters = listing(
        "redis", "elasticache.describe_cache_clusters",
        cw.list_elasticache_clusters, lambda row: row["id"])

    available = len(degraded) < 3
    return JsonResponse({
        "ec2":       ec2_instances,
        "rds":       rds_instances,
        "redis":     redis_clusters,
        "degraded":  degraded,
        "available": available,
        "reason":    None if available else _single_cause(set(degraded.values())),
        "status":    True,
    })


@md.GET("cloudwatch/fetch")
@md.requires_global_perms("manage_aws")
@md.requires_params("account", "category")
def on_cloudwatch_fetch(request):
    """
    Fetch CloudWatch time-series metric data.

    When slugs are omitted, all instances for the account type are discovered
    automatically. Response shape is identical to the metrics app fetch endpoint.

    `available` and `reason` live INSIDE `data` alongside the series, because
    the Admin portal client unwraps `payload.data` and drops any top-level
    sibling. Parameter validation still raises a 400.
    """
    account = request.DATA.get("account")
    category = request.DATA.get("category")

    # Validate account type and category combo up front for a clean 400
    if account not in ACCOUNT_NAMESPACE:
        raise mojo.errors.ValueException(
            "unknown account '{}'. Valid values: {}".format(
                account, ", ".join(sorted(ACCOUNT_NAMESPACE.keys()))
            )
        )
    try:
        resolve_metric(account, category)
    except ValueError as exc:
        raise mojo.errors.ValueException(str(exc))

    # Accept dr_start/dr_end (Unix timestamps) or dt_start/dt_end (ISO datetime)
    _start = request.DATA.get("dr_start") or request.DATA.get("dt_start")
    _end = request.DATA.get("dr_end") or request.DATA.get("dt_end")
    dt_start = mdates.parse_datetime(_start) if _start else None
    dt_end = mdates.parse_datetime(_end) if _end else None
    granularity = request.DATA.get("granularity", "hours")
    stat = request.DATA.get("stat", "avg")

    # slugs may be friendly names (e.g. EC2 Name tag values) or raw AWS IDs.
    # CloudWatchHelper.fetch() resolves them to AWS IDs internally.
    slugs = None
    if "slugs" in request.DATA:
        slugs = request.DATA.get_typed("slugs", typed=list)

    cw = _get_helper()
    try:
        data = cw.fetch(
            account=account,
            category=category,
            slugs=slugs,
            dt_start=dt_start,
            dt_end=dt_end,
            granularity=granularity,
            stat=stat,
        )
    except Exception as exc:
        reason = _degraded_reason(exc, "cloudwatch.get_metric_statistics")
        if reason is None:
            raise
        return JsonResponse(dict(status=True, data={
            "data": {}, "labels": [], "available": False, "reason": reason}))

    data["available"] = True
    data["reason"] = None
    return JsonResponse(dict(status=True, data=data))