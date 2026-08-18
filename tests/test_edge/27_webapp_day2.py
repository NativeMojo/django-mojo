"""WebApp day-2 management: deployment history, rollback, health, safe delete.

The onboarding surface (25_webapp_onboarding) gets a site live; this module is
everything a person does to it afterward. Rollback is a deliberate policy
change from 10_promote_rollback's "no human promote endpoint": it exists, but
it is human-only (`denies_key_backed_session`), so CI keys still cannot start a
deployment out of band.
"""

import uuid
from unittest import mock

from testit import helpers as th

from tests.test_edge._helpers import (
    cleanup, declare_pools, declare_release_buckets, login,
    make_certificate, make_domain, make_group, make_group_member, make_release,
    make_user, make_vhost, make_webapp,
)


@th.django_unit_setup()
def setup_day2(opts):
    from mojo.apps.account.models import ApiKey

    cleanup()
    ApiKey.objects.filter(name__startswith="webapp:").delete()
    declare_pools()
    declare_release_buckets()

    opts.group = make_group("edgeday2")
    opts.domain = make_domain(group=opts.group)
    opts.certificate = make_certificate(opts.domain)
    opts.vhost = make_vhost(opts.domain, opts.certificate, label="www")
    opts.webapp = make_webapp(opts.group, slug="day2app", vhost=opts.vhost)
    opts.v1 = make_release(opts.webapp, "v1", status="uploaded")
    opts.v2 = make_release(opts.webapp, "v2", status="uploaded")

    opts.admin, opts.admin_email, opts.admin_pw = make_user(
        ["manage_dns", "manage_webapp"], is_superuser=True)


def _use_apikey(opts, token):
    opts.client.logout()
    opts.client.session.headers["Authorization"] = f"apikey {token}"


def _clear_apikey(opts):
    opts.client.session.headers.pop("Authorization", None)


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------
@th.django_unit_test("rollback repoints the site and records a deployment")
def test_rollback_repoints_and_deploys(opts):
    from mojo.apps.edge.services import releases

    releases.promote(opts.webapp, opts.v1)
    releases.promote(opts.webapp, opts.v2)
    opts.webapp.refresh_from_db()
    assert opts.webapp.current_release_id == opts.v2.pk, "setup did not reach v2"

    login(opts, opts.admin_email, opts.admin_pw)
    resp = opts.client.post("/api/edge/webapp/rollback", json=dict(
        webapp=opts.webapp.pk, release=opts.v1.pk))
    assert resp.status_code == 200, f"rollback failed: {resp.status_code} {resp.body}"
    data = resp.json.get("data") or {}
    assert data.get("release") == opts.v1.pk, \
        f"rollback payload did not name the target release: {data}"

    opts.webapp.refresh_from_db()
    assert opts.webapp.current_release_id == opts.v1.pk, \
        "rollback did not repoint current_release to the earlier release"


@th.django_unit_test("rollback refuses a PENDING (unverified) release")
def test_rollback_refuses_pending_release(opts):
    pending = make_release(opts.webapp, "pend2099", status="pending")
    login(opts, opts.admin_email, opts.admin_pw)
    resp = opts.client.post("/api/edge/webapp/rollback", json=dict(
        webapp=opts.webapp.pk, release=pending.pk))
    assert resp.status_code >= 400, (
        "an unverified release was rolled back to; an abandoned upload would go "
        f"live (status {resp.status_code})")


@th.django_unit_test("rollback scopes the release id to the target site")
def test_rollback_refuses_cross_site_release(opts):
    other = make_webapp(opts.group, slug="day2other")
    other_release = make_release(other, "otherv1", status="uploaded")
    login(opts, opts.admin_email, opts.admin_pw)
    resp = opts.client.post("/api/edge/webapp/rollback", json=dict(
        webapp=opts.webapp.pk, release=other_release.pk))
    assert resp.status_code == 404, (
        "a release from another site was accepted for rollback; a cross-site "
        f"id must 404, not leak (status {resp.status_code})")


