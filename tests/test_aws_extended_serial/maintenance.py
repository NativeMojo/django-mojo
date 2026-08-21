"""Moved from the default-tier sibling (maestro item #1839): these tests mutate django.conf.settings process-wide via a save/restore helper, which is unsafe under the parallel default tier.
"""
"""Admin Maintenance: engine-version helpers, the apply service, and its gate.

The helper tests use ``botocore.stub.Stubber`` rather than the plain Mock
clients the sibling AWS tests use, and deliberately so: a Mock accepts any
keyword, so it would have accepted ``AllowMajorVersionUpgrade`` on the
ElastiCache modify operations — a member those operations do not define, and
an API error against a live cache. Stubber validates every request against the
real service model, which is the only thing that can catch that.
"""

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from testit import helpers as th


@contextmanager
def _override_setting(name, value):
    """In-process Django settings override (th.server_settings only affects the
    separate server process; override_settings is banned by testing rules)."""
    import django.conf
    sentinel = object()
    original = getattr(django.conf.settings, name, sentinel)
    setattr(django.conf.settings, name, value)
    try:
        yield
    finally:
        if original is sentinel:
            delattr(django.conf.settings, name)
        else:
            setattr(django.conf.settings, name, original)


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







# ── helpers: ElastiCache mutations ──────────────────────────────────────────







# ── helpers: reads ──────────────────────────────────────────────────────────







# ── service: report, cache, offered target ──────────────────────────────────







# ── service: single flight ──────────────────────────────────────────────────





# ── service: poll ───────────────────────────────────────────────────────────





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


















# ── INFRASTRUCTURE_MODE ─────────────────────────────────────────────────────

def _body():
    return {"kind": "rds-instance", "resource": INSTANCE, "target_version": "16.4",
            "confirm_resource": INSTANCE, "apply_immediately": False}


def _payload(response):
    return json.loads(response.content.decode())


@th.django_unit_test("only 'external' turns the switch on, and everything unrecognized fails closed")
def test_infrastructure_mode_resolution(opts):
    from mojo.helpers import infrastructure

    assert infrastructure.infrastructure_mode() == infrastructure.MANAGED, \
        "an installation that never set INFRASTRUCTURE_MODE is not managed"

    for value in ("", "managed", " MANAGED "):
        with _override_setting("INFRASTRUCTURE_MODE", value):
            assert infrastructure.infrastructure_mode() == infrastructure.MANAGED, \
                f"{value!r} should mean managed"
            assert infrastructure.is_external() is False, \
                f"{value!r} reported an external installation"

    for value in ("external", " External "):
        with _override_setting("INFRASTRUCTURE_MODE", value):
            assert infrastructure.infrastructure_mode() == infrastructure.EXTERNAL, \
                f"{value!r} should mean external"
            assert infrastructure.is_external() is True, \
                f"{value!r} did not report an external installation"

    # A typo in a switch whose whole job is to refuse must not disable it.
    for value in ("externl", "off", True, 1):
        with _override_setting("INFRASTRUCTURE_MODE", value):
            assert infrastructure.infrastructure_mode() == infrastructure.EXTERNAL, \
                f"the unrecognized value {value!r} failed OPEN to managed"

    # A settings read that blows up is not a licence to mutate either.
    with mock.patch("mojo.helpers.infrastructure.settings.get_static",
                    side_effect=RuntimeError("settings backend down")):
        assert infrastructure.infrastructure_mode() == infrastructure.EXTERNAL, \
            "a failed settings read fell back to managed"


@th.django_unit_test("an external installation refuses the engine upgrade with a named 403")
def test_apply_refused_in_external_mode(opts):
    from mojo.helpers import infrastructure
    from mojo.apps.aws.services import maintenance

    view = _view("on_maintenance_apply")
    with _override_setting("INFRASTRUCTURE_MODE", "external"):
        with mock.patch.object(maintenance, "apply_upgrade") as applied:
            response = view(_request(_user(superuser=True), **_body()))
        assert applied.call_count == 0, \
            "an external installation still reached the apply service"
    assert response.status_code == 403, \
        f"the refusal is not a 403: {response.status_code}"
    payload = _payload(response)
    assert payload["error_code"] == infrastructure.ERROR_CODE, \
        f"the refusal does not carry its documented code: {payload}"
    assert payload["status"] is False, f"the refusal claims success: {payload}"
    assert payload["data"] == {"mode": "external", "setting": "INFRASTRUCTURE_MODE"}, \
        f"the refusal does not name the mode and the setting: {payload}"
    assert "INFRASTRUCTURE_MODE" in payload["error"], \
        f"the refusal message never names the switch: {payload['error']}"


