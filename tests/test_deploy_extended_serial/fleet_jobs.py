"""A current web process alone must not prove application configuration health."""
from unittest import mock
from testit import helpers as th

REVISION = "a" * 32


def _receipt(web=False):
    return dict(installed_revision=REVISION, target_revision=REVISION,
                installed_digest="b" * 64, installed_at=100,
                status="restart_requested", request_service_required=web)


@th.django_unit_test()
def test_worker_only_fleet_requires_jobs_proof(opts):
    from mojo.deploy import fleet_config_node as node, fleet_config_role
    with mock.patch.object(node, "_supported", return_value=True), \
            mock.patch.object(fleet_config_role, "read", return_value={"request_service_required": False, "installed_at": 100}), \
            mock.patch.object(node, "read_receipt", return_value=_receipt()), \
            mock.patch.object(node, "current_digest", return_value="b" * 64), \
            mock.patch.object(node, "_jobs_proof", create=True, return_value={
                "restarted": True, "healthy": True, "error_code": None}), \
            mock.patch.object(node, "_service_state", return_value={}) as service:
        report = node.report({"revision": REVISION})
        assert report["healthy"] is True, "Worker-only node must prove jobs without an API unit"
        assert report["restarted"] is True, "Worker restart must be established by loaded jobs proof"
        service.assert_not_called()


@th.django_unit_test()
def test_web_health_cannot_hide_old_jobs(opts):
    from mojo.deploy import fleet_config_node as node, fleet_config_role
    with mock.patch.object(node, "_supported", return_value=True), \
            mock.patch.object(fleet_config_role, "read", return_value={"request_service_required": True, "installed_at": 100}), \
            mock.patch.object(node, "read_receipt", return_value=_receipt(True)), \
            mock.patch.object(node, "current_digest", return_value="b" * 64), \
            mock.patch.object(node, "_jobs_proof", create=True, return_value={
                "restarted": False, "healthy": False, "error_code": "engine_revision_pending"}), \
            mock.patch.object(node, "_service_state", return_value={"active": True, "pid": 12, "started_at": 101}), \
            mock.patch.object(node, "_serving_proof", return_value={"healthy": True}):
        report = node.report({"revision": REVISION})
        assert report["healthy"] is False, "Healthy web service cannot hide stale jobs"
        assert report["error_code"] == "engine_revision_pending", "Report the specific jobs activation failure"


@th.django_unit_test()
def test_jobs_proof_requires_exact_current_engine_and_scheduler(opts):
    import os
    from mojo.deploy import fleet_config_node as node, jobman
    from mojo.apps.jobs import fleet_state, execution_context
    from mojo.helpers.settings import settings
    from mojo.apps.account.services import admin_platform
    proof = {"loaded_revision": REVISION, "started_at": 101, "draining": False, "ready": True}
    with mock.patch.object(execution_context, "current_runner_incarnation", return_value={"runner_id": "node-engine", "started": "now"}), \
            mock.patch.object(settings, "get_static", return_value=REVISION), \
            mock.patch.object(jobman, "exact_processes", side_effect=lambda root, component: [str(os.getpid())] if component == "engine" else ["42"]) as inventory, \
            mock.patch.object(fleet_state, "read", return_value=proof) as read, \
            mock.patch.object(admin_platform, "_database", return_value={"reachable": True}), \
            mock.patch.object(admin_platform, "_redis", return_value={"reachable": True}):
        assert node._jobs_proof(REVISION, 100)["healthy"], "Both fresh component proofs must establish jobs health"
        for component in ("engine", "scheduler"):
            read.side_effect = lambda name, pid: dict(proof, loaded_revision="c" * 32) if name == component else proof
            result = node._jobs_proof(REVISION, 100)
            assert not result["healthy"] and result["error_code"] == component + "_revision_pending", "Either stale component must prevent health"
        read.side_effect = None
        read.return_value = dict(proof, draining=True)
        assert node._jobs_proof(REVISION, 100)["error_code"] == "engine_draining", "A draining engine cannot count as its replacement"
        read.return_value = proof
        inventory.side_effect = lambda root, component: [str(os.getpid()), "99"] if component == "engine" else ["42"]
        assert node._jobs_proof(REVISION, 100)["error_code"] == "engine_ambiguous", "A fresh engine cannot hide another local predecessor"
        inventory.side_effect = OSError("process inventory unavailable")
        assert node._jobs_proof(REVISION, 100)["error_code"] == "jobs_process_inventory_unavailable", "Unreadable inventory proves nothing"