@th.django_unit_test("rollback refuses a CI key-backed session")
def test_rollback_refuses_key_backed_session(opts):
    from mojo.apps.edge.services import webapp_keys

    _, _, token, _ = webapp_keys.link(opts.webapp)
    _use_apikey(opts, token)
    try:
        resp = opts.client.post("/api/edge/webapp/rollback", json=dict(
            webapp=opts.webapp.pk, release=opts.v1.pk))
        assert resp.status_code == 403, (
            "a CI deployment key started a rollback; the human-only gate is the "
            f"whole point of keeping promotion out of automation (status {resp.status_code})")
    finally:
        _clear_apikey(opts)


# ---------------------------------------------------------------------------
# deployment history
# ---------------------------------------------------------------------------
@th.django_unit_test("deployment history is group-scoped, not cross-tenant")
def test_deployment_history_is_group_scoped(opts):
    from mojo.apps.edge.models import WebAppDeployment
    from mojo.apps.edge.services import releases

    releases.promote(opts.webapp, opts.v1)
    ours = WebAppDeployment.objects.filter(webapp=opts.webapp).first()
    assert ours is not None, "promote did not record a deployment"

    foreign = make_group("edgeday2-foreign")
    foreign_domain = make_domain(group=foreign)
    foreign_vhost = make_vhost(
        foreign_domain, make_certificate(foreign_domain), label="www")
    foreign_app = make_webapp(foreign, slug="foreignday2", vhost=foreign_vhost)
    foreign_release = make_release(foreign_app, "fv1", status="uploaded")
    releases.promote(foreign_app, foreign_release)
    foreign_dep = WebAppDeployment.objects.filter(webapp=foreign_app).first()

    member, member_email, member_pw, _ = make_group_member(
        ["manage_dns"], group=opts.group)
    login(opts, member_email, member_pw)

    resp = opts.client.get(f"/api/edge/deployment/{foreign_dep.pk}")
    assert resp.status_code in (401, 403, 404), (
        "a member of one group read another group's deployment "
        f"(status {resp.status_code})")

    own = opts.client.get(f"/api/edge/deployment?webapp={opts.webapp.pk}")
    assert own.status_code == 200, f"own deployment list failed: {own.body}"
    rows = (own.json.get("data") or own.json.get("results") or [])
    assert any(row.get("id") == ours.pk for row in rows), \
        "a member could not see their own site's deployment history"


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------
@th.django_unit_test("health reports not_configured for a site with no address")
def test_health_not_configured_without_vhost(opts):
    site = make_webapp(opts.group, slug="day2novhost")
    login(opts, opts.admin_email, opts.admin_pw)
    resp = opts.client.get(f"/api/edge/webapp/health?webapp={site.pk}")
    assert resp.status_code == 200, f"health failed: {resp.body}"
    data = resp.json.get("data") or {}
    assert data.get("status") == "not_configured", (
        "a site with no serving vhost was not reported as not_configured "
        f"({data.get('status')})")


@th.django_unit_test("health probes a configured address and returns a live/dead verdict")
def test_health_probes_configured_address(opts):
    login(opts, opts.admin_email, opts.admin_pw)
    resp = opts.client.get(f"/api/edge/webapp/health?webapp={opts.webapp.pk}")
    assert resp.status_code == 200, f"health failed: {resp.body}"
    data = resp.json.get("data") or {}
    # The fixture hostname does not resolve, so the real probe returns
    # unhealthy — the point is that a configured site takes the probe branch
    # and never reports not_configured.
    assert data.get("status") in ("healthy", "unhealthy"), (
        "a configured site did not take the probe branch "
        f"({data.get('status')})")
    assert data.get("checked"), "a probed health result carried no checked-at stamp"


