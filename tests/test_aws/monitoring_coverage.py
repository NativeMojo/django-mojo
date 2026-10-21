"""One-node coverage shares raw source reads and never loses expected gaps."""
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import time
from testit import helpers as th


class Actor:
    is_active = True
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
        return {"status": "verified", "results": [{"host": row["hostname"], "status": "success", "result": {
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
        "identity_cache": SimpleNamespace(read=lambda *args, **kwargs: None), "diagnostics_batch": diagnostics, "recent": recent}, calls


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
    assert "inventory" not in calls and "diagnostics" not in calls and "recent" not in calls, "source denial made privileged control/provider calls"
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
