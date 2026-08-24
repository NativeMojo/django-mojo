"""Admin Maintenance: engine-version helpers, the apply service, and its gate.

The helper tests use ``botocore.stub.Stubber`` rather than the plain Mock
clients the sibling AWS tests use, and deliberately so: a Mock accepts any
keyword, so it would have accepted ``AllowMajorVersionUpgrade`` on the
ElastiCache modify operations — a member those operations do not define, and
an API error against a live cache. Stubber validates every request against the
real service model, which is the only thing that can catch that.
"""

TESTIT_TIER = "edge"

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from testit import helpers as th




REGION = "us-test-1"
INSTANCE = "mojo-test-postgres"
CLUSTER = "mojo-test-aurora"
CACHE_GROUP = "mojo-test-redis"


# ── fixtures ────────────────────────────────────────────────────────────────

def _stub(service):
    """A real bounded client plus its Stubber. Credentials are never used."""
    import boto3
    from botocore.stub import Stubber

    client = boto3.client(
        service, region_name="us-east-1",
        aws_access_key_id="testing", aws_secret_access_key="testing")
    return client, Stubber(client)


def _finding(kind, resource_id, current, target, days=None, engine="postgres"):
    return {
        "kind": kind, "resource_id": resource_id, "engine": engine,
        "current_version": current, "available_major": target,
        "deadline": None, "extended_deadline": None, "days_remaining": days,
        "note": f"{engine} {current} has a major upgrade to {target}",
        "release_notes_url": "https://docs.aws.amazon.com/",
    }


def _scan(findings=(), status="ok", warnings=()):
    return {
        "schema_version": 1, "generated_at": "2026-08-18T18:00:00Z",
        "region": REGION, "status": status, "level": 8 if findings else 1,
        "findings": [dict(row) for row in findings], "warnings": list(warnings),
    }


class _Scanner:
    """Stands in for VersionDriftScanner. Counts scans so caching is provable."""

    def __init__(self, report):
        self._report = report
        self.calls = 0

    def scan(self):
        self.calls += 1
        return {**self._report, "findings": [dict(row) for row in self._report["findings"]]}


def _rds_client(instances=(), clusters=()):
    client = mock.Mock()
    client.describe_db_instances.return_value = {"DBInstances": list(instances)}
    client.describe_db_clusters.return_value = {"DBClusters": list(clusters)}
    return client


def _cache_client(clusters=()):
    client = mock.Mock()
    client.describe_cache_clusters.return_value = {"CacheClusters": list(clusters)}
    return client


def _settings(**overrides):
    values = {"AWS_REGION": REGION, "AWS_VERSION_DRIFT_ENABLED": True}
    values.update(overrides)
    return lambda name, default=None, kind=None: values.get(name, default)


@th.django_unit_setup()
def setup_maintenance(opts):
    from mojo.apps.account.models import User
    User.objects.filter(username__in=("maint-aws", "maint-root")).delete()
    aws_only = User.objects.create_user(
        email="maint-aws@test.com", username="maint-aws", password="example")
    aws_only.is_active = True
    aws_only.save()
    aws_only.add_permission("manage_aws")
    aws_only.save()
    root = User.objects.create_user(
        email="maint-root@test.com", username="maint-root", password="example")
    root.is_active = True
    root.is_superuser = True
    root.save()
    root.add_permission(["manage_aws", "manage_platform"])
    root.save()
    opts.maint_aws = aws_only.pk
    opts.maint_root = root.pk


# ── helpers: RDS mutations ──────────────────────────────────────────────────

@th.django_unit_test("an RDS instance upgrade allows the major bump and honors the window")
def test_rds_instance_modify_request(opts):
    from mojo.helpers.aws import rds

    client, stubber = _stub("rds")
    stubber.add_response("modify_db_instance", {"DBInstance": {}}, {
        "DBInstanceIdentifier": INSTANCE, "EngineVersion": "16.4",
        "AllowMajorVersionUpgrade": True, "ApplyImmediately": True})
    with stubber:
        rds.modify_instance_engine_version(INSTANCE, "16.4", True, client=client)
    stubber.assert_no_pending_responses()


