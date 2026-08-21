"""Split out of tests/test_edge/22_webapp_deploy.py (maestro #1839).

These tests patch shared production surfaces (mojo.apps.incident.reporter,
mojo.apps.jobs.get_runners_bounded) — process-global, so unsafe under the
parallel default tier.
"""

from unittest import mock

from testit import helpers as th

from tests.test_edge._helpers import (
    cleanup, declare_pools, declare_release_buckets, make_certificate,
    make_domain, make_group, make_release, make_vhost, make_webapp,
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
    opts.webapp = make_webapp(opts.group, slug="deployapp", vhost=opts.vhost)
    opts.v1 = make_release(opts.webapp, "a" * 40, status="uploaded")
    opts.v2 = make_release(opts.webapp, "b" * 40, status="uploaded")


def _promote_without_publish(webapp, release):
    from django.db import transaction
    from mojo.apps.edge.services import releases

    with mock.patch.object(transaction, "on_commit"):
        return releases.promote(webapp, release)


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


@th.django_unit_test("WebApp fleet discovery uses the bounded edge roster")
def test_webapp_roster_is_bounded(opts):
    from mojo.apps.edge.services import webapp_deploy

    rows = [
        {"runner_id": "edge-b-engine", "alive": True},
        {"runner_id": "edge-a-engine", "alive": True},
    ]
    with mock.patch(
            "mojo.apps.jobs.get_runners_bounded",
            return_value=rows) as get_runners:
        runners = webapp_deploy._alive_edge_runners()
    assert runners == ["edge-a-engine", "edge-b-engine"]
    get_runners.assert_called_once_with(
        channel="edge", limit=128, max_scan_pages=16, timeout=1.0)

