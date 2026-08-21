"""Fleet topology/proof and WebApp deploy-key readiness contracts."""

import json
import os
import tempfile
from unittest import mock
from pathlib import Path

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
    assert set(proof) == {
        "node_id", "django_mojo_version", "platform_sha",
        "platform_deployment", "observed_at", "pools"}, \
        f"proof grew an unreviewed top-level surface: {proof}"
    assert set(proof["pools"]["default"]) == {
        "generation", "excluded", "www_pending", "cert_pending",
        "serving_generation", "current_generation"}, \
        f"proof exposed more than convergence metadata: {proof}"
    assert "token" not in str(proof).lower() and "secret" not in str(proof).lower(), \
        f"proof exposed credential material: {proof}"


@th.django_unit_test("atomic deployment identity is authoritative with one legacy fallback")
def test_atomic_deploy_identity_manifest_and_legacy_fallback(opts):
    from mojo.apps.edge.services import readiness

    sha = "a" * 40
    deployment = "12345678-1234-4123-8123-123456789abc"
    with tempfile.TemporaryDirectory() as root:
        var = Path(root)
        (var / "deploy_identity.json").write_text(json.dumps({
            "schema": 2, "sha": sha, "deployment": deployment,
        }), encoding="utf-8")
        th.assert_eq(readiness._read_deploy_identity(var), (sha, deployment),
                     "a valid v2 manifest must be the live identity")

        manifest = var / "deploy_identity.json"
        metadata = manifest.stat()
        before_growth = mock.Mock(
            st_mode=metadata.st_mode, st_size=0, st_dev=metadata.st_dev,
            st_ino=metadata.st_ino, st_mtime_ns=metadata.st_mtime_ns,
            st_ctime_ns=metadata.st_ctime_ns)
        with mock.patch.object(
                readiness.os, "fstat", side_effect=[before_growth, metadata]):
            th.assert_eq(readiness._read_deploy_identity(var), ("", ""),
                         "growth during one descriptor read must fail closed")

        (var / "deploy_sha").write_text("b" * 40, encoding="utf-8")
        (var / "deployment_uuid").write_text(
            "87654321-4321-4321-8321-cba987654321", encoding="utf-8")
        (var / "deploy_identity.json").write_text("{broken", encoding="utf-8")
        th.assert_eq(readiness._read_deploy_identity(var), ("", ""),
                     "a present but invalid manifest must fail closed, never "
                     "borrow a legacy pair")

        (var / "deploy_identity.invalid").write_text("invalid\n", encoding="utf-8")
        (var / "deploy_identity.json").write_text(json.dumps({
            "schema": 2, "sha": sha, "deployment": deployment,
        }), encoding="utf-8")
        th.assert_eq(readiness._read_deploy_identity(var), ("", ""),
                     "the invalidation marker must suppress even a candidate "
                     "manifest until publication commits")

        (var / "deploy_identity.invalid").unlink()
        (var / "deploy_identity.json").unlink()
        th.assert_eq(
            readiness._read_deploy_identity(var),
            ("b" * 40, "87654321-4321-4321-8321-cba987654321"),
            "one predecessor generation must still read its bounded legacy pair")

        (var / "deploy_identity.invalid").write_text("invalid\n", encoding="utf-8")
        th.assert_eq(readiness._read_deploy_identity(var), ("", ""),
                     "an invalidation marker must suppress stale legacy proof "
                     "after a failed rollback")

        (var / "deploy_identity.invalid").unlink()
        (var / "deploy_sha").write_text("b" * 5000, encoding="utf-8")
        th.assert_eq(readiness._read_deploy_identity(var), ("", ""),
                     "legacy fallback must refuse unbounded files")

        (var / "deploy_sha").unlink()
        os.symlink(var / "deployment_uuid", var / "deploy_identity.json")
        th.assert_eq(readiness._read_deploy_identity(var), ("", ""),
                     "an authoritative manifest symlink must fail closed")
        (var / "deploy_identity.json").unlink()
        os.mkfifo(var / "deploy_identity.json")
        th.assert_eq(readiness._read_deploy_identity(var), ("", ""),
                     "an authoritative manifest FIFO must fail without blocking")


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


@th.django_unit_test("serving-destination readiness reports the target and steers when absent")
def test_webapp_destination_readiness(opts):
    from mojo.apps.edge.services import readiness

    def check():
        return readiness.check_webapp_destination({})[0]

    # Resolvable from the platform address → pass, naming the destination.
    def resolved():
        with mock.patch("mojo.apps.edge.services.webapp_destination._base_url",
                        return_value="https://api.stage.example"):
            return check()
    row = with_setting("EDGE_WEBAPP_CNAME_TARGET", "", resolved)
    assert row["status"] == "pass", f"a resolvable destination was not green: {row}"
    assert "api.stage.example" in row["explanation"], \
        f"the readiness row did not name the destination: {row}"

    # Nothing configured yet → pending (not broken), with remediation.
    def unset():
        with mock.patch("mojo.apps.edge.services.webapp_destination._base_url",
                        return_value=""):
            return check()
    pending = with_setting("EDGE_WEBAPP_CNAME_TARGET", "", unset)
    assert pending["status"] == "pending", \
        f"an unconfigured destination was not pending: {pending}"
    assert pending["remediation"], "the pending destination row carried no remediation"

    # A set-but-invalid override is a misconfiguration to fix now → fail.
    invalid = with_setting("EDGE_WEBAPP_CNAME_TARGET", "not a hostname", check)
    assert invalid["status"] == "fail", \
        f"a set-but-invalid override was not a hard failure: {invalid}"


@th.django_unit_test("apps-domain readiness reports instant go-live or what converging creates")
def test_apps_domain_readiness(opts):
    from objict import objict

    from mojo.apps.edge.services import readiness

    domain = make_domain(group=opts.group, provider="route53")
    make_certificate(domain)  # active apex+wildcard
    target = "edge-apps-target.example.net"
    zone = [objict(type="CNAME", name=f"*.{domain.name}",
                   record_values=[target], ttl=300)]

    def check(records):
        def run():
            with mock.patch("mojo.apps.dnsman.services.dns.list_records",
                            return_value=records), \
                    mock.patch(
                        "mojo.apps.edge.services.webapp_apps_domain._base_url",
                        return_value=f"https://api.{domain.name}"):
                return readiness.check_apps_domain({})[0]
        return with_setting("EDGE_WEBAPP_CNAME_TARGET", target, run)

    # Wildcard record + covering certificate in place → pass, naming the domain.
    row = check(zone)
    assert row["status"] == "pass", f"a fully covered apps domain was not green: {row}"
    assert domain.name in row["explanation"] and "instantly" in row["explanation"], \
        f"the pass row did not state the instant go-live under the domain: {row}"

    # Missing wildcard record → pending, naming what converging will create.
    pending = check([])
    assert pending["status"] == "pending", \
        f"a missing wildcard record was not pending: {pending}"
    assert f"*.{domain.name}" in pending["explanation"], \
        f"the pending row did not name the missing record: {pending}"
    assert "converging will create it" in pending["explanation"], \
        f"the pending row did not say converging creates it: {pending}"
    assert pending["remediation"], "the pending apps-domain row had no remediation"

