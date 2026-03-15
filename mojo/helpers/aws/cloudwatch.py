"""
AWS CloudWatch Helper Module

Provides a simple interface for fetching time-series infrastructure metrics
from AWS CloudWatch for EC2 instances, RDS databases, and ElastiCache clusters.

High-level fetch() response shape mirrors the metrics app (periods + data) so
frontend chart components and the REST layer work without modification.
"""

import datetime
import botocore

from .client import get_session
from mojo.helpers.settings import settings
from mojo.helpers import logit

logger = logit.get_logger(__name__)


# ------------------------------------------------------------------
# Mapping tables
# ------------------------------------------------------------------

# Friendly granularity names -> period in seconds (mirrors metrics app)
GRANULARITY_SECONDS = {
    "minutes": 60,
    "hours":   3600,
    "days":    86400,
    "weeks":   604800,
}

# account param -> CloudWatch namespace
ACCOUNT_NAMESPACE = {
    "ec2":         "AWS/EC2",
    "rds":         "AWS/RDS",
    "redis":       "AWS/ElastiCache",
    "elasticache": "AWS/ElastiCache",
    "memstore":    "AWS/ElastiCache",
}

# Per-(account, category) namespace override.
# Used when a category requires a different namespace than the account default.
# EC2 memory and disk come from the CloudWatch Agent (CWAgent) not AWS/EC2.
CATEGORY_NAMESPACE_OVERRIDE = {
    ("ec2", "memory"): "CWAgent",
    ("ec2", "disk"):   "CWAgent",
}

# Per-(account, category) extra fixed dimensions appended after the primary
# instance dimension.  Used when CWAgent metrics require more than one dimension
# to uniquely identify a series (e.g. disk metrics need path="/").
CATEGORY_EXTRA_DIMENSIONS = {
    ("ec2", "disk"): [{"Name": "path", "Value": "/"}],
}

# account param -> CloudWatch dimension key name
ACCOUNT_DIMENSION = {
    "ec2":         "InstanceId",
    "rds":         "DBInstanceIdentifier",
    "redis":       "CacheClusterId",
    "elasticache": "CacheClusterId",
    "memstore":    "CacheClusterId",
}

# category -> CloudWatch metric name.
# A plain string means the same metric name applies to all account types.
# A dict means the metric name varies by account type; missing key = not supported.
CATEGORY_METRIC = {
    "cpu":            "CPUUtilization",
    "net_in":         {"ec2": "NetworkIn",    "rds": "NetworkReceiveThroughput",  "redis": "NetworkBytesIn",  "elasticache": "NetworkBytesIn",  "memstore": "NetworkBytesIn"},
    "net_out":        {"ec2": "NetworkOut",   "rds": "NetworkTransmitThroughput", "redis": "NetworkBytesOut", "elasticache": "NetworkBytesOut", "memstore": "NetworkBytesOut"},
    "conns":          {"rds": "DatabaseConnections", "redis": "CurrConnections",  "elasticache": "CurrConnections", "memstore": "CurrConnections"},
    "free_storage":   {"rds": "FreeStorageSpace"},
    "free_memory":    {"rds": "FreeableMemory"},
    "read_iops":      {"rds": "ReadIOPS"},
    "write_iops":     {"rds": "WriteIOPS"},
    "read_latency":   {"rds": "ReadLatency"},
    "write_latency":  {"rds": "WriteLatency"},
    "cache_memory":   {"redis": "BytesUsedForCache",  "elasticache": "BytesUsedForCache",  "memstore": "BytesUsedForCache"},
    "cache_hits":     {"redis": "CacheHits",          "elasticache": "CacheHits",          "memstore": "CacheHits"},
    "cache_misses":   {"redis": "CacheMisses",        "elasticache": "CacheMisses",        "memstore": "CacheMisses"},
    "replication_lag":{"redis": "ReplicationLag",     "elasticache": "ReplicationLag",     "memstore": "ReplicationLag"},
    "disk_read":      {"ec2": "DiskReadOps"},
    "disk_write":     {"ec2": "DiskWriteOps"},
    "status_check":   {"ec2": "StatusCheckFailed"},
    # Requires CloudWatch Agent installed on the instance (namespace: CWAgent).
    "memory":         {"ec2": "mem_used_percent"},
    # disk_used_percent on root filesystem (/). Requires CWAgent (namespace: CWAgent).
    # Extra dimension path="/" is appended automatically via CATEGORY_EXTRA_DIMENSIONS.
    "disk":           {"ec2": "disk_used_percent"},
}