@th.django_unit_test("an RDS cluster upgrade sends the cluster parameter group, not the instance one")
def test_rds_cluster_modify_request(opts):
    from mojo.helpers.aws import rds

    client, stubber = _stub("rds")
    stubber.add_response("modify_db_cluster", {"DBCluster": {}}, {
        "DBClusterIdentifier": CLUSTER, "EngineVersion": "16.2",
        "AllowMajorVersionUpgrade": True, "ApplyImmediately": False,
        "DBClusterParameterGroupName": "aurora-pg16"})
    with stubber:
        rds.modify_cluster_engine_version(
            CLUSTER, "16.2", False, parameter_group="aurora-pg16", client=client)
    stubber.assert_no_pending_responses()


@th.django_unit_test("apply_immediately is passed through, never defaulted to now")
def test_rds_window_is_the_callers_choice(opts):
    from mojo.helpers.aws import rds

    client, stubber = _stub("rds")
    stubber.add_response("modify_db_instance", {"DBInstance": {}}, {
        "DBInstanceIdentifier": INSTANCE, "EngineVersion": "16.4",
        "AllowMajorVersionUpgrade": True, "ApplyImmediately": False})
    with stubber:
        rds.modify_instance_engine_version(INSTANCE, "16.4", False, client=client)
    stubber.assert_no_pending_responses()


# ── helpers: ElastiCache mutations ──────────────────────────────────────────

@th.django_unit_test("a replication group upgrade sends no AllowMajorVersionUpgrade")
def test_replication_group_modify_request(opts):
    from mojo.helpers.aws import elasticache

    client, stubber = _stub("elasticache")
    stubber.add_response("modify_replication_group", {"ReplicationGroup": {}}, {
        "ReplicationGroupId": CACHE_GROUP, "EngineVersion": "7.1",
        "ApplyImmediately": True})
    with stubber:
        elasticache.modify_replication_group_engine_version(
            CACHE_GROUP, "7.1", True, client=client)
    stubber.assert_no_pending_responses()


@th.django_unit_test("a standalone cache upgrade sends no AllowMajorVersionUpgrade")
def test_cache_cluster_modify_request(opts):
    from mojo.helpers.aws import elasticache

    client, stubber = _stub("elasticache")
    stubber.add_response("modify_cache_cluster", {"CacheCluster": {}}, {
        "CacheClusterId": CACHE_GROUP, "EngineVersion": "7.1",
        "ApplyImmediately": False, "CacheParameterGroupName": "redis7"})
    with stubber:
        elasticache.modify_cache_cluster_engine_version(
            CACHE_GROUP, "7.1", False, parameter_group="redis7", client=client)
    stubber.assert_no_pending_responses()


@th.django_unit_test("ElastiCache would REFUSE AllowMajorVersionUpgrade — the Mock that accepts it lies")
def test_elasticache_rejects_major_version_flag(opts):
    from botocore.exceptions import ParamValidationError

    # No stubber here on purpose: botocore validates the request against the
    # service model BEFORE it would reach the network, so this raises locally.
    # It is the proof that the sibling tests' explicit param assertions are
    # protecting a real constraint and not a stylistic preference.
    client, _ = _stub("elasticache")
    with th.assert_raises(ParamValidationError):
        client.modify_replication_group(
            ReplicationGroupId=CACHE_GROUP, EngineVersion="7.1",
            ApplyImmediately=True, AllowMajorVersionUpgrade=True)
    with th.assert_raises(ParamValidationError):
        client.modify_cache_cluster(
            CacheClusterId=CACHE_GROUP, EngineVersion="7.1",
            ApplyImmediately=True, AllowMajorVersionUpgrade=True)


# ── helpers: reads ──────────────────────────────────────────────────────────

@th.django_unit_test("a cache group is only as settled as its least settled member")
def test_group_status_is_least_settled(opts):
    from mojo.helpers.aws import elasticache

    client, stubber = _stub("elasticache")
    stubber.add_response("describe_cache_clusters", {"CacheClusters": [
        {"CacheClusterId": f"{CACHE_GROUP}-001", "ReplicationGroupId": CACHE_GROUP,
         "Engine": "redis", "EngineVersion": "7.1", "CacheClusterStatus": "available"},
        {"CacheClusterId": f"{CACHE_GROUP}-002", "ReplicationGroupId": CACHE_GROUP,
         "Engine": "redis", "EngineVersion": "6.2", "CacheClusterStatus": "modifying"},
    ]}, {})
    with stubber:
        groups = elasticache.group_statuses(client=client)
    entry = groups[CACHE_GROUP]
    assert entry["status"] == "modifying", \
        f"an available first member masked a modifying replica: {entry['status']!r}"
    assert entry["is_replication_group"] is True, \
        "a replication group member was keyed as a standalone cluster"
    assert entry["engine_versions"] == ["6.2", "7.1"], \
        f"both member versions must be visible: {entry['engine_versions']}"


