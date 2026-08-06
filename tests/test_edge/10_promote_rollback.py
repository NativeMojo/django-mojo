"""
Promote, rollback, and the permission split that is the point of the item.

`release_webapp` is what CI holds and reaches `uploaded`. `manage_webapp` is
what a human holds and reaches `live`. With CI writing a `current` pointer in
S3, the credential a web developer's pipeline holds could make any build live —
that is the problem this separation exists to remove, so it is asserted on the
wire rather than described in a comment.

The revocation invariant has its own test because it is an ABSENCE of coupling:
disabling a site's key must stop future releases and must not change what any
node is serving.
"""

from testit import helpers as th

from tests.test_edge._helpers import (
    declare_pools,
    cleanup, declare_release_buckets, declare_reserved_names, login,
    make_certificate, make_domain, make_group, make_manifest, make_release,
    make_user, make_vhost, make_webapp,
)


@th.django_unit_setup()
def setup_promote(opts):
    from mojo.apps.account.models import ApiKey, GroupMember

    cleanup()
    ApiKey.objects.filter(name__startswith="webapp:").delete()
    declare_reserved_names()
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


@th.django_unit_test("auto_promote=False means a verified upload does NOT go live")
def test_auto_promote_off(opts):
    from mojo.apps.edge.services import releases

    assert not opts.webapp.auto_promote, "the fixture should default to off"
    release = make_release(opts.webapp, "manual1", status="uploaded")
    promoted = releases.maybe_auto_promote(release)

    assert promoted is None, "a release auto-promoted on a site that opted out"
    opts.webapp.refresh_from_db()
    assert opts.webapp.current_release_id != release.pk, \
        "the release went live without an explicit promote"


@th.django_unit_test("auto_promote=True promotes on verification")
def test_auto_promote_on(opts):
    from mojo.apps.edge.services import releases

    site = make_webapp(opts.group, slug="autosite", auto_promote=True)
    release = make_release(site, "auto1", status="uploaded")
    releases.maybe_auto_promote(release)

    site.refresh_from_db()
    assert site.current_release_id == release.pk, \
        "a site that opted into auto_promote did not go live"


@th.django_unit_test("manage_dns alone cannot promote")
def test_promote_needs_manage_webapp(opts):
    """The whole point of the item: uploading and promoting are different
    permissions. A web-dev-scoped credential must not reach `live`."""
    login(opts, opts.dnsonly_email, opts.dnsonly_pw)
    resp = opts.client.post("/api/edge/webapp/promote", json=dict(
        webapp=opts.webapp.pk, release=opts.v1.pk))
    assert resp.status_code in (401, 403), (
        "a manage_dns holder promoted a release without manage_webapp "
        f"(status {resp.status_code})")


@th.django_unit_test("a site's CI key cannot promote")
def test_ci_key_cannot_promote(opts):
    """A permission test, not a policy comment — the workspec asked for exactly
    this. The key carries `release_webapp` and nothing else."""
    from mojo.apps.account.models import ApiKey

    key, token = ApiKey.create_for_group(
        opts.group, "webapp:citest", permissions={"release_webapp": True})
    opts.webapp.api_key = key
    opts.webapp.save()

    _use_apikey(opts, token)
    try:
        resp = opts.client.post("/api/edge/webapp/promote", json=dict(
            webapp=opts.webapp.pk, release=opts.v1.pk))
        assert resp.status_code in (401, 403), (
            "a site's CI credential promoted a release — upload and promote "
            f"are supposed to be different permissions (status {resp.status_code})")
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

    resp = opts.client.post("/api/edge/webapp/link_key", json=dict(
        webapp=site.pk))
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

    # Rotation is a hard cutover: no grace window, because two live credentials
    # for one site is the state that makes revocation unprovable.
    first_key_id = site.api_key_id
    resp = opts.client.post("/api/edge/webapp/link_key", json=dict(
        webapp=site.pk))
    assert resp.status_code == 200, f"re-linking failed: {resp.body}"
    old = ApiKey.objects.get(pk=first_key_id)
    assert not old.is_active, \
        "the previous CI credential is still active after rotation"


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
