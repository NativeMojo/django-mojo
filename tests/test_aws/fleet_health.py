"""Fleet contract fixtures: expected denominator, grants, identity and continuation."""
from datetime import datetime, timezone
from types import SimpleNamespace
from testit import helpers as th


class Actor:
    is_active = True
    is_request_user = True
    def __init__(self, *permissions):
        self.permissions = set(permissions)
    def has_permission(self, permissions):
        return bool(set(permissions) & self.permissions)


class Cache:
    def __init__(self):
        self.data = None
    def write(self, grants, revision, rows, deadline):
        import copy
        self.data = {"revision": revision, "rows": copy.deepcopy(rows)}
        return True
    def read(self, grants, deadline):
        return self.data


def _readers(nodes=("node-a", "node-b", "node-c")):
    calls = []
    cache = Cache()
    def expected(grants, deadline):
        declarations = ([{"node_id": node, "hostname": node, "source": "configuration"}
                         for node in nodes] if grants["platform"] else [])
        return {"declarations": declarations, "sources": {}, "complete": True,
                "configured": bool(declarations), "spec": None}
    def inventory(spec, deadline):
        calls.append("aws")
        return {"instances": {}, "complete": True, "serving": {"complete": True}}
    def heartbeats(deadline):
        calls.append("heartbeats")
        return [{"hostname": node, "runner_id": f"arbitrary:{node}", "alive": True,
                 "started": "2026-10-20T01:00:00+00:00", "last_heartbeat": datetime.now(timezone.utc).isoformat()}
                for node in nodes[:2]]
    def diagnostics(*args):
        calls.append("diagnostics")
        return {"status": "unknown", "anomalies": ["incompatible_host"], "results": []}
    def trends(*args):
        calls.append("trends")
        return {"status": "unavailable", "reason": "no_series"}
    return {"expected": expected, "inventory": inventory, "heartbeats": heartbeats,
            "scheduler": lambda deadline: {"status": "ok", "scope": "installation", "scheduler_active": True},
            "diagnostics": diagnostics, "trends": trends, "identity_cache": cache}, calls


@th.django_unit_test()
def test_missing_expected_node_and_sorted_pagination(opts):
    from mojo.apps.aws.services.fleet_health import report, FleetHealthError
    readers, calls = _readers()
    actor = Actor("view_platform", "manage_aws", "view_jobs")
    first = report(actor, limit=2, readers=readers)
    assert first["coverage"]["expected_total"] == 3, "two responders must never replace the three-node denominator"
    assert first["coverage"]["missing_total"] == 1, "the absent expected node must remain missing"
    assert first["health"] != "healthy" and not first["coverage"]["complete"], "a responding subset cannot establish healthy complete coverage"
    assert "diagnostics" not in calls and "trends" not in calls, "fleet read must have no per-node control or metrics fan-out"
    second = report(actor, limit=2, cursor=first["coverage"]["next_cursor"], readers=readers)
    assert [row["node_id"] for row in second["nodes"]] == ["node-c"], "continuation must retain the expected-but-missing row"
    assert second["nodes"][0]["lifecycle"] == "missing", "absence is not intentional retirement"
    changed, _ = _readers(("node-a", "node-b", "node-d"))
    try:
        report(actor, cursor=first["coverage"]["next_cursor"], readers=changed)
    except FleetHealthError as exc:
        assert exc.error_code == "roster_changed", "changed expectation must invalidate continuation"
    else:
        raise AssertionError("changed roster cannot reuse a continuation")


@th.django_unit_test()
def test_source_permissions_and_direct_service_refusals(opts):
    from mojo.apps.aws.services.fleet_health import report, node, FleetHealthError
    readers, calls = _readers()
    platform = report(Actor("view_platform"), readers=readers)
    assert "aws" not in calls, "platform permission must not issue AWS inventory calls"
    assert platform["sources"]["inventory"]["status"] == "permission_denied", "denied cloud source must remain explicit"
    result = node(Actor("view_platform"), "node-a", readers=readers)
    assert "diagnostics" not in calls and "trends" not in calls, "platform summary grant must not gain host control or cloud metrics"
    assert result["node"]["current"]["status"] == "permission_denied", "host read denial must survive node projection"
    jobs_readers, jobs_calls = _readers()
    jobs_only = report(Actor("view_jobs"), readers=jobs_readers)
    assert not jobs_calls, "jobs-only fleet read must not inspect cloud or platform heartbeat sources"
    assert jobs_only["coverage"]["expected_total"] is None, "denied/unconfigured expectation is not an empty healthy fleet"
    empty, empty_calls = _readers()
    try:
        report(Actor("group_only"), readers=empty)
    except FleetHealthError as exc:
        assert exc.error_code == "permission_denied", "group-member-only grant must not authorize global sources"
    else:
        raise AssertionError("direct Python callers must pass global source authorization")
    assert not empty_calls, "a global refusal must trigger no source collection"


