"""Fleet topology/proof and WebApp deploy-key readiness contracts."""

import json
import os
import tempfile
from unittest import mock

from testit import helpers as th

from tests.test_edge._helpers import (
    cleanup, declare_pools, declare_release_buckets, make_certificate,
    make_domain, make_group, make_route, make_upstream, make_vhost,
    make_webapp, with_setting)


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
                                "www_pending": 0, "cert_pending": 0,
                                "serving_generation": "combined",
                                "current_generation": "combined"}}}}
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
                                "www_pending": 0, "cert_pending": 0,
                                "serving_generation": "combined",
                                "current_generation": "combined"}}}}
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


@th.django_unit_test("fleet proof refuses failed, stale, wrong, and degraded evidence")
def test_fleet_proof_adverse_states(opts):
    import mojo
    from mojo.apps.edge.services import readiness

    topology = {"nodes": ["edge-a"], "pools": ["default"]}
    runner = {"runner_id": "edge-a-engine", "alive": True, "channels": ["edge"]}

    def check(response):
        manager = mock.Mock()
        manager.execute_on_runner.return_value = response
        with mock.patch("mojo.apps.jobs.get_runners", return_value=[runner]), \
                mock.patch("mojo.apps.jobs.manager.get_manager", return_value=manager), \
                mock.patch.object(readiness, "_desired_generations",
                                  return_value={"default": "wanted"}), \
                mock.patch("mojo.apps.account.services.system_settings.get_value",
                           return_value=topology):
            return readiness.check_fleet({"timeout": 0.1})[0]["status"]

    assert check({"status": "error", "error": "private remote detail"}) == "pending", \
        "a failed node-proof response was reported green"
    base = {
        "node_id": "edge-a", "django_mojo_version": mojo.__version__,
        "pools": {"default": {
            "generation": "wanted", "excluded": 0, "www_pending": 0,
            "cert_pending": 0, "serving_generation": "combined",
            "current_generation": "combined"}}}
    stale = dict(base, django_mojo_version="0.0-stale")
    assert check({"status": "success", "result": stale}) == "fail", \
        "a stale django-mojo version was not a hard readiness failure"
    for field, value in (
            ("generation", "wrong"), ("excluded", 1), ("www_pending", 1),
            ("cert_pending", 1), ("current_generation", "other")):
        proof = {**base, "pools": {"default": {
            **base["pools"]["default"], field: value}}}
        assert check({"status": "success", "result": proof}) == "pending", \
            f"degraded fleet proof {field}={value!r} was reported green"


@th.django_unit_test("deploy-key readiness uses metadata and distinguishes lifecycle")
def test_webapp_key_lifecycle_metadata(opts):
    from mojo.apps.edge.models import WebAppKeyOperation
    from mojo.apps.edge.services import readiness, webapp_keys

    def summary():
        return readiness.check_webapp_keys({})[0]

    missing = summary()
    assert missing["details"]["pending"] >= 1, \
        "a missing deploy key was absent from global readiness counts"
    exact = webapp_keys.status(opts.webapp)
    assert exact["linked"] is False and exact["active"] is False \
           and exact["last_action"] is None, \
        f"the target WebApp was not exactly missing: {exact}"
    assert WebAppKeyOperation.objects.filter(web_app=opts.webapp).first() is None, \
        "a missing WebApp unexpectedly had a key-operation receipt"

    minted = webapp_keys.link_once(
        opts.webapp, WebAppKeyOperation.ACTION_MINT, None,
        "00000000-0000-0000-0000-000000000022")
    opts.webapp.refresh_from_db()
    token = minted["token"]
    active = summary()
    assert active["details"]["pass"] >= 1, \
        "an active deploy key was absent from global readiness counts"
    assert token not in str(active), "readiness leaked the reveal-once deployment token"
    exact = webapp_keys.status(opts.webapp)
    latest = WebAppKeyOperation.objects.filter(web_app=opts.webapp).first()
    assert exact["linked"] and exact["active"] \
           and exact["api_key"] == opts.webapp.api_key_id \
           and exact["last_action"] == WebAppKeyOperation.ACTION_MINT, \
        f"the target WebApp was not exactly active after mint: {exact}"
    assert latest and latest.action == WebAppKeyOperation.ACTION_MINT \
           and latest.api_key_id == opts.webapp.api_key_id, \
        f"the target WebApp's latest receipt was not its mint: {latest}"

    rotated = webapp_keys.link_once(
        opts.webapp, WebAppKeyOperation.ACTION_ROTATE, None,
        "00000000-0000-0000-0000-000000000023")
    opts.webapp.refresh_from_db()
    rotated_ready = summary()
    assert rotated_ready["details"]["pass"] >= 1, \
        "a rotated active key was absent from global readiness counts"
    assert rotated_ready["details"].get(
        f"action_{WebAppKeyOperation.ACTION_ROTATE}", 0) >= 1, \
        "readiness lost the safe rotation receipt"
    assert rotated["token"] not in str(rotated_ready), \
        "readiness recovered a rotated reveal-once token"
    exact = webapp_keys.status(opts.webapp)
    latest = WebAppKeyOperation.objects.filter(web_app=opts.webapp).first()
    assert exact["linked"] and exact["active"] \
           and exact["api_key"] == opts.webapp.api_key_id \
           and exact["last_action"] == WebAppKeyOperation.ACTION_ROTATE, \
        f"the target WebApp was not exactly active after rotation: {exact}"
    assert latest and latest.action == WebAppKeyOperation.ACTION_ROTATE \
           and latest.api_key_id == opts.webapp.api_key_id, \
        f"the target WebApp's latest receipt was not its rotation: {latest}"

    revoked_key_id = opts.webapp.api_key_id
    webapp_keys.revoke_once(
        opts.webapp, None, "00000000-0000-0000-0000-000000000024")
    opts.webapp.refresh_from_db()
    revoked = summary()
    assert revoked["details"]["warn"] >= 1, \
        "a revoked deploy key was absent from global readiness counts"
    assert revoked["details"].get(
        f"action_{WebAppKeyOperation.ACTION_REVOKE}", 0) >= 1, \
        "readiness lost the non-secret revoke receipt"
    exact = webapp_keys.status(opts.webapp)
    latest = WebAppKeyOperation.objects.filter(web_app=opts.webapp).first()
    assert exact["linked"] is False and exact["active"] is False \
           and exact["last_action"] == WebAppKeyOperation.ACTION_REVOKE, \
        f"the target WebApp was not exactly revoked: {exact}"
    assert latest and latest.action == WebAppKeyOperation.ACTION_REVOKE \
           and latest.api_key_id == revoked_key_id, \
        f"the target WebApp's latest receipt was not its revoke: {latest}"


