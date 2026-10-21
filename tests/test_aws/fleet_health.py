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
    from mojo.apps.aws.services.fleet_health import IdentityRoster, reconcile
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
    assert writer.write(grants, "a" * 64, rows, time.monotonic() + 1), "bounded identity write must succeed"
    assert reader.read(grants, time.monotonic() + 1)["rows"][0]["node_id"] == "node-a", "another service instance/worker must resolve the same shared identity"
    denied_scope = dict(grants, aws=False)
    assert reader.read(denied_scope, time.monotonic() + 1) is None, "a cached source scope must not grant another scope identity access"
    assert all(item["read_from_replicas"] is False and item["deadline"] > 0 for item in options), "identity cache operations must share the primary deadline budget"
    clock[0] = 221
    assert reader.read(grants, time.monotonic() + 1) is None, "expired identity evidence must require a fresh fleet read"
