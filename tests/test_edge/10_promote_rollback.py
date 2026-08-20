"""Verified release deployment, automated rollback, and key isolation.

GitHub's WebApp-linked key completes verification and that completion always
starts deployment. There is no separate human promotion endpoint.

The revocation invariant has its own test because it is an ABSENCE of coupling:
disabling a site's key must stop future releases and must not change what any
node is serving.
"""

import uuid
import time

from testit import helpers as th

from tests.test_edge._helpers import (
    declare_pools,
    cleanup, declare_release_buckets, login,
    make_certificate, make_domain, make_group, make_manifest, make_release,
    make_user, make_vhost, make_webapp,
)


@th.django_unit_setup()
def setup_promote(opts):
    from mojo.apps.account.models import ApiKey, GroupMember

    cleanup()
    ApiKey.objects.filter(name__startswith="webapp:").delete()
    declare_pools()
    declare_release_buckets()

    opts.group = make_group("edgepromote")
    opts.domain = make_domain(group=opts.group)
    opts.certificate = make_certificate(opts.domain)
    opts.vhost = make_vhost(opts.domain, opts.certificate, label="www")
    opts.webapp = make_webapp(opts.group, slug="promoted", vhost=opts.vhost)

    opts.v1 = make_release(opts.webapp, "v1", status="uploaded")
    opts.v2 = make_release(opts.webapp, "v2", status="uploaded")

    opts.admin, opts.admin_email, opts.admin_pw = make_user(
        ["manage_dns", "manage_webapp"], is_superuser=True)
    opts.dnsonly, opts.dnsonly_email, opts.dnsonly_pw = make_user(["manage_dns"])


def _use_apikey(opts, token):
    opts.client.logout()
    opts.client.session.headers["Authorization"] = f"apikey {token}"


def _clear_apikey(opts):
    opts.client.session.headers.pop("Authorization", None)


@th.django_unit_test("promotion and rollback are the same operation")
def test_promote_and_rollback(opts):
    from mojo.apps.edge.services import releases

    releases.promote(opts.webapp, opts.v1)
    opts.webapp.refresh_from_db()
    opts.v1.refresh_from_db()
    assert opts.webapp.current_release_id == opts.v1.pk, "v1 did not go live"
    assert opts.v1.status == "live", f"v1 is {opts.v1.status}, not live"

    releases.promote(opts.webapp, opts.v2)
    opts.webapp.refresh_from_db()
    opts.v1.refresh_from_db()
    opts.v2.refresh_from_db()
    assert opts.webapp.current_release_id == opts.v2.pk, "v2 did not go live"
    assert opts.v1.status == "superseded", \
        f"the previous release is {opts.v1.status}, not superseded"

    # Rollback: the SAME call, an older id.
    releases.promote(opts.webapp, opts.v1)
    opts.webapp.refresh_from_db()
    opts.v1.refresh_from_db()
    opts.v2.refresh_from_db()
    assert opts.webapp.current_release_id == opts.v1.pk, "rollback did not take"
    assert opts.v1.status == "live", "the rolled-back release is not live"
    assert opts.v2.status == "superseded", \
        f"the rolled-past release is {opts.v2.status}, not superseded"


@th.django_unit_test("a PENDING release cannot be promoted")
def test_pending_cannot_be_promoted(opts):
    from mojo.apps.edge.services import releases
    from tests.test_edge._helpers import raises

    pending = make_release(opts.webapp, "pending1", status="pending")
    err = raises(releases.promote, opts.webapp, pending)
    assert err is not None, (
        "an unverified release was promoted — an abandoned CI run would go "
        "live and 404 in production")


@th.django_unit_test("a release cannot be promoted onto another site")
def test_cross_site_promote_refused(opts):
    from mojo.apps.edge.services import releases
    from tests.test_edge._helpers import raises

    other = make_webapp(opts.group, slug="othersite")
    err = raises(releases.promote, other, opts.v1)
    assert err is not None, "a release was promoted onto a different site"