@th.django_unit_test("a missing replication group means standalone, not a failure")
def test_resolve_kind_not_found_is_standalone(opts):
    from mojo.helpers.aws import elasticache

    client, stubber = _stub("elasticache")
    stubber.add_client_error(
        "describe_replication_groups", "ReplicationGroupNotFoundFault",
        "not found", 404)
    with stubber:
        assert elasticache.resolve_kind(CACHE_GROUP, client=client) == "standalone", \
            "a ReplicationGroupNotFound must resolve to the standalone modify path"

    client, stubber = _stub("elasticache")
    stubber.add_response("describe_replication_groups", {"ReplicationGroups": [
        {"ReplicationGroupId": CACHE_GROUP, "Status": "available"}]},
        {"ReplicationGroupId": CACHE_GROUP})
    with stubber:
        assert elasticache.resolve_kind(CACHE_GROUP, client=client) == "replication_group", \
            "an existing replication group must resolve to the group modify path"


@th.django_unit_test("a denied describe raises a wire-safe error, never provider text")
def test_denied_describe_is_sanitized(opts):
    from mojo.helpers.aws import rds
    from mojo.helpers.aws.provider_call import ProviderCallError

    secret = "SIGNEDURL-should-never-leak"
    client, stubber = _stub("rds")
    stubber.add_client_error(
        "describe_db_instances", "AccessDenied", secret, 403)
    with stubber:
        try:
            rds.instance_statuses(client=client)
            raise AssertionError("a denied describe must raise")
        except ProviderCallError as err:
            detail = err.detail()
            assert err.denied is True, "a 403 was not classified as a denial"
            assert detail.get("iam_action") == "rds:DescribeDBInstances", \
                f"the denial does not name the missing IAM action: {detail}"
            assert secret not in json.dumps(detail), \
                f"raw provider text reached the wire shape: {detail}"


# ── service: report, cache, offered target ──────────────────────────────────

@th.django_unit_test("the report is cached, refresh bypasses it, and a cache outage still answers")
def test_report_caching(opts):
    from django.core.cache import cache
    from mojo.apps.aws.services import maintenance

    scanner = _Scanner(_scan([_finding("rds-instance", INSTANCE, "14.11", "16.4")]))
    rds = _rds_client(instances=[
        {"DBInstanceIdentifier": INSTANCE, "DBInstanceStatus": "available",
         "EngineVersion": "14.11"}])
    with mock.patch.object(maintenance, "_setting", _settings()):
        cache.delete(maintenance._report_key(REGION))
        try:
            first = maintenance.report(scanner=scanner, rds_client=rds)
            assert scanner.calls == 1, "the first read did not scan"
            maintenance.report(scanner=scanner, rds_client=rds)
            assert scanner.calls == 1, \
                f"a second read re-scanned AWS instead of using the cache ({scanner.calls})"
            maintenance.report(refresh=True, scanner=scanner, rds_client=rds)
            assert scanner.calls == 2, \
                f"refresh=1 did not bypass the cache ({scanner.calls})"
            assert first["findings"][0]["status"] == "available", \
                f"the finding was not enriched with live status: {first['findings'][0]}"
            assert first["scheduled"] is True, \
                "the report does not say whether scheduled scanning is on"

            # A cache that cannot answer costs a scan, never the page.
            with mock.patch.object(maintenance, "cache") as broken:
                broken.get.side_effect = RuntimeError("redis down")
                broken.set.side_effect = RuntimeError("redis down")
                degraded = maintenance.report(scanner=scanner, rds_client=rds)
            assert degraded["findings"], \
                "a cache outage emptied the report instead of falling through to a scan"
            assert scanner.calls == 3, "the cache outage did not fall through to a live scan"
        finally:
            cache.delete(maintenance._report_key(REGION))


