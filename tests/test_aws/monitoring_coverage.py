"""One-node coverage shares raw source reads and never loses expected gaps."""
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import time
from testit import helpers as th


class Actor:
    is_active = True
    is_authenticated = True
    is_request_user = True
    def __init__(self, *permissions):
        self.permissions = set(permissions)
    def has_permission(self, permissions):
        return bool(self.permissions.intersection(permissions))


def _readers():
    now, calls = datetime.now(timezone.utc).isoformat(), []
    def expected(grants, deadline):
        return {"configured": True, "complete": True, "spec": None, "sources": {}, "declarations": [
            {"node_id": node, "hostname": node, "source": "configuration", "request_service_required": False} for node in ("a", "b", "missing")]}
    def inventory(spec, deadline):
        calls.append("inventory")
        return {"complete": True, "instances": {}, "serving": {"complete": True}}
    def diagnostics(roster, deadline):
        calls.append("diagnostics")
        family = {"status": "ok", "observed_at": now, "unit": "Percent", "percent": 0}
        return {"status": "verified", "results": [{"host": row["hostname"], "started": row["started"], "status": "success", "result": {
            "hostname": row["hostname"], "current": {"cpu": family, "memory": family,
            "filesystems": {"status": "ok", "observed_at": now, "mounts": [{**family, "path": "/", "total": 10, "free": 10,
                "inodes": {"status": "unsupported", "reason": "statvfs_unsupported"}}],
                "missing": [{"path": "/data", "status": "unavailable", "reason": "configured_mount_absent"}]}}}} for row in roster]}
    def recent(rows, namespace, mounts, deadline):
        calls.append("recent")
        metric = {"status": "ok", "observed_at": now, "last_sample_at": now, "value": 0, "period_seconds": 60, "unit": "Percent"}
        return {"nodes": {row["node_id"]: {"cpu": metric, "memory": metric, "filesystems": []} for row in rows}}
    return {"expected": expected, "inventory": inventory,
        "heartbeats": lambda deadline: [{"hostname": node, "runner_id": node, "started": now, "alive": True, "last_heartbeat": now} for node in ("a", "b")],
        "scheduler": lambda deadline: {"status": "ok", "scope": "installation", "scheduler_active": True, "observed_at": now},
        "identity_cache": SimpleNamespace(read=lambda *args, **kwargs: None, write=lambda *args, **kwargs: True), "diagnostics_batch": diagnostics, "recent": recent}, calls


@th.django_unit_test()
def test_coverage_traversal_and_named_mount_gap(opts):
    from mojo.apps.aws.services import fleet_health as fh
    readers, calls = _readers()
    actor = Actor("view_platform", "manage_aws", "view_jobs")
    first = fh.coverage(actor, readers=readers)
    assert first["coverage"]["expected_total"] == 3 and first["coverage"]["returned"] == 1, "returned node replaced the global expected denominator"
    assert calls.count("inventory") == 1 and calls.count("diagnostics") == 1, "coverage repeated inventory or per-node control acquisition"
    data = next(row for row in first["measurements"] if row["key"] == "filesystem:/data")
    assert data["sources"]["runtime"]["coverage_status"] == "missing", "declared missing data mount was hidden behind root evidence"
    assert first["agent_installation"]["status"] == "unknown", "recipe/runtime evidence was promoted into installation proof"
    second = fh.coverage(actor, cursor=first["coverage"]["next_cursor"], readers=readers)
    last = fh.coverage(actor, cursor=second["coverage"]["next_cursor"], readers=readers)
    assert last["node"]["node_id"] == "missing" and not last["coverage"]["has_more"], "missing expected host vanished from traversal"
    assert not last["coverage"]["complete"] and not last["measurement_coverage"]["complete"], "final partial page established complete fleet coverage"
    before = calls.count("diagnostics")
    with th.assert_raises(fh.FleetHealthError):
        fh.coverage(actor, node_id="unknown", readers=readers)
    assert calls.count("diagnostics") == before, "unknown identity started detailed collection"


