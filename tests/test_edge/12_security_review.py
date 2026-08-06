"""
Regressions for the post-build security review of items #1433 / #1435.

Every test here corresponds to a finding a fresh-context reviewer produced
against the committed code. They are gathered in one file, rather than scattered
into the modules they touch, so the review's results stay legible as a set — and
so a future reader can see what was nearly shipped.
"""

from testit import helpers as th

from tests.test_edge._helpers import (
    cleanup, declare_pools, declare_release_buckets, declare_reserved_names,
    login, make_certificate, make_domain, make_group, make_manifest,
    make_user, make_vhost, make_webapp, raises,
)


@th.django_unit_setup()
def setup_security_review(opts):
    from mojo.apps.account.models import ApiKey

    cleanup()
    ApiKey.objects.filter(name__startswith="webapp:").delete()
    declare_reserved_names()
    declare_pools()
    declare_release_buckets()

    opts.group = make_group("edgesec")
    opts.domain = make_domain(group=opts.group)
    opts.certificate = make_certificate(opts.domain)

    # A HOUSE domain — group-less, so it is platform property.
    opts.house_domain = make_domain(group=None)
    opts.house_cert = make_certificate(opts.house_domain)
    opts.house_vhost = make_vhost(opts.house_domain, opts.house_cert, label="www")

    opts.manager, opts.manager_email, opts.manager_pw = make_user(["manage_dns"])
    opts.admin, opts.admin_email, opts.admin_pw = make_user(
        ["manage_dns", "manage_webapp"], is_superuser=True)


@th.django_unit_test("the privileged argv cannot be redirected by a DB Setting")
def test_privileged_commands_are_file_settings(opts):
    """CRITICAL regression.

    `settings.get` resolves a DB-backed `Setting` row BEFORE the file setting,
    and `Setting` is REST-writable by any holder of a global `manage_settings`
    or `groups` grant. With `get`, writing
    `EDGE_NGINX_TEST_CMD = ["/bin/sh", "-c", "..."]` globally would have the
    convergence sweep execute it on every node in the fleet, as the user that
    owns EDGE_ROOT and every private key in it. `get_static` never consults the
    database.
    """
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.edge.services import installer, render

    Setting.set("EDGE_NGINX_TEST_CMD", ["/bin/sh", "-c", "touch /tmp/pwned"],
                group=None)
    Setting.set("EDGE_NGINX_RELOAD_CMD", ["/bin/sh", "-c", "touch /tmp/pwned"],
                group=None)
    Setting.set("EDGE_ROOT", "/tmp/attacker-controlled", group=None)
    try:
        argv = installer._nginx_test_argv()
        assert "/bin/sh" not in argv, (
            "a DB Setting redirected the nginx -t command — that is remote "
            f"code execution on every node: {argv}")

        reload_argv = installer._nginx_reload_argv()
        assert "/bin/sh" not in reload_argv, \
            f"a DB Setting redirected the reload command: {reload_argv}"

        assert render.edge_root() != "/tmp/attacker-controlled", (
            "a DB Setting redirected where the installer writes certificate "
            "material")
    finally:
        Setting.remove("EDGE_NGINX_TEST_CMD", group=None)
        Setting.remove("EDGE_NGINX_RELOAD_CMD", group=None)
        Setting.remove("EDGE_ROOT", group=None)


@th.django_unit_test("global manage_dns cannot CREATE a vhost on a house domain")
def test_house_domain_create_is_guarded(opts):
    """The read guard was there; the create path was not.

    A global `manage_dns` holder — the identity that provably cannot READ a
    house vhost — could still mint one, claiming a serving name on a
    platform-owned zone with a valid house certificate.
    """
    login(opts, opts.manager_email, opts.manager_pw)
    resp = opts.client.post("/api/edge/vhost", json=dict(
        domain=opts.house_domain.pk, certificate=opts.house_cert.pk,
        label="phish", kind="spa"))
    assert resp.status_code in (401, 403), (
        "a global manage_dns holder created a vhost on a HOUSE domain "
        f"(status {resp.status_code})")

    from mojo.apps.edge.models import Vhost
    assert not Vhost.objects.filter(
        domain=opts.house_domain, label="phish").exists(), \
        "the house vhost was created despite the refusal"


@th.django_unit_test("a platform admin CAN still create a house vhost")
def test_house_domain_create_allowed_for_admin(opts):
    """The guard must not break the legitimate path."""
    login(opts, opts.admin_email, opts.admin_pw)
    resp = opts.client.post("/api/edge/vhost", json=dict(
        domain=opts.house_domain.pk, certificate=opts.house_cert.pk,
        label="admin1", kind="static"))
    assert resp.status_code == 200, \
        f"a platform admin could not create a house vhost: {resp.status_code} {resp.body}"


