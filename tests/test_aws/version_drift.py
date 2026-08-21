"""Managed-service version drift: scanner, cronjob, asyncjob and rule wiring.

AWS is faked with plain `unittest.mock.Mock` clients injected through the
scanner's `clients=` dict — the same idiom as tests/test_aws/aws_check.py.
There is no moto and no botocore.Stubber anywhere in this repo.
"""

import datetime
from unittest import mock

from botocore.exceptions import ClientError, NoCredentialsError

from testit import helpers as th


CHANNEL = "testit_aws_version_drift"
RULESET_NAME = "Health - AWS Version Drift"


def _setting_values(**overrides):
    values = {
        "AWS_REGION": "us-east-1",
        "AWS_KEY": None,
        "AWS_SECRET": None,
        "AWS_MONITORING_NAME": "testdeploy",
        "BASE_URL": "https://api.example.com",
        "AWS_VERSION_DRIFT_DEADLINE_DAYS": 180,
        "AWS_CLOUDWATCH_ALARM_TOPIC_ARNS": [],
    }
    values.update(overrides)
    return lambda name, default=None, kind=None: values.get(name, default)


def _denied(operation):
    return ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, operation)


def _rds_client(clusters=(), instances=(), engine_versions=(), lifecycles=(), denied=()):
    """A Mock rds client answering only the four APIs the scanner calls."""
    client = mock.Mock()

    def answer(name, payload):
        if name in denied:
            raise _denied(name)
        return payload

    client.describe_db_clusters.side_effect = lambda **kw: answer(
        "describe_db_clusters", {"DBClusters": list(clusters)})
    client.describe_db_instances.side_effect = lambda **kw: answer(
        "describe_db_instances", {"DBInstances": list(instances)})
    client.describe_db_engine_versions.side_effect = lambda **kw: answer(
        "describe_db_engine_versions", {"DBEngineVersions": list(engine_versions)})
    client.describe_db_major_engine_versions.side_effect = lambda **kw: answer(
        "describe_db_major_engine_versions", {"DBMajorEngineVersions": list(lifecycles)})
    return client


def _elasticache_client(clusters=(), versions=(), denied=()):
    client = mock.Mock()

    def answer(name, payload):
        if name in denied:
            raise _denied(name)
        return payload

    client.describe_cache_clusters.side_effect = lambda **kw: answer(
        "describe_cache_clusters", {"CacheClusters": list(clusters)})
    client.describe_cache_engine_versions.side_effect = lambda **kw: answer(
        "describe_cache_engine_versions", {"CacheEngineVersions": list(versions)})
    return client


def _aurora_fixture(now, standard_days=60, extended_days=1155):
    """One Aurora PostgreSQL 13 cluster with a major target and BOTH lifecycles."""
    clusters = [{
        "DBClusterIdentifier": "prod-aurora",
        "Engine": "aurora-postgresql",
        "EngineVersion": "13.12",
    }]
    engine_versions = [{
        "Engine": "aurora-postgresql",
        "EngineVersion": "13.12",
        "MajorEngineVersion": "13",
        "ValidUpgradeTarget": [
            {"EngineVersion": "13.14", "IsMajorVersionUpgrade": False},
            {"EngineVersion": "15.4", "IsMajorVersionUpgrade": True},
            {"EngineVersion": "16.1", "IsMajorVersionUpgrade": True},
        ],
    }]
    lifecycles = [{
        "Engine": "aurora-postgresql",
        "MajorEngineVersion": "13",
        "SupportedEngineLifecycles": [
            {"LifecycleSupportName": "open-source-rds-standard-support",
             "LifecycleSupportEndDate": now + datetime.timedelta(days=standard_days)},
            {"LifecycleSupportName": "open-source-rds-extended-support",
             "LifecycleSupportEndDate": now + datetime.timedelta(days=extended_days)},
        ],
    }]
    return clusters, engine_versions, lifecycles


