"""What the cloud tools are allowed to put in front of the model.

`_dumps_tool_result` serializes whatever a handler returns, so every bound is
this module's own. The contract has two halves and both are asserted here:
nothing sensitive or unbounded SURVIVES, and the facts an operator actually
needs are not thrown away by the bounding — a flat depth cap would empty the
envelope tools, which is why the budget, not the depth, is what bounds them.

No AWS, no settings, no patching: the capacity envelope is built from injected
mock clients (which also means `report()` does NOT write the shared cache), and
the remaining envelopes are real-shaped fixtures.
"""

import json
from unittest import mock

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


SOURCE = "assistant-cloud-test-projection"
SHA = "c" * 40
SECRET = "AKIAIOSFODNN7EXAMPLE super-secret-value"
NODE_A = "i-0a1b2c3d4e5f60011"
GROUP_ARN = ("arn:aws:elasticloadbalancing:us-east-1:123456789012:"
             "targetgroup/mojo-api/abcdef0123456789")
BALANCER_ARN = ("arn:aws:elasticloadbalancing:us-east-1:123456789012:"
                "loadbalancer/net/mojo-api-nlb/0123456789abcdef")
ALLOCATION_ID = "eipalloc-0123456789abcdef0"


# --- fixtures -------------------------------------------------------------

def _elbv2_client():
    client = mock.Mock()
    client.describe_load_balancers.return_value = {"LoadBalancers": [{
        "LoadBalancerArn": BALANCER_ARN, "LoadBalancerName": "mojo-api-nlb",
        "Type": "network", "Scheme": "internet-facing",
        "State": {"Code": "active"}, "DNSName": "nlb.example.com",
        "AvailabilityZones": [{"ZoneName": "us-east-1a",
                               "LoadBalancerAddresses": []}]}]}
    client.describe_target_groups.return_value = {"TargetGroups": [{
        "TargetGroupArn": GROUP_ARN, "TargetGroupName": "mojo-api",
        "TargetType": "instance", "Protocol": "TCP", "Port": 443,
        "LoadBalancerArns": [BALANCER_ARN]}]}
    client.describe_target_health.side_effect = lambda TargetGroupArn: {
        "TargetHealthDescriptions": [
            {"Target": {"Id": NODE_A, "Port": 443},
             "TargetHealth": {"State": "healthy"}}]}
    client.describe_target_group_attributes.return_value = {
        "Attributes": [{"Key": "deregistration_delay.timeout_seconds",
                        "Value": "30"}]}
    return client


def _ec2_client():
    client = mock.Mock()
    client.describe_instances.return_value = {"Reservations": [{"Instances": [{
        "InstanceId": NODE_A, "InstanceType": "m6i.large",
        "ImageId": "ami-0source", "SubnetId": "subnet-0aaa", "VpcId": "vpc-0aaa",
        "State": {"Name": "running"},
        "Placement": {"AvailabilityZone": "us-east-1a"},
        "PrivateIpAddress": "10.0.1.11",
        "PrivateDnsName": "ip-10-0-1-11.ec2.internal",
        "SecurityGroups": [{"GroupId": "sg-0aaa"}],
        "Tags": [{"Key": "Name", "Value": "mojo-api-a"}],
    }]}]}
    # The allocation ids the projector must never surface.
    client.describe_addresses.return_value = {"Addresses": [{
        "AllocationId": ALLOCATION_ID, "PublicIp": "198.51.100.7",
        "InstanceId": NODE_A, "Domain": "vpc",
        "Tags": [{"Key": "mojo:eip", "Value": "stable-egress"}]}]}
    return client


def _rds_client():
    client = mock.Mock()
    client.describe_db_clusters.return_value = {"DBClusters": []}
    client.describe_db_instances.return_value = {"DBInstances": []}
    return client


def _cache_client():
    client = mock.Mock()
    client.describe_replication_groups.return_value = {"ReplicationGroups": []}
    return client


