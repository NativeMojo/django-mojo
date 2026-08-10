"""Fleet topology/proof and WebApp deploy-key readiness contracts."""

import json
import os
import tempfile
from unittest import mock

from testit import helpers as th

from tests.test_edge._helpers import (
    cleanup, declare_pools, declare_release_buckets, make_group, make_webapp)


@th.django_unit_setup()
def setup_readiness(opts):
    cleanup()
    declare_pools(["default", "blue"])
    declare_release_buckets()
    opts.group = make_group("readiness")
    opts.webapp = make_webapp(opts.group, slug="readinessapp")


@th.django_unit_test("per-pool evidence never overwrites another pool")
def test_installed_evidence_is_per_pool(opts):
    from mojo.apps.edge.services import installer, render

    with tempfile.TemporaryDirectory() as root, \
            mock.patch.object(render, "edge_root", return_value=root):
        installer.write_installed("generation-default", pool="default")
        installer.write_installed("generation-blue", pool="blue")
        assert installer.read_installed("default")["generation"] == "generation-default", \
            "blue pool evidence overwrote default"
        assert installer.read_installed("blue")["generation"] == "generation-blue", \
            "default pool evidence overwrote blue"


@th.django_unit_test("legacy installed.json is read only for the default pool")
def test_default_pool_legacy_read_only_fallback(opts):
    from mojo.apps.edge.services import installer, render

    with tempfile.TemporaryDirectory() as root, \
            mock.patch.object(render, "edge_root", return_value=root):
        with open(os.path.join(root, "installed.json"), "w") as handle:
            json.dump({"generation": "legacy"}, handle)
        assert installer.read_installed("default")["generation"] == "legacy", \
            "default pool did not honor the migration fallback"
        assert installer.read_installed("blue") == {}, \
            "a non-default pool borrowed default's legacy proof"
        installer.write_installed("current", pool="default")
        with open(os.path.join(root, "installed.json")) as handle:
            assert json.load(handle)["generation"] == "legacy", \
                "new evidence mutated the read-only legacy file"


@th.django_unit_test("fleet discovery calls only live edge-channel runners")
def test_fleet_discovery_is_edge_channel_only(opts):
    from mojo.apps.edge.services import readiness

    runner = {"runner_id": "edge-a-engine", "alive": True, "channels": ["edge"]}
    proof = {"status": "success", "result": {
        "node_id": "edge-a", "django_mojo_version": "1.9.0",
        "pools": {"default": {"generation": "g", "excluded": 0,
                                "www_pending": 0, "cert_pending": 0}}}}
    manager = mock.Mock()
    manager.execute_on_runner.return_value = proof
    with mock.patch("mojo.apps.jobs.get_runners", return_value=[runner]) as get_runners, \
            mock.patch("mojo.apps.jobs.manager.get_manager", return_value=manager), \
            mock.patch.object(readiness, "_desired_generations",
                              return_value={"default": "g"}), \
            mock.patch("mojo.apps.account.services.system_settings.get_value",
                       return_value={"nodes": ["edge-a"], "pools": ["default"]}):
        rows = readiness.check_fleet({"timeout": 0.1})
    get_runners.assert_called_once_with(channel="edge")
    assert rows[0]["status"] == "pass", f"valid node/pool proof was not green: {rows}"


@th.django_unit_test("a missing node or pool proof can never report green")
def test_missing_node_and_pool_are_pending(opts):
    from mojo.apps.edge.services import readiness

    topology = {"nodes": ["edge-a", "edge-b"], "pools": ["default", "blue"]}
    runner = {"runner_id": "edge-a-engine", "alive": True, "channels": ["edge"]}
    proof = {"status": "success", "result": {
        "node_id": "edge-a", "django_mojo_version": "1.9.0",
        "pools": {"default": {"generation": "gd", "excluded": 0,
                                "www_pending": 0, "cert_pending": 0}}}}
    manager = mock.Mock()
    manager.execute_on_runner.return_value = proof
    with mock.patch("mojo.apps.jobs.get_runners", return_value=[runner]), \
            mock.patch("mojo.apps.jobs.manager.get_manager", return_value=manager), \
            mock.patch.object(readiness, "_desired_generations",
                              return_value={"default": "gd", "blue": "gb"}), \
            mock.patch("mojo.apps.account.services.system_settings.get_value",
                       return_value=topology):
        rows = readiness.check_fleet({"timeout": 0.1})
    by_code = {row["code"]: row for row in rows}
    assert by_code["fleet.node.edge-a.pool.blue"]["status"] == "pending", \
        "missing pool proof was reported green"
    assert by_code["fleet.node.edge-b"]["status"] == "pending", \
        "missing expected node was reported green"


@th.django_unit_test("deploy-key readiness uses metadata and distinguishes lifecycle")
def test_webapp_key_lifecycle_metadata(opts):
    from mojo.apps.edge.models import WebAppKeyOperation
    from mojo.apps.edge.services import readiness, webapp_keys

    def mine():
        return next(row for row in readiness.check_webapp_keys({})
                    if row["code"] == f"webapp.key.{opts.webapp.pk}")

    missing = mine()
    assert missing["status"] == "pending", "a missing deploy key was reported green"
    linked, api_key, token, rotated = webapp_keys.link(opts.webapp)
    active = mine()
    assert active["status"] == "pass", "an active deploy key was not green"
    assert token not in str(active), "readiness leaked the reveal-once deployment token"
    rotated = webapp_keys.link_once(
        linked, WebAppKeyOperation.ACTION_ROTATE, None,
        "00000000-0000-0000-0000-000000000023")
    rotated_ready = mine()
    assert rotated_ready["status"] == "pass", "a rotated active key was not green"
    assert rotated_ready["details"]["last_action"] == \
        WebAppKeyOperation.ACTION_ROTATE, "readiness lost the safe rotation receipt"
    assert rotated["token"] not in str(rotated_ready), \
        "readiness recovered a rotated reveal-once token"
    webapp_keys.revoke_once(linked, None, "00000000-0000-0000-0000-000000000024")
    revoked = mine()
    assert revoked["status"] == "warn", "a revoked deploy key was not distinguished"
    assert revoked["details"]["last_action"] == WebAppKeyOperation.ACTION_REVOKE, \
        "readiness lost the non-secret revoke receipt"


@th.django_unit_test("vhost and route changes publish after commit with one generation key")
def test_convergence_publication_is_post_commit_and_idempotent(opts):
    from django.db import transaction
    from mojo.apps.edge.services import convergence

    callbacks = []
    with mock.patch.object(transaction, "on_commit",
                           side_effect=lambda callback: callbacks.append(callback)), \
            mock.patch("mojo.apps.jobs.publish", return_value=["job-a"]) as publish, \
            mock.patch.object(convergence, "desired_generation", return_value="generation"):
        convergence.publish_after_commit("default", "default")
        assert not publish.called, "convergence published before the database commit"
        assert len(callbacks) == 1, f"duplicate pool callbacks were registered: {callbacks}"
        callbacks[0]()
    publish.assert_called_once()
    assert publish.call_args.kwargs["idempotency_key"] == \
        "edge-converge:default:generation", "publication key was not generation-idempotent"


@th.django_unit_test("publication failure is explicit pending evidence")
def test_convergence_publish_failure_is_pending(opts):
    from mojo.apps.edge.services import convergence

    with mock.patch.object(convergence, "desired_generation", return_value="generation"), \
            mock.patch("mojo.apps.jobs.publish", side_effect=RuntimeError("redis down")):
        result = convergence.publish_pool("default")
    assert result.status == "pending", \
        f"failed convergence publication was treated as success: {result}"