def _scan(rds, elasticache, now, **kwargs):
    from mojo.apps.aws.services import version_drift
    with mock.patch.object(version_drift, "_setting", side_effect=_setting_values()):
        scanner = version_drift.VersionDriftScanner(
            clients={"rds": rds, "elasticache": elasticache},
            now=lambda: now, **kwargs)
        return scanner.scan()


@th.django_unit_setup()
def setup_version_drift(opts):
    """Long-lived DB: clear anything a previous run of this module created."""
    from django.utils import timezone
    from mojo.apps.aws.services import version_drift
    from mojo.apps.incident.models import Event, RuleSet

    Event.objects.filter(category=version_drift.CATEGORY).delete()
    RuleSet.objects.filter(category=version_drift.RULESET_CATEGORY).delete()
    RuleSet.objects.filter(name=RULESET_NAME).delete()
    th.clear_jobs(channel=CHANNEL)
    opts.now = timezone.now()


@th.django_unit_test()
def test_aurora_major_upgrade_with_deadline_is_level_8(opts):
    from mojo.apps.aws.services import version_drift

    clusters, engine_versions, lifecycles = _aurora_fixture(opts.now, standard_days=60)
    report = _scan(
        _rds_client(clusters=clusters, engine_versions=engine_versions, lifecycles=lifecycles),
        _elasticache_client(), opts.now)

    assert report["status"] == "ok", f"A fully answered scan must be ok, got {report}"
    assert len(report["findings"]) == 1, \
        f"One Aurora cluster with a major target is exactly one finding, got {report['findings']}"
    finding = report["findings"][0]
    assert finding["kind"] == "rds-cluster", \
        f"Aurora's upgrade unit is the cluster, got kind={finding['kind']}"
    assert finding["available_major"] == "16.1", \
        f"The HIGHEST major target must win, got {finding['available_major']}"
    assert finding["days_remaining"] == 60, \
        f"Deadline is 60 days out, got days_remaining={finding['days_remaining']}"
    assert report["level"] == 8, \
        f"A published deadline inside the window is level 8, got {report['level']} for {finding}"
    assert version_drift.CATEGORY == "system:health:aws_versions", \
        "The event category must stay on the health strip"
    assert version_drift.RULESET_CATEGORY == "aws:versions", \
        "The RuleSet category must stay OUT of the system:health: namespace"


@th.django_unit_test()
def test_standard_support_wins_over_extended_support(opts):
    """The ~3-years-late bug: a naive max() over SupportedEngineLifecycles."""
    clusters, engine_versions, lifecycles = _aurora_fixture(
        opts.now, standard_days=60, extended_days=1155)
    report = _scan(
        _rds_client(clusters=clusters, engine_versions=engine_versions, lifecycles=lifecycles),
        _elasticache_client(), opts.now)

    finding = report["findings"][0]
    expected_standard = (opts.now + datetime.timedelta(days=60)).isoformat()
    expected_extended = (opts.now + datetime.timedelta(days=1155)).isoformat()
    assert finding["deadline"] == expected_standard, (
        "deadline must be the open-source-rds-standard-support date, not the "
        f"later extended one; got {finding['deadline']} expected {expected_standard}")
    assert finding["extended_deadline"] == expected_extended, (
        "the paid extended-support date must be carried in its own labelled "
        f"field; got {finding['extended_deadline']} expected {expected_extended}")
    assert finding["days_remaining"] == 60, (
        "days_remaining must count to standard support ending, not extended; "
        f"got {finding['days_remaining']}")


@th.django_unit_test()
def test_standard_support_absent_never_falls_back_to_extended(opts):
    clusters, engine_versions, lifecycles = _aurora_fixture(opts.now)
    lifecycles[0]["SupportedEngineLifecycles"] = [
        {"LifecycleSupportName": "open-source-rds-extended-support",
         "LifecycleSupportEndDate": opts.now + datetime.timedelta(days=1155)},
    ]
    report = _scan(
        _rds_client(clusters=clusters, engine_versions=engine_versions, lifecycles=lifecycles),
        _elasticache_client(), opts.now)

    finding = report["findings"][0]
    assert finding["deadline"] is None, (
        "With no standard-support lifecycle the deadline is unknown and must "
        f"never fall back to extended support; got {finding['deadline']}")
    assert report["level"] == 5, \
        f"An upgrade with no known deadline is level 5, got {report['level']}"


