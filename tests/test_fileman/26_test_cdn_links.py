"""CDN delivery links (maestro #8250): resolution, signing, caching, gates.

No AWS is reached. The signer is injected through the backend's config seam
(`cdn_signer=`), because `CdnSigningKey` decrypts through real KMS and the
test environment has none; what is proven is that every link path — File,
FileRendition, shortlink resolve, the token download route — follows the
seam, that a manager without a CDN produces byte-identical output to before,
and that signed links verify against the test's own public key.
"""

import base64
import json
from datetime import datetime, timedelta, timezone

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

PREFIX = "fm_cdn_"
BUCKET = "cdn-links-test-bucket"
OTHER_BUCKET = "cdn-links-plain-bucket"
CDN_HOST = "d1cdnlinks.cloudfront.net"
KEY_ID = "K2CDNLINKSTEST"


def _signer():
    from mojo.apps.fileman.services import cdn
    return cdn.Signer(KEY_ID, cdn.load_private_key(_PEM))


def _public_key():
    from mojo.apps.fileman.services import cdn
    return cdn.load_private_key(_PEM).public_key()


def _make_manager(name, bucket, is_public, **secrets):
    from mojo.apps.fileman.models import FileManager
    fm = FileManager(
        name=name, backend_type="s3",
        backend_url=f"s3://{bucket}/fileman/{name}",
        is_active=True, is_default=False, is_public=is_public)
    fm.save()
    fm.set_secrets({"aws_key": "AKIACDNLINKS", "aws_secret": "cdn-links-secret",
                    "aws_region": "us-east-1", **secrets})
    fm.save()
    return fm


@th.django_unit_setup()
def setup_cdn_links(opts):
    from mojo.apps.account.models import User
    from mojo.apps.fileman.models import FileManager, File, CdnDistribution, CdnSigningKey
    from mojo.apps.fileman.services import cdn

    # Long-lived database — clear leftovers before creating.
    File.objects.filter(file_manager__name__startswith=PREFIX).delete()
    FileManager.objects.filter(name__startswith=PREFIX).delete()
    CdnDistribution.objects.filter(bucket_name__in=[BUCKET, OTHER_BUCKET]).delete()
    CdnSigningKey.objects.filter(key_id=KEY_ID).delete()
    User.objects.filter(username__startswith=PREFIX).delete()

    global _PEM
    _PEM = cdn.generate_private_key_pem()

    opts.superuser = User.objects.create(
        username=f"{PREFIX}super", email=f"{PREFIX}super@example.com", is_superuser=True)
    opts.member = User.objects.create(
        username=f"{PREFIX}member", email=f"{PREFIX}member@example.com", is_superuser=False)
    # A global file admin: not a superuser, holds manage_files.
    opts.file_admin = User.objects.create(
        username=f"{PREFIX}fadmin", email=f"{PREFIX}fadmin@example.com", is_superuser=False,
        permissions={"manage_files": True})

    # The signing key row carries no PEM (no KMS here); the backend seam
    # supplies the signer. key_id is what reaches Key-Pair-Id.
    key = CdnSigningKey(key_id=KEY_ID, public_key_name="mojo-fm-key-test", is_active=True)
    key.save()
    opts.key_id = key.pk
    distribution = CdnDistribution.objects.create(
        bucket_name=BUCKET, distribution_id="E1CDNLINKS", domain_name=CDN_HOST,
        oac_id="E2OAC", key_group_id="K3GROUP", signing_key=key,
        source="provisioned", is_enabled=True, status="deployed")
    opts.distribution_id = distribution.pk

    opts.public_cdn = _make_manager(f"{PREFIX}public", BUCKET, True).pk
    opts.private_cdn = _make_manager(f"{PREFIX}private", BUCKET, False).pk
    opts.optout_cdn = _make_manager(f"{PREFIX}optout", BUCKET, True, cdn="off").pk
    opts.public_plain = _make_manager(f"{PREFIX}plainpub", OTHER_BUCKET, True).pk
    opts.private_plain = _make_manager(f"{PREFIX}plainpriv", OTHER_BUCKET, False).pk


def _manager(pk, signer=True):
    """A fresh manager whose backend carries the injected signer."""
    from mojo.apps.fileman.models import FileManager
    from mojo.apps.fileman.backends.s3 import S3StorageBackend
    fm = FileManager.objects.get(pk=pk)
    fm._backend = S3StorageBackend(fm, cdn_signer=_signer() if signer else None)
    return fm