@th.django_unit_test()
def test_identity_join_and_role_scope(opts):
    from mojo.apps.aws.services.fleet_health import reconcile, _services
    declarations = [{"node_id": "worker", "hostname": "worker", "source": "provision",
                     "managed_name": "worker", "role": "queue", "request_service_required": False}]
    instances = {identifier: {"instance_id": identifier, "name": "worker", "state": "running"}
                 for identifier in ("i-one", "i-two")}
    rows = reconcile(declarations, {"instances": instances})
    worker = next(row for row in rows if row["node_id"] == "worker")
    assert worker["identity_status"] == "ambiguous", "duplicate scoped names must not merge instances"
    assert len(rows) == 3, "both ambiguous observations and expected identity must stay visible"
    services = _services(worker, {"scheduler": {"status": "ok", "scope": "installation"}})
    assert services["request_service"]["status"] == "not_applicable", "proved worker role must not require API service"
    assert services["scheduler"]["status"] == "unknown", "installation scheduler lock cannot prove scheduler on each node"
    now = datetime.now(timezone.utc).isoformat()
    runners = [{"hostname": "worker", "runner_id": value, "alive": True, "started": now,
                "last_heartbeat": now} for value in ("custom-runner-a", "custom-runner-b")]
    rows = reconcile(declarations, runners=runners)
    assert len(rows) == 1 and rows[0]["runner_id"] == "custom-runner-a", "multiple engines on one hostname are one node without suffix guessing"


@th.django_unit_test()
def test_unknown_node_refuses_before_remote_sources(opts):
    from mojo.apps.aws.services.fleet_health import node, FleetHealthError
    readers, calls = _readers()
    try:
        node(Actor("view_platform", "manage_aws", "view_jobs"), "nonexistent", readers=readers)
    except FleetHealthError as exc:
        assert exc.error_code == "unknown_resource", "unknown id must have the documented refusal"
    else:
        raise AssertionError("unknown id cannot dispatch collectors")
    assert not calls, "unknown id must refuse before cloud inventory, metrics and runner collection"


@th.django_unit_test()
def test_shared_identity_cache_expiry_and_grant_partition(opts):
    import time
    from mojo.apps.aws.services.fleet_health import IdentityRoster, reconcile, _revision
    data, clock, options = {}, [100], []
    class Client:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def set(self, key, value, ex):
            data[key] = value
            return True
        def get(self, key):
            return data.get(key)
    def connect(**kwargs):
        options.append(kwargs)
        return Client()
    writer = IdentityRoster(connect, clock=lambda: clock[0], prefix="testit")
    reader = IdentityRoster(connect, clock=lambda: clock[0], prefix="testit")
    grants = {"platform": True, "aws": True, "jobs": False}
    rows = reconcile([{"node_id": "node-a", "hostname": "node-a", "source": "configuration"}])
    assert writer.write(grants, _revision(rows), rows, time.monotonic() + 1), "bounded identity write must succeed"
    assert reader.read(grants, time.monotonic() + 1)["rows"][0]["node_id"] == "node-a", "another service instance/worker must resolve the same shared identity"
    denied_scope = dict(grants, aws=False)
    assert reader.read(denied_scope, time.monotonic() + 1) is None, "a cached source scope must not grant another scope identity access"
    assert all(item["read_from_replicas"] is False and item["deadline"] > 0 for item in options), "identity cache operations must share the primary deadline budget"
    clock[0] = 221
    assert reader.read(grants, time.monotonic() + 1) is None, "expired identity evidence must require a fresh fleet read"