# Friendly stat names -> CloudWatch Statistics value
STAT_MAP = {
    "avg":     "Average",
    "average": "Average",
    "sum":     "Sum",
    "max":     "Maximum",
    "maximum": "Maximum",
    "min":     "Minimum",
    "minimum": "Minimum",
}

# Period label format by period_seconds
_PERIOD_LABEL_FORMATS = {
    60:     "%H:%M",
    3600:   "%H:%M",
    86400:  "%Y-%m-%d",
    604800: "%Y-%m-%d",
}

_DEFAULT_GRANULARITY = "hours"
_DEFAULT_STAT = "Average"


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def resolve_namespace(account, category):
    """
    Return the CloudWatch namespace for an (account, category) pair.

    Most categories use the account-level default namespace (e.g. AWS/EC2).
    A small number of categories require a different namespace — for example,
    EC2 memory metrics are pushed by the CloudWatch Agent under 'CWAgent',
    not 'AWS/EC2'. CATEGORY_NAMESPACE_OVERRIDE handles these exceptions.
    """
    return CATEGORY_NAMESPACE_OVERRIDE.get((account, category), ACCOUNT_NAMESPACE[account])


def resolve_metric(account, category):
    """
    Return the CloudWatch metric name for an (account, category) pair.
    Raises ValueError for unknown category or unsupported account+category combos.
    """
    if category not in CATEGORY_METRIC:
        raise ValueError("unknown category '{}'. Valid categories: {}".format(
            category, ", ".join(sorted(CATEGORY_METRIC.keys()))
        ))
    mapping = CATEGORY_METRIC[category]
    if isinstance(mapping, str):
        return mapping
    if account not in mapping:
        raise ValueError(
            "category '{}' is not supported for account type '{}'. "
            "Supported account types for this category: {}".format(
                category, account, ", ".join(sorted(mapping.keys()))
            )
        )
    return mapping[account]


def granularity_to_seconds(granularity):
    """
    Convert a granularity string to period_seconds.
    Falls back gracefully: if the string is already an integer string, use it.
    """
    if granularity in GRANULARITY_SECONDS:
        return GRANULARITY_SECONDS[granularity]
    try:
        return max(60, int(granularity))
    except (TypeError, ValueError):
        return GRANULARITY_SECONDS[_DEFAULT_GRANULARITY]


def normalize_stat(stat):
    """Convert a friendly stat name to the CloudWatch Statistics value."""
    if stat is None:
        return _DEFAULT_STAT
    return STAT_MAP.get(stat.lower(), stat)


def _period_label(dt, period_seconds):
    fmt = _PERIOD_LABEL_FORMATS.get(period_seconds, "%Y-%m-%d %H:%M")
    return dt.strftime(fmt)


# ------------------------------------------------------------------
# CloudWatchHelper
# ------------------------------------------------------------------