@th.django_unit_test("only the version the server itself offers can be applied")
def test_offered_target_is_server_derived(opts):
    from mojo.apps.aws.services import maintenance

    current = {"findings": [
        _finding("rds-instance", INSTANCE, "14.11", "16.4"),
        _finding("elasticache", CACHE_GROUP, "6.2", "7.1", engine="redis"),
    ]}
    assert maintenance.offered_target("rds-instance", INSTANCE, current) == "16.4", \
        "the offered target for a known finding was not returned"
    assert maintenance.offered_target("rds-cluster", INSTANCE, current) is None, \
        "a finding was matched on identifier alone, ignoring its kind"
    assert maintenance.offered_target("rds-instance", "other-db", current) is None, \
        "an unlisted resource was offered an upgrade"
    assert maintenance.offered_target("rds-instance", INSTANCE, {"findings": []}) is None, \
        "an empty report still offered a target"


@th.django_unit_test("a requested version the server is not offering is refused")
def test_apply_refuses_unoffered_version(opts):
    from django.core.cache import cache
    from mojo.apps.aws.services import maintenance

    scanner = _Scanner(_scan([_finding("rds-instance", INSTANCE, "14.11", "16.4")]))
    rds = _rds_client(instances=[
        {"DBInstanceIdentifier": INSTANCE, "DBInstanceStatus": "available",
         "EngineVersion": "14.11"}])
    actor = SimpleNamespace(pk=1)
    with mock.patch.object(maintenance, "_setting", _settings()):
        cache.delete(maintenance._report_key(REGION))
        try:
            try:
                maintenance.apply_upgrade(actor, "rds-instance", INSTANCE, "17.0", False,
                                          scanner=scanner, rds_client=rds)
                raise AssertionError("a version the server never offered was applied")
            except maintenance.MaintenanceError as err:
                assert err.error_code == "upgrade_not_offered" and err.status == 409, \
                    f"the refusal is not the documented one: {err.error_code}/{err.status}"
            assert rds.modify_db_instance.call_count == 0, \
                "a refused upgrade still reached the provider"

            try:
                maintenance.apply_upgrade(actor, "rds-instance", "unknown-db", "16.4", False,
                                          scanner=scanner, rds_client=rds)
                raise AssertionError("an unlisted resource was upgraded")
            except maintenance.MaintenanceError as err:
                assert err.error_code == "upgrade_not_offered", \
                    f"an unlisted resource produced {err.error_code}"
        finally:
            cache.delete(maintenance._report_key(REGION))


# ── service: single flight ──────────────────────────────────────────────────

@th.django_unit_test("a second concurrent apply is refused, and an unreadable lock is never a go-ahead")
def test_apply_single_flight(opts):
    from mojo.apps.aws.services import maintenance

    scanner = _Scanner(_scan([_finding("rds-instance", INSTANCE, "14.11", "16.4")]))
    rds = _rds_client(instances=[
        {"DBInstanceIdentifier": INSTANCE, "DBInstanceStatus": "available",
         "EngineVersion": "14.11"}])
    actor = SimpleNamespace(pk=7)

    def run():
        return maintenance.apply_upgrade(
            actor, "rds-instance", INSTANCE, "16.4", False,
            scanner=scanner, rds_client=rds)

    with mock.patch.object(maintenance, "_setting", _settings()):
        with mock.patch.object(maintenance, "cache") as fake:
            fake.get.return_value = None
            fake.add.return_value = True
            assert run()["requested"] is True, "the first apply did not go through"

            # Somebody holds the key: a real, nameable conflict.
            fake.add.return_value = False
            fake.get.return_value = 7
            try:
                run()
                raise AssertionError("a second concurrent apply was allowed")
            except maintenance.MaintenanceError as err:
                assert err.error_code == "upgrade_in_progress" and err.status == 409, \
                    f"a live conflict answered {err.error_code}/{err.status}"

            # The backend cannot be read back. It is NOT proof nobody holds it.
            fake.add.return_value = False
            fake.get.side_effect = RuntimeError("redis down")
            try:
                run()
                raise AssertionError("an unreadable lock was treated as free")
            except maintenance.MaintenanceError as err:
                assert err.error_code == "cache_unavailable" and err.status == 503, \
                    f"an unreadable lock answered {err.error_code}/{err.status}"

            # add() itself failing is the same refusal, for the same reason.
            fake.add.side_effect = RuntimeError("redis down")
            try:
                run()
                raise AssertionError("a failed lock acquisition was treated as success")
            except maintenance.MaintenanceError as err:
                assert err.error_code == "cache_unavailable" and err.status == 503, \
                    f"a failed acquisition answered {err.error_code}/{err.status}"
        assert rds.modify_db_instance.call_count == 1, \
            f"the refused attempts still reached AWS ({rds.modify_db_instance.call_count})"