@th.django_unit_test("the mode is answered before the caller's grants are")
def test_mode_is_first_in_body_check(opts):
    from mojo.helpers import infrastructure
    from mojo.apps.aws.services import maintenance

    view = _view("on_maintenance_apply")
    # manage_aws alone is a permission denial on a managed install. On an
    # external one the answer must be about the INSTALLATION, because no
    # additional grant would change it.
    with _override_setting("INFRASTRUCTURE_MODE", "external"):
        with mock.patch.object(maintenance, "apply_upgrade") as applied:
            response = view(_request(_user(perms=["manage_aws"]), **_body()))
        assert applied.call_count == 0, \
            "a refused caller still reached the apply service"
    assert response.status_code == 403, \
        f"the refusal is not a 403: {response.status_code}"
    assert _payload(response)["error_code"] == infrastructure.ERROR_CODE, \
        ("an external installation answered a permission question instead of "
         f"naming the mode: {_payload(response)}")


@th.django_unit_test("the apply service refuses an external installation on its own")
def test_apply_service_backstop_refuses(opts):
    from mojo.helpers import infrastructure
    from mojo.apps.aws.services import maintenance

    rds = _rds_client()
    cache_client = _cache_client()
    scanner = _Scanner(_scan([_finding("rds-instance", INSTANCE, "15.6", "16.4", days=10)]))
    with _override_setting("INFRASTRUCTURE_MODE", "external"):
        try:
            maintenance.apply_upgrade(
                None, "rds-instance", INSTANCE, "16.4", False,
                scanner=scanner, rds_client=rds, elasticache_client=cache_client)
            raise AssertionError(
                "a non-REST caller mutated AWS on an external installation")
        except maintenance.MaintenanceError as err:
            assert err.error_code == infrastructure.ERROR_CODE, \
                f"the backstop refusal is not the documented code: {err.error_code}"
            assert err.status == 403, f"the backstop refusal is not a 403: {err.status}"
    assert scanner.calls == 0, "the refused apply still scanned AWS"
    assert rds.describe_db_instances.call_count == 0, \
        "the refused apply still described RDS"
    assert cache_client.describe_cache_clusters.call_count == 0, \
        "the refused apply still described ElastiCache"


@th.django_unit_test("reads are untouched by the mode — only the mutation is gated")
def test_reads_answer_in_external_mode(opts):
    from mojo.apps.aws.services import maintenance

    versions = _view("on_maintenance_versions")
    status = _view("on_maintenance_status")
    report = _scan([_finding("rds-instance", INSTANCE, "15.6", "16.4", days=10)])
    live = {"schema_version": 1, "resource": INSTANCE, "found": True,
            "status": "available", "settled": True, "upgraded": True,
            "engine_version": "16.4", "target_version": "16.4"}
    with _override_setting("INFRASTRUCTURE_MODE", "external"):
        with mock.patch.object(maintenance, "report", return_value=report):
            listed = versions(_request(_user(perms=["manage_aws"])))
        with mock.patch.object(maintenance, "resource_status", return_value=live):
            polled = status(_request(_user(perms=["manage_aws"]),
                                     kind="rds-instance", resource=INSTANCE,
                                     target_version="16.4"))
    assert listed.status_code == 200, \
        f"the versions read was refused on an external installation: {listed.status_code}"
    listed_data = _payload(listed)["data"]
    assert "findings" in listed_data and "status" in listed_data, \
        f"the versions read lost its documented keys: {sorted(listed_data)}"
    assert polled.status_code == 200, \
        f"the status read was refused on an external installation: {polled.status_code}"
    polled_data = _payload(polled)["data"]
    assert polled_data["settled"] is True and polled_data["upgraded"] is True, \
        f"the status read lost its documented keys: {sorted(polled_data)}"
