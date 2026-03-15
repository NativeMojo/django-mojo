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
    dt_start    - start of range, UTC datetime (optional, default: 24 h ago)
    dt_end      - end of range, UTC datetime (optional, default: now)
    granularity - "minutes", "hours" (default), or "days"
    stat        - "avg" (default), "max", "min", or "sum"

All endpoints require the manage_aws permission.
"""

import datetime

from mojo import decorators as md
from mojo.helpers.response import JsonResponse
from mojo.helpers.aws.cloudwatch import CloudWatchHelper, resolve_metric, resolve_namespace, CATEGORY_METRIC, ACCOUNT_NAMESPACE
import mojo.errors


def _get_helper():
    return CloudWatchHelper()


@md.GET("cloudwatch/resources")
@md.requires_perms("manage_aws")
def on_cloudwatch_resources(request):
    """
    List all EC2 instances, RDS DB instances, and ElastiCache clusters.

    Each resource includes a `slug` field — the friendly name used in chart
    labels and accepted as input by the fetch endpoint:
      - EC2: the Name tag value, or the instance ID when no Name tag is set.
      - RDS / ElastiCache: the identifier (already a human-readable name).

    Use the `slug` value (not the raw `id`) when targeting a specific instance
    via the fetch endpoint's `slugs` parameter.
    """
    cw = _get_helper()
    ec2_instances = cw.list_ec2_instances()
    # Attach the friendly slug to each EC2 entry so callers can see both.
    for inst in ec2_instances:
        inst["slug"] = inst["name"] if inst.get("name") else inst["id"]

    rds_instances = cw.list_rds_instances()
    for inst in rds_instances:
        inst["slug"] = inst["id"]

    redis_clusters = cw.list_elasticache_clusters()
    for cluster in redis_clusters:
        cluster["slug"] = cluster["id"]

    return JsonResponse({
        "ec2":    ec2_instances,
        "rds":    rds_instances,
        "redis":  redis_clusters,
        "status": True,
    })


@md.GET("cloudwatch/fetch")
@md.requires_perms("manage_aws")
@md.requires_params("account", "category")
def on_cloudwatch_fetch(request):
    """
    Fetch CloudWatch time-series metric data.

    When slugs are omitted, all instances for the account type are discovered
    automatically. Response shape is identical to the metrics app fetch endpoint.
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

    dt_start = request.DATA.get_typed("dt_start", typed=datetime.datetime)
    dt_end = request.DATA.get_typed("dt_end", typed=datetime.datetime)
    granularity = request.DATA.get("granularity", "hours")
    stat = request.DATA.get("stat", "avg")

    # slugs may be friendly names (e.g. EC2 Name tag values) or raw AWS IDs.
    # CloudWatchHelper.fetch() resolves them to AWS IDs internally.
    slugs = None
    if "slugs" in request.DATA:
        slugs = request.DATA.get_typed("slugs", typed=list)

    cw = _get_helper()
    data = cw.fetch(
        account=account,
        category=category,
        slugs=slugs,
        dt_start=dt_start,
        dt_end=dt_end,
        granularity=granularity,
        stat=stat,
    )

    return JsonResponse(dict(status=True, data=data))