@th.django_unit_test()
def test_runtime_failure_keeps_cloud_and_grants_prevent_source_calls(opts):
    from mojo.apps.aws.services import fleet_health as fh
    readers, calls = _readers()
    def broken(*args):
        raise RuntimeError("runtime unavailable")
    readers["diagnostics_batch"] = broken
    result = fh.coverage(Actor("view_platform", "manage_aws", "view_jobs"), node_id="a", readers=readers)
    cpu = next(row for row in result["measurements"] if row["key"] == "cpu")
    assert cpu["sources"]["cloudwatch"]["value"] == 0, "failed runtime erased a real independent cloud zero"
    assert cpu["sources"]["runtime"]["coverage_status"] != "covered", "failed runtime became current evidence"
    readers, calls = _readers()
    denied = fh.coverage(Actor("view_platform"), node_id="a", readers=readers)
    assert "inventory" in calls and "recent" in calls and "diagnostics" not in calls, "bounded platform health facts gained host diagnostics or lost dashboard-visible AWS observations"
    assert not denied["measurement_coverage"]["complete"], "source denials established complete coverage"


@th.django_unit_test()
def test_recent_metrics_batch_keeps_exact_dimensions_zero_and_null(opts):
    from mojo.helpers.aws.cloudwatch import CloudWatchHelper
    from mojo.apps.account.services.health_evidence import CycleBudget
    now, calls = datetime.now(timezone.utc), []
    class Provider:
        def list_metrics(self, **params):
            calls.append("list")
            dims = [{"Name": "InstanceId", "Value": "i-one"}, {"Name": "ImageId", "Value": "ami-x"}]
            if params["MetricName"] == "disk_used_percent":
                dims += [{"Name": "path", "Value": "/data"}, {"Name": "device", "Value": "disk1"}]
            return {"Metrics": [{"Namespace": params["Namespace"], "MetricName": params["MetricName"], "Dimensions": dims}]}
        def get_metric_data(self, **params):
            calls.append("data")
            return {"MetricDataResults": [{"Id": query["Id"], "StatusCode": "Complete", "Timestamps": [now - timedelta(seconds=1)], "Values": [0]} for query in params["MetricDataQueries"]]}
    deadline = time.monotonic() + 5
    budget = CycleBudget(deadline)
    result = CloudWatchHelper(access_key="fake", secret_key="fake").get_recent_evidence(
        [{"node_id": "one", "instance_id": "i-one"}], "mojo/custom", {"one": ["/data"]}, deadline=deadline, client=Provider(), now=now, budget=budget)
    assert calls == ["list", "list", "data"], "recent evidence used per-metric/provider N+1 acquisition"
    assert result["nodes"]["one"]["memory"]["value"] == 0, "recent batch dropped actual zero evidence"
    filesystem = next(row for row in result["nodes"]["one"]["filesystems"] if row["path"] == "/data")
    assert {row["Name"] for row in filesystem["dimensions"]} == {"InstanceId", "ImageId", "path", "device"}, "recent batch lost exact full dimensions"
    assert next(row for row in result["nodes"]["one"]["filesystems"] if row["path"] == "/")["value"] is None, "missing root series became a reassuring zero"
    assert budget.counts["provider_operations"] == 3, "whole-cycle provider accounting omitted a data/discovery call"


@th.django_unit_test()
def test_checked_custom_alias_and_competing_proofs_are_reconciled_before_binding(opts):
    from mojo.apps.aws.services import fleet_health as fh
    readers, calls = _readers()
    now = datetime.now(timezone.utc).isoformat()
    declarations = [{"node_id": "logical", "source": "edge_topology"}]
    readers["expected"] = lambda *args: {"configured": True, "complete": True, "spec": None, "sources": {}, "declarations": declarations}
    runners = [{"hostname": "host-a", "runner_id": "runner-a", "started": now, "alive": True, "last_heartbeat": now}]
    readers["heartbeats"] = lambda *args: runners
    def diagnostics(roster, end):
        family = {"status": "ok", "observed_at": now, "percent": 95}
        return {"status": "verified", "results": [{"host": row["hostname"], "started": row["started"], "status": "success", "result": {
            "hostname": row["hostname"], "proof": {"node_id": "logical", "observed_at": now}, "current": {"cpu": family, "memory": family}}} for row in roster]}
    readers["diagnostics_batch"] = diagnostics
    actor = Actor("view_platform", "manage_aws", "view_jobs")
    acquired = fh.acquire_snapshot(actor, deadline=time.monotonic()+5, readers=readers)
    assert acquired["rows"][0]["hostname"] == "host-a" and acquired["evidence"]["logical"]["current"]["cpu"]["percent"] == 95, "cold checked custom node alias required a prior manual drill-down"
    runners.append({"hostname": "host-b", "runner_id": "runner-b", "started": now, "alive": True, "last_heartbeat": now})
    acquired = fh.acquire_snapshot(actor, deadline=time.monotonic()+5, readers=readers)
    row = next(row for row in acquired["rows"] if row["expected"])
    assert row["identity_status"] == "ambiguous" and not acquired["roster_complete"], "competing checked aliases established a resolved complete roster"
    assert acquired["evidence"]["logical"]["current"]["reason"] == "identity_ambiguous", "ambiguous logical identity accepted one claimant's runtime facts"
    assert acquired["evidence"]["logical"]["trends"]["reason"] == "identity_ambiguous", "ambiguous identity accepted instance-specific provider facts"
    from mojo.apps.account.services import health_evidence as he
    components = he.build_components({"source_id": "a"*64, "services": [], "channels": [], "channels_complete": False}, {}, {}, acquired, {})
    fleet = next(row for row in components if row["kind"] == "fleet")
    assert not fleet["coverage"]["complete"] and all(slot["observed_at"] is None for slot in fleet["evidence"]), "snapshot assembly trusted samples bound to an ambiguous roster identity"