@th.django_unit_test()
def test_minor_only_upgrade_target_is_not_a_finding(opts):
    clusters = [{
        "DBClusterIdentifier": "prod-aurora",
        "Engine": "aurora-postgresql",
        "EngineVersion": "15.4",
    }]
    engine_versions = [{
        "Engine": "aurora-postgresql", "EngineVersion": "15.4", "MajorEngineVersion": "15",
        "ValidUpgradeTarget": [
            {"EngineVersion": "15.5", "IsMajorVersionUpgrade": False},
            {"EngineVersion": "15.6", "IsMajorVersionUpgrade": False},
        ],
    }]
    report = _scan(
        _rds_client(clusters=clusters, engine_versions=engine_versions),
        _elasticache_client(), opts.now)

    assert report["findings"] == [], (
        "Minor upgrades are already handled by auto-minor-version-upgrade and "
        f"must not be reported as drift; got {report['findings']}")
    assert report["level"] == 1, \
        f"Nothing to do means level 1, got {report['level']}"


@th.django_unit_test()
def test_aurora_member_instances_are_not_double_counted(opts):
    clusters, engine_versions, lifecycles = _aurora_fixture(opts.now)
    instances = [
        {"DBInstanceIdentifier": "prod-aurora-1", "DBClusterIdentifier": "prod-aurora",
         "Engine": "aurora-postgresql", "EngineVersion": "13.12"},
        {"DBInstanceIdentifier": "prod-aurora-2", "DBClusterIdentifier": "prod-aurora",
         "Engine": "aurora-postgresql", "EngineVersion": "13.12"},
    ]
    report = _scan(
        _rds_client(clusters=clusters, instances=instances,
                    engine_versions=engine_versions, lifecycles=lifecycles),
        _elasticache_client(), opts.now)

    assert len(report["findings"]) == 1, (
        "Instances carrying a DBClusterIdentifier belong to the cluster and "
        f"must not each produce a finding; got {report['findings']}")


@th.django_unit_test()
def test_elasticache_reports_major_with_no_deadline_and_never_valkey(opts):
    clusters = [{
        "CacheClusterId": "prod-redis-001", "ReplicationGroupId": "prod-redis",
        "Engine": "redis", "EngineVersion": "6.2.6",
    }]
    versions = [
        {"Engine": "redis", "EngineVersion": "6.2.6"},
        {"Engine": "redis", "EngineVersion": "7.1.0"},
        {"Engine": "valkey", "EngineVersion": "8.0.0"},
    ]
    report = _scan(_rds_client(), _elasticache_client(clusters=clusters, versions=versions),
                   opts.now)

    assert len(report["findings"]) == 1, \
        f"One replication group is one finding, got {report['findings']}"
    finding = report["findings"][0]
    assert finding["resource_id"] == "prod-redis", (
        "The replication group, not the member cluster, is the reported "
        f"resource; got {finding['resource_id']}")
    assert finding["available_major"] == "7.1.0", (
        "redis -> valkey is an engine migration, not a version bump, and must "
        f"never be offered as an upgrade; got {finding['available_major']}")
    assert finding["deadline"] is None, (
        "The ElastiCache API publishes no lifecycle data, so the deadline must "
        f"be None rather than a guess; got {finding['deadline']}")
    assert report["level"] == 5, \
        f"An upgrade with no deadline is level 5, got {report['level']}"


