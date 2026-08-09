"""WebApp release deployment orchestration and CI status visibility."""

from unittest import mock

from testit import helpers as th

from tests.test_edge._helpers import (
    cleanup, declare_pools, declare_release_buckets, make_certificate,
    make_domain, make_group, make_release, make_vhost, make_webapp, raises,
)


@th.django_unit_setup()
def setup_webapp_deploy(opts):
    cleanup()
    declare_pools()
    declare_release_buckets()
    opts.group = make_group("webdeploy")
    opts.domain = make_domain(group=opts.group)
    opts.cert = make_certificate(opts.domain)
    opts.vhost = make_vhost(opts.domain, opts.cert, label="deploy")
    opts.webapp = make_webapp(
        opts.group, slug="deployapp", vhost=opts.vhost, auto_promote=True)
    opts.v1 = make_release(opts.webapp, "a" * 40, status="uploaded")
    opts.v2 = make_release(opts.webapp, "b" * 40, status="uploaded")


def _promote_without_publish(webapp, release):
    from django.db import transaction
    from mojo.apps.edge.services import releases

    with mock.patch.object(transaction, "on_commit"):
        return releases.promote(webapp, release)


@th.django_unit_test("promotion publishes only through transaction.on_commit")
def test_publish_after_commit(opts):
    from django.db import transaction
    from mojo.apps.edge.services import releases, webapp_deploy

    callbacks = []
    with mock.patch.object(
            transaction, "on_commit",
            side_effect=lambda callback, **kwargs: callbacks.append(callback)), \
         mock.patch.object(webapp_deploy, "publish") as publish:
        deployment = releases.promote(opts.webapp, opts.v1)
        assert callbacks, "promotion did not register an on-commit callback"
        assert not publish.called, "the coordinator published before commit"
        callbacks[0]()

    publish.assert_called_once_with(deployment.pk)


@th.django_unit_test("a coordinator queue failure restores desired state")
def test_publish_failure_restores(opts):
    from mojo.apps.edge.services import webapp_deploy

    deployment = _promote_without_publish(opts.webapp, opts.v1)
    with mock.patch.object(
            webapp_deploy, "publish", side_effect=RuntimeError("redis down")), \
         mock.patch("mojo.apps.incident.reporter.report_event"):
        result = webapp_deploy.publish_or_restore(deployment.pk)

    opts.webapp.refresh_from_db()
    deployment.refresh_from_db()
    assert result is None
    assert opts.webapp.current_release_id is None
    assert deployment.status == "rolled_back", deployment.detail


@th.django_unit_test("all active edge runners must finish before deployment is live")
def test_active_fleet_success(opts):
    from mojo.apps.edge.services import webapp_deploy

    deployment = _promote_without_publish(opts.webapp, opts.v1)
    targets = [
        {"runner": "edge-a-engine", "job": "job-a"},
        {"runner": "edge-b-engine", "job": "job-b"},
    ]
    rows = [dict(row, status="completed", error="") for row in targets]
    with mock.patch.object(
            webapp_deploy, "_alive_edge_runners",
            return_value=["edge-a-engine", "edge-b-engine"]), \
         mock.patch.object(webapp_deploy, "_publish_targets", return_value=targets), \
         mock.patch.object(webapp_deploy, "_wait", return_value=("completed", rows)):
        result = webapp_deploy.orchestrate(deployment.pk)

    deployment.refresh_from_db()
    assert result == "live:2", f"unexpected coordinator result: {result}"
    assert deployment.status == "live", f"deployment ended {deployment.status}"
    assert deployment.targets == targets, "active-runner snapshot was not retained"


