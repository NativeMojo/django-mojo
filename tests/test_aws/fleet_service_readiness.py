"""Current signed service evidence through the existing checked fleet readers."""
import copy
import time
from datetime import datetime, timedelta, timezone

from testit import helpers as th


REVISION = "a" * 32


def _fixture(required=None, physical="node-a"):
    from test_aws.fleet_health import _readers
    host = physical.lower().replace(".", "-").replace("_", "-")
    readers, calls = _readers((host,))
    now = datetime.now(timezone.utc)
    at = now.isoformat()
    started = (now - timedelta(hours=1)).isoformat()
    declaration = {"node_id": host, "hostname": host, "source": "configuration",
                   "request_service_required": required}
    readers["expected"] = lambda grants, end: {"declarations": [dict(declaration)], "sources": {},
        "complete": True, "configured": True, "spec": None}
    readers["heartbeats"] = lambda end: [{"hostname": host, "runner_id": "checked-engine",
        "started": started, "last_heartbeat": at, "alive": True}]
    receipt = {"host": host, "started": started, "status": "success", "result": {
        "hostname": host, "observed_at": at, "current": {},
        "proof": {"node_id": host, "observed_at": at},
        "activation": {"status": "ok", "scope": "configuration_activation", "revision": REVISION,
            "request_service_required": True, "request_service_profile": "opt_api_asgi_v1", "observed_at": at},
        "request_service": {"status": "ok", "node": physical.lower(), "loaded_revision": REVISION,
            "pid": 123, "healthy": True, "profile": "opt_api_asgi_v1", "observed_at": at}}}
    checked = {"status": "verified", "anomalies": [], "results": [receipt]}
    readers["revision"] = REVISION
    def diagnostics(row, roster, end, revision):
        calls.append(("revision", revision))
        return copy.deepcopy(checked)
    readers["diagnostics"] = diagnostics
    readers["diagnostics_batch"] = lambda roster, end: copy.deepcopy(checked)
    readers["recent"] = lambda *args: {"nodes": {}}
    return readers, receipt, checked, calls, host


def _services(readers, host, actor=None):
    from test_aws.fleet_health import Actor
    from mojo.apps.aws.services.fleet_health import node, acquire_snapshot
    actor = actor or Actor("view_platform", "view_jobs")
    detail = node(actor, host, readers=readers)["node"]
    snapshot = acquire_snapshot(actor, readers=readers, deadline=time.monotonic() + 5)
    return [("node", detail, detail["services"]),
            ("snapshot", next(row for row in snapshot["rows"] if row["node_id"] == host),
             snapshot["evidence"][host]["services"])]


@th.django_unit_test()
def test_current_signed_readiness_is_known_in_node_and_snapshot(opts):
    for required in (None, True):
        readers, receipt, checked, calls, host = _fixture(required)
        for path, row, services in _services(readers, host):
            service = services["request_service"]
            assert service["status"] == "ok", f"{path}: a current bound signed request proof must become known"
            assert service.get("healthy") is True, f"{path}: accepted readiness must retain the actual healthy boolean"
            assert row["request_service_required"] is True, f"{path}: a bound required role must establish the denominator"
            assert service.get("observed_at") == receipt["result"]["request_service"]["observed_at"], f"{path}: projection must preserve the actual proof instant"
            assert services["scheduler"]["status"] == "unknown", f"{path}: request readiness cannot prove the per-node scheduler"


@th.django_unit_test()
def test_signed_not_ready_is_known_failure_and_physical_hostname_is_preserved(opts):
    readers, receipt, checked, calls, host = _fixture(physical="Api_One.Example")
    receipt["result"]["request_service"]["healthy"] = False
    for path, row, services in _services(readers, host):
        service = services["request_service"]
        assert service["status"] == "ok" and service.get("healthy") is False, f"{path}: a valid negative readiness observation must remain known"
        assert service.get("node") == "api_one.example", f"{path}: retain the raw physical hostname while matching the existing host-channel normalization"