@th.django_unit_test()
def test_recent_series_partial_and_mount_caps_remain_required_coverage_gaps(opts):
    from mojo.helpers.aws.cloudwatch import CloudWatchHelper
    from mojo.apps.aws.services import fleet_health as fh
    now = datetime.now(timezone.utc)
    class Provider:
        def list_metrics(self, **params):
            return {"Metrics": [{"Namespace": params["Namespace"], "MetricName": params["MetricName"], "Dimensions": [{"Name": "InstanceId", "Value": "i-one"}]}]}
        def get_metric_data(self, **params):
            return {"MetricDataResults": [{"Id": query["Id"], "StatusCode": "PartialData", "Timestamps": [now-timedelta(seconds=1)], "Values": [0]} for query in params["MetricDataQueries"]]}
    cloud = CloudWatchHelper(access_key="fake", secret_key="fake").get_recent_evidence(
        [{"node_id": "one", "instance_id": "i-one"}], "CWAgent", {"one": ["/data%d" % index for index in range(10)]}, deadline=time.monotonic()+5, client=Provider(), now=now)
    filesystems = cloud["nodes"]["one"]["filesystems"]
    assert len(filesystems) == 11 and cloud["omitted_count"] == 3, "metric cap redefined the known filesystem obligation roster"
    assert sum(row.get("reason") == "not_collected_budget" for row in filesystems) == 3, "capped named mounts were mislabeled as absent provider series"
    slot = fh._coverage_slot(cloud["nodes"]["one"]["cpu"], source="cloudwatch", now=now, freshness=900)
    assert slot["truncated"] and slot["coverage_status"] == "incomplete" and slot["value"] == 0, "retained PartialData sample established complete series coverage"


@th.django_unit_test()
def test_default_snapshot_readers_charge_inventory_and_redis_to_shared_caps(opts):
    from mojo.apps.aws.services import fleet_health as fh
    from mojo.apps.account.services.health_evidence import CycleBudget
    from mojo.deploy.provision.spec import Spec
    deadline, calls = time.monotonic()+5, []
    budget = CycleBudget(deadline, limits={"provider_operations": 1, "inventory_pages": 1, "redis_commands": 2})
    class Provider:
        def describe_instances(self, **params):
            calls.append("inventory")
            return {"Reservations": [], "NextToken": "second-page"}
    class Redis:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, key):
            calls.append("redis")
            return None
    readers = fh._snapshot_readers(deadline, budget, {}, connect_factory=lambda **kwargs: Redis(), provider_factory=lambda *args: Provider())
    result = readers["inventory"](Spec("testit-budget", "production", "us-east-1", balancer=False), deadline)
    assert calls.count("inventory") == 1 and not result["complete"], "default inventory bypassed the whole-cycle provider/page cap"
    readers["identity_cache"].read({"platform": True, "aws": True, "jobs": True}, deadline)
    readers["scheduler"](deadline)
    with th.assert_raises(TimeoutError):
        readers["scheduler"](deadline)
    assert budget.counts["redis_commands"] == 2 and calls.count("redis") == 2, "default cache/scheduler reads bypassed cumulative Redis accounting"
    with th.assert_raises(TimeoutError):
        budget.take("provider_operations")
    assert budget.counts["provider_operations"] == 1, "a later metric/managed binding operation received a reset provider allowance"