def _file(fm, filename="idle video.mp4", user=None):
    """A completed File row. The completed state is set directly: the
    S3 backend's mark_as_completed probes the bucket, and no AWS is reachable."""
    import uuid
    from mojo.apps.fileman.models import File
    f = File(filename=filename, content_type="video/mp4", category="video",
             file_size=3, file_manager=fm, user=user,
             upload_status=File.COMPLETED, upload_token=uuid.uuid4().hex)
    f.generate_storage_filename()
    f.save()
    f.refresh_from_db()
    f.file_manager = fm  # keep the backend with the injected signer
    return f


def _verify_signed(url, expected_resource):
    """Rebuild the canned policy and verify the signature with our public key."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from mojo.apps.fileman.services import cdn

    resource, params = cdn.parse_signed_url(url)
    assert_eq(resource, expected_resource, f"signed resource mismatch in {url}")
    for name in ("Expires", "Signature", "Key-Pair-Id"):
        assert_true(name in params, f"signed URL lacks {name}: {url}")
    assert_eq(params["Key-Pair-Id"], KEY_ID, "Key-Pair-Id must be the signing key id")
    expires = int(params["Expires"])
    policy = json.dumps({"Statement": [{"Resource": resource,
                                        "Condition": {"DateLessThan": {"AWS:EpochTime": expires}}}]},
                        separators=(",", ":")).encode()
    signature = base64.b64decode(
        params["Signature"].replace("-", "+").replace("_", "=").replace("~", "/"))
    _public_key().verify(signature, policy, padding.PKCS1v15(), hashes.SHA1())
    return expires


@th.tier("core")
@th.django_unit_test("CDN: a manager without a CDN produces byte-identical links to before")
def test_no_cdn_is_byte_identical(opts):
    pub = _manager(opts.public_plain)
    priv = _manager(opts.private_plain)
    assert_true(not pub.uses_cdn and not priv.uses_cdn,
                "a bucket with no CdnDistribution must not use the CDN")
    key = "fileman/x/a b.mp4"
    assert_eq(pub.backend.get_url(key), pub.backend.get_public_url(key),
              "an unset expiry must still return the raw public S3 URL")
    expected = priv.backend.client.generate_presigned_url(
        "get_object", Params={"Bucket": OTHER_BUCKET, "Key": key}, ExpiresIn=3600)
    actual = priv.backend.get_url(key, 3600)
    # Presigned URLs embed a timestamp; compare everything but the date-bound parts.
    strip = lambda u: {k: v for k, v in dict(p.split("=", 1) for p in u.split("?", 1)[1].split("&")).items()
                       if k not in ("X-Amz-Date", "X-Amz-Signature")}
    assert_eq(actual.split("?")[0], expected.split("?")[0], "presigned resource changed")
    assert_eq(strip(actual), strip(expected), "presigned parameters changed for a non-CDN manager")


@th.tier("core")
@th.django_unit_test("CDN: a public manager hands out unsigned CDN links on every path")
def test_public_manager_unsigned_cdn_links(opts):
    from mojo.apps.fileman.utils.upload import get_download_url
    fm = _manager(opts.public_cdn)
    assert_true(fm.uses_cdn, "the public manager's bucket has an enabled distribution")
    f = _file(fm, filename="idle video*.mp4", user=opts.superuser)
    expected = f"https://{CDN_HOST}/{f.storage_file_path}".replace(" ", "%20").replace("*", "%2A")
    assert_eq(f.get_direct_download_url(), expected,
              "File.get_direct_download_url must be the unsigned CDN URL with the key percent-encoded")
    url = f.url
    if url.startswith("/s/"):
        # Shortlinks are on in this environment: the click must land on the CDN.
        from mojo.apps.shortlink.models import ShortLink
        link = ShortLink.objects.get(code=url.rstrip("/").rsplit("/", 1)[-1])
        link.file = f  # resolve() reads the file's manager; keep the injected backend
        assert_eq(link.resolve(), expected, "a shortlink click must resolve to the CDN URL")
    else:
        assert_eq(url, expected, "File.url must be the CDN URL when shortlinks are off")
    assert_true("?" not in expected, "a public manager's link carries no signature")

    from mojo.apps.fileman.models import FileRendition
    r = FileRendition.objects.create(
        original_file=f, role="thumbnail", filename="thumb.jpg",
        storage_path=f"{fm.root_path}/thumbs/thumb.jpg", content_type="image/jpeg",
        category="image", upload_status=FileRendition.COMPLETED)
    r.original_file = f
    assert_eq(r.get_direct_download_url(), f"https://{CDN_HOST}/{fm.root_path}/thumbs/thumb.jpg",
              "a rendition follows the same seam")

    class _Req:
        user = opts.superuser
    result = get_download_url(_Req(), f.upload_token)
    assert_eq(result.get("status_code"), 200, f"token download route failed: {result}")
    resource, params = __import__("mojo.apps.fileman.services.cdn", fromlist=["x"]).parse_signed_url(
        result["download_url"])
    assert_eq(resource, expected, "the token download route must build on the CDN host")
    # The route builds its own backend, so the injected signer is not in play
    # and the KMS-backed key row holds no PEM here: the link is the unsigned
    # CDN URL. Signing through get_url is proven on the private-manager path
    # above; the live module proves it end to end with a real key.
    assert_true("Signature" not in params, "with no loadable signing key the route issues the unsigned CDN URL")


@th.tier("core")
@th.django_unit_test("CDN: a private manager's link is a signed canned-policy URL that verifies")
def test_private_manager_signed_link_verifies(opts):
    from mojo.apps.fileman.services import cdn
    fm = _manager(opts.private_cdn)
    f = _file(fm, user=opts.superuser)
    before = datetime.now(tz=timezone.utc)
    url = f.get_direct_download_url()
    expires = _verify_signed(url, f"https://{CDN_HOST}/{f.storage_file_path}".replace(" ", "%20"))
    floor = cdn.ceil_to_hour(before + timedelta(seconds=cdn.DEFAULT_EXPIRES_IN)).timestamp()
    assert_true(abs(expires - floor) <= 3600,
                f"default lifetime must be ~7 days rounded up to the hour, got {expires - before.timestamp()} s")
    assert_eq(expires % 3600, 0, "Expires must sit on an hour boundary")
    assert_true(expires - before.timestamp() > 7 * 86400 - 3600,
                "a CDN lifetime must be allowed to exceed what S3 presigning permits")
    # Repeated calls inside the hour give the same URL (client caches keep working).
    assert_eq(f.get_direct_download_url(), url, "links must be stable within the hour")
    # The download_url cache is never consulted in CDN mode.
    f.download_url = "https://stale.s3.amazonaws.com/old"
    assert_true("stale.s3" not in f.get_direct_download_url(),
                "a stale raw S3 URL in download_url must be ignored in CDN mode")


@th.tier("core")
@th.django_unit_test("CDN: the lifetime honours the caller, the manager setting and the cap")
def test_lifetime_rules(opts):
    from mojo.apps.fileman.services import cdn
    fm = _manager(opts.private_cdn)
    key = "fileman/p/q?r.mp4"
    now = datetime.now(tz=timezone.utc)

    # An explicit caller value (the token download route passes 3600) is kept.
    short = fm.backend.get_url(key, 3600)
    expires = _verify_signed(short, f"https://{CDN_HOST}/fileman/p/q%3Fr.mp4")
    assert_true(expires <= cdn.ceil_to_hour(now + timedelta(seconds=3600)).timestamp(),
                "an explicit 3600 s request must not become a week-long link")

    # Above the cap is clamped to the cap.
    cap = cdn.max_expires_in()
    long = fm.backend.get_url(key, cap * 10)
    expires = _verify_signed(long, f"https://{CDN_HOST}/fileman/p/q%3Fr.mp4")
    assert_true(expires <= cdn.ceil_to_hour(now + timedelta(seconds=cap)).timestamp(),
                "a lifetime above FILEMAN_CDN_MAX_EXPIRES_IN must be clamped")

    # A manager setting drives File links.
    fm.set_setting("cdn_url_expires_in", 2 * 86400)
    fm.save()
    fm = _manager(opts.private_cdn)
    f = _file(fm, user=opts.superuser)
    expires = _verify_signed(f.get_direct_download_url(),
                             f"https://{CDN_HOST}/{f.storage_file_path}".replace(" ", "%20"))
    assert_true(abs(expires - cdn.ceil_to_hour(now + timedelta(days=2)).timestamp()) <= 3600,
                "cdn_url_expires_in must set the File link lifetime")
    fm.set_setting("cdn_url_expires_in", None)
    fm.save()


@th.tier("core")
@th.django_unit_test("CDN: an opted-out manager and a disabled distribution fall back to S3")
def test_opt_out_and_disable(opts):
    from mojo.apps.fileman.models import CdnDistribution
    fm = _manager(opts.optout_cdn)
    assert_true(not fm.uses_cdn, "cdn=off must opt a manager out of its bucket's CDN")
    assert_true(CDN_HOST not in fm.backend.get_url("fileman/o/x.mp4"),
                "an opted-out manager must get a raw S3 link")

    CdnDistribution.objects.filter(pk=opts.distribution_id).update(is_enabled=False)
    try:
        fm = _manager(opts.public_cdn)
        assert_true(not fm.uses_cdn, "a disabled distribution must take every manager off the CDN")
        assert_true(CDN_HOST not in fm.backend.get_url("fileman/o/x.mp4"),
                    "links revert to S3 when the distribution is disabled")
    finally:
        CdnDistribution.objects.filter(pk=opts.distribution_id).update(is_enabled=True)


@th.tier("core")
@th.django_unit_test("CDN: a child manager under a CDN bucket keeps is_public and inherits the opt-out")
def test_child_manager_keeps_public_and_inherits_switch(opts):
    from mojo.apps.fileman.models import FileManager
    parent = FileManager.objects.get(pk=opts.public_cdn)
    child = FileManager(name=f"{PREFIX}child", backend_type="s3",
                        backend_url=f"s3://{BUCKET}/fileman/child", parent=parent,
                        is_active=True, is_default=False, is_public=True)
    child.save()
    child.set_secrets(parent.secrets)
    child.save()
    # The S3 audit is skipped in CDN mode: it would read the private bucket
    # and rewrite is_public to False (the get_for_group path).
    assert_eq(child.audit_is_public(force=True), True,
              "audit_is_public must not touch S3 or flip is_public in CDN mode")
    child.refresh_from_db()
    assert_true(child.is_public, "a CDN child keeps the stored is_public")
    assert_true(child.uses_cdn, "a child on the CDN bucket uses the CDN")

    parent.set_setting("cdn", "off")
    parent.save()
    try:
        child = FileManager.objects.get(pk=child.pk)
        assert_true(not child.uses_cdn, "a child inherits the top parent's cdn=off")
    finally:
        parent.set_setting("cdn", None)
        parent.save()
        child.delete()


@th.tier("core")
@th.django_unit_test("CDN: settings are file-admin-only, S3-only and validated")
def test_cdn_settings_gates(opts):
    from mojo.models.rest import ACTIVE_REQUEST
    from mojo import errors as me
    from mojo.apps.fileman.models import FileManager

    class _Req:
        def __init__(self, user):
            self.user = user

    fm = FileManager.objects.get(pk=opts.private_cdn)
    token = ACTIVE_REQUEST.set(_Req(opts.member))
    try:
        fm.set_setting("cdn_url_expires_in", 86400)
        err = None
        try:
            fm.on_rest_pre_save({}, False)
        except me.PermissionDeniedException as exc:
            err = exc
        assert_true(err is not None, "a user without manage_files must not change cdn settings")
    finally:
        ACTIVE_REQUEST.reset(token)

    fm = FileManager.objects.get(pk=opts.private_cdn)
    token = ACTIVE_REQUEST.set(_Req(opts.file_admin))
    try:
        fm.set_setting("cdn_url_expires_in", 43200)
        fm.on_rest_pre_save({}, False)
        assert_eq(fm.get_setting("cdn_url_expires_in"), 43200,
                  "a global file admin (manage_files, not superuser) may set the lifetime")
    finally:
        ACTIVE_REQUEST.reset(token)

    fm = FileManager.objects.get(pk=opts.private_cdn)
    token = ACTIVE_REQUEST.set(_Req(opts.superuser))
    try:
        fm.set_setting("cdn_url_expires_in", 86400)
        fm.on_rest_pre_save({}, False)
        assert_eq(fm.get_setting("cdn_url_expires_in"), 86400, "a superuser may set the lifetime")
        fm.set_setting("cdn_url_expires_in", 10)
        err = None
        try:
            fm.on_rest_pre_save({}, False)
        except me.ValueException as exc:
            err = exc
        assert_true(err is not None, "a lifetime below 60 s must be refused")
        fm.set_setting("cdn_url_expires_in", None)
        fm.set_setting("cdn", "maybe")
        err = None
        try:
            fm.on_rest_pre_save({}, False)
        except me.ValueException as exc:
            err = exc
        assert_true(err is not None, "cdn must be on or off")
    finally:
        ACTIVE_REQUEST.reset(token)

    plain = FileManager(name=f"{PREFIX}filesys", backend_type="file", backend_url="file://",
                        is_active=True)
    plain.save()
    try:
        plain.set_setting("cdn", "on")
        token = ACTIVE_REQUEST.set(_Req(opts.superuser))
        try:
            err = None
            try:
                plain.on_rest_pre_save({}, False)
            except me.ValueException as exc:
                err = exc
            assert_true(err is not None, "cdn settings must be refused on a non-S3 manager")
        finally:
            ACTIVE_REQUEST.reset(token)
    finally:
        plain.delete()


@th.tier("core")
@th.django_unit_test("CDN: no graph carries key material; the manager graph reports CDN status")
def test_graphs_hold_no_secrets(opts):
    from mojo.apps.fileman.models import FileManager, CdnSigningKey, CdnDistribution
    key = CdnSigningKey.objects.get(pk=opts.key_id)
    for graph in ("default", "basic"):
        encoded = json.dumps(key.to_dict(graph=graph), default=str)
        assert_true("PRIVATE KEY" not in encoded and "mojo_secrets" not in encoded,
                    f"the {graph} graph of CdnSigningKey must carry no key material: {encoded}")
    encoded = json.dumps(CdnDistribution.objects.get(pk=opts.distribution_id).to_dict(graph="default"),
                         default=str)
    assert_true("PRIVATE KEY" not in encoded and "mojo_secrets" not in encoded,
                "the CdnDistribution graph must carry no key material")
    status = FileManager.objects.get(pk=opts.private_cdn).to_dict(graph="default").get("cdn")
    assert_eq(status.get("enabled"), True, f"the manager graph must report CDN status: {status}")
    assert_eq(status.get("url"), f"https://{CDN_HOST}", "the manager graph must carry the CDN base URL")
    assert_eq(status.get("signed"), True, "a private manager reports signed delivery")
    assert_eq(FileManager.objects.get(pk=opts.public_plain).to_dict(graph="default").get("cdn"),
              {"enabled": False}, "a manager without a CDN reports enabled=False")


@th.tier("core")
@th.django_unit_test("CDN: the signing key model refuses anything but RSA-2048")
def test_signing_key_validation(opts):
    from mojo.apps.fileman.services import cdn
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    err = None
    try:
        cdn.load_private_key("not a key")
    except ValueError as exc:
        err = exc
    assert_true(err is not None, "garbage must be refused")
    ecdsa = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())
    err = None
    try:
        cdn.load_private_key(ecdsa)
    except ValueError as exc:
        err = exc
    assert_true(err is not None, "an ECDSA key must be refused (the signer is RSA)")
    assert_true(cdn.load_private_key(_PEM).key_size == 2048, "the generated key is RSA-2048")


@th.tier("core")
@th.django_unit_test("CDN: a public manager at the top of the bucket keeps S3 links; a private one signs")
def test_root_manager_links(opts):
    from mojo.apps.fileman.models import FileManager
    from mojo.apps.fileman.backends.s3 import S3StorageBackend
    created = []
    try:
        for name, is_public in ((f"{PREFIX}rootpub", True), (f"{PREFIX}rootpriv", False)):
            fm = FileManager(name=name, backend_type="s3", backend_url=f"s3://{BUCKET}",
                             is_active=True, is_default=False, is_public=is_public)
            fm.save()
            fm.set_secrets({"aws_key": "AKIACDNLINKS", "aws_secret": "cdn-links-secret",
                            "aws_region": "us-east-1"})
            fm.save()
            created.append(fm.pk)
        pub = FileManager.objects.get(pk=created[0])
        pub._backend = S3StorageBackend(pub, cdn_signer=_signer())
        assert_true(not pub.uses_cdn, "a public root manager does not deliver through the CDN")
        url = pub.backend.get_url("x/a.mp4")
        assert_true(CDN_HOST not in url, f"a public root manager gets a raw S3 link: {url}")
        assert_eq(url, pub.backend.get_public_url("x/a.mp4"), "the S3 link is the plain public URL")
        priv = FileManager.objects.get(pk=created[1])
        priv._backend = S3StorageBackend(priv, cdn_signer=_signer())
        assert_true(priv.uses_cdn, "a private root manager still delivers through the CDN")
        _verify_signed(priv.backend.get_url("x/b.mp4", 3600), f"https://{CDN_HOST}/x/b.mp4")
    finally:
        FileManager.objects.filter(pk__in=created).delete()
