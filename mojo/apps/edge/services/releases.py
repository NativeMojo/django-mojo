"""
Register a build, verify it landed, and hand desired state to the fleet
deployment coordinator.

## The upload flow

```
1. CI:  POST edge/release          {webapp, version, manifest}
        -> WebAppRelease(status=pending)
        -> one presigned PUT per manifest entry
2. CI:  PUT <url>                  straight to S3, never through this process
3. CI:  POST edge/release/complete {release}
        -> HeadObject per entry, compare checksum and size
        -> status=uploaded
4. WebAppDeployment                 automatic by default; manual hold opt-out
5. CI polls deployment status       live only after active-runner proof
```

**Why presigned PUTs rather than STS session credentials.** The item's own
comment proposed minting short-lived STS credentials scoped to the release
prefix. Two reasons this does not:

- `mojo/helpers/aws/` has no STS support at all, so that flow needs a new
  helper, a new IAM role and a terraform change before a line of it is
  testable.
- A session policy over `releases/<id>/*` still permits **arbitrary keys**
  under that prefix. A presigned PUT permits exactly one key — and since CI
  must declare the manifest before uploading anyway (step 3 cannot verify
  otherwise), every key is already known.

The honest trade: a presigned URL is tighter **per-object** and wider
**per-signer**, because it carries the authority of the platform's static AWS
credentials rather than a scoped role. That is precisely why `WebApp.bucket`
comes from an allowlist and `WebApp.prefix` is derived — see the model.

**Step 3 verifies, it does not trust.** A CI job that half-failed can still
call `complete`; the client asserting "I am done" is not evidence. The checksum
is bound into the presigned URL, so S3 rejects a mismatched body outright, and
`complete` reads the stored checksum back rather than re-hashing anything here.
"""

import base64

from mojo import errors as me
from mojo.helpers import logit
from mojo.helpers.aws import s3
from mojo.helpers.settings import settings

from mojo.apps.edge import validators
from mojo.apps.edge.models import WebApp, WebAppDeployment, WebAppRelease
from mojo.apps.edge.models.web_app_deployment import (
    ACTIVE_STATUSES, STATUS_LIVE as DEPLOYMENT_LIVE,
)
from mojo.apps.edge.models.web_app_release import (
    STATUS_LIVE, STATUS_PENDING, STATUS_SUPERSEDED, STATUS_UPLOADED,
)


def upload_ttl():
    return int(settings.get("EDGE_RELEASE_UPLOAD_TTL", 3600))


def hex_to_b64(digest_hex):
    """S3 wants a base64 checksum; a manifest carries hex.

    This conversion is where the whole scheme fails first if it is skipped —
    a hex value passed through produces a signature no upload can satisfy.
    """
    return base64.b64encode(bytes.fromhex(digest_hex)).decode("ascii")


def _upload_urls(release):
    """Mint upload URLs for a pending release's immutable manifest."""
    web_app = release.webapp
    prefix = release.storage_prefix()
    uploads = []
    for entry in release.manifest:
        key = f"{prefix}/{entry['path']}"
        checksum = hex_to_b64(entry["sha256"])
        uploads.append(dict(
            path=entry["path"],
            url=s3.presigned_put_url(
                web_app.bucket, key,
                expires=upload_ttl(),
                sha256_b64=checksum,
                content_length=entry["size"]),
            headers={"x-amz-checksum-sha256": checksum},
        ))
    return uploads


def register(web_app, version, manifest, user=None):
    """Create or safely reuse one immutable release version."""
    validators.validate_release_version(version)
    # Re-check the bucket at MINT time, not only when the row was created: a
    # bucket removed from EDGE_RELEASE_BUCKETS would otherwise keep receiving
    # presigned writes from existing sites.
    validators.validate_release_bucket(web_app.bucket or "")
    cleaned = validators.validate_manifest(manifest)

    existing = WebAppRelease.objects.filter(
        webapp=web_app, version=version).first()
    if existing is not None:
        if existing.manifest != cleaned:
            # Reuse is allowed only when it proves the exact same artifact.
            raise me.ValueException(
                f"release {version} already exists with a different manifest")
        uploads = _upload_urls(existing) if existing.status == STATUS_PENDING else []
        return existing, uploads

    release = WebAppRelease.objects.create(
        webapp=web_app, version=version, manifest=cleaned,
        status=STATUS_PENDING, created_by=user)

    uploads = _upload_urls(release)

    logit.info(
        f"edge: registered release {version} for {web_app.slug} "
        f"({len(cleaned)} files)")
    return release, uploads