@th.django_unit_test()
def test_six_dot_x_engine_version_does_not_raise(opts):
    clusters = [{
        "CacheClusterId": "legacy-001", "ReplicationGroupId": "legacy",
        "Engine": "redis", "EngineVersion": "6.x",
    }]
    versions = [{"Engine": "redis", "EngineVersion": "7.1.0"}]
    report = _scan(_rds_client(), _elasticache_client(clusters=clusters, versions=versions),
                   opts.now)

    assert report["status"] == "ok", (
        "ElastiCache really reports '6.x' for Redis; int('x') must never be "
        f"reached by the version key; got {report}")
    assert report["findings"][0]["current_version"] == "6.x", \
        f"The reported version is passed through verbatim, got {report['findings']}"


@th.django_unit_test()
def test_missing_credentials_is_unavailable_and_files_nothing(opts):
    from mojo.apps import jobs
    from mojo.apps.aws import asyncjobs
    from mojo.apps.aws.services import version_drift
    from mojo.apps.incident.models import Event

    Event.objects.filter(category=version_drift.CATEGORY).delete()
    th.clear_jobs(channel=CHANNEL)

    rds = mock.Mock()
    rds.describe_db_clusters.side_effect = NoCredentialsError()
    report = _scan(rds, _elasticache_client(), opts.now)
    assert report["status"] == "unavailable", (
        "A box with no AWS credentials — every dev machine and the test suite — "
        f"must report unavailable rather than 'nothing is out of date'; got {report}")
    assert "NoCredentialsError" in report["reason"], \
        f"The reason must name what failed, got {report.get('reason')}"

    with mock.patch.object(asyncjobs.version_drift, "scan", return_value=report):
        jobs.publish(func="mojo.apps.aws.asyncjobs.check_version_drift",
                     channel=CHANNEL, payload={})
        executed = th.run_pending_jobs(channel=CHANNEL)
    assert executed >= 1, f"The drift job must have run, executed={executed}"
    assert Event.objects.filter(category=version_drift.CATEGORY).count() == 0, (
        "An unavailable scan must file no event — a daily 'couldn't check' on "
        "every dev box is pure noise")


@th.django_unit_test()
def test_denied_api_warns_by_exact_iam_action_and_still_files(opts):
    from mojo.apps import jobs
    from mojo.apps.aws import asyncjobs
    from mojo.apps.aws.services import version_drift
    from mojo.apps.incident.models import Event

    Event.objects.filter(category=version_drift.CATEGORY).delete()
    th.clear_jobs(channel=CHANNEL)

    elasticache = _elasticache_client(
        clusters=[{"CacheClusterId": "prod-redis-001", "ReplicationGroupId": "prod-redis",
                   "Engine": "redis", "EngineVersion": "6.2.6"}],
        versions=[{"Engine": "redis", "EngineVersion": "7.1.0"}])
    partial = _scan(
        _rds_client(denied=("describe_db_clusters", "describe_db_instances")),
        elasticache, opts.now)

    assert partial["status"] == "ok", (
        f"One denied API must not abort the whole scan, got {partial['status']}")
    actions = [warning["iam_action"] for warning in partial["warnings"]]
    assert "rds:DescribeDBClusters" in actions, (
        f"The warning must name the EXACT missing IAM action, got {actions}")
    assert "rds:DescribeDBInstances" in actions, (
        f"Every denied API must be named, got {actions}")
    assert len(partial["findings"]) == 1, (
        "The APIs that DID answer must still produce findings, got "
        f"{partial['findings']}")

    # Denied everywhere: the level-4 floor is what keeps a locked-down
    # deployment from looking identical to "nothing is out of date".
    silent = _scan(
        _rds_client(denied=("describe_db_clusters", "describe_db_instances")),
        _elasticache_client(denied=("describe_cache_clusters",)), opts.now)
    assert silent["findings"] == [], f"Nothing could be inventoried, got {silent['findings']}"
    assert silent["level"] == 4, (
        "A scan that inventoried nothing because of IAM must still be level 4 "
        f"so an event is filed; got {silent['level']}")

    with mock.patch.object(asyncjobs.version_drift, "scan", return_value=silent):
        jobs.publish(func="mojo.apps.aws.asyncjobs.check_version_drift",
                     channel=CHANNEL, payload={})
        th.run_pending_jobs(channel=CHANNEL)
    events = list(Event.objects.filter(category=version_drift.CATEGORY))
    assert len(events) == 1, (
        f"A denied scan must file exactly one event, not stay silent; got {events}")
    assert events[0].level == 4, \
        f"The denied-only event is level 4, got {events[0].level}"