@th.django_unit_test("an ElastiCache apply resolves the resource shape against the provider")
def test_apply_dispatches_by_resolved_kind(opts):
    from mojo.apps.aws.services import maintenance

    scanner = _Scanner(_scan([
        _finding("elasticache", CACHE_GROUP, "6.2", "7.1", engine="redis")]))
    actor = SimpleNamespace(pk=3)
    cache_client = _cache_client(clusters=[
        {"CacheClusterId": f"{CACHE_GROUP}-001", "ReplicationGroupId": CACHE_GROUP,
         "Engine": "redis", "EngineVersion": "6.2", "CacheClusterStatus": "available"}])

    with mock.patch.object(maintenance, "_setting", _settings()):
        with mock.patch.object(maintenance, "cache") as fake:
            fake.get.return_value = None
            fake.add.return_value = True
            cache_client.describe_replication_groups.return_value = {
                "ReplicationGroups": [{"ReplicationGroupId": CACHE_GROUP}]}
            result = maintenance.apply_upgrade(
                actor, "elasticache", CACHE_GROUP, "7.1", True,
                scanner=scanner, elasticache_client=cache_client)
            assert result["operation"] == "elasticache.modify_replication_group", \
                f"a replication group took the standalone path: {result['operation']}"
            assert cache_client.modify_cache_cluster.call_count == 0, \
                "the standalone modify was called for a replication group"

            # Same identifier, now absent from describe_replication_groups.
            from botocore.exceptions import ClientError
            cache_client.describe_replication_groups.side_effect = ClientError(
                {"Error": {"Code": "ReplicationGroupNotFoundFault", "Message": "no"}},
                "DescribeReplicationGroups")
            scanner.calls = 0
            result = maintenance.apply_upgrade(
                actor, "elasticache", CACHE_GROUP, "7.1", True,
                scanner=scanner, elasticache_client=cache_client)
            assert result["operation"] == "elasticache.modify_cache_cluster", \
                f"a standalone cluster took the replication-group path: {result['operation']}"


# ── service: poll ───────────────────────────────────────────────────────────

@th.django_unit_test("a group with one stale member is settled but NOT upgraded")
def test_resource_status_requires_every_member(opts):
    from mojo.apps.aws.services import maintenance

    client = _cache_client(clusters=[
        {"CacheClusterId": f"{CACHE_GROUP}-001", "ReplicationGroupId": CACHE_GROUP,
         "Engine": "redis", "EngineVersion": "7.1", "CacheClusterStatus": "available"},
        {"CacheClusterId": f"{CACHE_GROUP}-002", "ReplicationGroupId": CACHE_GROUP,
         "Engine": "redis", "EngineVersion": "6.2", "CacheClusterStatus": "available"},
    ])
    row = maintenance.resource_status(
        "elasticache", CACHE_GROUP, "7.1", elasticache_client=client)
    assert row["settled"] is True, "every member is available; the group is settled"
    assert row["upgraded"] is False, \
        "a group with a member still on 6.2 was reported as upgraded"

    client = _cache_client(clusters=[
        {"CacheClusterId": f"{CACHE_GROUP}-001", "ReplicationGroupId": CACHE_GROUP,
         "Engine": "redis", "EngineVersion": "7.1", "CacheClusterStatus": "available"},
        {"CacheClusterId": f"{CACHE_GROUP}-002", "ReplicationGroupId": CACHE_GROUP,
         "Engine": "redis", "EngineVersion": "7.1", "CacheClusterStatus": "available"},
    ])
    row = maintenance.resource_status(
        "elasticache", CACHE_GROUP, "7.1", elasticache_client=client)
    assert row["upgraded"] is True, \
        f"a fully upgraded group was not reported as upgraded: {row}"