@th.django_unit_test("verified completion always starts deployment")
def test_verified_release_deploys(opts):
    from mojo.apps.edge.services import releases

    release = make_release(opts.webapp, "github1", status="uploaded")
    deployment = releases.deploy_verified(release)

    opts.webapp.refresh_from_db()
    assert deployment.release_id == release.pk, "completion deployed another release"
    assert opts.webapp.current_release_id == release.pk, \
        "verified completion did not start deployment"


@th.django_unit_test("promotion has no endpoint and rollback stays human-only")
def test_no_manual_promote_endpoint(opts):
    # Forward promotion is still not an endpoint: deployment starts only from
    # release completion.
    login(opts, opts.dnsonly_email, opts.dnsonly_pw)
    resp = opts.client.post("/api/edge/webapp/promote", json=dict(
        webapp=opts.webapp.pk, release=opts.v1.pk))
    assert resp.status_code == 404, (
        "manual promotion endpoint still exists; deployment must start only "
        f"from release completion (status {resp.status_code})")

    # Rollback (item 2099) IS an endpoint, but a CI key cannot drive it — the
    # invariant this module protects is that automation cannot start a
    # deployment out of band, and denies_key_backed_session keeps that intact.
    from mojo.apps.edge.services import webapp_keys

    _, _, token, _ = webapp_keys.link(opts.webapp)
    _use_apikey(opts, token)
    try:
        machine = opts.client.post("/api/edge/webapp/rollback", json=dict(
            webapp=opts.webapp.pk, release=opts.v1.pk))
        assert machine.status_code == 403, (
            "a CI deployment key drove a rollback; automation must never start a "
            f"deployment out of band (status {machine.status_code})")
    finally:
        _clear_apikey(opts)


@th.django_unit_test("a key cannot register a release for ANOTHER site")
def test_cross_site_key_refused(opts):
    """The comment's open question. With `WebApp.api_key` a OneToOne, the check
    is an integer comparison rather than a JSON permissions lookup."""
    from mojo.apps.account.models import ApiKey

    key, token = ApiKey.create_for_group(
        opts.group, "webapp:sitea", permissions={"release_webapp": True})
    opts.webapp.api_key = key
    opts.webapp.save()

    victim = make_webapp(opts.group, slug="victimsite")

    _use_apikey(opts, token)
    try:
        resp = opts.client.post("/api/edge/release", json=dict(
            webapp=victim.pk, version="v9", manifest=make_manifest()))
        assert resp.status_code in (401, 403, 404), (
            "site A's key registered a release for site B "
            f"(status {resp.status_code})")
    finally:
        _clear_apikey(opts)


@th.django_unit_test("an UNLINKED site refuses every key — fail closed on null")
def test_unlinked_site_refuses_keys(opts):
    """A falsy-tolerant comparison would turn `api_key_id is None` into "any
    key matches", which is the opposite of the intent."""
    from mojo.apps.account.models import ApiKey

    key, token = ApiKey.create_for_group(
        opts.group, "webapp:orphan", permissions={"release_webapp": True})
    unlinked = make_webapp(opts.group, slug="unlinkedsite")
    assert unlinked.api_key_id is None, "the fixture should have no key"

    _use_apikey(opts, token)
    try:
        resp = opts.client.post("/api/edge/release", json=dict(
            webapp=unlinked.pk, version="v1", manifest=make_manifest()))
        assert resp.status_code in (401, 403, 404), (
            "a site with NO linked key accepted a release from an arbitrary "
            f"key (status {resp.status_code})")
    finally:
        _clear_apikey(opts)