def complete(release):
    """Verify every declared file actually landed, then mark it `uploaded`.

    Returns the release. Raises with the failing paths named — a CI job that
    half-failed should learn which objects are missing, not just that
    something is.
    """
    if release.status in (STATUS_UPLOADED, STATUS_LIVE, STATUS_SUPERSEDED):
        return release
    if release.status != STATUS_PENDING:
        raise me.ValueException(
            f"release {release.version} is {release.status}, not pending")

    web_app = release.webapp
    prefix = release.storage_prefix()
    problems = []

    for entry in release.manifest or []:
        key = f"{prefix}/{entry['path']}"
        try:
            head = s3.head_object(web_app.bucket, key)
        except Exception as err:
            problems.append(f"{entry['path']}: {err}")
            continue

        if head is None:
            problems.append(f"{entry['path']}: missing")
            continue

        size = head.get("ContentLength")
        if size is not None and int(size) != int(entry["size"]):
            problems.append(
                f"{entry['path']}: size {size}, declared {entry['size']}")
            continue

        stored = head.get("ChecksumSHA256")
        expected = hex_to_b64(entry["sha256"])
        if not stored:
            # An object written WITHOUT a checksum bypassed the bound presigned
            # URL. Treating absent as "fine" would remove the verification
            # entirely, which is the one thing this step exists to do.
            problems.append(f"{entry['path']}: no stored checksum")
            continue
        if stored != expected:
            problems.append(f"{entry['path']}: checksum mismatch")

    if problems:
        shown = "; ".join(problems[:10])
        more = "" if len(problems) <= 10 else f" (+{len(problems) - 10} more)"
        raise me.ValueException(
            f"release {release.version} did not verify: {shown}{more}")

    release.status = STATUS_UPLOADED
    release.save()
    logit.info(f"edge: release {release.version} verified for {web_app.slug}")
    return release


def promote(web_app, release, user=None):
    """Point a site at a release and coordinate active-fleet convergence.

    Wrapped in a row lock so two concurrent promotes cannot interleave and
    leave `current_release` disagreeing with the `live` row.
    """
    from django.db import transaction
    from mojo.apps.edge.services import webapp_deploy

    if release.webapp_id != web_app.pk:
        raise me.ValueException("that release belongs to another site")
    if not release.is_promotable:
        raise me.ValueException(
            f"release {release.version} is {release.status} and has not been "
            f"verified")

    with transaction.atomic():
        locked = WebApp.objects.select_for_update().get(pk=web_app.pk)
        active = WebAppDeployment.objects.filter(
            webapp=locked, release=release,
            status__in=ACTIVE_STATUSES + (DEPLOYMENT_LIVE,)
        ).order_by("-created").first()
        if locked.current_release_id == release.pk and active is not None:
            return active

        previous = locked.current_release
        release_previous_status = release.status

        if previous and previous.pk != release.pk:
            WebAppRelease.objects.filter(
                pk=previous.pk, status=STATUS_LIVE).update(
                    status=STATUS_SUPERSEDED)

        WebAppRelease.objects.filter(pk=release.pk).update(status=STATUS_LIVE)
        locked.current_release = release
        locked.save()

        deployment = WebAppDeployment.objects.create(
            webapp=locked,
            release=release,
            previous_release=previous if previous != release else None,
            release_previous_status=release_previous_status,
        )
        transaction.on_commit(
            lambda deployment_id=deployment.pk:
            webapp_deploy.publish_or_restore(deployment_id),
            robust=True)

    release.refresh_from_db()
    logit.info(
        f"edge: {web_app.slug} now serving {release.version} "
        f"(was {previous.version if previous else 'nothing'})")
    return deployment


def maybe_auto_promote(release):
    """Deploy on verification unless this site explicitly uses manual hold."""
    web_app = release.webapp
    if not web_app.auto_promote:
        return None
    return promote(web_app, release)


def desired_webapps(vhosts):
    """The `webapps` key of the desired-state payload.

    Only sites with BOTH a vhost and a current release appear — a site served
    by CloudFront (no vhost) is registerable and rollbackable but is not
    something a node installs.

    Keyed by vhost id because that, not the slug, is what the node turns into a
    filesystem path.
    """
    vhost_ids = [v.pk for v in vhosts]
    rows = (
        WebApp.objects
        .filter(vhost_id__in=vhost_ids, current_release__isnull=False)
        .select_related("current_release")
        .order_by("vhost_id")
    )
    return [
        dict(
            vhost=row.vhost_id,
            slug=row.slug,
            bucket=row.bucket,
            release=dict(
                id=row.current_release_id,
                version=row.current_release.version,
                prefix=row.current_release.storage_prefix(),
                manifest=row.current_release.manifest,
            ),
        )
        for row in rows
    ]
