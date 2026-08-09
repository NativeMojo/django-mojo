"""
The upload flow: register -> presigned PUT -> complete.

Presigning is faked at the module boundary (`s3.presigned_put_url`) — it is
pure local signing with no request contract to honour. HeadObject is faked one
level DOWN, at the boto client (`s3.S3._client`), so the REAL `s3.head_object`
helper runs: its request parameters are the regression surface for #1770.
Real S3 returns `ChecksumSHA256` from HeadObject only when the request passes
`ChecksumMode="ENABLED"`; a double that returns the checksum unconditionally
cannot catch the helper forgetting to ask, which is exactly how the release
plane shipped unable to verify anything.

These are in-process service-level tests, so `mock.patch` reaches them (the
rule about patches not reaching the server applies to `opts.client` calls,
which this file does not make — `10_promote_rollback.py` covers the wire).

The property that matters most here: **`complete` verifies, it does not trust.**
A CI job that half-failed can still call it, and the client asserting "I am
done" is not evidence.
"""

from unittest import mock

from botocore.exceptions import ClientError
from testit import helpers as th

from tests.test_edge._helpers import (
    declare_pools,
    cleanup, declare_release_buckets, make_manifest,
    make_group, make_webapp, raises,
)


class FakeS3:
    """A fake S3 keyspace, shared by a test through `store`.

    Presigning is patched as the MODULE function rather than `S3Item`,
    matching the real seam: presigning deliberately does not go through
    `S3Item`, whose constructor would issue a HeadObject per file.
    """

    store = {}

    @staticmethod
    def presigned_put_url(bucket, key, expires=3600, sha256_b64=None,
                          content_length=None):
        return (f"https://{bucket}.s3.example/{key}"
                f"?sig=1&len={content_length}&ck={sha256_b64}")


class FakeS3Client:
    """The HeadObject half of real S3's contract, at the boto-client seam.

    `ChecksumSHA256` appears in the response ONLY when the request passes
    `ChecksumMode="ENABLED"` — like real S3, and unlike the module-level fake
    this replaced, which returned the stored checksum no matter what was
    asked and so could never catch #1770.
    """

    def head_object(self, Bucket=None, Key=None, **kwargs):
        entry = FakeS3.store.get(Key)
        if entry is None:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        head = {k: v for k, v in entry.items() if k != "ChecksumSHA256"}
        if kwargs.get("ChecksumMode") == "ENABLED" and "ChecksumSHA256" in entry:
            head["ChecksumSHA256"] = entry["ChecksumSHA256"]
        return head


def _put(key, sha256_hex=None, size=None, checksum=True):
    """Pretend CI uploaded an object."""
    from mojo.apps.edge.services.releases import hex_to_b64

    entry = {"ContentLength": size}
    if checksum and sha256_hex:
        entry["ChecksumSHA256"] = hex_to_b64(sha256_hex)
    FakeS3.store[key] = entry


@th.django_unit_setup()
def setup_release_upload(opts):
    cleanup()
    declare_pools()
    declare_release_buckets()
    opts.group = make_group("edgeupload")
    opts.webapp = make_webapp(opts.group, slug="uploads")
    FakeS3.store = {}


def _fake_s3():
    from contextlib import ExitStack

    from mojo.helpers.aws import s3

    stack = ExitStack()
    stack.enter_context(mock.patch(
        "mojo.apps.edge.services.releases.s3.presigned_put_url",
        FakeS3.presigned_put_url))
    stack.enter_context(mock.patch.object(s3.S3, "_client", FakeS3Client()))
    return stack


