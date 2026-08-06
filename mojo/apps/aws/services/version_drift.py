"""Managed-service version drift inventory for RDS/Aurora and ElastiCache.

Read-only. Answers one question an operator cannot answer from the console
without clicking through every engine: *is any managed database or cache
running a major version that AWS will stop supporting, and when?*

Deliberately NOT in scope: applying an upgrade (never autonomous), AMI/EC2
image age, and any hardcoded end-of-life table. What AWS does not publish is
reported as ``deadline=None``, never guessed.

Two categories, on purpose:

* ``CATEGORY`` (``system:health:aws_versions``) is the EVENT category, so the
  finding lands on the ``/api/incident/health/summary`` strip with the rest of
  the subsystem health rows.
* ``RULESET_CATEGORY`` (``aws:versions``) is the RuleSet category, matched
  through the event's ``scope``. It is deliberately outside the
  ``system:health:`` namespace so it can never satisfy the
  ``_ensure_health_defaults()`` bootstrap guard in incident cronjobs.
"""

import datetime

from botocore.exceptions import (
    ClientError, ConnectTimeoutError, EndpointConnectionError,
    NoCredentialsError, PartialCredentialsError, ReadTimeoutError,
)
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from mojo.helpers import logit
from mojo.helpers.aws.client import get_client, get_session
from mojo.helpers.settings import settings


logger = logit.get_logger("aws_versions", "aws.log")

SCHEMA_VERSION = 1
# The EVENT category — keeps the health-strip row.
CATEGORY = "system:health:aws_versions"
# The RULESET category — matched via Event.scope. NOT in the health namespace.
RULESET_CATEGORY = "aws:versions"

# RDS publishes several lifecycles per major version. Standard support is the
# only one that means "AWS still maintains this for free"; extended support is
# a paid extension roughly three years later and must never be read as the
# deadline.
STANDARD_SUPPORT = "open-source-rds-standard-support"
EXTENDED_SUPPORT = "open-source-rds-extended-support"

RDS_RELEASE_NOTES = "https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_Upgrading.html"
AURORA_RELEASE_NOTES = "https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/USER_UpgradeDBCluster.html"
ELASTICACHE_RELEASE_NOTES = "https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/VersionManagement.html"


def _setting(name, default=None, kind=None):
    try:
        return settings.get_static(name, default, kind=kind) if kind else settings.get_static(name, default)
    except Exception:
        return default


def _error_code(exc):
    return str((getattr(exc, "response", {}).get("Error") or {}).get("Code") or "")


def _version_key(value):
    """Sortable key for an engine version string.

    Numeric components sort numerically; anything else (ElastiCache really
    reports ``6.x`` for Redis) sorts after every number at that position rather
    than raising. Every element is a 2-tuple, so two keys are always
    comparable.
    """
    key = []
    for chunk in str(value or "").split("."):
        try:
            key.append((0, int(chunk)))
        except ValueError:
            key.append((1, chunk))
    return tuple(key)


def _major_key(value):
    """Sortable key for just the leading (major) component of a version."""
    return _version_key(str(value or "").split(".")[0])


def _parse_deadline(value):
    """Coerce an AWS lifecycle end date (datetime/date/string) to an aware datetime."""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value if timezone.is_aware(value) else timezone.make_aware(value, datetime.timezone.utc)
    if isinstance(value, datetime.date):
        return datetime.datetime(value.year, value.month, value.day, tzinfo=datetime.timezone.utc)
    text = str(value)
    parsed = parse_datetime(text)
    if parsed is None:
        day = parse_date(text)
        parsed = datetime.datetime(day.year, day.month, day.day, tzinfo=datetime.timezone.utc) if day else None
    if parsed is not None and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, datetime.timezone.utc)
    return parsed