@th.django_unit_test()
def test_checked_custom_alias_and_duplicate_proof(opts):
    from mojo.apps.aws.services.fleet_health import reconcile
    now = datetime.now(timezone.utc).isoformat()
    declarations = [{"node_id": "logical-edge", "source": "edge_topology"},
                    {"node_id": "physical-host", "hostname": "physical-host", "source": "configuration"}]
    runners = [{"hostname": host, "runner_id": host + ":custom", "started": now,
                "alive": True, "last_heartbeat": now} for host in ("physical-host", "other-host")]
    proof = {"host": "physical-host", "started": now,
             "proof": {"node_id": "logical-edge", "observed_at": now}}
    rows = reconcile(declarations, runners=runners[:1], proofs=[proof])
    assert len(rows) == 1 and rows[0]["node_id"] == "logical-edge", "fresh checked custom id must resolve the hostname declaration as one node"
    assert rows[0]["hostname"] == "physical-host" and rows[0]["aliases"] == ["physical-host"], "checked aliases must remain explicit"
    assert rows[0]["membership_sources"] == ["configuration", "edge_topology"], "reconciliation must preserve merged expectation provenance"
    duplicate = dict(proof, host="other-host")
    rows = reconcile(declarations, runners=runners, proofs=[proof, duplicate])
    logical = next(row for row in rows if row["node_id"] == "logical-edge")
    assert logical["identity_status"] == "ambiguous", "two hosts claiming one EDGE_NODE_ID cannot form a usable mapping"
    replaced = dict(proof, started="2020-01-01T00:00:00+00:00")
    rows = reconcile(declarations, runners=runners[:1], proofs=[replaced])
    logical = next(row for row in rows if row["node_id"] == "logical-edge")
    assert logical["hostname"] is None, "a predecessor incarnation cannot resolve a current custom alias"


@th.django_unit_test()
def test_inventory_pages_overflow_and_partial_facts(opts):
    import time
    from mojo.helpers.aws.ec2 import fleet_inventory_bounded
    class Client:
        def __init__(self, fail=False):
            self.calls = 0
            self.fail = fail
        def describe_instances(self, **params):
            self.calls += 1
            if self.fail and self.calls == 2:
                raise TimeoutError("provider unavailable")
            return {"Reservations": [{"Instances": [{"InstanceId": "i-" + str(self.calls),
                "State": {"Name": "running"}, "Tags": [
                    {"Key": "mojo:project", "Value": "sample"},
                    {"Key": "mojo:env", "Value": "prod"}, {"Key": "mojo:role", "Value": "node"}]}]}],
                    "NextToken": str(self.calls)}
    client = Client()
    result = fleet_inventory_bounded("sample", "prod", client=client, deadline=time.monotonic() + 5)
    assert client.calls == 4 and len(result["instances"]) == 4, "inventory must stop at four pages while retaining every bounded fact"
    assert not result["complete"] and result["has_more"], "provider continuation cannot imply complete coverage"
    assert "inventory_page_limit" in result["reasons"], "bounded provider continuation must be disclosed"
    partial = fleet_inventory_bounded("sample", "prod", client=Client(True), deadline=time.monotonic() + 5)
    assert len(partial["instances"]) == 1 and not partial["complete"], "a later provider failure must preserve valid independent facts"
    overflow = fleet_inventory_bounded("sample", "prod", client=Client(), limit=1, deadline=time.monotonic() + 5)
    assert "inventory_overflow" in overflow["reasons"], "row overflow must be explicit"


@th.django_unit_test()
def test_cache_rejects_malformed_identity_and_ambiguous_node(opts):
    import json
    import time
    from mojo.apps.aws.services.fleet_health import IdentityRoster, node, FleetHealthError
    class Client:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, key):
            return json.dumps({"revision": "a" * 64, "expires_at": time.time() + 120,
                               "rows": [{"node_id": "malformed"}]})
    cache = IdentityRoster(lambda **kwargs: Client(), prefix="testit")
    assert cache.read({"platform": True, "aws": True, "jobs": True}, time.monotonic() + 1) is None, "malformed shared-cache rows cannot be used as identity authority"
    readers, calls = _readers()
    readers["identity_cache"].data = {"rows": [{"node_id": "node-a", "identity_status": "ambiguous"}]}
    try:
        node(Actor("view_platform", "manage_aws", "view_jobs"), "node-a", readers=readers)
    except FleetHealthError as exc:
        assert exc.error_code == "identity_ambiguous", "known ambiguous id must refuse explicitly"
    else:
        raise AssertionError("ambiguous cached mapping cannot dispatch diagnostics")
    assert not calls, "ambiguous identity must refuse before provider and runner collection"