@th.django_unit_test("readiness counts failures after the first 64 rows")
def test_readiness_global_aggregation_is_not_truncated(opts):
    from mojo.apps.edge.models import WebApp
    from mojo.apps.edge.services import readiness, webapp_keys

    apps = [make_webapp(opts.group, slug=f"bounded{i:02d}") for i in range(70)]
    last_pk = apps[-1].pk

    def status(webapp):
        if webapp.pk == last_pk:
            return {"linked": False, "active": False, "last_action": None}
        return {"linked": True, "active": True, "last_action": "mint"}

    with mock.patch.object(webapp_keys, "status", side_effect=status):
        rows = readiness.check_webapp_keys({})
    assert rows[0]["status"] == "pending", \
        "a missing deploy key after row 64 was truncated into a green summary"
    assert rows[0]["details"]["total"] >= 71, \
        f"global aggregation did not scan every WebApp: {rows[0]}"
    assert len(rows) <= readiness.DETAIL_LIMIT + 1, \
        f"readiness returned unbounded row detail: {len(rows)}"
    WebApp.objects.filter(pk__in=[row.pk for row in apps]).delete()


@th.django_unit_test("local proof contains only bounded serving metadata")
def test_local_node_proof_no_secret_shape(opts):
    from mojo.apps.edge.services import installer, readiness, render

    with tempfile.TemporaryDirectory() as root, \
            mock.patch.object(render, "edge_root", return_value=root):
        generation = os.path.join(root, "generations", "combined")
        os.makedirs(generation)
        os.symlink(generation, os.path.join(root, "current"))
        installer.write_installed(
            "desired-default", pool="default", serving_generation="combined")
        proof = with_setting(
            "EDGE_NODE_ID", "edge-proof-a",
            lambda: readiness.local_node_proof({"pools": ["default"]}))
    assert set(proof) == {"node_id", "django_mojo_version", "pools"}, \
        f"proof grew an unreviewed top-level surface: {proof}"
    assert set(proof["pools"]["default"]) == {
        "generation", "excluded", "www_pending", "cert_pending",
        "serving_generation", "current_generation"}, \
        f"proof exposed more than convergence metadata: {proof}"
    assert "token" not in str(proof).lower() and "secret" not in str(proof).lower(), \
        f"proof exposed credential material: {proof}"


@th.django_unit_test("vhost and route changes publish after commit with one generation key")
def test_convergence_publication_is_post_commit_and_idempotent(opts):
    import hashlib
    from django.db import transaction
    from mojo.apps.edge.services import convergence

    callbacks = []
    jobs = mock.Mock()
    jobs.get_runners.return_value = []
    jobs.publish.return_value = ["job-a"]
    with mock.patch.object(transaction, "on_commit",
                           side_effect=lambda callback: callbacks.append(callback)), \
            convergence.publisher_scope(
                lambda pool: convergence.publish_pool(
                    pool, jobs_module=jobs, generation="generation")):
        convergence.publish_after_commit("default", "default")
        assert not jobs.publish.called, "convergence published before the database commit"
        assert len(callbacks) == 1, f"duplicate pool callbacks were registered: {callbacks}"
    result = callbacks[0]()
    assert result.status == "published", \
        f"the captured commit callback did not reach publication: {result}"
    jobs.publish.assert_called_once()
    expected = hashlib.sha256(
        b"edge-converge:default:generation:edge").hexdigest()
    assert jobs.publish.call_args.kwargs["idempotency_key"] == expected, \
        "publication key was not target/generation-idempotent"
    assert len(expected) == 64, "publication idempotency key exceeds the DB bound"


