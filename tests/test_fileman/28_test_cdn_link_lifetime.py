"""CDN link lifetime (maestro #8487): shorter wins, never rounded past the setting.

A private manager in CDN mode signs for the shorter of `cdn_url_expires_in`
(else 7 days) and an explicitly set `urls_expire_in` (own or top parent), and
the signed `Expires` never lands later than `now + lifetime`. Fixture style
follows 26_test_cdn_links: no AWS, the signer injected through the backend's
`cdn_signer=` seam, links verified against the test's own public key.
"""

import base64
import json
import time

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

PREFIX = "fm_cdnlife_"
BUCKET = "cdn-lifetime-test-bucket"
OTHER_BUCKET = "cdn-lifetime-plain-bucket"
CDN_HOST = "d1cdnlifetime.cloudfront.net"
KEY_ID = "K2CDNLIFETIMETEST"
DAY = 86400


def _signer():
    from mojo.apps.fileman.services import cdn
    return cdn.Signer(KEY_ID, cdn.load_private_key(_PEM))


def _make_manager(name, bucket, is_public, parent=None, **settings):
    from mojo.apps.fileman.models import FileManager
    fm = FileManager(
        name=f"{PREFIX}{name}", backend_type="s3",
        backend_url=f"s3://{bucket}/fileman/{name}", parent=parent,
        is_active=True, is_default=False, is_public=is_public)
    fm.save()
    secrets = {"aws_key": "AKIACDNLIFE", "aws_secret": "cdn-life-secret",
               "aws_region": "us-east-1", "use_shortlinks": False}
    if parent is None:
        secrets.update(settings)
    else:
        secrets = dict(settings)
    fm.set_secrets(secrets)
    fm.save()
    return fm.pk


@th.django_unit_setup()
def setup_cdn_link_lifetime(opts):
    from mojo.apps.account.models import User
    from mojo.apps.fileman.models import FileManager, File, CdnDistribution, CdnSigningKey
    from mojo.apps.fileman.services import cdn

    # Long-lived database: clear leftovers before creating.
    File.objects.filter(file_manager__name__startswith=PREFIX).delete()
    FileManager.objects.filter(name__startswith=PREFIX, parent__isnull=False).delete()
    FileManager.objects.filter(name__startswith=PREFIX).delete()
    CdnDistribution.objects.filter(bucket_name__in=[BUCKET, OTHER_BUCKET]).delete()
    CdnSigningKey.objects.filter(key_id=KEY_ID).delete()
    User.objects.filter(username__startswith=PREFIX).delete()

    global _PEM
    _PEM = cdn.generate_private_key_pem()

    opts.superuser = User.objects.create(
        username=f"{PREFIX}super", email=f"{PREFIX}super@example.com", is_superuser=True)

    key = CdnSigningKey(key_id=KEY_ID, public_key_name="mojo-fm-key-life", is_active=True)
    key.save()
    CdnDistribution.objects.create(
        bucket_name=BUCKET, distribution_id="E1CDNLIFE", domain_name=CDN_HOST,
        oac_id="E2OAC", key_group_id="K3GROUP", signing_key=key,
        source="provisioned", is_enabled=True, status="deployed")

    opts.urls300 = _make_manager("urls300", BUCKET, False, urls_expire_in=300)
    opts.cdn600 = _make_manager("cdn600", BUCKET, False, cdn_url_expires_in=600)
    opts.neither = _make_manager("neither", BUCKET, False)
    opts.short60 = _make_manager("short60", BUCKET, False, urls_expire_in=60)
    opts.cdn2d_urls5d = _make_manager("cdn2d", BUCKET, False,
                                      cdn_url_expires_in=2 * DAY, urls_expire_in=5 * DAY)
    opts.public_cdn = _make_manager("public", BUCKET, True, urls_expire_in=300)
    opts.plain = _make_manager("plain", OTHER_BUCKET, False, urls_expire_in=300)
    opts.optout = _make_manager("optout", BUCKET, False, cdn="off", urls_expire_in=300)
    parent = FileManager.objects.get(pk=_make_manager("parent", BUCKET, False, urls_expire_in=300))
    opts.child = _make_manager("child", BUCKET, False, parent=parent)
    opts.urls30d = _make_manager("urls30d", BUCKET, False, urls_expire_in=30 * DAY)
    parent30d = FileManager.objects.get(pk=_make_manager("parent30d", BUCKET, False,
                                                         urls_expire_in=30 * DAY))
    opts.child30d = _make_manager("child30d", BUCKET, False, parent=parent30d)
    opts.frac_num = _make_manager("fracnum", BUCKET, False, urls_expire_in=300.5)
    opts.frac_text = _make_manager("fractext", BUCKET, False, urls_expire_in="300.5")
    opts.unset_values = [_make_manager(f"unset{index}", BUCKET, False, urls_expire_in=value)
                         for index, value in enumerate(("abc", 0, -5))]
    opts.shortlinked = _make_manager("shortlinked", BUCKET, False, urls_expire_in=300)
    fm = FileManager.objects.get(pk=opts.shortlinked)
    fm.set_setting("use_shortlinks", True)
    fm.save()