def _leaky_evidence():
    """Evidence entries shaped exactly as platform_deploy records them."""
    return [{
        "runner": "mojo-api-a-engine", "state": "failed",
        "at": "2026-08-21T00:00:00+00:00",
        "detail": {"reason": "update_failed", "stderr_tail": SECRET},
    }]


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_cloud_projections(opts):
    from mojo.apps.edge.models import PlatformDeployment

    # Delete ONLY what this module creates: tests/test_edge owns this table
    # too, and a blanket delete would take its rows with it.
    PlatformDeployment.objects.filter(source=SOURCE).delete()


def _deployment_row():
    from mojo.apps.edge.models import PlatformDeployment

    PlatformDeployment.objects.filter(source=SOURCE).delete()
    row = PlatformDeployment.objects.create(
        sha=SHA, source=SOURCE, actor="test",
        status=PlatformDeployment.STATUS_FAILED,
        framework_version="1.15.15",
        frozen_roster=["mojo-api-a-engine", "mojo-api-b-engine"],
        node_evidence=_leaky_evidence(),
        transitions=[{"status": "failed", "detail": {"stderr_tail": SECRET}}],
        diagnosis=[{"runner": "mojo-api-a-engine",
                    "detail": {"stderr_tail": SECRET}}],
    )
    return row


# ---------------------------------------------------------------------------
# Deployment projection
# ---------------------------------------------------------------------------

@th.django_unit_test("the deployment projection drops every evidence journal, "
                     "keeps the summary, and leaks no stderr tail")
def test_deployment_projection_is_evidence_free(opts):
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import platform_deploy
    from mojo.apps.assistant.services.tools.cloud.reads import project_deployment

    row = _deployment_row()
    try:
        # include_stderr=True is the WORST case: the tail is present in the
        # serialized row, so only this projector stands between it and the model.
        serialized = platform_deploy.serialize(row, include_stderr=True)
        assert_true("stderr_tail" in json.dumps(serialized),
                    "the fixture is not exercising anything — the serialized "
                    "row carries no stderr_tail to drop")
        projected = project_deployment(serialized)
        text = json.dumps(projected)
        assert_true(SECRET not in text,
                    "a deploy stderr tail reached the model's projection")
        for key in ("stderr_tail", "node_evidence", "transitions", "diagnosis",
                    "frozen_roster"):
            assert_true(key not in text,
                        f"'{key}' survived the deployment projection: {text[:300]}")
        assert_eq(projected["node_summary"]["expected"], 2,
                  f"node_summary must survive — it is the bounded answer the "
                  f"dropped journals were the unbounded version of: {projected}")
        assert_eq(projected["sha"], SHA, "the projection lost the commit")
        assert_eq(projected["status"], "failed", "the projection lost the status")
    finally:
        PlatformDeployment.objects.filter(source=SOURCE).delete()


# ---------------------------------------------------------------------------
# bounded()
# ---------------------------------------------------------------------------

@th.django_unit_test("bounded() masks sensitive keys, drops the evidence names, "
                     "and caps strings")
def test_bounded_redacts_and_caps(opts):
    from mojo.apps.assistant.services.tools.cloud.common import MAX_STRING, bounded

    result = bounded({
        "password": "hunter2", "api_secret": "shhh", "auth_token": "abc",
        "stderr_tail": SECRET,
        "node_evidence": _leaky_evidence(),
        "long": "x" * 900,
        "keep": "visible",
    }, depth=3)
    text = json.dumps(result)
    assert_true("hunter2" not in text and "shhh" not in text,
                f"a sensitive value survived bounding: {text[:300]}")
    assert_eq(result["password"], "*****",
              f"a password-bearing key must be MASKED, not silently dropped: "
              f"{result.get('password')!r}")
    assert_true("stderr_tail" not in result and "node_evidence" not in result,
                f"an evidence key survived by name: {sorted(result)}")
    assert_eq(len(result["long"]), MAX_STRING,
              f"a long string was capped to {len(result['long'])}, expected "
              f"{MAX_STRING} including the marker")
    assert_eq(result["keep"], "visible", "bounding destroyed an ordinary value")