@th.django_unit_test("link_key mints a release-only credential and revokes the old one")
def test_link_key(opts):
    from mojo.apps.account.models import ApiKey

    site = make_webapp(opts.group, slug="keyedsite")
    login(opts, opts.admin_email, opts.admin_pw)

    mint_operation = str(uuid.uuid4())
    resp = opts.client.post("/api/edge/webapp/link_key", json=dict(
        webapp=site.pk, action="mint", operation_id=mint_operation))
    assert resp.status_code == 200, \
        f"link_key failed: {resp.status_code} {resp.body}"
    first_token = (resp.json.get("data") or {}).get("token")
    assert first_token, f"link_key returned no token: {resp.json}"

    site.refresh_from_db()
    assert site.api_key_id, "the key was not linked"
    key = ApiKey.objects.get(pk=site.api_key_id)
    assert key.has_permission("release_webapp"), \
        "the minted key cannot register releases"
    assert not key.has_permission("manage_webapp"), \
        "the minted CI key can PROMOTE — that defeats the whole split"
    assert key.get_token() is None, \
        "the reveal-once deployment token remained recoverable at rest"

    # A transport retry is safe and cannot reveal the token twice or create a
    # second key. The operator must rotate if the first response was lost.
    resp = opts.client.post("/api/edge/webapp/link_key", json=dict(
        webapp=site.pk, action="mint", operation_id=mint_operation))
    replay = resp.json.get("data") or {}
    assert resp.status_code == 200 and replay.get("replayed") is True, \
        f"operation replay was not recognized: {resp.body}"
    assert replay.get("token") is None, "a replay disclosed the token again"
    assert ApiKey.objects.filter(name="webapp:keyedsite").count() == 1, \
        "an idempotent replay created a duplicate deployment key"

    # Rotation is a hard cutover: no grace window, because two live credentials
    # for one site is the state that makes revocation unprovable.
    first_key_id = site.api_key_id
    resp = opts.client.post("/api/edge/webapp/link_key", json=dict(
        webapp=site.pk, action="rotate", operation_id=str(uuid.uuid4())))
    assert resp.status_code == 200, f"re-linking failed: {resp.body}"
    old = ApiKey.objects.get(pk=first_key_id)
    assert not old.is_active, \
        "the previous CI credential is still active after rotation"


@th.django_unit_test("deployment-key revoke is explicit, replay-safe, and unlinks the WebApp")
def test_revoke_linked_key(opts):
    from mojo.apps.account.models import ApiKey

    site = make_webapp(opts.group, slug="revokekey")
    login(opts, opts.admin_email, opts.admin_pw)
    minted = opts.client.post("/api/edge/webapp/link_key", json=dict(
        webapp=site.pk, action="mint", operation_id=str(uuid.uuid4())))
    assert minted.status_code == 200, f"mint failed: {minted.body}"
    site.refresh_from_db()
    key_id = site.api_key_id

    operation_id = str(uuid.uuid4())
    revoked = opts.client.post("/api/edge/webapp/revoke_key", json=dict(
        webapp=site.pk, operation_id=operation_id))
    assert revoked.status_code == 200, f"revoke failed: {revoked.body}"
    site.refresh_from_db()
    assert site.api_key_id is None, "revoke left the credential linked"
    assert ApiKey.objects.get(pk=key_id).is_active is False, \
        "revoke left the credential active"

    replay = opts.client.post("/api/edge/webapp/revoke_key", json=dict(
        webapp=site.pk, operation_id=operation_id))
    data = replay.json.get("data") or {}
    assert replay.status_code == 200 and data.get("replayed") is True, \
        "repeated revoke was not served from its receipt"