def _manager(pk):
    from mojo.apps.fileman.models import FileManager
    from mojo.apps.fileman.backends.s3 import S3StorageBackend
    fm = FileManager.objects.get(pk=pk)
    fm._backend = S3StorageBackend(fm, cdn_signer=_signer())
    return fm


def _file(fm, user, filename="lifetime.bin"):
    import uuid
    from mojo.apps.fileman.models import File
    f = File(filename=filename, content_type="application/octet-stream", category="other",
             file_size=3, file_manager=fm, user=user,
             upload_status=File.COMPLETED, upload_token=uuid.uuid4().hex)
    f.generate_storage_filename()
    f.save()
    f.refresh_from_db()
    f.file_manager = fm  # keep the backend with the injected signer
    return f


def _expires(url):
    """Verify the canned-policy signature against our public key; return Expires."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from mojo.apps.fileman.services import cdn

    resource, params = cdn.parse_signed_url(url)
    assert_true(resource.startswith(f"https://{CDN_HOST}/"), f"not a CDN link: {url}")
    for name in ("Expires", "Signature", "Key-Pair-Id"):
        assert_true(name in params, f"signed URL lacks {name}: {url}")
    expires = int(params["Expires"])
    policy = json.dumps({"Statement": [{"Resource": resource,
                                        "Condition": {"DateLessThan": {"AWS:EpochTime": expires}}}]},
                        separators=(",", ":")).encode()
    signature = base64.b64decode(
        params["Signature"].replace("-", "+").replace("_", "=").replace("~", "/"))
    cdn.load_private_key(_PEM).public_key().verify(signature, policy, padding.PKCS1v15(), hashes.SHA1())
    return expires


def _assert_within(url, before, after, lifetime, label):
    """now < Expires <= now + lifetime, and not born nearly expired."""
    expires = _expires(url)
    assert_true(expires <= after + lifetime,
                f"{label}: link lives {expires - before} s, more than the {lifetime} s setting")
    assert_true(expires >= int(before) + lifetime // 2,
                f"{label}: link lives only {expires - before} s of {lifetime} s")
    assert_true(expires > after, f"{label}: link already expired")


def _s3_expires(url):
    query = url.split("?", 1)[1]
    params = dict(part.split("=", 1) for part in query.split("&"))
    return params


# ----------------------------------------------------------------------
# red on main 682a1691 / 4bbc5333, green after
# ----------------------------------------------------------------------

@th.tier("core")
@th.django_unit_test("CDN lifetime: urls_expire_in=300 bounds the File url, direct and in the default graph")
def test_urls_expire_in_bounds_file_url(opts):
    fm = _manager(opts.urls300)
    f = _file(fm, opts.superuser)
    before = time.time()
    direct = f.get_direct_download_url()
    graphed = f.to_dict(graph="default")["url"]
    after = time.time()
    _assert_within(direct, before, after, 300, "get_direct_download_url")
    _assert_within(graphed, before, after, 300, "default graph url")


@th.tier("core")
@th.django_unit_test("CDN lifetime: urls_expire_in=300 bounds a FileRendition url and the File thumbnail")
def test_urls_expire_in_bounds_rendition_and_thumbnail(opts):
    from mojo.apps.fileman.models import File, FileRendition
    fm = _manager(opts.urls300)
    f = _file(fm, opts.superuser, filename="thumbed.bin")
    FileRendition.objects.create(
        original_file=f, role="thumbnail", filename="thumb.jpg",
        storage_path=f"{fm.root_path}/thumbs/thumb.jpg", content_type="image/jpeg",
        category="image", upload_status=FileRendition.COMPLETED)
    # Prefetch so the rendition's original_file is this instance and its
    # manager keeps the injected signer.
    f = File.objects.prefetch_related("file_renditions").get(pk=f.pk)
    f.file_manager = fm
    rendition = f.get_rendition_by_role("thumbnail")
    before = time.time()
    rendition_url = rendition.get_direct_download_url()
    thumbnail = f.thumbnail
    after = time.time()
    _assert_within(rendition_url, before, after, 300, "rendition url")
    _assert_within(thumbnail, before, after, 300, "File thumbnail")


@th.tier("core")
@th.django_unit_test("CDN lifetime: cdn_url_expires_in=600 is not rounded up past 600 s")
def test_cdn_url_expires_in_not_rounded_past(opts):
    fm = _manager(opts.cdn600)
    f = _file(fm, opts.superuser)
    before = time.time()
    url = f.get_direct_download_url()
    after = time.time()
    _assert_within(url, before, after, 600, "cdn_url_expires_in=600")


@th.tier("core")
@th.django_unit_test("CDN lifetime: an explicit get_url(key, 3600) lives at most 3600 s")
def test_explicit_expires_in_not_rounded_past(opts):
    fm = _manager(opts.neither)
    before = time.time()
    url = fm.backend.get_url("fileman/neither/x.bin", 3600)
    after = time.time()
    _assert_within(url, before, after, 3600, "get_url(key, 3600)")


@th.tier("core")
@th.django_unit_test("CDN lifetime: urls_expire_in set on the top parent bounds the child's links")
def test_parent_urls_expire_in_bounds_child(opts):
    fm = _manager(opts.child)
    assert_true(fm.uses_cdn, "the child sits on the CDN bucket")
    assert_eq(fm.get_secret("urls_expire_in"), None, "the child sets no urls_expire_in itself")
    f = _file(fm, opts.superuser)
    before = time.time()
    url = f.get_direct_download_url()
    after = time.time()
    _assert_within(url, before, after, 300, "child of a urls_expire_in=300 parent")


@th.tier("core")
@th.django_unit_test("CDN lifetime: a 60 s lifetime gives now < Expires <= now + 60")
def test_sixty_second_lifetime(opts):
    fm = _manager(opts.short60)
    f = _file(fm, opts.superuser)
    before = time.time()
    url = f.get_direct_download_url()
    explicit = fm.backend.get_url("fileman/short60/x.bin", 60)
    after = time.time()
    _assert_within(url, before, after, 60, "urls_expire_in=60")
    _assert_within(explicit, before, after, 60, "get_url(key, 60)")


@th.tier("core")
@th.django_unit_test("CDN lifetime: a shortlink click re-signs with the shorter lifetime")
def test_shortlink_resolve_uses_shorter_lifetime(opts):
    fm = _manager(opts.shortlinked)
    f = _file(fm, opts.superuser)
    url = f.url
    if not url.startswith("/s/") and "/s/" not in url:
        # The shortlink app is off in this environment: the direct link is the url.
        before = time.time()
        _assert_within(url, before - 1, time.time(), 300, "url with shortlinks off")
        return
    from mojo.apps.shortlink.models import ShortLink
    link = ShortLink.objects.get(code=url.rstrip("/").rsplit("/", 1)[-1])
    link.file = f  # resolve() reads the file's manager; keep the injected backend
    before = time.time()
    destination = link.resolve()
    after = time.time()
    _assert_within(destination, before, after, 300, "shortlink resolve")


@th.tier("core")
@th.django_unit_test("CDN lifetime: urls_expire_in above 7 days cannot lengthen the default CDN lifetime")
def test_urls_expire_in_cannot_lengthen_default(opts):
    for pk, label in ((opts.urls30d, "own urls_expire_in=30 d"),
                      (opts.child30d, "inherited urls_expire_in=30 d")):
        fm = _manager(pk)
        f = _file(fm, opts.superuser)
        before = time.time()
        direct = f.get_direct_download_url()
        graphed = f.to_dict(graph="default")["url"]
        after = time.time()
        _assert_within(direct, before, after, 7 * DAY, f"{label}, direct")
        _assert_within(graphed, before, after, 7 * DAY, f"{label}, default graph")


@th.tier("core")
@th.django_unit_test("CDN lifetime: a fractional urls_expire_in is floored, an unusable one counts as unset")
def test_fractional_and_unusable_values(opts):
    for pk, label in ((opts.frac_num, "300.5"), (opts.frac_text, '"300.5"')):
        fm = _manager(pk)
        f = _file(fm, opts.superuser)
        before = time.time()
        url = f.get_direct_download_url()
        after = time.time()
        _assert_within(url, before, after, 300, f"urls_expire_in={label}")
    for pk, label in zip(opts.unset_values, ('"abc"', "0", "-5")):
        fm = _manager(pk)
        f = _file(fm, opts.superuser)
        before = time.time()
        url = f.get_direct_download_url()
        after = time.time()
        _assert_within(url, before, after, 7 * DAY, f"urls_expire_in={label} counts as unset")


# ----------------------------------------------------------------------
# controls: pass before and after
# ----------------------------------------------------------------------

@th.tier("core")
@th.django_unit_test("CDN lifetime: a manager that set neither value keeps about 7 days")
def test_neither_setting_keeps_seven_days(opts):
    fm = _manager(opts.neither)
    f = _file(fm, opts.superuser)
    before = time.time()
    expires = _expires(f.get_direct_download_url())
    after = time.time()
    assert_true(expires >= before + 3.5 * DAY, f"default link too short: {expires - before} s")
    assert_true(expires <= after + 7 * DAY + 3600, f"default link too long: {expires - before} s")


@th.tier("core")
@th.django_unit_test("CDN lifetime: cdn_url_expires_in 2 days with urls_expire_in 5 days gives 2 days")
def test_cdn_setting_shorter_than_urls(opts):
    fm = _manager(opts.cdn2d_urls5d)
    f = _file(fm, opts.superuser)
    before = time.time()
    expires = _expires(f.get_direct_download_url())
    after = time.time()
    assert_true(expires >= before + DAY, f"2-day link too short: {expires - before} s")
    assert_true(expires <= after + 2 * DAY + 3600, f"2-day link too long: {expires - before} s")


@th.tier("core")
@th.django_unit_test("CDN lifetime: a public CDN manager's link stays unsigned")
def test_public_manager_unsigned(opts):
    fm = _manager(opts.public_cdn)
    f = _file(fm, opts.superuser)
    url = f.get_direct_download_url()
    assert_true(url.startswith(f"https://{CDN_HOST}/"), f"public link not on the CDN: {url}")
    assert_true("?" not in url, f"public link must carry no signature: {url}")


@th.tier("core")
@th.django_unit_test("CDN lifetime: a non-CDN private manager presigns for urls_expire_in")
def test_non_cdn_manager_presigns_urls_expire_in(opts):
    fm = _manager(opts.plain)
    assert_true(not fm.uses_cdn, "the plain bucket has no distribution")
    f = _file(fm, opts.superuser)
    url = f.get_direct_download_url()
    assert_true(CDN_HOST not in url, f"non-CDN link on the CDN: {url}")
    assert_eq(_s3_expires(url).get("X-Amz-Expires"), "300", "S3 presign must use urls_expire_in")


@th.tier("core")
@th.django_unit_test("CDN lifetime: a cdn=off manager on a CDN bucket presigns S3 for urls_expire_in")
def test_opted_out_manager_presigns_urls_expire_in(opts):
    fm = _manager(opts.optout)
    assert_true(not fm.uses_cdn, "cdn=off must opt the manager out")
    f = _file(fm, opts.superuser)
    url = f.get_direct_download_url()
    assert_true(CDN_HOST not in url, f"opted-out link on the CDN: {url}")
    assert_eq(_s3_expires(url).get("X-Amz-Expires"), "300", "S3 presign must use urls_expire_in")