@th.django_unit_test("bounded() never nests past the depth it was given")
def test_bounded_depth(opts):
    from mojo.apps.assistant.services.tools.cloud.common import bounded

    deep = {"a": {"b": {"c": {"d": {"e": "leaf"}}}}}

    def depth_of(value):
        if isinstance(value, dict):
            return 1 + max([depth_of(item) for item in value.values()] or [0])
        if isinstance(value, list):
            return 1 + max([depth_of(item) for item in value] or [0])
        return 0

    shallow = bounded(deep, depth=2)
    assert_true(depth_of(shallow) <= 2,
                f"depth=2 produced a value {depth_of(shallow)} levels deep: {shallow}")
    deeper = bounded(deep, depth=5)
    assert_eq(deeper["a"]["b"]["c"]["d"]["e"], "leaf",
              f"depth=5 must keep a leaf five levels down: {deeper}")


@th.django_unit_test("a per-tool depth keeps the envelope tools' real rows")
def test_bounded_keeps_deep_inventory_rows(opts):
    from mojo.apps.assistant.services.tools.cloud.common import bounded

    # The Advanced inventory's shape: sections -> envelope -> data ->
    # resources -> ec2[] -> row. A flat depth-2 cap would leave status strings
    # and nothing an operator can act on.
    envelope = {"status": "ok", "data": {"resources": {"ec2": [
        {"id": NODE_A, "state": "running", "type": "m6i.large"}]}}}
    projected = bounded(envelope, depth=5)
    assert_eq(projected["data"]["resources"]["ec2"][0]["id"], NODE_A,
              f"a real EC2 instance id did not survive the inventory "
              f"projection: {projected}")
    dashboard = {"load_balancer": {"status": "healthy",
                                   "data": {"registered": 2, "healthy": 2}}}
    kept = bounded(dashboard, depth=3)
    assert_eq(kept["load_balancer"]["status"], "healthy",
              f"a per-source status did not survive: {kept}")


@th.django_unit_test("bounded() caps container width and total size")
def test_bounded_budget(opts):
    from mojo.apps.assistant.services.tools.cloud.common import (
        MAX_BYTES, MAX_ITEMS, bounded,
    )

    wide = bounded({"rows": [{"n": index} for index in range(500)]}, depth=3)
    kept = [row for row in wide["rows"] if row.get("truncated") is not True]
    assert_true(len(kept) <= MAX_ITEMS,
                f"a 500-row list kept {len(kept)} rows, over the {MAX_ITEMS} cap")
    assert_true(len(json.dumps(bounded(
        {"rows": ["y" * 190 for _ in range(500)]}, depth=3))) < MAX_BYTES * 2,
        "the byte budget did not bound a wide payload")


# ---------------------------------------------------------------------------
# Capacity and metric projections
# ---------------------------------------------------------------------------

@th.django_unit_test("the capacity projection keeps the fleet and withholds "
                     "the Elastic IP allocation ids")
def test_capacity_projection(opts):
    from mojo.apps.aws.services import capacity
    from mojo.apps.assistant.services.tools.cloud.reads import project_capacity

    # Injected clients: report() does NOT write the shared report cache on this
    # path, which is what makes calling it safe in the parallel tier.
    envelope = capacity.report(
        elbv2_client=_elbv2_client(), ec2_client=_ec2_client(),
        rds_client=_rds_client(), cache_client=_cache_client())
    projected = project_capacity(envelope)
    text = json.dumps(projected)

    assert_true(ALLOCATION_ID not in text,
                f"an Elastic IP allocation id reached the model: {text[:400]}")
    assert_true("assign" not in text,
                "the raw assign map is an input to a mutation this domain does "
                "not offer and must not be projected")
    node_ids = [row["id"] for row in projected["nodes"]]
    assert_true(NODE_A in node_ids,
                f"the fleet's node did not survive the projection: {node_ids}")
    assert_eq(projected["nodes"][0]["instance_type"], "m6i.large",
              f"the node's instance type was lost: {projected['nodes'][0]}")
    assert_true(isinstance(projected["egress"]["attached_count"], int),
                f"egress must be counts and booleans only: {projected['egress']}")
    assert_true("actions" in projected and isinstance(projected["actions"], dict),
                "the server's offered/blocked_reason map must be projected — it "
                "is what stops the model proposing a refused action")
    assert_true(all(isinstance(code, str) for code in projected["warnings"]),
                f"warnings must project to codes only: {projected['warnings']}")