@th.django_unit_test("a partial fleet failure restores and converges the prior release")
def test_partial_failure_rolls_back(opts):
    from mojo.apps.edge.services import webapp_deploy

    opts.webapp.current_release = opts.v1
    opts.webapp.save()
    opts.v1.status = "live"
    opts.v1.save()
    deployment = _promote_without_publish(opts.webapp, opts.v2)

    deploy_targets = [{"runner": "edge-a-engine", "job": "deploy-a"}]
    rollback_targets = [{"runner": "edge-a-engine", "job": "rollback-a"}]
    failed = [dict(deploy_targets[0], status="failed", error="S3 unavailable")]
    restored = [dict(rollback_targets[0], status="completed", error="")]
    with mock.patch.object(
            webapp_deploy, "_alive_edge_runners", return_value=["edge-a-engine"]), \
         mock.patch.object(
             webapp_deploy, "_publish_targets",
             side_effect=[deploy_targets, rollback_targets]), \
         mock.patch.object(
             webapp_deploy, "_wait",
             side_effect=[("failed", failed), ("completed", restored)]):
        result = webapp_deploy.orchestrate(deployment.pk)

    opts.webapp.refresh_from_db()
    opts.v1.refresh_from_db()
    opts.v2.refresh_from_db()
    deployment.refresh_from_db()
    assert result == "rolled_back", f"unexpected rollback result: {result}"
    assert opts.webapp.current_release_id == opts.v1.pk, "prior release was not restored"
    assert opts.v1.status == "live", "prior release did not return to live"
    assert opts.v2.status == "uploaded", "failed target did not return to uploaded"
    assert deployment.status == "rolled_back", deployment.detail


@th.django_unit_test("no active runners restores desired state without waiting")
def test_no_active_runners_rolls_back(opts):
    from mojo.apps.edge.services import webapp_deploy

    opts.webapp.current_release = opts.v1
    opts.webapp.save()
    opts.v1.status = "live"
    opts.v1.save()
    deployment = _promote_without_publish(opts.webapp, opts.v2)
    with mock.patch.object(webapp_deploy, "_alive_edge_runners", return_value=[]):
        result = webapp_deploy.orchestrate(deployment.pk)

    opts.webapp.refresh_from_db()
    deployment.refresh_from_db()
    assert result == "rolled_back", result
    assert opts.webapp.current_release_id == opts.v1.pk
    assert deployment.status == "rolled_back"


@th.django_unit_test("a failed old deployment never rolls back a newer release")
def test_superseded_failure_is_safe(opts):
    from mojo.apps.edge.services import webapp_deploy

    deployment = _promote_without_publish(opts.webapp, opts.v1)
    opts.webapp.current_release = opts.v2
    opts.webapp.save()

    restored = webapp_deploy._restore_previous(deployment.pk, "late failure")
    opts.webapp.refresh_from_db()
    deployment.refresh_from_db()
    assert restored is None, "a superseded deployment attempted rollback"
    assert opts.webapp.current_release_id == opts.v2.pk, "newer release was overwritten"
    assert deployment.status == "superseded", deployment.detail


@th.django_unit_test("node proof fails while the requested vhost still awaits bytes")
def test_node_rejects_pending_release(opts):
    from mojo.apps.edge.services import webapp_deploy

    deployment = _promote_without_publish(opts.webapp, opts.v1)
    job = mock.Mock()
    job.payload = {
        "deployment": deployment.pk,
        "expected_release": opts.v1.pk,
        "rollback": False,
    }
    job.metadata = {}
    installed = {
        "generation": "gen1", "changed": True, "excluded": [],
        "www_pending": {str(opts.vhost.pk): opts.v1.version},
    }
    with mock.patch(
            "mojo.apps.edge.services.installer.install", return_value=installed):
        err = raises(webapp_deploy.install_node, job)
    assert err is not None, "pending release bytes were reported as installed"


@th.django_unit_test("linked CI key sees only its WebApp deployment")
def test_deployment_status_key_scope(opts):
    from mojo.apps.account.models import ApiKey

    deployment = _promote_without_publish(opts.webapp, opts.v1)
    key, token = ApiKey.create_for_group(
        opts.group, "webapp:deploy-status", permissions={"release_webapp": True})
    opts.webapp.api_key = key
    opts.webapp.save()

    opts.client.logout()
    opts.client.session.headers["Authorization"] = f"apikey {token}"
    try:
        response = opts.client.get(
            f"/api/edge/release/deployment/{deployment.pk}")
        assert response.status_code == 200, response.body

        other = make_webapp(opts.group, slug="other-status", auto_promote=True)
        other_release = make_release(other, "c" * 40, status="uploaded")
        other_deployment = _promote_without_publish(other, other_release)
        response = opts.client.get(
            f"/api/edge/release/deployment/{other_deployment.pk}")
        assert response.status_code == 404, \
            "one WebApp key could inspect another WebApp deployment"
    finally:
        opts.client.session.headers.pop("Authorization", None)