@th.django_unit_test("deployment-key mutation refuses machine sessions and stale logins")
def test_link_key_requires_recent_interactive_auth(opts):
    from mojo.apps.account.utils.jwtoken import JWToken
    from mojo.apps.edge.services import webapp_keys

    site = make_webapp(opts.group, slug="keysessiongate")
    _, _, machine_token, _ = webapp_keys.link(site)
    _use_apikey(opts, machine_token)
    try:
        machine = opts.client.post("/api/edge/webapp/link_key", json=dict(
            webapp=site.pk, action="rotate", operation_id=str(uuid.uuid4())))
        assert machine.status_code == 403, \
            f"deployment API key rotated itself ({machine.status_code})"
    finally:
        _clear_apikey(opts)

    opts.client.is_authenticated = True
    opts.client.bearer = "bearer"
    opts.client.access_token = JWToken(opts.admin.get_auth_key()).create_access_token(
        uid=opts.admin.pk, auth_time=int(time.time()) - 601)
    stale = opts.client.post("/api/edge/webapp/link_key", json=dict(
        webapp=site.pk, action="rotate", operation_id=str(uuid.uuid4())))
    assert stale.status_code == 440, \
        f"stale interactive login bypassed ten-minute step-up ({stale.status_code})"


@th.django_unit_test("revoking a site's key stops releases and changes nothing served")
def test_revocation_does_not_affect_serving(opts):
    """The invariant the whole design is for: a compromised web-dev credential
    is contained by disabling one key, with no site going down.

    Desired state is driven by `WebApp.current_release`, which has no
    dependency on the key — an absence of coupling, so it gets a test.
    """
    from mojo.apps.account.models import ApiKey
    from mojo.apps.edge.services import releases

    key, token = ApiKey.create_for_group(
        opts.group, "webapp:revoked", permissions={"release_webapp": True})
    opts.webapp.api_key = key
    opts.webapp.save()
    releases.promote(opts.webapp, opts.v1)

    before = releases.desired_webapps([opts.vhost])
    assert before, "the site is not in the desired state to begin with"

    key.is_active = False
    key.save()

    _use_apikey(opts, token)
    try:
        resp = opts.client.post("/api/edge/release", json=dict(
            webapp=opts.webapp.pk, version="afterrevoke",
            manifest=make_manifest()))
        assert resp.status_code in (401, 403, 404), (
            "a revoked key still registered a release "
            f"(status {resp.status_code})")
    finally:
        _clear_apikey(opts)

    after = releases.desired_webapps([opts.vhost])
    assert after == before, (
        "revoking the CI key changed what nodes should be serving — revocation "
        "is supposed to be containable without taking the site down")


@th.django_unit_test("a promote moves the generation id")
def test_promote_moves_the_generation(opts):
    """If the hash did not cover webapps, a promote would never reach a node."""
    from mojo.apps.edge.rest.node import enabled_vhosts
    from mojo.apps.edge.services import releases, render

    def generation():
        vhosts = enabled_vhosts("default")
        return render.desired_state(
            vhosts, webapps=releases.desired_webapps(vhosts))["generation"]

    releases.promote(opts.webapp, opts.v1)
    first = generation()
    releases.promote(opts.webapp, opts.v2)
    second = generation()

    assert first != second, \
        "promoting a different release did not move the generation id"