@th.django_unit_test("the tools' documented caps survive bounded(), not just "
                     "their own projectors")
def test_documented_caps_are_not_re_truncated(opts):
    from mojo.apps.assistant.services.tools.cloud import reads
    from mojo.apps.assistant.services.tools.cloud.common import bounded

    # 1. fetch_cloud_metrics documents 60 buckets across up to 10 slugs. The
    #    default 40-item / 400-node envelope would silently cut both.
    series = reads.project_series(
        {f"web-{index}": [float(n) for n in range(500)] for index in range(12)},
        [f"t{n}" for n in range(500)])
    metrics = bounded(series, depth=4, max_items=reads.MAX_METRIC_BUCKETS,
                      max_nodes=reads.METRIC_NODE_BUDGET)
    assert_eq(len(metrics["series"]), reads.MAX_METRIC_SLUGS,
              f"the projector dropped series below the documented "
              f"{reads.MAX_METRIC_SLUGS}: {len(metrics['series'])}")
    for slug, row in metrics["series"].items():
        assert_eq(len(row["values"]), reads.MAX_METRIC_BUCKETS,
                  f"series {slug} came back with {len(row['values'])} buckets, "
                  f"not the documented {reads.MAX_METRIC_BUCKETS}")
    assert_eq(len(metrics["labels"]), reads.MAX_METRIC_BUCKETS,
              f"labels were re-truncated to {len(metrics['labels'])}")

    # 2. list_cloud_resources documents 100 rows per service.
    rows = [{"id": f"i-{index:017d}", "slug": f"web-{index}",
             "name": f"web-{index}", "type": "m6i.large", "state": "running"}
            for index in range(reads.MAX_RESOURCE_ROWS)]
    listing = bounded(
        {"ec2": list(rows), "rds": list(rows), "redis": list(rows),
         "degraded": {}, "available": True, "reason": None},
        depth=4, max_items=reads.MAX_RESOURCE_ROWS,
        max_nodes=reads.RESOURCE_NODE_BUDGET)
    for service in ("ec2", "rds", "redis"):
        kept = [row for row in listing[service]
                if row.get("truncated") is not True]
        assert_eq(len(kept), reads.MAX_RESOURCE_ROWS,
                  f"{service} came back with {len(kept)} rows, not the "
                  f"documented {reads.MAX_RESOURCE_ROWS}")

    # 3. the named deployment projection must survive inside a platform
    #    section — node_summary sits two levels deeper than the others.
    section = {"status": "healthy", "data": {"items": [{
        "id": "abc", "sha": SHA, "status": "failed",
        "node_summary": {"expected": 3, "proven": 2, "failed": 1},
        "current_commits": [SHA]}]}}
    projected = bounded(section, depth=reads.DEPLOYMENTS_DEPTH)
    item = projected["data"]["items"][0]
    assert_eq(item["node_summary"]["expected"], 3,
              f"node_summary collapsed inside the section walk: {item}")
    assert_eq(item["current_commits"], [SHA],
              f"current_commits collapsed inside the section walk: {item}")


@th.django_unit_test("a long metric series is capped to the most recent buckets")
def test_series_projection(opts):
    from mojo.apps.assistant.services.tools.cloud.reads import (
        MAX_METRIC_BUCKETS, project_series,
    )

    labels = [f"t{index}" for index in range(500)]
    data = {"web-1": [float(index) for index in range(500)]}
    projected = project_series(data, labels)
    series = projected["series"]["web-1"]
    assert_eq(len(series["values"]), MAX_METRIC_BUCKETS,
              f"a 500-point series kept {len(series['values'])} points")
    assert_eq(len(projected["labels"]), MAX_METRIC_BUCKETS,
              f"labels were not capped with the series: {len(projected['labels'])}")
    assert_eq(series["truncated"], True,
              "a truncated series must say so rather than look complete")
    assert_eq(series["values"][-1], 499.0,
              f"the MOST RECENT buckets must be the ones kept: {series['values'][-3:]}")
    assert_eq(series["max"], 499.0, f"the series max is wrong: {series['max']}")
    assert_eq(series["min"], 440.0,
              f"min/max/avg must describe the kept window: {series['min']}")