@th.django_unit_test("an available instance still on its old version is not an upgrade")
def test_resource_status_settled_is_not_upgraded(opts):
    from mojo.apps.aws.services import maintenance

    client = _rds_client(instances=[
        {"DBInstanceIdentifier": INSTANCE, "DBInstanceStatus": "available",
         "EngineVersion": "14.11"}])
    row = maintenance.resource_status(
        "rds-instance", INSTANCE, "16.4", rds_client=client)
    assert row["settled"] is True and row["upgraded"] is False, \
        f"'available' on the old engine was read as success: {row}"

    client = _rds_client(instances=[
        {"DBInstanceIdentifier": INSTANCE, "DBInstanceStatus": "upgrading",
         "EngineVersion": "14.11",
         "PendingModifiedValues": {"EngineVersion": "16.4"}}])
    row = maintenance.resource_status(
        "rds-instance", INSTANCE, "16.4", rds_client=client)
    assert row["settled"] is False and row["pending_version"] == "16.4", \
        f"an in-flight upgrade did not report its pending version: {row}"


# ── REST ────────────────────────────────────────────────────────────────────

def _view(name):
    import inspect
    from mojo.apps.aws.rest import maintenance as views
    return inspect.unwrap(getattr(views, name))


def _request(user, **data):
    from objict import objict
    return SimpleNamespace(user=user, DATA=objict(**data), META={})


def _user(pk=1, superuser=False, perms=()):
    granted = set(perms)
    user = mock.Mock(is_superuser=superuser, pk=pk, username=f"user-{pk}")
    user.has_permission.side_effect = lambda wanted: bool(granted & set(wanted))
    return user


@th.django_unit_test("manage_aws alone cannot apply an infrastructure upgrade")
def test_apply_requires_a_platform_tier(opts):
    from mojo import errors as me
    from mojo.apps.aws.services import maintenance

    view = _view("on_maintenance_apply")
    body = {"kind": "rds-instance", "resource": INSTANCE, "target_version": "16.4",
            "confirm_resource": INSTANCE, "apply_immediately": False}
    with mock.patch.object(maintenance, "apply_upgrade") as applied:
        with th.assert_raises(me.PermissionDeniedException):
            view(_request(_user(perms=["manage_aws"]), **body))
        assert applied.call_count == 0, \
            "a caller without platform authority still reached the apply service"

        # The platform tier, and a superuser, both pass.
        applied.return_value = {"target_version": "16.4"}
        with mock.patch("mojo.apps.account.services.admin_platform.audit_after_commit"):
            view(_request(_user(perms=["manage_aws", "manage_platform"]), **body))
            view(_request(_user(superuser=True), **body))
        assert applied.call_count == 2, \
            f"a permitted caller was refused ({applied.call_count} applies)"


@th.django_unit_test("a mismatched typed confirmation is refused before anything reaches AWS")
def test_apply_confirmation_is_checked_first(opts):
    from mojo import errors as me
    from mojo.apps.aws.services import maintenance

    view = _view("on_maintenance_apply")
    actor = _user(superuser=True)
    with mock.patch.object(maintenance, "apply_upgrade") as applied:
        with th.assert_raises(me.ValueException):
            view(_request(actor, kind="rds-instance", resource=INSTANCE,
                          target_version="16.4", confirm_resource="mojo-test-postgre",
                          apply_immediately=False))
        with th.assert_raises(me.ValueException):
            view(_request(actor, kind="rds-instance", resource=INSTANCE,
                          target_version="16.4", confirm_resource="",
                          apply_immediately=False))
        assert applied.call_count == 0, \
            "a mismatched confirmation still reached the apply service"


@th.django_unit_test("the apply window has no default — a missing choice is a 400")
def test_apply_window_must_be_stated(opts):
    from mojo import errors as me
    from mojo.apps.aws.services import maintenance

    view = _view("on_maintenance_apply")
    actor = _user(superuser=True)
    with mock.patch.object(maintenance, "apply_upgrade") as applied:
        with th.assert_raises(me.ValueException):
            view(_request(actor, kind="rds-instance", resource=INSTANCE,
                          target_version="16.4", confirm_resource=INSTANCE))
        # A string is not a decision either; "false" would be truthy.
        with th.assert_raises(me.ValueException):
            view(_request(actor, kind="rds-instance", resource=INSTANCE,
                          target_version="16.4", confirm_resource=INSTANCE,
                          apply_immediately="false"))
        assert applied.call_count == 0, \
            "an unstated apply window still reached the apply service"