# ---------------------------------------------------------------------------
# safe delete + detach
# ---------------------------------------------------------------------------
@th.django_unit_test("deleting a site deactivates its key and removes its vhost")
def test_safe_delete_tears_down_key_and_vhost(opts):
    from mojo.apps.account.models import ApiKey
    from mojo.apps.edge.models import Vhost, WebApp
    from mojo.apps.edge.services import webapp_keys

    domain = make_domain(group=opts.group)
    vhost = make_vhost(domain, make_certificate(domain), label="del")
    site = make_webapp(opts.group, slug="day2delete", vhost=vhost)
    _, key, _, _ = webapp_keys.link(site)
    site.refresh_from_db()
    assert site.api_key_id == key.pk and key.is_active, "setup did not link an active key"

    login(opts, opts.admin_email, opts.admin_pw)
    resp = opts.client.delete(f"/api/edge/webapp/{site.pk}")
    assert resp.status_code == 200, f"delete failed: {resp.status_code} {resp.body}"

    assert not WebApp.objects.filter(pk=site.pk).exists(), "the site row survived delete"
    assert not Vhost.objects.filter(pk=vhost.pk).exists(), \
        "the serving vhost was orphaned, not deleted"
    assert ApiKey.objects.get(pk=key.pk).is_active is False, \
        "the CI deploy key stayed active after its site was deleted"


@th.django_unit_test("a failed row delete rolls the whole teardown back")
def test_safe_delete_is_all_or_nothing(opts):
    from mojo.apps.account.models import ApiKey
    from mojo.apps.edge.models import Vhost, WebApp
    from mojo.apps.edge.services import webapp_keys

    domain = make_domain(group=opts.group)
    vhost = make_vhost(domain, make_certificate(domain), label="atomic")
    site = make_webapp(opts.group, slug="day2atomic", vhost=vhost)
    _, key, _, _ = webapp_keys.link(site)

    # Force the final row delete to raise; teardown (key + vhost) must not
    # commit independently — on_rest_pre_delete runs outside the txn, which is
    # exactly the trap this override closes.
    with mock.patch.object(WebApp, "delete", side_effect=RuntimeError("boom")):
        response = site.on_rest_delete(None)
    assert response.status_code == 400, \
        f"a failed delete did not report an error ({response.status_code})"

    assert WebApp.objects.filter(pk=site.pk).exists(), "the site row vanished on a failed delete"
    assert Vhost.objects.filter(pk=vhost.pk).exists(), \
        "the vhost was deleted despite the transaction rolling back"
    assert ApiKey.objects.get(pk=key.pk).is_active is True, \
        "the key was deactivated despite the transaction rolling back"
    site.refresh_from_db()
    assert site.api_key_id == key.pk, "the key was unlinked despite the rollback"


@th.django_unit_test("detach_address takes a site offline but keeps the app")
def test_detach_address_keeps_app(opts):
    from mojo.apps.edge.models import Vhost, WebApp

    domain = make_domain(group=opts.group)
    vhost = make_vhost(domain, make_certificate(domain), label="detach")
    site = make_webapp(opts.group, slug="day2detach", vhost=vhost)

    login(opts, opts.admin_email, opts.admin_pw)
    resp = opts.client.post("/api/edge/webapp/detach_address", json=dict(webapp=site.pk))
    assert resp.status_code == 200, f"detach failed: {resp.status_code} {resp.body}"

    assert WebApp.objects.filter(pk=site.pk).exists(), "detach deleted the whole app"
    assert not Vhost.objects.filter(pk=vhost.pk).exists(), "detach left the vhost serving"
    site.refresh_from_db()
    assert site.vhost_id is None, "detach left the app linked to a deleted vhost"


# ---------------------------------------------------------------------------
# orchestrate hardening
# ---------------------------------------------------------------------------
@th.django_unit_test("orchestrate no-ops on a deployment deleted mid-flight")
def test_orchestrate_no_ops_on_deleted_deployment(opts):
    from mojo.apps.edge.services import webapp_deploy

    result = webapp_deploy.orchestrate(999_000_999)
    assert result == "superseded:deleted", (
        "a deployment row cascade-deleted with its WebApp must be a superseded "
        f"no-op, not a crashing DoesNotExist (got {result})")