@th.django_unit_test()
def test_asyncjob_files_one_event_with_the_worst_level(opts):
    from mojo.apps import jobs
    from mojo.apps.aws import asyncjobs
    from mojo.apps.aws.services import version_drift
    from mojo.apps.incident.models import Event

    Event.objects.filter(category=version_drift.CATEGORY).delete()
    th.clear_jobs(channel=CHANNEL)

    clusters, engine_versions, lifecycles = _aurora_fixture(opts.now, standard_days=-3)
    report = _scan(
        _rds_client(clusters=clusters, engine_versions=engine_versions, lifecycles=lifecycles),
        _elasticache_client(
            clusters=[{"CacheClusterId": "prod-redis-001", "ReplicationGroupId": "prod-redis",
                       "Engine": "redis", "EngineVersion": "6.2.6"}],
            versions=[{"Engine": "redis", "EngineVersion": "7.1.0"}]),
        opts.now)
    assert report["level"] == 10, (
        "A deadline already in the past is level 10, got "
        f"{report['level']} for {report['findings']}")

    with mock.patch.object(asyncjobs.version_drift, "scan", return_value=report):
        jobs.publish(func="mojo.apps.aws.asyncjobs.check_version_drift",
                     channel=CHANNEL, payload={})
        th.run_pending_jobs(channel=CHANNEL)

    events = list(Event.objects.filter(category=version_drift.CATEGORY))
    assert len(events) == 1, f"The job must file exactly ONE event, got {events}"
    event = events[0]
    assert event.level == 10, f"The event carries the worst level, got {event.level}"
    assert event.scope == version_drift.RULESET_CATEGORY, (
        "RuleSets are matched by scope first, so the scope must be the ruleset "
        f"category; got {event.scope}")
    rows = event.metadata.get("findings") or []
    assert len(rows) == 2, \
        f"Both finding rows must reach the event metadata, got {rows}"
    kinds = sorted(row["kind"] for row in rows)
    assert kinds == ["elasticache", "rds-cluster"], \
        f"Both services must be represented, got {kinds}"


@th.django_unit_test()
def test_cronjob_is_daily_and_gated_by_the_setting(opts):
    from mojo.apps.aws import cronjobs
    from mojo.decorators.cron import schedule

    specs = [spec for spec in getattr(schedule, "scheduled_functions", [])
             if spec["func"].__module__ == "mojo.apps.aws.cronjobs"
             and spec["func"].__name__ == "check_version_drift"]
    assert len(specs) == 1, \
        f"check_version_drift must be registered exactly once, got {len(specs)}"
    spec = specs[0]
    assert (spec["minutes"], spec["hours"]) == ("0", "7"), (
        "The scan runs daily at 07:00 — a monthly single-minute window has no "
        f"catch-up and would cost 30 days per miss; got {spec}")
    assert (spec["days"], spec["months"], spec["weekdays"]) == ("*", "*", "*"), \
        f"Daily means every day/month/weekday, got {spec}"

    with mock.patch.object(cronjobs, "jobs") as disabled, \
            mock.patch.object(cronjobs, "_setting", return_value=False):
        cronjobs.check_version_drift()
    assert disabled.publish.call_count == 0, \
        "AWS_VERSION_DRIFT_ENABLED=False must publish nothing"

    with mock.patch.object(cronjobs, "jobs") as enabled, \
            mock.patch.object(cronjobs, "_setting", return_value=True):
        cronjobs.check_version_drift()
    enabled.publish.assert_called_once_with(
        func="mojo.apps.aws.asyncjobs.check_version_drift", channel="cleanup", payload={})