class VersionDriftScanner:
    """Inventory managed-service versions and report available MAJOR upgrades.

    Minor upgrades are already solved by RDS auto-minor-version-upgrade, so a
    version with only minor upgrade targets is NOT a finding.
    """

    def __init__(self, region=None, profile=None, timeout=5, clients=None,
                 session=None, now=None, deadline_days=None):
        self.region = region or _setting("AWS_REGION", "us-east-1")
        self.profile = profile
        self.timeout = max(1, min(int(timeout or 5), 30))
        self.clients = clients or {}
        self.session = session
        self.now = now or timezone.now
        if deadline_days is None:
            deadline_days = _setting("AWS_VERSION_DRIFT_DEADLINE_DAYS", 180) or 180
        try:
            self.deadline_days = max(1, int(deadline_days))
        except (TypeError, ValueError):
            self.deadline_days = 180
        self.warnings = []

    # ── AWS plumbing (mirrors AWSCheckRunner's injection contract) ──

    def _session(self, region=None):
        if self.session is not None:
            return self.session
        access_key = secret_key = None
        if not self.profile:
            access_key = _setting("AWS_KEY")
            secret_key = _setting("AWS_SECRET")
        return get_session(
            access_key=access_key, secret_key=secret_key, region=region or self.region,
            profile=self.profile if not access_key and not secret_key else None,
        )

    def _client(self, service, region=None):
        if service in self.clients:
            return self.clients[service]
        return get_client(
            service, session=self._session(region),
            region=region or self.region, timeout=self.timeout,
        )

    def _paginate(self, client, method, result_key, token_field="Marker", **params):
        """Hand-rolled token loop — RDS/ElastiCache use `Marker`, EC2 `NextToken`.

        Matches AWSCheckRunner._list_paginated, which is this codebase's
        convention; `get_paginator` is deliberately not used.
        """
        rows, token = [], None
        while True:
            request = dict(params)
            if token:
                request[token_field] = token
            response = getattr(client, method)(**request)
            rows.extend(response.get(result_key) or [])
            token = response.get(token_field)
            if not token:
                return rows

    def _warn_denied(self, iam_action, exc):
        """Record a denied API by the EXACT missing IAM action and keep going.

        A silent skip here would make a correctly-locked-down deployment look
        identical to one where nothing is out of date — the precise failure
        this scanner exists to prevent.
        """
        self.warnings.append({
            "iam_action": iam_action,
            "aws_code": _error_code(exc),
            "message": f"{iam_action} was denied; version drift for this API was not inventoried",
        })
        logger.warning("version drift: %s denied (%s)", iam_action, _error_code(exc))

    # ── RDS / Aurora ──

    def scan_rds(self):
        findings = []
        client = self._client("rds")
        resources = []
        try:
            for row in self._paginate(client, "describe_db_clusters", "DBClusters"):
                resources.append((
                    "rds-cluster", row.get("DBClusterIdentifier"),
                    row.get("Engine"), row.get("EngineVersion"),
                ))
        except ClientError as exc:
            self._warn_denied("rds:DescribeDBClusters", exc)
        try:
            for row in self._paginate(client, "describe_db_instances", "DBInstances"):
                # Aurora members belong to their cluster — the cluster is the
                # upgrade unit, so counting the member too double-reports it.
                if row.get("DBClusterIdentifier"):
                    continue
                resources.append((
                    "rds-instance", row.get("DBInstanceIdentifier"),
                    row.get("Engine"), row.get("EngineVersion"),
                ))
        except ClientError as exc:
            self._warn_denied("rds:DescribeDBInstances", exc)

        cache = {}
        for kind, resource_id, engine, version in resources:
            if not resource_id or not engine or not version:
                continue
            key = (engine, version)
            if key not in cache:
                cache[key] = self._rds_upgrade(client, engine, version)
            target = cache[key]
            if not target:
                continue
            findings.append(self._finding(
                kind=kind, resource_id=resource_id, engine=engine,
                current_version=version, available_major=target["available_major"],
                deadline=target["deadline"], extended_deadline=target["extended_deadline"],
                note=target["note"],
                release_notes_url=AURORA_RELEASE_NOTES if str(engine).startswith("aurora") else RDS_RELEASE_NOTES,
            ))
        return findings

    def _rds_upgrade(self, client, engine, version):
        """Highest MAJOR upgrade target for one (engine, version), or None."""
        try:
            rows = self._paginate(
                client, "describe_db_engine_versions", "DBEngineVersions",
                Engine=engine, EngineVersion=version,
            )
        except ClientError as exc:
            self._warn_denied("rds:DescribeDBEngineVersions", exc)
            return None
        best = None
        major_engine_version = None
        for row in rows:
            # The major id is engine-specific (postgres 15.4 -> "15" but mysql
            # 8.0.35 -> "8.0"), so read it off the response instead of slicing.
            major_engine_version = row.get("MajorEngineVersion") or major_engine_version
            for target in row.get("ValidUpgradeTarget") or []:
                if target.get("IsMajorVersionUpgrade") is not True:
                    continue
                candidate = target.get("EngineVersion")
                if not candidate:
                    continue
                if best is None or _version_key(candidate) > _version_key(best):
                    best = candidate
        if best is None:
            return None
        deadline, extended = self._rds_lifecycle(client, engine, major_engine_version)
        note = f"{engine} {version} has a major upgrade to {best}"
        if major_engine_version:
            note += f" (current major {major_engine_version})"
        if deadline is None:
            note += "; AWS publishes no standard-support end date for this major version"
        return {
            "available_major": best,
            "deadline": deadline,
            "extended_deadline": extended,
            "note": note,
        }

    def _rds_lifecycle(self, client, engine, major_engine_version):
        """Standard-support end date for a major version, and the extended one separately.

        CRITICAL: Aurora PostgreSQL returns BOTH a standard and an extended
        lifecycle, and the extended date is roughly three years later. Taking
        the max would report the deadline ~3 years late — a silent wrong answer
        on this scanner's core output. The standard entry is selected by name;
        when it is absent the deadline is None and NEVER falls back to extended.
        """
        if not major_engine_version:
            return None, None
        # Older botocore (and a lean mock) has no such API — degrade to
        # deadline=None rather than raising.
        if not hasattr(client, "describe_db_major_engine_versions"):
            return None, None
        try:
            rows = self._paginate(
                client, "describe_db_major_engine_versions", "DBMajorEngineVersions",
                Engine=engine, MajorEngineVersion=major_engine_version,
            )
        except ClientError as exc:
            self._warn_denied("rds:DescribeDBMajorEngineVersions", exc)
            return None, None
        standard = extended = None
        for row in rows:
            for cycle in row.get("SupportedEngineLifecycles") or []:
                name = cycle.get("LifecycleSupportName")
                end = cycle.get("LifecycleSupportEndDate")
                if name == STANDARD_SUPPORT:
                    standard = end
                elif name == EXTENDED_SUPPORT:
                    extended = end
        return _parse_deadline(standard), _parse_deadline(extended)

    # ── ElastiCache ──

    def scan_elasticache(self):
        """Report available MAJOR engine upgrades per replication group/cluster.

        The ElastiCache API exposes no lifecycle or deprecation member
        anywhere, so every finding here carries ``deadline=None``. A hardcoded
        EOL table would be a guess with a decay date and is deliberately absent.
        """
        findings = []
        client = self._client("elasticache")
        try:
            clusters = self._paginate(client, "describe_cache_clusters", "CacheClusters")
        except ClientError as exc:
            self._warn_denied("elasticache:DescribeCacheClusters", exc)
            return findings

        groups = {}
        for row in clusters:
            engine = row.get("Engine")
            version = row.get("EngineVersion")
            if not engine or not version:
                continue
            # The ReplicationGroup shape carries no EngineVersion member, so the
            # version has to come from a member CacheCluster.
            resource_id = row.get("ReplicationGroupId") or row.get("CacheClusterId")
            if not resource_id:
                continue
            current = groups.get(resource_id)
            # A group is only as current as its OLDEST member.
            if current is None or _version_key(version) < _version_key(current[1]):
                groups[resource_id] = (engine, version)
        if not groups:
            return findings

        available = {}
        for engine in sorted({engine for engine, _ in groups.values()}):
            try:
                rows = self._paginate(
                    client, "describe_cache_engine_versions", "CacheEngineVersions",
                    Engine=engine,
                )
            except ClientError as exc:
                self._warn_denied("elasticache:DescribeCacheEngineVersions", exc)
                continue
            # Compare only WITHIN the same engine — redis -> valkey is an engine
            # migration, not a version bump, and must never read as an upgrade.
            available[engine] = [
                row.get("EngineVersion") for row in rows
                if row.get("EngineVersion")
                and str(row.get("Engine") or engine).lower() == str(engine).lower()
            ]

        for resource_id, (engine, version) in sorted(groups.items()):
            newest = None
            for candidate in available.get(engine) or []:
                if _major_key(candidate) <= _major_key(version):
                    continue
                if newest is None or _version_key(candidate) > _version_key(newest):
                    newest = candidate
            if newest is None:
                continue
            findings.append(self._finding(
                kind="elasticache", resource_id=resource_id, engine=engine,
                current_version=version, available_major=newest,
                deadline=None, extended_deadline=None,
                note=(f"{engine} {version} has a major upgrade to {newest}; "
                      f"the ElastiCache API publishes no end-of-life date"),
                release_notes_url=ELASTICACHE_RELEASE_NOTES,
            ))
        return findings

    # ── report ──

    def _finding(self, kind, resource_id, engine, current_version, available_major,
                 deadline, extended_deadline, note, release_notes_url):
        days_remaining = None
        if deadline is not None:
            days_remaining = int((deadline - self.now()).days)
        return {
            "kind": kind,
            "resource_id": resource_id,
            "engine": engine,
            "current_version": current_version,
            "available_major": available_major,
            "deadline": deadline.isoformat() if deadline is not None else None,
            "extended_deadline": extended_deadline.isoformat() if extended_deadline is not None else None,
            "days_remaining": days_remaining,
            "note": note,
            "release_notes_url": release_notes_url,
        }

    def _level(self, findings, warnings):
        """1 = current; 4 = an API was denied; 5 = upgrade available;
        8 = published deadline inside the window; 10 = deadline already past."""
        level = 1
        if warnings:
            level = 4
        if findings:
            level = max(level, 5)
        for finding in findings:
            days = finding.get("days_remaining")
            if days is None:
                continue
            if days < 0:
                level = max(level, 10)
            elif days <= self.deadline_days:
                level = max(level, 8)
        return level

    def scan(self):
        report = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": self.now().isoformat(),
            "region": self.region,
            "status": "ok",
            "level": 1,
            "findings": [],
            "warnings": [],
        }
        self.warnings = []
        try:
            findings = self.scan_rds() + self.scan_elasticache()
        except (NoCredentialsError, PartialCredentialsError, EndpointConnectionError,
                ConnectTimeoutError, ReadTimeoutError) as exc:
            # No credentials at all is the normal state on a dev box and in the
            # suite. Say so and file nothing — a monthly "couldn't check" on
            # every laptop is noise.
            report["status"] = "unavailable"
            report["reason"] = f"{type(exc).__name__}: {exc}"
            return report
        report["findings"] = findings
        report["warnings"] = list(self.warnings)
        report["level"] = self._level(findings, self.warnings)
        return report


def scan(**kwargs):
    """Convenience wrapper — build a scanner from settings and run it."""
    return VersionDriftScanner(**kwargs).scan()
