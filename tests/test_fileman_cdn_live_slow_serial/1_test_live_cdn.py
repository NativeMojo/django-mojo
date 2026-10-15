"""Live proof of CDN delivery against a real bucket (maestro #8250).

Runs only when the environment names a test bucket under a customer-managed
KMS key and carries AWS credentials and KMS_KEY_ID:

    FILEMAN_CDN_LIVE_BUCKET=some-test-bucket
    FILEMAN_CDN_LIVE_KMS_KEY=arn:aws:kms:...:key/...   (customer managed)
    KMS_KEY_ID=...                                      (the deployment envelope key)
    AWS_KEY / AWS_SECRET or an ambient identity with the enable permissions

What it proves, in order: enable creates the distribution and policies;
a second enable makes no mutating call; after CloudFront reports Deployed,
an unsigned CDN GET returns 200 on a public manager's prefix and 403 on a
private one; a signed GET returns 200; an anonymous raw S3 GET returns 403
(the bucket stays private); key rotation then revocation makes the old
signed link fail; a BYO check of the same distribution passes. Teardown
retires and deletes the distribution it created — the one place deletion
is allowed, because this test created and tagged it.
"""

import io
import os
import time
import urllib.error
import urllib.request

from testit import TestitSkip
from testit import helpers as th
from testit.helpers import assert_eq, assert_true

PREFIX = "fm_cdnlive_"
DEPLOY_TIMEOUT = 900


def _env():
    bucket = os.environ.get("FILEMAN_CDN_LIVE_BUCKET")
    kms_key = os.environ.get("FILEMAN_CDN_LIVE_KMS_KEY")
    from mojo.helpers.settings import settings
    if not bucket or not kms_key or not settings.get("KMS_KEY_ID", None):
        raise TestitSkip("set FILEMAN_CDN_LIVE_BUCKET, FILEMAN_CDN_LIVE_KMS_KEY and KMS_KEY_ID "
                         "to run the live CDN proof")
    return bucket, kms_key


def _get(url, timeout=20):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def _wait_deployed(cf, distribution_id):
    deadline = time.time() + DEPLOY_TIMEOUT
    while time.time() < deadline:
        status = cf.get_distribution(Id=distribution_id)["Distribution"]["Status"]
        if status == "Deployed":
            return True
        time.sleep(20)
    return False


@th.django_unit_setup()
def setup_live_cdn(opts):
    from mojo.apps.fileman.models import FileManager
    bucket, kms_key = _env()
    FileManager.objects.filter(name__startswith=PREFIX).delete()
    opts.bucket = bucket
    managers = {}
    for name, is_public in (("public", True), ("private", False)):
        fm = FileManager(name=f"{PREFIX}{name}", backend_type="s3",
                         backend_url=f"s3://{bucket}/fileman-cdn-live/{name}",
                         is_active=True, is_default=False, is_public=is_public)
        fm.save()
        fm.set_secrets({"server_side_encryption": "aws:kms", "kms_key_id": kms_key})
        fm.save()
        managers[name] = fm.pk
    opts.managers = managers
    opts.created_distribution = None


@th.requires_extra("slow")
@th.django_unit_test("live: enable provisions, re-run creates nothing, KMS objects are served through the CDN")
def test_live_cdn_delivery(opts):
    from mojo.apps.fileman.models import CdnDistribution, FileManager
    from mojo.apps.fileman.services import cdn_provision
    from mojo.apps.fileman.services.cdn_provision import CdnProvisioner

    public = FileManager.objects.get(pk=opts.managers["public"])
    private = FileManager.objects.get(pk=opts.managers["private"])

    first = cdn_provision.enable(public)
    assert_true(first["converged"], f"enable did not converge: {first['items']}")
    row = CdnDistribution.objects.get(bucket_name=opts.bucket)
    opts.created_distribution = row.distribution_id if "distribution.created" in {
        i["code"] for i in first["items"]} else None

    # Idempotent: the second run performs no mutation.
    provisioner = CdnProvisioner(public, apply=True)
    second = provisioner.run()
    assert_eq([a.as_dict() for a in provisioner.actions], [],
              f"a converged account must see no action on re-run: {second['items']}")

    cf = provisioner.client("cloudfront")
    assert_true(_wait_deployed(cf, row.distribution_id), "the distribution did not reach Deployed in time")

    # One object per prefix, encrypted with the customer-managed key.
    keys = {}
    for name, manager in (("public", public), ("private", private)):
        backend = manager.backend
        key = f"{manager.root_path}/probe-{int(time.time())}.txt"
        backend.save(io.BytesIO(b"cdn live probe"), key, "text/plain")
        keys[name] = key

    public = FileManager.objects.get(pk=public.pk)
    private = FileManager.objects.get(pk=private.pk)
    public_url = public.backend.get_url(keys["public"])
    assert_true("?" not in public_url, "a public manager's CDN link is unsigned")
    assert_eq(_get(public_url), 200, f"unsigned CDN GET on the public prefix: {public_url}")
    assert_eq(_get(public.backend.public_s3_url(keys["public"])), 403,
              "the bucket stays private: anonymous raw S3 GET is refused")
    unsigned_private = cdn_provision.cdn.build_url(row, keys["private"])
    assert_eq(_get(unsigned_private), 403, "an unsigned GET on a private prefix is refused at the edge")
    signed = private.backend.get_url(keys["private"], 3600)
    assert_eq(_get(signed), 200, f"a signed CDN GET on the private prefix: {signed}")

    # Rotation keeps the old link alive; revocation kills it (after propagation).
    old_key = row.signing_key.key_id
    rotated = cdn_provision.rotate_key(manager=public)
    assert_true("key.rotated" in {i["code"] for i in rotated["items"]}, f"rotation: {rotated['items']}")
    assert_eq(_get(signed), 200, "a link signed before rotation still works")
    revoked = cdn_provision.revoke_key(old_key, manager=public)
    assert_true(revoked["status"], f"revocation: {revoked}")
    deadline = time.time() + DEPLOY_TIMEOUT
    while _get(signed) == 200 and time.time() < deadline:
        time.sleep(20)
    assert_eq(_get(signed), 403, "a link signed with a revoked key fails once the edge sees the change")
    private = FileManager.objects.get(pk=private.pk)
    assert_eq(_get(private.backend.get_url(keys["private"], 3600)), 200, "the new key signs working links")

    # BYO: the same distribution checked as one an operator brought.
    check = cdn_provision.check(public)
    assert_true(check["converged"], f"check of the live setup: {check['items']}")


@th.requires_extra("slow")
@th.django_unit_test("live: teardown disables and deletes only what this test created")
def test_live_cdn_teardown(opts):
    from mojo.apps.fileman.models import CdnDistribution, FileManager
    from mojo.apps.fileman.services import cdn_provision
    from mojo.apps.fileman.services.cdn_provision import CdnProvisioner
    public = FileManager.objects.get(pk=opts.managers["public"])
    row = CdnDistribution.objects.filter(bucket_name=opts.bucket).first()
    if row is None:
        return
    cdn_provision.disable(public, retire=True)
    if not getattr(opts, "created_distribution", None):
        return  # adopted, not ours to delete
    cf = CdnProvisioner(public).client("cloudfront")
    assert_true(_wait_deployed(cf, row.distribution_id), "the retired distribution did not finish deploying")
    config = cf.get_distribution_config(Id=row.distribution_id)
    cf.delete_distribution(Id=row.distribution_id, IfMatch=config["ETag"])
    row.delete()