@th.django_unit_test("a WebApp cannot be attached to another group's vhost")
def test_webapp_vhost_group_must_match(opts):
    """`vhost` is caller-writable, and the framework's FK-attach gate resolves a
    HOUSE vhost against global permissions. Without this check, a global
    manage_dns holder could attach the platform's own vhost to a web app in a
    group they control, promote a release, and serve their content on the
    platform's hostname.
    """
    from mojo.apps.edge.models import WebApp

    err = raises(
        WebApp.objects.create, group=opts.group, slug="hijack",
        bucket="edge-test-releases", prefix="x", vhost=opts.house_vhost)
    assert err is not None, \
        "a web app was attached to a HOUSE vhost belonging to no group"

    other_group = make_group("edgesecother")
    other_domain = make_domain(group=other_group)
    other_cert = make_certificate(other_domain)
    other_vhost = make_vhost(other_domain, other_cert, label="www")

    err = raises(
        WebApp.objects.create, group=opts.group, slug="crossgroup",
        bucket="edge-test-releases", prefix="x", vhost=other_vhost)
    assert err is not None, (
        "a web app was attached to ANOTHER tenant's vhost — group A's build "
        "output would serve on group B's hostname")


@th.django_unit_test("registering a release re-checks the bucket allowlist")
def test_register_rechecks_bucket(opts):
    """A bucket removed from EDGE_RELEASE_BUCKETS must stop receiving presigned
    writes, not keep them for rows created while it was allowed."""
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.edge.services import releases

    web_app = make_webapp(opts.group, slug="bucketcheck")
    Setting.set("EDGE_RELEASE_BUCKETS", ["some-other-bucket"], group=None)
    try:
        err = raises(releases.register, web_app, "v1", make_manifest())
        assert err is not None, (
            "an upload URL was minted for a bucket that is no longer on the "
            "allowlist")
    finally:
        declare_release_buckets()


@th.django_unit_test("a tenant cannot move a vhost into an undeclared pool")
def test_pool_must_be_declared(opts):
    """`pool` is tenant-writable. An arbitrary value would let a tenant land
    their vhost — and therefore their certificate's private key — on the nodes
    of a pool they invented, including an isolated one."""
    from mojo.apps.edge.models import Vhost

    err = raises(
        Vhost.objects.create, domain=opts.domain, certificate=opts.certificate,
        label="poolhop", kind="static", pool="someone-elses-pool")
    assert err is not None, \
        "a vhost was created in a pool this deployment never declared"


@th.django_unit_test("a key-authenticated release register SUCCEEDS")
def test_key_auth_register_happy_path(opts):
    """The CI happy path had no test, and it 500'd.

    `created_by` was stamped from `request.user`, which for a reference-mode
    ApiKey IS the ApiKey — it has a `pk`, so the obvious truthiness check
    passed and handed a non-User to a ForeignKey. Every existing test asserted
    a REFUSAL, so nothing caught it.
    """
    from mojo.apps.account.models import ApiKey

    web_app = make_webapp(opts.group, slug="cihappy")
    key, token = ApiKey.create_for_group(
        opts.group, "webapp:cihappy", permissions={"release_webapp": True})
    web_app.api_key = key
    web_app.save()

    opts.client.logout()
    opts.client.session.headers["Authorization"] = f"apikey {token}"
    try:
        # `mock.patch` cannot reach the server process, so S3 is given dummy
        # credentials instead. Presigning is pure local signing — boto3 makes
        # no network call — so this exercises the real code path offline. The
        # test project ships no AWS credentials, which is why the endpoint
        # otherwise 500s on "Unable to locate credentials".
        with th.server_settings(AWS_KEY="AKIAEDGETESTKEY00000",
                                AWS_SECRET="edge-test-secret-not-real",
                                AWS_REGION="us-west-2"):
            resp = opts.client.post("/api/edge/release", json=dict(
                webapp=web_app.pk, version="ci1", manifest=make_manifest()))

        assert resp.status_code == 200, (
            "a site's own CI key could not register a release: "
            f"{resp.status_code} {resp.body}")
        data = resp.json.get("data") or {}
        assert data.get("status") == "pending", \
            f"the release is not pending: {data}"

        from mojo.apps.edge.models import WebAppRelease
        release = WebAppRelease.objects.get(pk=data["release"])
        assert release.created_by_id is None, (
            "an ApiKey was stamped into created_by — that FK takes a User, "
            "and a non-User there is a 500")
    finally:
        opts.client.session.headers.pop("Authorization", None)