@th.django_unit_test("vhost and route lifecycle mutations publish every affected pool")
def test_model_lifecycle_publications(opts):
    from django.db import transaction
    from mojo.apps.edge.services import convergence

    domain = make_domain(group=opts.group)
    certificate = make_certificate(domain)
    upstream = make_upstream(group=opts.group)
    callbacks = []
    publisher = mock.Mock(return_value={"status": "published"})
    with mock.patch.object(transaction, "on_commit",
                           side_effect=lambda callback: callbacks.append(callback)), \
            convergence.publisher_scope(publisher):
        vhost = make_vhost(
            domain, certificate, label="app", kind="site_api", pool="default")
        assert len(callbacks) == 1, "Vhost create did not register after-commit publication"
        vhost.spa = True
        vhost.save()
        vhost.pool = "blue"
        vhost.save()
        assert len(callbacks) == 4, \
            "a pool move did not publish both the old and new pool"

        route = make_route(vhost, "/api", upstream)
        route.path_prefix = "/api-v2"
        route.save()
        route.delete()
        vhost.delete()

    assert not publisher.called, "a model published before its transaction committed"
    results = [callback() for callback in callbacks]
    pools = [call.args[0] for call in publisher.call_args_list]
    assert pools == ["default", "default", "blue", "default",
                     "blue", "blue", "blue", "blue"], \
        f"create/update/move/delete publications were incomplete: {pools}"
    assert all(result["status"] == "published" for result in results), \
        f"captured model callbacks did not reach the scoped publisher: {results}"


@th.django_unit_test("partial route work remains durable and pending publication repairs")
def test_sequential_partial_route_pending_repair(opts):
    from django.db import transaction
    from mojo.apps.edge.models import VhostRoute
    from mojo.apps.edge.services import convergence

    domain = make_domain(group=opts.group)
    certificate = make_certificate(domain)
    upstream = make_upstream(group=opts.group)
    vhost = make_vhost(
        domain, certificate, label="partial", kind="site_api", pool="default")
    callbacks = []
    jobs = mock.Mock()
    jobs.get_runners.return_value = []
    jobs.publish.side_effect = [
        RuntimeError("private transport detail"), "job-ok"]
    publications = []

    def publisher(pool):
        result = convergence.publish_pool(
            pool, jobs_module=jobs, generation="generation")
        publications.append(result)
        return result

    with mock.patch.object(transaction, "on_commit",
                           side_effect=lambda callback: callbacks.append(callback)), \
            convergence.publisher_scope(publisher):
        first = make_route(vhost, "/one", upstream)
        pending = callbacks.pop()()
        assert pending.status == "pending", \
            "a publication failure did not leave explicit pending evidence"
        raised = None
        try:
            make_route(vhost, "bad;prefix", upstream)
        except Exception as error:
            raised = error
        assert raised is not None, "an invalid second Route unexpectedly persisted"
        assert VhostRoute.objects.filter(pk=first.pk).exists(), \
            "the valid first Route was rolled back with a later invalid row"
        assert not callbacks, "the invalid Route registered a convergence publication"
        repaired = make_route(vhost, "/two", upstream)
        published = callbacks.pop()()
        assert published.status == "published", \
            f"retry did not repair pending publication evidence: {published}"
        assert VhostRoute.objects.filter(pk__in=[first.pk, repaired.pk]).count() == 2, \
            "sequential repair lost one of the valid Route rows"
    assert [result.status for result in publications] == ["pending", "published"], \
        f"captured Route callbacks lost pending/repair evidence: {publications}"


@th.django_unit_test("publication failure is explicit pending evidence")
def test_convergence_publish_failure_is_pending(opts):
    from mojo.apps.edge.services import convergence

    jobs = mock.Mock()
    jobs.get_runners.return_value = []
    jobs.publish.side_effect = RuntimeError("token=do-not-log-this")
    with mock.patch.object(convergence.logit, "error") as logged:
        result = convergence.publish_pool(
            "default", jobs_module=jobs, generation="generation")
    assert result.status == "pending", \
        f"failed convergence publication was treated as success: {result}"
    assert "do-not-log-this" not in str(logged.call_args), \
        f"convergence logged a raw provider exception: {logged.call_args}"
    assert "RuntimeError" in str(logged.call_args), \
        "convergence lost bounded exception class evidence"