@th.django_unit_test("one apply writes exactly one audit event naming the resource and target")
def test_apply_audits_once(opts):
    from mojo.apps.aws.services import maintenance

    view = _view("on_maintenance_apply")
    with mock.patch.object(maintenance, "apply_upgrade") as applied:
        applied.return_value = {"requested": True, "target_version": "16.4"}
        with mock.patch("mojo.apps.account.services.admin_platform.audit_after_commit") as audit:
            view(_request(_user(superuser=True), kind="rds-instance", resource=INSTANCE,
                          target_version="16.4", confirm_resource=INSTANCE,
                          apply_immediately=True))
        assert audit.call_count == 1, \
            f"one apply wrote {audit.call_count} audit events"
        _, action, target = audit.call_args[0]
        assert action == "aws_engine_upgrade", f"the audit action is {action!r}"
        assert INSTANCE in target and "16.4" in target, \
            f"the audit target does not identify what changed: {target!r}"


@th.django_unit_test("all maintenance writes declare key denial and fixed fresh auth")
def test_maintenance_write_decorators(opts):
    from mojo.apps.aws.rest import maintenance as views

    func = views.on_maintenance_apply
    assert getattr(func, "_mojo_denies_key_backed_session", False), \
        "the apply endpoint accepts key-backed sessions"
    assert getattr(func, "_mojo_requires_fresh_auth", False), \
        "the apply endpoint does not require fresh auth"
    assert getattr(func, "_mojo_fresh_auth_seconds", None) == 600, \
        "the apply endpoint does not pin its fresh-auth window"


@th.django_unit_test("maintenance endpoints refuse an unauthenticated caller")
def test_maintenance_endpoints_require_auth(opts):
    opts.client.logout()
    for path in ("/api/aws/maintenance/versions",
                 "/api/aws/maintenance/status?kind=rds-instance&resource=x"):
        response = opts.client.get(path)
        assert response.status_code in (401, 403), \
            f"{path} answered {response.status_code} to an anonymous caller"
    response = opts.client.post("/api/aws/maintenance/apply", {"kind": "rds-instance"})
    assert response.status_code in (401, 403), \
        f"apply answered {response.status_code} to an anonymous caller"


@th.django_unit_test("maintenance endpoints refuse a caller without manage_aws")
def test_maintenance_endpoints_require_manage_aws(opts):
    from mojo.apps.account.models import User

    User.objects.filter(username="maint-none").delete()
    plain = User.objects.create_user(
        email="maint-none@test.com", username="maint-none", password="example")
    plain.is_active = True
    plain.save()
    try:
        assert opts.client.login("maint-none@test.com", "example"), \
            "the unprivileged fixture could not establish a session"
        for path in ("/api/aws/maintenance/versions",
                     "/api/aws/maintenance/status?kind=rds-instance&resource=x"):
            response = opts.client.get(path)
            assert response.status_code in (401, 403), \
                f"{path} answered {response.status_code} without manage_aws"
    finally:
        opts.client.logout()
        User.objects.filter(username="maint-none").delete()


@th.django_unit_test("manage_aws reads the maintenance report over HTTP")
def test_maintenance_versions_readable(opts):
    try:
        assert opts.client.login("maint-aws@test.com", "example"), \
            "the manage_aws fixture could not establish a session"
        response = opts.client.get("/api/aws/maintenance/versions")
        assert response.status_code == 200, \
            f"manage_aws could not read the report: {opts.client.last_response.body}"
        # No credentials in the suite, so the honest answer is 'unavailable'.
        data = (response.response or {}).get("data") or {}
        assert data.get("status") in ("ok", "unavailable"), \
            f"the report envelope is not one of the two documented states: {data}"
        assert "findings" in data and "scheduled" in data, \
            f"the report is missing its documented keys: {sorted(data)}"
    finally:
        opts.client.logout()


# ── INFRASTRUCTURE_MODE ─────────────────────────────────────────────────────

def _body():
    return {"kind": "rds-instance", "resource": INSTANCE, "target_version": "16.4",
            "confirm_resource": INSTANCE, "apply_immediately": False}


def _payload(response):
    return json.loads(response.content.decode())