@th.django_unit_test()
def test_aws_check_versions_section_is_opt_in_and_never_fails(opts):
    from mojo.apps.aws.services import aws_check, version_drift

    assert "versions" not in aws_check.SECTIONS, (
        "SECTIONS is what `selected` defaults to, so `versions` must stay out "
        f"of it; got {aws_check.SECTIONS}")
    assert "versions" in aws_check.ALL_SECTIONS, \
        f"`versions` must still be selectable, got {aws_check.ALL_SECTIONS}"

    clusters, engine_versions, lifecycles = _aurora_fixture(opts.now, standard_days=60)
    clients = {
        "rds": _rds_client(clusters=clusters, engine_versions=engine_versions,
                           lifecycles=lifecycles),
        "elasticache": _elasticache_client(),
    }
    with mock.patch.object(aws_check, "_setting", side_effect=_setting_values()), \
            mock.patch.object(version_drift, "_setting", side_effect=_setting_values()):
        report = aws_check.AWSCheckRunner(clients=clients).run(["versions"])
    sections = [item["section"] for item in report["items"]]
    assert sections and set(sections) == {"versions"}, (
        "run(['versions']) must actually EXECUTE the section — iterating only "
        f"SECTIONS would validate and then do nothing; got {report['items']}")
    assert report["overall"] == "pass", \
        f"The versions section must never fail the overall run, got {report}"

    with mock.patch.object(aws_check, "_setting", side_effect=_setting_values()):
        default = aws_check.AWSCheckRunner(clients=clients).run(["prerequisites"])
    assert not [item for item in default["items"] if item["section"] == "versions"], (
        "A run that did not select `versions` must contain no versions item; "
        f"got {default['items']}")

    denied_clients = {
        "rds": _rds_client(denied=("describe_db_clusters", "describe_db_instances")),
        "elasticache": _elasticache_client(denied=("describe_cache_clusters",)),
    }
    with mock.patch.object(aws_check, "_setting", side_effect=_setting_values()), \
            mock.patch.object(version_drift, "_setting", side_effect=_setting_values()):
        denied = aws_check.AWSCheckRunner(clients=denied_clients).run(["versions"])
    statuses = {item["status"] for item in denied["items"]}
    assert statuses == {"warn"}, (
        "AccessDenied is the expected first-run outcome and must be a warn, "
        f"never a fail; got {denied['items']}")
    assert denied["overall"] == "pass", (
        "`aws-check --check --section versions` must not exit 1 just because "
        f"the IAM grant is not in place yet; got {denied}")


@th.django_unit_test()
def test_ensure_aws_version_rules_is_idempotent_and_gated_at_level_5(opts):
    from mojo.apps.aws.services import version_drift
    from mojo.apps.incident.models import RuleSet

    RuleSet.objects.filter(name=RULESET_NAME).delete()
    first, created = RuleSet.ensure_aws_version_rules()
    assert created, "The first call must create the RuleSet"
    second, created_again = RuleSet.ensure_aws_version_rules()
    assert not created_again, "The second call must reuse the existing RuleSet"
    assert first.pk == second.pk, \
        f"Idempotent means one row, got {first.pk} and {second.pk}"
    assert RuleSet.objects.filter(name=RULESET_NAME).count() == 1, \
        "Calling twice must leave exactly one RuleSet"

    assert first.category == version_drift.RULESET_CATEGORY, (
        "The RuleSet must live outside the system:health: namespace so it can "
        f"never satisfy the health-defaults guard; got {first.category}")
    assert "ticket://" in first.handler, \
        f"The handler must open a ticket, got {first.handler}"
    assert "maestro=1" in first.handler, \
        f"The ticket must be pushed to the board, got {first.handler}"

    rules = list(first.rules.all())
    assert len(rules) == 1, f"Exactly one gate rule is expected, got {rules}"
    rule = rules[0]
    assert (rule.field_name, rule.comparator, rule.value) == ("level", ">=", "5"), (
        "A level-1 'everything is current' event must never open a ticket; got "
        f"{rule.field_name} {rule.comparator} {rule.value}")