@th.django_unit_test()
def test_configured_heartbeat_cadence_and_edge_only_identity(opts):
    from datetime import timedelta
    from mojo.apps.aws.services.fleet_health import reconcile
    now = datetime.now(timezone.utc)
    runners = [{"hostname": "edge-one", "runner_id": "custom-runner", "started": now.isoformat(),
                "last_heartbeat": (now - timedelta(seconds=45)).isoformat(), "alive": True}]
    rows = reconcile([{"node_id": "edge-one", "source": "edge_topology"}], runners=runners,
                     now=now, heartbeat_ttl=60)
    assert len(rows) == 1 and rows[0]["runner_id"] == "custom-runner", "a valid slower configured heartbeat must not split into a missing expected row plus observed host"
    assert rows[0]["hostname"] == "edge-one", "exact stable id/hostname match must allow checked drill-down"
    assert rows[0]["identity_status"] == "unresolved", "heartbeat alone is provisional identity evidence"


@th.django_unit_test()
def test_aws_only_cannot_claim_full_expected_denominator(opts):
    import time
    from mojo.apps.aws.services.fleet_health import _read_expected, _envelope
    from mojo.deploy.provision.spec import Spec
    def forbidden(*args):
        raise AssertionError("AWS-only reader must not inspect configuration/edge sources")
    expected = _read_expected({"platform": False, "aws": True, "jobs": False}, time.monotonic() + 1,
                              get_static=forbidden, topology_reader=forbidden,
                              spec_reader=lambda: Spec("sample", "prod", "us-east-1"))
    assert expected["declarations"], "AWS-owned provision identities must remain available under manage_aws"
    assert not expected["complete"], "denied independent declaration sources cannot establish the full expected denominator"
    output = _envelope(expected, [], {}, False)
    assert output["coverage"]["expected_total"] is None, "AWS-only denominator must remain explicitly unknown"


@th.django_unit_test()
def test_family_failure_preserves_trends_and_oversize_dimensions_are_whole(opts):
    import json
    from mojo.apps.aws.services.fleet_health import node
    readers, calls = _readers(("node-a",))
    now = datetime.now(timezone.utc).isoformat()
    current = {name: {"status": "ok", "source": "host", "unit": "bytes", "observed_at": now}
               for name in ("cpu", "load", "memory", "swap", "network")}
    current["swap"] = {"status": "unavailable", "reason": "permission_denied"}
    current["users"] = ["private-user"]
    current["filesystems"] = {"status": "ok", "mounts": [
        {"path": "/", "status": "ok", "inodes": {"status": "ok", "free": 100}}], "missing": [], "omitted_count": 0}
    def diagnostics(*args):
        return {"status": "verified", "anomalies": [], "results": [{
            "host": "node-a", "status": "success", "started": now,
            "result": {"hostname": "node-a", "observed_at": now, "current": current,
                       "proof": {"node_id": "node-a", "observed_at": now}}}]}
    readers["diagnostics"] = diagnostics
    readers["trends"] = lambda *args: {"status": "ok", "cpu": {"status": "ok", "average": 0}, "filesystems": []}
    output = node(Actor("view_platform", "manage_aws", "view_jobs"), "node-a", readers=readers)
    assert output["collection_status"] == "partial" and not output["coverage"]["complete"], "one unavailable metric family prevents complete diagnostic coverage"
    assert output["node"]["trends"]["cpu"]["average"] == 0, "current swap failure must preserve independent real cloud evidence"
    assert "users" not in output["node"]["current"], "checked host payload cannot widen the projected diagnostic families"
    dimensions = [{"Name": "dimension-" + str(i), "Value": "x" * 1024} for i in range(30)]
    readers["trends"] = lambda *args: {"status": "ok", "cpu": {"status": "ok", "dimensions": dimensions,
                                                                 "unit": "Percent", "buckets": [], "average": 0},
                                       "filesystems": []}
    output = node(Actor("view_platform", "manage_aws", "view_jobs"), "node-a", readers=readers)
    assert len(json.dumps(output, separators=(",", ":")).encode()) <= 24 * 1024, "even no-bucket dimension-heavy evidence must fit the contract ceiling"
    cpu = output["node"]["trends"]["cpu"]
    assert cpu["dimensions"] is None and cpu["dimensions_omitted"] is True, "an oversized dimension identity must be omitted whole, never clipped into another series"
    assert cpu["omitted_series_count"] == 1 and output["truncated"], "whole-series omission must be explicit"
