"""
WebApp / WebAppRelease models.

Two of these assert the finding the challenge pass produced: `bucket` and
`prefix` must not be caller-controllable, because the API signs uploads with
the platform's own static AWS credentials (`mojo/helpers/aws/s3.py` holds one
global `S3Config`, shared with KMS). A writable `bucket` would let a tenant
holding `manage_dns` name any bucket that key can reach and have us sign writes
into it.
"""

from testit import helpers as th

from tests.test_edge._helpers import (
    cleanup, declare_reserved_names, declare_release_buckets, make_certificate,
    make_domain, make_group, make_release, make_vhost, make_webapp, raises,
    RELEASE_BUCKET,
)


@th.django_unit_setup()
def setup_webapp_models(opts):
    cleanup()
    declare_reserved_names()
    declare_release_buckets()
    opts.group = make_group("edgewebapp")
    opts.other_group = make_group("edgewebother")
    opts.domain = make_domain(group=opts.group)
    opts.certificate = make_certificate(opts.domain)
    opts.vhost = make_vhost(opts.domain, opts.certificate, label="www")


@th.django_unit_test("a web app derives its storage prefix from group and id")
def test_prefix_is_derived(opts):
    web_app = make_webapp(opts.group, slug="site1")

    assert web_app.prefix == f"webapps/{opts.group.pk}/{web_app.pk}", \
        f"prefix is not derived: {web_app.prefix}"
    assert web_app.release_prefix("v1") == f"{web_app.prefix}/releases/v1", \
        f"release prefix is wrong: {web_app.release_prefix('v1')}"


@th.django_unit_test("a bucket outside the allowlist is refused")
def test_bucket_allowlist(opts):
    from mojo.apps.edge.models import WebApp

    err = raises(
        WebApp.objects.create, group=opts.group, slug="badbucket",
        bucket="some-other-companys-bucket", prefix="x")
    assert err is not None, (
        "a web app was created against an undeclared bucket — the API would "
        "sign uploads into it with the platform's own credentials")


@th.django_unit_test("with no declared buckets, registering a site fails closed")
def test_bucket_fail_closed(opts):
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.edge.models import WebApp

    Setting.remove("EDGE_RELEASE_BUCKETS", group=None)
    try:
        err = raises(
            WebApp.objects.create, group=opts.group, slug="noconfig",
            bucket=RELEASE_BUCKET, prefix="x")
        assert err is not None, \
            "a site was registered with no release buckets declared"
    finally:
        declare_release_buckets()


@th.django_unit_test("two tenants may use the same slug")
def test_slug_scoped_per_group(opts):
    """Global uniqueness would let one tenant squat another's slug, and the
    duplicate error would leak that it exists."""
    make_webapp(opts.group, slug="shared")
    err = raises(make_webapp, opts.other_group, slug="shared")
    assert err is None, \
        f"two tenants were blocked from using the same slug: {err}"


@th.django_unit_test("one tenant cannot reuse its own slug")
def test_slug_unique_within_group(opts):
    make_webapp(opts.group, slug="onlyone")
    err = raises(make_webapp, opts.group, slug="onlyone")
    assert err is not None, "a tenant registered the same slug twice"


@th.django_unit_test("a release version is immutable and unique per site")
def test_version_unique(opts):
    from mojo.apps.edge.models import WebAppRelease

    web_app = make_webapp(opts.group, slug="versions")
    make_release(web_app, "abc123")
    err = raises(
        WebAppRelease.objects.create, webapp=web_app, version="abc123",
        manifest=[], status="pending")
    assert err is not None, (
        "a version was re-registered — an older, still-referenced release row "
        "would silently change meaning")


@th.django_unit_test("a version that could climb an S3 prefix is refused")
def test_version_shape(opts):
    from mojo.apps.edge import validators

    for candidate in ["../etc", "a/b", "", ".", "..", "v1;rm", "v1 2", "v1\n"]:
        err = raises(validators.validate_release_version, candidate)
        assert err is not None, \
            f"validate_release_version accepted {candidate!r} — it becomes an object key"

    for candidate in ["v1.2.3", "abc123", "2026-08-06_build.7", "a"]:
        err = raises(validators.validate_release_version, candidate)
        assert err is None, f"a legitimate version was refused: {candidate!r} {err}"


@th.django_unit_test("only a VERIFIED release is promotable")
def test_pending_is_not_promotable(opts):
    """`pending` is what an abandoned CI run leaves behind. A promotable
    pending row is a release that 404s in production."""
    web_app = make_webapp(opts.group, slug="promotable")

    pending = make_release(web_app, "pend1", status="pending")
    assert not pending.is_promotable, "a pending release reported as promotable"

    for status in ("uploaded", "live", "superseded"):
        row = make_release(web_app, f"ok-{status}", status=status)
        assert row.is_promotable, f"a {status} release reported as not promotable"


@th.django_unit_test("a manifest with a traversing path is refused")
def test_manifest_paths(opts):
    from mojo.apps.edge import validators

    good = "a" * 64
    for path in ["../secret", "/etc/passwd", "a/../../b", "a//b", "",
                 "a\nb", "a;b", "a b"]:
        err = raises(validators.validate_manifest,
                     [dict(path=path, sha256=good, size=1)])
        assert err is not None, (
            f"manifest accepted the path {path!r} — a presigned URL is minted "
            f"per path, so that is a write primitive")


@th.django_unit_test("a manifest entry needs a real sha256 and a real size")
def test_manifest_entry_shape(opts):
    from mojo.apps.edge import validators

    good = "a" * 64
    bad_entries = [
        dict(path="index.html", sha256="nope", size=1),
        dict(path="index.html", sha256=good, size=-1),
        dict(path="index.html", sha256=good, size="10"),
        dict(path="index.html", sha256=good),
        dict(path="index.html", size=1),
    ]
    for entry in bad_entries:
        err = raises(validators.validate_manifest, [entry])
        assert err is not None, f"manifest accepted {entry!r}"

    err = raises(validators.validate_manifest, [
        dict(path="a.js", sha256=good, size=1),
        dict(path="a.js", sha256=good, size=1),
    ])
    assert err is not None, "manifest accepted a duplicate path"

    cleaned = validators.validate_manifest(
        [dict(path="index.html", sha256=good.upper(), size=10, extra="ignored")])
    assert cleaned == [dict(path="index.html", sha256=good, size=10)], \
        f"manifest was not normalised: {cleaned}"


@th.django_unit_test("a manifest over the file cap is refused")
def test_manifest_cap(opts):
    from mojo.apps.edge import validators

    good = "a" * 64
    huge = [dict(path=f"f{i}.js", sha256=good, size=1) for i in range(5001)]
    err = raises(validators.validate_manifest, huge)
    assert err is not None, "manifest accepted more files than the cap allows"


@th.django_unit_test("a site with no vhost is registerable but never installed")
def test_vhost_is_optional(opts):
    """D2: the whole CloudFront answer is a nullable FK, not a mode enum."""
    from mojo.apps.edge.services import releases

    web_app = make_webapp(opts.group, slug="cloudfronted", vhost=None)
    release = make_release(web_app, "cf1", status="uploaded")
    releases.promote(web_app, release)

    rows = releases.desired_webapps([opts.vhost])
    assert all(row["slug"] != "cloudfronted" for row in rows), \
        "a site with no vhost appeared in a node's desired state"