class CloudWatchHelper:
    """
    Simple wrapper around boto3 CloudWatch, EC2, RDS, and ElastiCache clients.

    Credentials are read from settings by default (AWS_KEY, AWS_SECRET, AWS_REGION).
    Pass explicit values to override for multi-account scenarios.
    """

    def __init__(self, access_key=None, secret_key=None, region=None):
        self.access_key = access_key or settings.AWS_KEY
        self.secret_key = secret_key or settings.AWS_SECRET
        self.region = region or getattr(settings, "AWS_REGION", "us-east-1")
        self._cw = None
        self._ec2 = None
        self._rds = None
        self._elasticache = None

    # ------------------------------------------------------------------
    # Lazy client accessors
    # ------------------------------------------------------------------

    @property
    def cw(self):
        if self._cw is None:
            session = get_session(self.access_key, self.secret_key, self.region)
            self._cw = session.client("cloudwatch")
        return self._cw

    @property
    def ec2(self):
        if self._ec2 is None:
            session = get_session(self.access_key, self.secret_key, self.region)
            self._ec2 = session.client("ec2")
        return self._ec2

    @property
    def rds(self):
        if self._rds is None:
            session = get_session(self.access_key, self.secret_key, self.region)
            self._rds = session.client("rds")
        return self._rds

    @property
    def elasticache(self):
        if self._elasticache is None:
            session = get_session(self.access_key, self.secret_key, self.region)
            self._elasticache = session.client("elasticache")
        return self._elasticache

    # ------------------------------------------------------------------
    # High-level fetch — mirrors metrics.fetch() response shape
    # ------------------------------------------------------------------

    def fetch(self, account, category, slugs=None, dt_start=None, dt_end=None,
              granularity=None, stat=None):
        """
        Fetch a CloudWatch metric for one or more instances and return data shaped
        identically to the metrics app fetch() response (periods + data).

        Args:
            account:     Resource type: "ec2", "rds", "redis".
            category:    Metric shortname: "cpu", "conns", "free_storage", etc.
            slugs:       Friendly names or AWS IDs (list or single string). When
                         omitted, all instances for the account type are discovered
                         automatically. Friendly names (e.g. EC2 Name tag values)
                         are accepted and resolved to the underlying AWS ID.
            dt_start:    Start of range (datetime). Defaults to 24 hours ago.
            dt_end:      End of range (datetime). Defaults to now (UTC).
            granularity: "minutes", "hours" (default), or "days".
            stat:        "avg" / "max" / "min" / "sum". Defaults to "avg".

        Returns:
            {
                "data":    [{"slug": "<friendly name>", "values": [...]}, ...]  # list when multiple slugs
                           or {"slug": "<friendly name>", "values": [...]}      # dict when single slug
                "periods": ["10:00", "11:00", ...]
            }
        """
        # Resolve account type to namespace / dimension
        if account not in ACCOUNT_NAMESPACE:
            raise ValueError("unknown account type '{}'. Valid types: {}".format(
                account, ", ".join(sorted(ACCOUNT_NAMESPACE.keys()))
            ))
        metric_name = resolve_metric(account, category)
        namespace = resolve_namespace(account, category)
        dimension_key = ACCOUNT_DIMENSION[account]
        period_seconds = granularity_to_seconds(granularity or _DEFAULT_GRANULARITY)
        cw_stat = normalize_stat(stat)

        # Resolve time range
        now = datetime.datetime.utcnow()
        if dt_end is None:
            dt_end = now
        if dt_start is None:
            dt_start = dt_end - datetime.timedelta(hours=24)

        # Build id <-> friendly-slug maps from resource metadata.
        # id_to_slug: AWS ID -> display name (e.g. "i-0abc1234" -> "web-server-1")
        # slug_to_id: display name -> AWS ID (for resolving caller-supplied slugs)
        # Also include id -> id in slug_to_id so raw IDs still work as inputs.
        resources = self.list_resource_slugs(account)
        id_to_slug = {r["id"]: r["slug"] for r in resources}
        slug_to_id = {r["slug"]: r["id"] for r in resources}
        slug_to_id.update({r["id"]: r["id"] for r in resources})

        # Resolve instance list
        if slugs is None:
            # Use all discovered instances; iteration order follows discovery order.
            instance_ids = [r["id"] for r in resources]
        else:
            if isinstance(slugs, str):
                slugs = [slugs]
            # Map each supplied slug (friendly name or raw ID) to its AWS ID.
            instance_ids = [slug_to_id.get(s, s) for s in slugs]

        # Build shared period buckets once
        buckets = _build_buckets(dt_start, dt_end, period_seconds)
        periods = [_period_label(b, period_seconds) for b in buckets]

        # Extra fixed dimensions for categories that need more than one dimension
        # (e.g. EC2 disk requires path="/" in addition to InstanceId).
        extra_dims = CATEGORY_EXTRA_DIMENSIONS.get((account, category), [])

        # Fetch each instance and label the record with the friendly slug
        records = []
        for instance_id in instance_ids:
            dimensions = [{"Name": dimension_key, "Value": instance_id}] + extra_dims
            values = self._fetch_values(
                namespace, metric_name, dimensions, dt_start, dt_end, period_seconds, cw_stat, buckets
            )
            friendly_slug = id_to_slug.get(instance_id, instance_id)
            records.append({"slug": friendly_slug, "values": values})

        # Mirror metrics app: unwrap single-slug response to a plain dict
        data = records[0] if len(records) == 1 else records

        return {"data": data, "periods": periods}

    # ------------------------------------------------------------------
    # Core metric fetch (low-level)
    # ------------------------------------------------------------------

    def get_metric(self, namespace, metric_name, dimensions, dt_start=None, dt_end=None,
                   period_seconds=None, stat=None):
        """
        Fetch a single CloudWatch metric and return periods + values aligned to
        the requested time range.

        Args:
            namespace:      CloudWatch namespace, e.g. "AWS/EC2"
            metric_name:    Metric name, e.g. "CPUUtilization"
            dimensions:     List of {"Name": ..., "Value": ...} dicts
            dt_start:       Start of range (datetime). Defaults to 24 hours ago.
            dt_end:         End of range (datetime). Defaults to now (UTC).
            period_seconds: Bucket size in seconds. Defaults to 3600.
            stat:           CloudWatch Statistics value ("Average", "Sum", etc.).
                            Defaults to "Average".

        Returns:
            dict with keys: periods, values, slug, namespace, dimension
        """
        if period_seconds is None:
            period_seconds = GRANULARITY_SECONDS[_DEFAULT_GRANULARITY]
        if stat is None:
            stat = _DEFAULT_STAT
        now = datetime.datetime.utcnow()
        if dt_end is None:
            dt_end = now
        if dt_start is None:
            dt_start = now - datetime.timedelta(hours=24)

        buckets = _build_buckets(dt_start, dt_end, period_seconds)
        values = self._fetch_values(namespace, metric_name, dimensions, dt_start, dt_end, period_seconds, stat, buckets)
        periods = [_period_label(b, period_seconds) for b in buckets]
        dimension_label = dimensions[0]["Value"] if dimensions else ""

        return {
            "periods":   periods,
            "values":    values,
            "slug":      metric_name,
            "namespace": namespace,
            "dimension": dimension_label,
        }

    def _fetch_values(self, namespace, metric_name, dimensions, dt_start, dt_end, period_seconds, stat, buckets):
        """
        Internal: call CloudWatch GetMetricStatistics and align the result to
        the supplied bucket list, filling gaps with 0.0.
        """
        try:
            resp = self.cw.get_metric_statistics(
                Namespace=namespace,
                MetricName=metric_name,
                Dimensions=dimensions,
                StartTime=dt_start,
                EndTime=dt_end,
                Period=period_seconds,
                Statistics=[stat],
            )
        except botocore.exceptions.ClientError as exc:
            logger.error("CloudWatch get_metric_statistics failed: %s", exc)
            raise

        datapoints = sorted(resp.get("Datapoints", []), key=lambda dp: dp["Timestamp"])
        # Strip tzinfo then floor to the period boundary so keys match the
        # pre-built buckets exactly.  CloudWatch returns timestamps at whatever
        # offset the period started (e.g. :16 past the hour), not on clean
        # boundaries, so a plain replace(second=0) is not sufficient.
        values_by_ts = {
            _align_to_period(dp["Timestamp"].replace(tzinfo=None), period_seconds): dp[stat]
            for dp in datapoints
        }
        values = []
        for bucket_dt in buckets:
            values.append(round(values_by_ts.get(bucket_dt, 0.0), 4))
        return values

    # ------------------------------------------------------------------
    # Resource-scoped convenience wrappers
    # ------------------------------------------------------------------

    def get_ec2_metrics(self, instance_id, metric_name, dt_start=None, dt_end=None,
                        period_seconds=None, stat=None):
        """
        Fetch a CloudWatch metric for a single EC2 instance.

        Common metric_name values:
            CPUUtilization, NetworkIn, NetworkOut, DiskReadOps, DiskWriteOps,
            StatusCheckFailed, StatusCheckFailed_Instance, StatusCheckFailed_System
        """
        dimensions = [{"Name": "InstanceId", "Value": instance_id}]
        return self.get_metric(
            namespace="AWS/EC2",
            metric_name=metric_name,
            dimensions=dimensions,
            dt_start=dt_start,
            dt_end=dt_end,
            period_seconds=period_seconds,
            stat=stat,
        )

    def get_rds_metrics(self, db_instance_id, metric_name, dt_start=None, dt_end=None,
                        period_seconds=None, stat=None):
        """
        Fetch a CloudWatch metric for a single RDS DB instance.

        Common metric_name values:
            CPUUtilization, DatabaseConnections, FreeStorageSpace,
            ReadIOPS, WriteIOPS, FreeableMemory, ReadLatency, WriteLatency
        """
        dimensions = [{"Name": "DBInstanceIdentifier", "Value": db_instance_id}]
        return self.get_metric(
            namespace="AWS/RDS",
            metric_name=metric_name,
            dimensions=dimensions,
            dt_start=dt_start,
            dt_end=dt_end,
            period_seconds=period_seconds,
            stat=stat,
        )

    def get_elasticache_metrics(self, cluster_id, metric_name, dt_start=None, dt_end=None,
                                period_seconds=None, stat=None):
        """
        Fetch a CloudWatch metric for a single ElastiCache cluster.

        Common metric_name values:
            CPUUtilization, CurrConnections, BytesUsedForCache,
            CacheHits, CacheMisses, ReplicationLag, NetworkBytesIn, NetworkBytesOut
        """
        dimensions = [{"Name": "CacheClusterId", "Value": cluster_id}]
        return self.get_metric(
            namespace="AWS/ElastiCache",
            metric_name=metric_name,
            dimensions=dimensions,
            dt_start=dt_start,
            dt_end=dt_end,
            period_seconds=period_seconds,
            stat=stat,
        )

    # ------------------------------------------------------------------
    # Resource discovery
    # ------------------------------------------------------------------

    def list_instance_ids(self, account):
        """
        Return a plain list of instance ID strings for the given account type.
        Used by fetch() when no slugs are specified.
        """
        if account == "ec2":
            return [r["id"] for r in self.list_ec2_instances()]
        if account == "rds":
            return [r["id"] for r in self.list_rds_instances()]
        if account in ("redis", "elasticache", "memstore"):
            return [r["id"] for r in self.list_elasticache_clusters()]
        raise ValueError("unknown account type '{}'".format(account))

    def list_resource_slugs(self, account):
        """
        Return a list of {id, slug} dicts for the given account type.

        slug is the human-friendly display name shown in charts:
          - EC2: the Name tag value, falling back to the instance ID when no tag is set.
          - RDS: the DBInstanceIdentifier (already a human name like "prod-postgres").
          - ElastiCache: the CacheClusterId (already a human name like "prod-redis-001").

        Used by fetch() to map AWS IDs to readable chart labels and to accept
        friendly names as input slugs (reverse lookup).
        """
        if account == "ec2":
            return [
                {"id": r["id"], "slug": r["name"] if r.get("name") else r["id"]}
                for r in self.list_ec2_instances()
            ]
        if account == "rds":
            return [{"id": r["id"], "slug": r["id"]} for r in self.list_rds_instances()]
        if account in ("redis", "elasticache", "memstore"):
            return [{"id": r["id"], "slug": r["id"]} for r in self.list_elasticache_clusters()]
        raise ValueError("unknown account type '{}'".format(account))

    def list_ec2_instances(self):
        """
        Return a list of EC2 instances visible to the configured credentials.

        Each entry: {id, name, state, instance_type, private_ip, public_ip}
        """
        results = []
        try:
            paginator = self.ec2.get_paginator("describe_instances")
            for page in paginator.paginate():
                for reservation in page.get("Reservations", []):
                    for inst in reservation.get("Instances", []):
                        name = ""
                        for tag in inst.get("Tags", []):
                            if tag["Key"] == "Name":
                                name = tag["Value"]
                                break
                        results.append({
                            "id":            inst["InstanceId"],
                            "name":          name,
                            "state":         inst.get("State", {}).get("Name", "unknown"),
                            "instance_type": inst.get("InstanceType", ""),
                            "private_ip":    inst.get("PrivateIpAddress", ""),
                            "public_ip":     inst.get("PublicIpAddress", ""),
                        })
        except botocore.exceptions.ClientError as exc:
            logger.error("list_ec2_instances failed: %s", exc)
            raise
        return results

    def list_rds_instances(self):
        """
        Return a list of RDS DB instances visible to the configured credentials.

        Each entry: {id, engine, status, instance_class, endpoint}
        """
        results = []
        try:
            paginator = self.rds.get_paginator("describe_db_instances")
            for page in paginator.paginate():
                for db in page.get("DBInstances", []):
                    endpoint = ""
                    ep = db.get("Endpoint")
                    if ep:
                        endpoint = "{}:{}".format(ep.get("Address", ""), ep.get("Port", ""))
                    results.append({
                        "id":             db["DBInstanceIdentifier"],
                        "engine":         "{} {}".format(db.get("Engine", ""), db.get("EngineVersion", "")).strip(),
                        "status":         db.get("DBInstanceStatus", "unknown"),
                        "instance_class": db.get("DBInstanceClass", ""),
                        "endpoint":       endpoint,
                    })
        except botocore.exceptions.ClientError as exc:
            logger.error("list_rds_instances failed: %s", exc)
            raise
        return results

    def list_elasticache_clusters(self):
        """
        Return a list of ElastiCache clusters visible to the configured credentials.

        Each entry: {id, engine, status, node_type, num_nodes}
        """
        results = []
        try:
            paginator = self.elasticache.get_paginator("describe_cache_clusters")
            for page in paginator.paginate():
                for cluster in page.get("CacheClusters", []):
                    results.append({
                        "id":        cluster["CacheClusterId"],
                        "engine":    "{} {}".format(
                                         cluster.get("Engine", ""),
                                         cluster.get("EngineVersion", "")
                                     ).strip(),
                        "status":    cluster.get("CacheClusterStatus", "unknown"),
                        "node_type": cluster.get("CacheNodeType", ""),
                        "num_nodes": cluster.get("NumCacheNodes", 0),
                    })
        except botocore.exceptions.ClientError as exc:
            logger.error("list_elasticache_clusters failed: %s", exc)
            raise
        return results


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _build_buckets(dt_start, dt_end, period_seconds):
    """
    Build a list of UTC datetime objects, one per period bucket, covering
    [dt_start, dt_end). Each bucket is aligned to period_seconds boundaries.
    """
    buckets = []
    current = _align_to_period(dt_start, period_seconds)
    while current < dt_end:
        buckets.append(current)
        current = current + datetime.timedelta(seconds=period_seconds)
    return buckets


def _align_to_period(dt, period_seconds):
    """
    Floor a datetime to the nearest period boundary.
    E.g. for period_seconds=3600, 10:47 -> 10:00.
    """
    epoch = datetime.datetime(1970, 1, 1)
    total_seconds = int((dt - epoch).total_seconds())
    aligned_seconds = (total_seconds // period_seconds) * period_seconds
    return epoch + datetime.timedelta(seconds=aligned_seconds)