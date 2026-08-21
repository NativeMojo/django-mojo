"""Moved from the default-tier sibling (maestro item #1839): these tests mutate shared testit/production module state process-wide (seam rebinding, module-attribute save/restore), which races every parallel module.
"""
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
def test_version_drift_ruleset_does_not_block_health_defaults(opts):
    """Regression: the health bootstrap guarded on the system:health: PREFIX.

    Any RuleSet in that namespace made it permanently true, so Runner Down /
    Scheduler Missing / TCP Overload were never installed and a level-10
    runner-down event fell through to the handler-less catch-all.
    """
    from mojo.apps.incident import cronjobs as incident_cronjobs
    from mojo.apps.incident.models import RuleSet

    RuleSet.objects.filter(name__in=incident_cronjobs.HEALTH_RULE_NAMES).delete()
    RuleSet.objects.filter(name=RULESET_NAME).delete()
    RuleSet.ensure_aws_version_rules()

    incident_cronjobs._health_defaults_checked = False
    incident_cronjobs._ensure_health_defaults()

    installed = set(RuleSet.objects.filter(
        name__in=incident_cronjobs.HEALTH_RULE_NAMES).values_list("name", flat=True))
    assert installed == set(incident_cronjobs.HEALTH_RULE_NAMES), (
        "Installing the AWS version-drift RuleSet first must not suppress the "
        f"real health defaults; missing {set(incident_cronjobs.HEALTH_RULE_NAMES) - installed}")
    runner = RuleSet.objects.filter(name="Health - Runner Down").first()
    assert runner is not None and "ticket://" in (runner.handler or ""), (
        "Health - Runner Down must exist WITH its notify+ticket handler, "
        f"got {runner and runner.handler}")