@th.django_unit_test()
def test_missing_stale_mismatched_and_replaced_evidence_stays_unknown(opts):
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    cases = {
        "missing service": lambda receipt, checked: receipt["result"].pop("request_service"),
        "stale service": lambda receipt, checked: receipt["result"]["request_service"].update(observed_at=old),
        "future service": lambda receipt, checked: receipt["result"]["request_service"].update(observed_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()),
        "wrong physical node": lambda receipt, checked: receipt["result"]["request_service"].update(node="other-host"),
        "wrong revision": lambda receipt, checked: receipt["result"]["request_service"].update(loaded_revision="b" * 32),
        "invalid pid": lambda receipt, checked: receipt["result"]["request_service"].update(pid=0),
        "nonboolean readiness": lambda receipt, checked: receipt["result"]["request_service"].update(healthy="true"),
        "wrong profile": lambda receipt, checked: receipt["result"]["request_service"].update(profile="run_mojo_asgi_v1"),
        "stale logical proof": lambda receipt, checked: receipt["result"]["proof"].update(observed_at=old),
        "wrong logical node": lambda receipt, checked: receipt["result"]["proof"].update(node_id="other-node"),
        "wrong payload host": lambda receipt, checked: receipt["result"].update(hostname="other-host"),
        "predecessor runner": lambda receipt, checked: receipt.update(started=old),
        "duplicate receipt": lambda receipt, checked: checked["results"].append(copy.deepcopy(receipt)),
        "activation wrong revision": lambda receipt, checked: receipt["result"]["activation"].update(revision="b" * 32),
        "activation missing": lambda receipt, checked: receipt["result"].pop("activation"),
        "activation stale": lambda receipt, checked: receipt["result"]["activation"].update(observed_at=old),
        "unsupported profile": lambda receipt, checked: receipt["result"]["activation"].update(request_service_profile="arbitrary"),
    }
    for label, change in cases.items():
        readers, receipt, checked, calls, host = _fixture(True)
        change(receipt, checked)
        for path, row, services in _services(readers, host):
            assert services["request_service"]["status"] == "unknown", f"{path}: {label} cannot prove current readiness"


@th.django_unit_test()
def test_explicit_bound_role_exclusion_and_conflict_agree(opts):
    readers, receipt, checked, calls, host = _fixture()
    receipt["result"]["activation"]["request_service_required"] = False
    receipt["result"]["activation"]["request_service_profile"] = None
    receipt["result"].pop("request_service")
    for path, row, services in _services(readers, host):
        assert services["request_service"]["status"] == "not_applicable", f"{path}: an explicit bound excluded role must remain excluded"
        assert row["request_service_required"] is False, f"{path}: explicit role exclusion must update the required denominator"
    readers, receipt, checked, calls, host = _fixture(False)
    for path, row, services in _services(readers, host):
        assert services["request_service"]["status"] == "unknown", f"{path}: conflicting declaration and bound required role cannot become green or excluded"
        assert row["request_service_required"] is None, f"{path}: a role conflict must leave requirement unresolved"


@th.django_unit_test()
def test_jobs_only_cannot_project_platform_activation_or_readiness(opts):
    from test_aws.fleet_health import Actor
    readers, receipt, checked, calls, host = _fixture(True)
    for path, row, services in _services(readers, host, Actor("view_jobs")):
        assert services["request_service"]["status"] == "unknown", f"{path}: jobs permission alone must not expose root-bound request readiness"
        assert "activation" not in services, f"{path}: an overbroad checked payload must not expose denied activation evidence"
    assert ("revision", None) in calls, "jobs-only node dispatch must omit the platform revision"
    readers, receipt, checked, calls, host = _fixture(True)
    for path, row, services in _services(readers, host, Actor("view_platform")):
        assert services["request_service"]["status"] == "unknown", f"{path}: platform permission alone cannot gather checked request readiness"
    assert not calls, "denied jobs permission must prevent checked diagnostic dispatch"


@th.django_unit_test()
def test_coverage_uses_the_shared_current_service_projection(opts):
    from test_aws.fleet_health import Actor
    from mojo.apps.aws.services.fleet_health import node, coverage
    for healthy in (True, False):
        readers, receipt, checked, calls, host = _fixture()
        receipt["result"]["request_service"]["healthy"] = healthy
        actor = Actor("view_platform", "view_jobs")
        node(actor, host, readers=readers)
        output = coverage(actor, node_id=host, readers=readers)
        measurement = next(row for row in output["measurements"] if row["key"] == "request_service")
        assert measurement["required"] is True, "coverage must use the same bound required role as node and snapshot"
        assert measurement["sources"]["runtime"]["coverage_status"] == "covered", "coverage must accept the same current signed request observation"
        assert measurement["sources"]["runtime"]["value"] == {"healthy": healthy}, "Known negative readiness must remain visible in monitoring coverage"
        assert output["node"]["request_service_required"] is True, "the selected coverage node must retain the proved role"