@th.django_unit_test()
def test_proved_single_node_coverage_targets_only_its_runtime_host(opts):
    from mojo.apps.aws.services import fleet_health as fh
    readers, calls = _readers()
    original = readers["diagnostics_batch"]
    targeted = []
    def diagnostics(roster, end):
        targeted.extend(row["hostname"] for row in roster)
        return original(roster, end)
    readers["diagnostics_batch"] = diagnostics
    acquired = fh.acquire_snapshot(Actor("view_platform", "manage_aws", "view_jobs"), deadline=time.monotonic()+5,
        readers=readers, node_ids=["a"])
    assert targeted == ["a"] and set(acquired["evidence"]) == {"a"}, "a proved one-node request broadcast checked collection to unrelated fleet runners"


@th.django_unit_test()
def test_default_bounded_topology_reader_decodes_owned_text_setting(opts):
    from mojo.apps.aws.services import fleet_health as fh
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services.health_evidence import CycleBudget
    import json
    key = "TESTIT_HEALTH_TOPOLOGY_JSON"
    Setting.objects.filter(key=key).delete()
    try:
        Setting.objects.create(key=key, value=json.dumps({"nodes": ["testit-offline"], "pools": ["web"]}))
        deadline = time.monotonic()+3
        readers = fh._snapshot_readers(deadline, CycleBudget(deadline), {}, topology_key=key)
        expected = readers["expected"]({"platform": True, "aws": False, "jobs": False}, deadline)
        assert expected["sources"]["edge_topology"]["status"] == "ok" and any(row["node_id"] == "testit-offline" for row in expected["declarations"]), "bounded TextField topology read dropped declared offline obligations instead of JSON-decoding them"
    finally:
        Setting.objects.filter(key=key).delete()


@th.django_unit_test()
def test_late_valid_checked_proof_uses_phase_time_instead_of_producer_start(opts):
    from mojo.apps.aws.services import fleet_health as fh
    readers, calls = _readers()
    started = datetime.now(timezone.utc)
    phase = [started]
    observed = (started+timedelta(seconds=40)).isoformat()
    readers["expected"] = lambda *args: {"configured": True, "complete": True, "spec": None, "sources": {}, "declarations": [{"node_id": "logical", "source": "edge_topology"}]}
    readers["heartbeats"] = lambda end: [{"hostname": "host-late", "runner_id": "runner-late", "started": observed, "last_heartbeat": observed, "alive": True}]
    def checked(roster, end):
        phase[0] = started+timedelta(seconds=40)
        return {"status": "verified", "results": [{"host": "host-late", "started": observed, "status": "success", "result": {
            "hostname": "host-late", "proof": {"node_id": "logical", "observed_at": observed},
            "current": {"cpu": {"status": "ok", "observed_at": observed, "percent": 1}}}}]}
    readers["diagnostics_batch"] = checked
    acquired = fh.acquire_snapshot(Actor("view_platform", "manage_aws", "view_jobs"), deadline=time.monotonic()+5,
        readers=readers, wall_clock=lambda: phase[0])
    assert acquired["rows"][0]["hostname"] == "host-late" and acquired["evidence"]["logical"]["current"]["cpu"]["percent"] == 1, "valid later phase proof was rejected as future relative to collector start"


@th.django_unit_test()
def test_host_measurements_do_not_require_optional_cloudwatch_samples(opts):
    from mojo.apps.aws.services import fleet_health as fh
    readers, _ = _readers()
    readers["recent"] = lambda *args: {"nodes": {}}
    acquired = fh.acquire_snapshot(Actor("view_platform", "view_jobs"), deadline=time.monotonic()+5, readers=readers)
    projected = fh.project_monitoring_coverage(acquired, "a")
    cpu = next(row for row in projected["measurements"] if row["key"] == "cpu")
    assert cpu["sources"]["runtime"]["required"] and not cpu["sources"]["cloudwatch"]["required"], "duplicate CloudWatch host stream remained a required source"
    assert cpu["sources"]["runtime"]["coverage_status"] == "covered" and cpu["sources"]["cloudwatch"]["last_real_sample_at"] is None, "optional absence fabricated a sample or hid authoritative runtime coverage"
    acquired["evidence"]["a"]["trends"] = {"cpu": {"status": "unknown", "reason": "no_datapoints", "required": True}}
    required = fh.project_monitoring_coverage(acquired, "a")
    required_cpu = next(row for row in required["measurements"] if row["key"] == "cpu")
    assert required_cpu["sources"]["cloudwatch"]["required"] and required["measurement_coverage"]["gap_total"] == projected["measurement_coverage"]["gap_total"] + 1, "explicit continuous-source obligation was silently relaxed"