@th.django_unit_test()
def test_unknown_request_role_cannot_skip_web_proof(opts):
    from mojo.deploy import fleet_config_node as node, fleet_config_role
    with mock.patch.object(node, "_supported", return_value=True), \
            mock.patch.object(fleet_config_role, "read", return_value=None), \
            mock.patch.object(node, "read_receipt", return_value=_receipt(None)), \
            mock.patch.object(node, "current_digest", return_value="b" * 64), \
            mock.patch.object(node, "_jobs_proof") as jobs:
        result = node.report({"revision": REVISION})
        assert not result["healthy"], "Unproven role cannot remove required service checks"
        assert result["error_code"] == "request_service_role_unknown", "Unknown role must be explicit"
        jobs.assert_not_called()


@th.django_unit_test()
def test_app_receipt_cannot_disable_request_service_proof(opts):
    from mojo.deploy import fleet_config_node as node, fleet_config_role
    with mock.patch.object(node, "_supported", return_value=True), \
            mock.patch.object(node, "read_receipt", return_value=_receipt(False)), \
            mock.patch.object(node, "current_digest", return_value="b" * 64), \
            mock.patch.object(fleet_config_role, "read", return_value={"request_service_required": True, "installed_at": 100}), \
            mock.patch.object(node, "_service_state", return_value={}), \
            mock.patch.object(node, "_jobs_proof", return_value={"healthy": True, "restarted": True}) as jobs:
        result = node.report({"revision": REVISION})
        assert not result["healthy"], "Writable progress cannot suppress independently required API proof"
        assert result["error_code"] == "service_state_unavailable", "The root role controls which service must prove readiness"
        jobs.assert_not_called()


@th.django_unit_test()
def test_app_receipt_cannot_lower_service_installation_time(opts):
    from mojo.deploy import fleet_config_node as node, fleet_config_role
    receipt = dict(_receipt(True), installed_at=1)
    with mock.patch.object(node, "_supported", return_value=True), \
            mock.patch.object(node, "read_receipt", return_value=receipt), \
            mock.patch.object(node, "current_digest", return_value="b" * 64), \
            mock.patch.object(fleet_config_role, "read", return_value={"request_service_required": True, "installed_at": 100}), \
            mock.patch.object(node, "_service_state", return_value={"active": True, "pid": 12, "started_at": 50}), \
            mock.patch.object(node, "_serving_proof") as serving:
        result = node.report({"revision": REVISION})
        assert not result["healthy"] and result["error_code"] == "service_restart_pending", "Only sealed installation time can establish process replacement"
        serving.assert_not_called()


@th.django_unit_test()
def test_sync_uses_sealed_time_and_preserves_custom_service_activation(opts):
    from mojo.deploy import config_sync, fleet_config_node as node, fleet_config_role as role
    receipt = dict(_receipt(False), installed_at=1, restart_requested_at=150)
    with mock.patch.object(config_sync.request_service, "read", return_value=False), \
            mock.patch.object(role, "write", return_value=True), \
            mock.patch.object(role, "read", return_value={"request_service_required": False, "installed_at": 200}), \
            mock.patch.object(role, "clear", return_value=True) as clear, \
            mock.patch.object(node, "write_receipt"):
        restart = mock.Mock(return_value=True)
        result = config_sync._finish_fleet_install(receipt, "b" * 64, node.TARGET, {"CONFIG_SYNC_RESTART": True}, restart)
        assert result == 0 and restart.call_count == 1, "First sealed generation must activate despite an older mutable restart receipt"
        assert receipt["installed_at"] == 200, "Progress must copy the sealed installation time"
        restart.reset_mock()
        custom = dict(_receipt(True), status="downloaded")
        result = config_sync._finish_fleet_install(custom, "b" * 64, node.TARGET,
            {"CONFIG_SYNC_RESTART": True, "CONFIG_SYNC_SERVICE": "custom.service"}, restart)
        assert result == 0 and restart.call_count == 1, "Custom services retain their established activation behavior"
        clear.assert_called_once()