@th.django_unit_test("an interactive session with the WebApp's SAVE_PERMS drives all three release calls")
def test_session_release_register_complete_status(opts):
    """The portal's Upload-a-build tab uses the SAME endpoints CI does, on a
    logged-in session: register -> complete -> deployment status. A holder of
    the WebApp's SAVE_PERMS (manage_dns) passes _authorized_webapp on all
    three with no deploy key linked.

    Register runs the real presign path against dummy AWS credentials (pure
    local signing — the 12_security_review CI happy path's exact pattern).
    Complete cannot HeadObject offline, so it exercises the idempotent
    short-circuit: a release already `uploaded` completes without S3 and
    starts deployment.
    """
    import base64

    from tests.test_edge._helpers import make_group_member

    site = make_webapp(opts.group, slug="sessionrel")
    # A group MEMBER holding manage_dns on the site's own group — the scoped
    # portal admin, not a global grant and not a superuser.
    _, member_email, member_pw, _ = make_group_member(
        ["manage_dns"], group=opts.group)
    login(opts, member_email, member_pw)

    manifest = make_manifest(["index.html", "app.js"])
    with th.server_settings(AWS_KEY="AKIAEDGETESTKEY00000",
                            AWS_SECRET="edge-test-secret-not-real",
                            AWS_REGION="us-west-2"):
        resp = opts.client.post("/api/edge/release", json=dict(
            webapp=site.pk, version="sess1", manifest=manifest))

    assert resp.status_code == 200, (
        "a manage_dns session could not register a release by hand: "
        f"{resp.status_code} {resp.body}")
    data = resp.json.get("data") or {}
    assert data.get("status") == "pending", f"the release is not pending: {data}"
    uploads = data.get("uploads") or []
    assert {u["path"] for u in uploads} == {"index.html", "app.js"}, \
        f"register did not mint one upload per manifest file: {uploads}"
    expected = base64.b64encode(bytes.fromhex(manifest[0]["sha256"])).decode()
    first = next(u for u in uploads if u["path"] == "index.html")
    assert first["headers"]["x-amz-checksum-sha256"] == expected, (
        "the response headers do not carry the base64 checksum the signed "
        f"URL binds: {first['headers']}")

    # Complete an already-verified release: same endpoint, no S3 round trip.
    verified = make_release(site, "sess2", status="uploaded")
    completed = opts.client.post("/api/edge/release/complete", json=dict(
        release=verified.pk))
    assert completed.status_code == 200, (
        "a manage_dns session could not complete a verified release: "
        f"{completed.status_code} {completed.body}")
    completed_data = completed.json.get("data") or {}
    deployment_id = completed_data.get("deployment")
    assert deployment_id, f"completion did not start a deployment: {completed_data}"

    status = opts.client.get(f"/api/edge/release/deployment/{deployment_id}")
    assert status.status_code == 200, (
        "a manage_dns session could not read its own deployment status: "
        f"{status.status_code} {status.body}")
    status_data = status.json.get("data") or {}
    assert status_data.get("webapp") == site.pk, \
        f"the deployment status is for another site: {status_data}"
    assert "terminal" in status_data and "success" in status_data, \
        f"the status payload lost the fields the portal polls on: {status_data}"


@th.django_unit_test("a session WITHOUT the WebApp's perms gets 404 from all three release calls")
def test_session_without_perms_is_refused(opts):
    site = make_webapp(opts.group, slug="sessdenied")
    verified = make_release(site, "denied1", status="uploaded")
    from mojo.apps.edge.services import releases

    deployment = releases.deploy_verified(
        make_release(site, "denied2", status="uploaded"))

    nobody, nobody_email, nobody_pw = make_user([])
    login(opts, nobody_email, nobody_pw)

    register = opts.client.post("/api/edge/release", json=dict(
        webapp=site.pk, version="deniedreg", manifest=make_manifest()))
    assert register.status_code == 404, (
        "a permission-less session registered a release "
        f"(status {register.status_code}) — not_found() must hide the site")

    complete = opts.client.post("/api/edge/release/complete", json=dict(
        release=verified.pk))
    assert complete.status_code == 404, (
        "a permission-less session completed a release "
        f"(status {complete.status_code})")

    status = opts.client.get(f"/api/edge/release/deployment/{deployment.pk}")
    assert status.status_code == 404, (
        "a permission-less session read a deployment's fleet status "
        f"(status {status.status_code})")


@th.django_unit_test("status is not writable over REST")
def test_status_is_not_a_field_write(opts):
    """Otherwise a manage_dns holder marks a pending release live with a field
    write, bypassing both verification and the manage_webapp gate."""
    pending = make_release(opts.webapp, "fieldwrite", status="pending")
    login(opts, opts.admin_email, opts.admin_pw)

    resp = opts.client.post(f"/api/edge/release/{pending.pk}", json=dict(
        status="live"))
    pending.refresh_from_db()
    assert pending.status == "pending", (
        "a release status was changed by a field write "
        f"(status {resp.status_code}, now {pending.status})")