@th.django_unit_test("register mints exactly one upload URL per declared file")
def test_register_mints_one_url_per_file(opts):
    """A presigned PUT permits ONE key. That is the property chosen over an
    STS session policy, which would allow arbitrary keys under a prefix."""
    from mojo.apps.edge.services import releases

    manifest = make_manifest(["index.html", "app.js", "assets/logo.svg"])
    with _fake_s3():
        release, uploads = releases.register(opts.webapp, "v1", manifest)

    assert release.status == "pending", \
        f"a freshly registered release must be pending, got {release.status}"
    assert len(uploads) == 3, f"expected 3 upload URLs, got {len(uploads)}"

    paths = {u["path"] for u in uploads}
    assert paths == {"index.html", "app.js", "assets/logo.svg"}, \
        f"upload URLs do not match the manifest: {paths}"

    for upload in uploads:
        assert upload["path"] in upload["url"], \
            f"an upload URL is not bound to its own key: {upload}"
        assert upload["headers"]["x-amz-checksum-sha256"], \
            "no checksum header was returned — S3 could not reject a bad body"


@th.django_unit_test("the checksum is converted from hex to base64")
def test_checksum_encoding(opts):
    """S3 wants base64; a manifest carries hex. Passing hex through produces a
    signature no upload can ever satisfy — this is where the scheme fails first."""
    import base64

    from mojo.apps.edge.services import releases

    digest = "a" * 64
    encoded = releases.hex_to_b64(digest)
    assert encoded == base64.b64encode(bytes.fromhex(digest)).decode(), \
        f"hex_to_b64 is wrong: {encoded}"
    assert encoded != digest, "the checksum was passed through as hex"


@th.django_unit_test("an identical immutable version is safely reusable")
def test_version_is_immutable(opts):
    from mojo.apps.edge.services import releases

    with _fake_s3():
        first, _ = releases.register(opts.webapp, "dup1", make_manifest())
        second, uploads = releases.register(
            opts.webapp, "dup1", make_manifest())
        err = raises(
            releases.register, opts.webapp, "dup1", make_manifest(["other.js"]))
    assert second.pk == first.pk, "an identical rerun created another release row"
    assert uploads, "a pending rerun did not receive fresh upload URLs"
    assert err is not None, "the same version accepted a different manifest"


@th.django_unit_test("complete FAILS when the declared files are not in S3")
def test_complete_requires_the_files(opts):
    """The workspec's first test: registering a release whose files are absent
    must not produce a promotable row."""
    from mojo.apps.edge.services import releases

    with _fake_s3():
        release, uploads = releases.register(
            opts.webapp, "missing1", make_manifest(["index.html"]))
        err = raises(releases.complete, release)

    assert err is not None, "complete accepted a release with no uploaded files"
    release.refresh_from_db()
    assert release.status == "pending", \
        f"a failed completion left the release {release.status}, not pending"
    assert not release.is_promotable, "an unverified release is promotable"


@th.django_unit_test("complete FAILS on a checksum mismatch")
def test_complete_rejects_bad_checksum(opts):
    from mojo.apps.edge.services import releases

    manifest = make_manifest(["index.html"])
    with _fake_s3():
        release, _ = releases.register(opts.webapp, "badck", manifest)
        prefix = release.storage_prefix()
        # Right size, wrong content.
        _put(f"{prefix}/index.html", sha256_hex="b" * 64,
             size=manifest[0]["size"])
        err = raises(releases.complete, release)

    assert err is not None, "complete accepted an object whose checksum differs"


@th.django_unit_test("complete FAILS when an object has NO stored checksum")
def test_complete_rejects_missing_checksum(opts):
    """An object written without a checksum bypassed the bound presigned URL.
    Treating absent as 'fine' would remove the verification entirely."""
    from mojo.apps.edge.services import releases

    manifest = make_manifest(["index.html"])
    with _fake_s3():
        release, _ = releases.register(opts.webapp, "nock", manifest)
        prefix = release.storage_prefix()
        _put(f"{prefix}/index.html", sha256_hex=manifest[0]["sha256"],
             size=manifest[0]["size"], checksum=False)
        err = raises(releases.complete, release)

    assert err is not None, \
        "complete accepted an object with no stored checksum"


@th.django_unit_test("head_object asks S3 for the checksum complete verifies")
def test_head_object_requests_checksum_mode(opts):
    """The regression for #1770: S3 omits `ChecksumSHA256` from HeadObject
    unless the request passes `ChecksumMode="ENABLED"`. A helper that forgets
    the parameter reads back None for every object, so `complete` rejects
    every correctly-uploaded release — the check cannot pass by construction.
    """
    from mojo.helpers.aws import s3

    seen = {}

    class RecordingClient:
        def head_object(self, **kwargs):
            seen.update(kwargs)
            return {"ContentLength": 1}

    with mock.patch.object(s3.S3, "_client", RecordingClient()):
        s3.head_object("bucket", "key")

    assert seen.get("ChecksumMode") == "ENABLED", (
        f"head_object sent ChecksumMode={seen.get('ChecksumMode')!r} — "
        "without ChecksumMode=ENABLED, S3 omits ChecksumSHA256 and no "
        "release can ever verify")


@th.django_unit_test("complete FAILS on a size mismatch")
def test_complete_rejects_bad_size(opts):
    from mojo.apps.edge.services import releases

    manifest = make_manifest(["index.html"])
    with _fake_s3():
        release, _ = releases.register(opts.webapp, "badsize", manifest)
        prefix = release.storage_prefix()
        _put(f"{prefix}/index.html", sha256_hex=manifest[0]["sha256"],
             size=manifest[0]["size"] + 99)
        err = raises(releases.complete, release)

    assert err is not None, "complete accepted an object of the wrong size"


@th.django_unit_test("a fully uploaded release verifies and becomes uploaded")
def test_complete_happy_path(opts):
    from mojo.apps.edge.services import releases

    manifest = make_manifest(["index.html", "app.js"])
    with _fake_s3():
        release, _ = releases.register(opts.webapp, "good1", manifest)
        prefix = release.storage_prefix()
        for entry in manifest:
            _put(f"{prefix}/{entry['path']}", sha256_hex=entry["sha256"],
                 size=entry["size"])
        releases.complete(release)

    release.refresh_from_db()
    assert release.status == "uploaded", \
        f"a verified release is {release.status}, not uploaded"
    assert release.is_promotable, "a verified release is not promotable"


@th.django_unit_test("complete names the failing paths")
def test_complete_reports_which_files(opts):
    """A CI job that half-failed should learn WHICH objects are missing."""
    from mojo.apps.edge.services import releases

    manifest = make_manifest(["index.html", "app.js", "missing.css"])
    with _fake_s3():
        release, _ = releases.register(opts.webapp, "partial", manifest)
        prefix = release.storage_prefix()
        for entry in manifest[:2]:
            _put(f"{prefix}/{entry['path']}", sha256_hex=entry["sha256"],
                 size=entry["size"])
        err = raises(releases.complete, release)

    assert err is not None, "a partial upload verified"
    assert "missing.css" in str(err), \
        f"the error does not name the failing path: {err}"


@th.django_unit_test("completing an already verified release is idempotent")
def test_complete_is_not_repeatable(opts):
    from mojo.apps.edge.services import releases

    manifest = make_manifest(["index.html"])
    with _fake_s3():
        release, _ = releases.register(opts.webapp, "twice", manifest)
        prefix = release.storage_prefix()
        _put(f"{prefix}/index.html", sha256_hex=manifest[0]["sha256"],
             size=manifest[0]["size"])
        releases.complete(release)
        again = releases.complete(release)

    assert again.pk == release.pk, "idempotent completion changed the release"
    assert again.status == "uploaded", "idempotent completion reopened the release"


@th.django_unit_test("uploads land under the site's DERIVED prefix")
def test_uploads_are_prefix_scoped(opts):
    """`prefix` is derived from group and id, never caller-supplied, so one
    site's upload URLs cannot address another site's objects."""
    from mojo.apps.edge.services import releases

    other = make_webapp(opts.group, slug="otherapp")
    with _fake_s3():
        release, uploads = releases.register(
            opts.webapp, "scoped", make_manifest(["index.html"]))

    expected = f"webapps/{opts.group.pk}/{opts.webapp.pk}/releases/scoped"
    assert release.storage_prefix() == expected, \
        f"release prefix is not derived: {release.storage_prefix()}"
    assert other.prefix not in uploads[0]["url"], \
        "an upload URL addressed another site's prefix"
