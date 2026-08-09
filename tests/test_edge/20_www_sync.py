"""
Node-side release FETCH: pulling promoted bytes from S3, verified per file.

`11_release_installer.py` stages release directories by hand and asserts what
the installer does with them. This module asserts how they get there — the
manifest sha256 is the only thing trusted, a failed fetch degrades exactly one
vhost, and a degraded node heals itself on a later converge.

S3 is a fake: `FakeS3` is assigned straight onto `www_sync._client`, which is
the module's one injection seam. No test here reaches the network, and a test
that tried would fail rather than silently use real credentials.
"""

import hashlib
import os
import shutil
import tempfile
import time
from unittest import mock

from testit import helpers as th

from tests.test_edge._helpers import (
    RELEASE_BUCKET, cleanup, declare_pools, declare_release_buckets,
    make_certificate, make_domain, make_group,
    make_release, make_vhost, make_webapp, raises,
)


POOL = "wwwsync"

# One nested path on purpose: manifest paths carry subdirectories, and the
# parent-directory creation and the recursive temp sweep both only matter for
# those.
FILES_V1 = {
    "index.html": b"<html>v1</html>",
    "assets/js/app.js": b"console.log('v1');\n",
}

FILES_V2 = {
    "index.html": b"<html>v2</html>",
    "assets/js/app.js": b"console.log('v2');\n",
}


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class Recorder:
    """Stands in for installer._run — every nginx call succeeds."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        return FakeProc(
            0, stderr="nginx: configuration file test is successful\n")


class FakeS3:
    """The boto3 s3 client, minus everything the fetcher does not call.

    Keys are (bucket, key) so a test can prove a fetch never asked for the
    wrong bucket. An unknown key raises, exactly as boto3 does.
    """

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.calls = []
        # Bytes actually handed to disk per key. A transfer the fetcher aborts
        # part-way is only distinguishable from one it rejects afterwards by
        # how much of it landed.
        self.written = {}

    def download_file(self, Bucket, Key, Filename, Callback=None):
        self.calls.append((Bucket, Key))
        if (Bucket, Key) not in self.objects:
            raise RuntimeError(f"no such object: s3://{Bucket}/{Key}")
        body = self.objects[(Bucket, Key)]
        # Written in pieces like s3transfer does, so a Callback that aborts
        # part-way through a transfer aborts here too — that is the whole
        # point of the size guard.
        with open(Filename, "wb") as handle:
            for start in range(0, len(body), 8):
                piece = body[start:start + 8]
                handle.write(piece)
                self.written[Key] = self.written.get(Key, 0) + len(piece)
                if Callback is not None:
                    Callback(len(piece))


@th.django_unit_setup()
def setup_www_sync(opts):
    cleanup()
    declare_pools()
    declare_release_buckets()
    opts.group = make_group("edgewwwsync")
    opts.domain = make_domain(group=opts.group)
    opts.certificate = make_certificate(opts.domain)
    opts.vhost = make_vhost(opts.domain, opts.certificate, label="www",
                            kind="site", pool=POOL)
    opts.webapp = make_webapp(opts.group, slug="syncsite", vhost=opts.vhost)


def _manifest(files):
    """A manifest hashed from the REAL bytes — the anchor every test leans on."""
    return [
        dict(path=path, sha256=hashlib.sha256(body).hexdigest(), size=len(body))
        for path, body in sorted(files.items())
    ]


def _objects(prefix, files, bucket=RELEASE_BUCKET):
    return {(bucket, f"{prefix}/{path}"): body for path, body in files.items()}


def _row(vhost_id, version, files, bucket=RELEASE_BUCKET, prefix=None):
    """A `desired_webapps` row, built by hand — no DB needed to fetch."""
    return dict(
        vhost=vhost_id, slug="syncsite", bucket=bucket,
        release=dict(id=1, version=version,
                     prefix=prefix or f"webapps/1/1/releases/{version}",
                     manifest=_manifest(files)))


def _patches(root, www_base):
    return [
        mock.patch("mojo.apps.edge.services.render.edge_root", return_value=root),
        mock.patch("mojo.apps.edge.validators.www_base", return_value=www_base),
        mock.patch(
            "mojo.apps.dnsman.models.certificate.Certificate.private_key_pem",
            new_callable=mock.PropertyMock,
            return_value="-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n"),
    ]


def _enter(patches):
    for patch in patches:
        patch.start()


def _exit(patches, *dirs):
    from mojo.apps.edge.services import www_sync

    for patch in reversed(patches):
        patch.stop()
    # Never leave a fake client behind for another module to fetch through.
    www_sync._client = None
    for path in dirs:
        shutil.rmtree(path, ignore_errors=True)


def _seed_cert(opts):
    from mojo.apps.dnsman.models import Certificate

    Certificate.objects.filter(pk=opts.certificate.pk).update(
        cert_pem="-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")


@th.django_unit_test("a fetch lands the manifest's exact bytes, nested paths included")
def test_fetch_lands_verified_bytes(opts):
    from mojo.apps.edge.services import installer, www_sync

    root = tempfile.mkdtemp(prefix="edge-sync-")
    www = tempfile.mkdtemp(prefix="edge-www-")
    patches = _patches(root, www)
    _enter(patches)
    try:
        row = _row(opts.vhost.pk, "fetch1", FILES_V1)
        www_sync._client = FakeS3(_objects(row["release"]["prefix"], FILES_V1))

        failures = www_sync.fetch_webapps([row])
        assert failures == {}, f"a healthy fetch reported failures: {failures}"

        target = installer.release_dir(opts.vhost.pk, "fetch1")
        for path, body in FILES_V1.items():
            landed = os.path.join(target, path)
            assert os.path.isfile(landed), \
                f"{path} was not fetched to {landed}"
            with open(landed, "rb") as handle:
                assert handle.read() == body, \
                    f"{path} on disk is not the byte content S3 held"
            mode = os.stat(landed).st_mode & 0o777
            assert mode == 0o644, \
                (f"{path} landed mode {oct(mode)} — nginx workers cannot read "
                 f"a 0600 release file")
    finally:
        _exit(patches, root, www)


@th.django_unit_test("a second fetch of the same release downloads nothing")
def test_fetch_is_incremental(opts):
    """The whole reason the fetch is safe to run inside every install."""
    from mojo.apps.edge.services import www_sync

    root = tempfile.mkdtemp(prefix="edge-sync-")
    www = tempfile.mkdtemp(prefix="edge-www-")
    patches = _patches(root, www)
    _enter(patches)
    try:
        row = _row(opts.vhost.pk, "incr1", FILES_V1)
        fake = FakeS3(_objects(row["release"]["prefix"], FILES_V1))
        www_sync._client = fake

        assert www_sync.fetch_webapps([row]) == {}, "the first fetch failed"
        first = len(fake.calls)
        assert first == len(FILES_V1), \
            f"the first fetch downloaded {first} files, expected {len(FILES_V1)}"

        assert www_sync.fetch_webapps([row]) == {}, "the second fetch failed"
        assert len(fake.calls) == first, \
            (f"a re-fetch of identical bytes downloaded "
             f"{len(fake.calls) - first} files again — the hash skip is dead")
    finally:
        _exit(patches, root, www)


@th.django_unit_test("an object whose bytes do not match the manifest is refused")
def test_corrupt_object_is_refused(opts):
    """The manifest sha256 is the anchor. A swapped or truncated object must
    not become the file nginx serves."""
    from mojo.apps.edge.services import installer, www_sync

    root = tempfile.mkdtemp(prefix="edge-sync-")
    www = tempfile.mkdtemp(prefix="edge-www-")
    patches = _patches(root, www)
    _enter(patches)
    try:
        row = _row(opts.vhost.pk, "corrupt1", FILES_V1)
        objects = _objects(row["release"]["prefix"], FILES_V1)
        prefix = row["release"]["prefix"]
        objects[(RELEASE_BUCKET, f"{prefix}/index.html")] = b"<html>evil</html>"
        www_sync._client = FakeS3(objects)

        failures = www_sync.fetch_webapps([row])
        assert opts.vhost.pk in failures, \
            "a checksum mismatch was not reported as a failure"
        assert "index.html" in failures[opts.vhost.pk], \
            f"the failure does not name the bad file: {failures[opts.vhost.pk]}"

        target = installer.release_dir(opts.vhost.pk, "corrupt1")
        assert not os.path.exists(os.path.join(target, "index.html")), \
            "the corrupt object was written to the release directory anyway"
        leftovers = [name for name in os.listdir(target)
                     if name.startswith(www_sync.TMP_PREFIX)]
        assert leftovers == [], \
            f"a refused download left temporary files behind: {leftovers}"
        # The rest of the release still landed — a partial fetch is resumable.
        assert os.path.isfile(os.path.join(target, "assets/js/app.js")), \
            "one bad object discarded the files that verified fine"
    finally:
        _exit(patches, root, www)


@th.django_unit_test("an object larger than the manifest declares is aborted mid-transfer")
def test_oversized_object_is_aborted(opts):
    """The sha256 cannot judge an object until all of it is on disk, so the
    declared size has to bound the transfer. Without it one overwritten key
    fills the node's disk and takes down every vhost on it — the pool-wide
    failure this module exists to prevent."""
    from mojo.apps.edge.services import installer, www_sync

    root = tempfile.mkdtemp(prefix="edge-sync-")
    www = tempfile.mkdtemp(prefix="edge-www-")
    patches = _patches(root, www)
    _enter(patches)
    try:
        row = _row(opts.vhost.pk, "huge1", FILES_V1)
        objects = _objects(row["release"]["prefix"], FILES_V1)
        prefix = row["release"]["prefix"]
        # The manifest still declares the small, legitimately-registered file.
        # The object behind the key was replaced with something enormous.
        objects[(RELEASE_BUCKET, f"{prefix}/index.html")] = b"x" * 100000
        fake = FakeS3(objects)
        www_sync._client = fake
        declared = len(FILES_V1["index.html"])

        failures = www_sync.fetch_webapps([row])
        assert opts.vhost.pk in failures, \
            "an oversized object was not reported as a failure"
        assert "index.html" in failures[opts.vhost.pk], \
            f"the failure does not name the bad file: {failures[opts.vhost.pk]}"

        # The load-bearing assertion. Rejecting the object AFTER it lands is
        # what the sha256 already did; the guard has to stop the bytes.
        landed = fake.written[f"{prefix}/index.html"]
        assert landed < declared + 64, \
            (f"{landed} bytes were written to disk for a file the manifest "
             f"declares as {declared} — the transfer was not bounded")

        target = installer.release_dir(opts.vhost.pk, "huge1")
        assert not os.path.exists(os.path.join(target, "index.html")), \
            "the oversized object became the file nginx serves"
        leftovers = [name
                     for dirpath, _dirs, names in os.walk(target)
                     for name in names
                     if name.startswith(www_sync.TMP_PREFIX)]
        assert leftovers == [], \
            f"an aborted download left temporary files behind: {leftovers}"
    finally:
        _exit(patches, root, www)


@th.django_unit_test("a release directory is created traversable for nginx's workers")
def test_release_directories_are_traversable(opts):
    """The fetching user is not the serving user. Under a hardened umask an
    inherited-mode directory is 0700 and every 0644 file inside it is
    unreachable."""
    import stat

    from mojo.apps.edge.services import installer, www_sync

    root = tempfile.mkdtemp(prefix="edge-sync-")
    www = tempfile.mkdtemp(prefix="edge-www-")
    patches = _patches(root, www)
    _enter(patches)
    old_umask = os.umask(0o077)
    try:
        row = _row(opts.vhost.pk, "modes1", FILES_V1)
        www_sync._client = FakeS3(_objects(row["release"]["prefix"], FILES_V1))

        assert www_sync.fetch_webapps([row]) == {}, \
            "the fetch failed under a restrictive umask"

        target = installer.release_dir(opts.vhost.pk, "modes1")
        for path in (target, os.path.join(target, "assets", "js")):
            mode = stat.S_IMODE(os.stat(path).st_mode)
            assert mode & 0o055 == 0o055, \
                (f"{path} is {oct(mode)} — nginx's workers cannot traverse "
                 f"into the release they are meant to serve")
    finally:
        os.umask(old_umask)
        _exit(patches, root, www)


@th.django_unit_test("a bucket outside the allowlist is refused before any S3 call")
def test_undeclared_bucket_is_refused(opts):
    """Fail closed, and fail early — an allowlist checked after the download
    is not an allowlist."""
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.edge.services import www_sync

    root = tempfile.mkdtemp(prefix="edge-sync-")
    www = tempfile.mkdtemp(prefix="edge-www-")
    patches = _patches(root, www)
    _enter(patches)
    try:
        fake = FakeS3()
        www_sync._client = fake

        row = _row(opts.vhost.pk, "bucket1", FILES_V1, bucket="not-declared")
        failures = www_sync.fetch_webapps([row])
        assert opts.vhost.pk in failures, \
            "a release in an undeclared bucket was fetched"
        assert fake.calls == [], \
            f"S3 was called for an undeclared bucket: {fake.calls}"

        # And with NO buckets declared at all, the declared one fails too.
        Setting.remove("EDGE_RELEASE_BUCKETS", group=None)
        row = _row(opts.vhost.pk, "bucket2", FILES_V1)
        failures = www_sync.fetch_webapps([row])
        assert opts.vhost.pk in failures, \
            "with no declared buckets the fetch did not fail closed"
        assert fake.calls == [], \
            f"S3 was called with no bucket allowlist declared: {fake.calls}"
    finally:
        declare_release_buckets()
        _exit(patches, root, www)


@th.django_unit_test("leftover temp files from a crashed fetch are swept, declared ones are not")
def test_stale_temp_files_are_swept(opts):
    from mojo.apps.edge.services import installer, www_sync

    root = tempfile.mkdtemp(prefix="edge-sync-")
    www = tempfile.mkdtemp(prefix="edge-www-")
    patches = _patches(root, www)
    _enter(patches)
    try:
        # A release that legitimately ships a file named like our temp files.
        declared_name = f"{www_sync.TMP_PREFIX}shipped.txt"
        files = dict(FILES_V1)
        files[declared_name] = b"this one belongs to the release\n"
        row = _row(opts.vhost.pk, "sweep1", files)
        www_sync._client = FakeS3(_objects(row["release"]["prefix"], files))

        target = installer.release_dir(opts.vhost.pk, "sweep1")
        os.makedirs(os.path.join(target, "assets/js"), exist_ok=True)
        stale_root = os.path.join(target, f"{www_sync.TMP_PREFIX}crashed")
        stale_nested = os.path.join(
            target, "assets/js", f"{www_sync.TMP_PREFIX}crashed2")
        for path in (stale_root, stale_nested):
            with open(path, "w") as handle:
                handle.write("half a download")

        failures = www_sync.fetch_webapps([row])
        assert failures == {}, f"the fetch failed: {failures}"

        assert not os.path.exists(stale_root), \
            "a stale temp file at the release root was not swept"
        assert not os.path.exists(stale_nested), \
            "a stale temp file in a nested directory was not swept"
        assert os.path.isfile(os.path.join(target, declared_name)), \
            ("the sweep deleted a file the manifest declares — it corrupted "
             "the release it just verified")
    finally:
        _exit(patches, root, www)


@th.django_unit_test("a fetch that runs past its budget stops and says so")
def test_fetch_budget_is_enforced(opts):
    """A converge runs on a timer; an unbounded fetch would still be running
    when the next one starts. The remainder is left for the next converge."""
    from mojo.apps.edge.services import www_sync

    root = tempfile.mkdtemp(prefix="edge-sync-")
    www = tempfile.mkdtemp(prefix="edge-www-")
    patches = _patches(root, www)
    _enter(patches)
    try:
        row = _row(opts.vhost.pk, "budget1", FILES_V1)
        fake = FakeS3(_objects(row["release"]["prefix"], FILES_V1))
        www_sync._client = fake

        with mock.patch.object(www_sync, "fetch_budget", return_value=0):
            failures = www_sync.fetch_webapps([row])

        assert opts.vhost.pk in failures, \
            "an exhausted fetch budget was not reported as a failure"
        assert "budget" in failures[opts.vhost.pk], \
            f"the failure does not name the budget: {failures[opts.vhost.pk]}"
        assert fake.calls == [], \
            f"files were downloaded after the budget was spent: {fake.calls}"
    finally:
        _exit(patches, root, www)


@th.django_unit_test("prune keeps every release a retained generation still points at")
def test_prune_keeps_referenced_releases(opts):
    """Rollback re-promotes an older version, so every file hash-skips and its
    directory keeps the OLDEST mtime. Pruning by age alone would delete the
    release the live generation is symlinked at."""
    from mojo.apps.edge.services import installer, render, www_sync

    root = tempfile.mkdtemp(prefix="edge-sync-")
    www = tempfile.mkdtemp(prefix="edge-www-")
    patches = _patches(root, www)
    _enter(patches)
    try:
        versions = ["old1", "old2", "old3", "old4", "new5"]
        dirs = {}
        for index, version in enumerate(versions):
            path = installer.release_dir(opts.vhost.pk, version)
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, "index.html"), "w") as handle:
                handle.write(version)
            # Oldest first, so `old1` — the rolled-back-to release — is the
            # one a pure mtime sort would delete first.
            stamp = time.time() - (len(versions) - index) * 1000
            os.utime(path, (stamp, stamp))
            dirs[version] = path

        # A retained generation still serving `old1`: this is the state a
        # rollback-then-promote leaves behind.
        gen_www = os.path.dirname(render.www_dir("genrollback", opts.vhost.pk))
        os.makedirs(gen_www, exist_ok=True)
        os.symlink(dirs["old1"], render.www_dir("genrollback", opts.vhost.pk))

        row = _row(opts.vhost.pk, "new5", FILES_V1)
        removed = www_sync.prune_releases([row], keep=2)

        assert os.path.isdir(dirs["new5"]), \
            "prune deleted the release the desired state names"
        assert os.path.isdir(dirs["old1"]), \
            ("prune deleted a release a retained generation still symlinks — "
             "rollback would now need a re-download")
        assert os.path.isdir(dirs["old4"]), \
            "prune did not retain the newest unreferenced release"
        for version in ("old2", "old3"):
            assert not os.path.exists(dirs[version]), \
                f"{version} is unreferenced and past keep, but was retained"
            assert dirs[version] in removed, \
                f"{version} was deleted but not reported in the return value"
    finally:
        _exit(patches, root, www)


@th.django_unit_test("a failed fetch keeps serving the previous release, and heals itself")
def test_install_degrades_then_heals(opts):
    """The end-to-end contract: a promote whose bytes are not in S3 yet must
    not take a live site down, must be recorded, must be retried on the very
    next converge even though the generation did not change, and must repoint
    the web root the moment the bytes arrive."""
    from mojo.apps.edge.services import installer, releases, render, www_sync

    root = tempfile.mkdtemp(prefix="edge-sync-")
    www = tempfile.mkdtemp(prefix="edge-www-")
    patches = _patches(root, www)
    _enter(patches)
    try:
        _seed_cert(opts)
        v1 = make_release(opts.webapp, "live1", status="uploaded",
                          manifest=_manifest(FILES_V1))
        releases.promote(opts.webapp, v1)

        fake = FakeS3(_objects(v1.storage_prefix(), FILES_V1))
        www_sync._client = fake
        with mock.patch.object(installer, "_run", Recorder()):
            first = installer.install(pool=POOL)

        v1_dir = installer.release_dir(opts.vhost.pk, "live1")
        link = render.www_dir(first.generation, opts.vhost.pk)
        assert os.path.realpath(link) == os.path.realpath(v1_dir), \
            "the first converge did not serve the release it fetched"

        # Promote a release S3 does not have yet.
        v2 = make_release(opts.webapp, "live2", status="uploaded",
                          manifest=_manifest(FILES_V2))
        releases.promote(opts.webapp, v2)

        with mock.patch.object(installer, "_run", Recorder()):
            second = installer.install(pool=POOL)

        assert second.changed, "the promote did not make the node reinstall"
        assert opts.vhost.pk not in second.excluded, \
            ("a vhost that was already serving went dark over an unfetchable "
             "release — it should serve the previous one")
        second_link = render.www_dir(second.generation, opts.vhost.pk)
        assert os.path.realpath(second_link) == os.path.realpath(v1_dir), \
            (f"the degraded vhost is not serving its previous release: "
             f"{os.path.realpath(second_link)}")
        with open(os.path.join(second_link, "index.html"), "rb") as handle:
            assert handle.read() == FILES_V1["index.html"], \
                "the stale-served bytes are not the ones it served before"
        installed = installer.read_installed()
        assert installed.get("www_pending") == {str(opts.vhost.pk): "live2"}, \
            (f"the pending fetch was not recorded: "
             f"{installed.get('www_pending')}")

        # S3 catches up. The generation has NOT changed, so this converge only
        # happens at all because www_pending defeats the short-circuit.
        fake.objects.update(_objects(v2.storage_prefix(), FILES_V2))
        with mock.patch.object(installer, "_run", Recorder()):
            third = installer.install(pool=POOL)

        assert third.changed, \
            ("a degraded node short-circuited on an unchanged generation — it "
             "would never retry the fetch")
        assert third.generation == second.generation, \
            "the healing converge should re-install the SAME generation"
        v2_dir = installer.release_dir(opts.vhost.pk, "live2")
        third_link = render.www_dir(third.generation, opts.vhost.pk)
        assert os.path.realpath(third_link) == os.path.realpath(v2_dir), \
            "the healed vhost is still not serving the promoted release"
        with open(os.path.join(third_link, "index.html"), "rb") as handle:
            assert handle.read() == FILES_V2["index.html"], \
                "the healed web root does not serve the new bytes"
        assert not installer.read_installed().get("www_pending"), \
            "a healed node still reports a pending fetch"
        assert os.path.isdir(v1_dir), \
            "the previous release was pruned — rollback would need a download"
    finally:
        from mojo.apps.edge.models import WebApp
        WebApp.objects.filter(pk=opts.webapp.pk).update(current_release=None)
        _exit(patches, root, www)


@th.django_unit_test("a release directory is never fetched outside the vhost's own tree")
def test_fetch_refuses_an_escaping_version(opts):
    """release_dir validates the version, so a row that somehow carried a
    traversal cannot make the fetcher write outside /opt/www/<vhost>/."""
    from mojo.apps.edge.services import www_sync

    root = tempfile.mkdtemp(prefix="edge-sync-")
    www = tempfile.mkdtemp(prefix="edge-www-")
    patches = _patches(root, www)
    _enter(patches)
    try:
        fake = FakeS3()
        www_sync._client = fake
        row = _row(opts.vhost.pk, "../../etc", FILES_V1,
                   prefix="webapps/1/1/releases/escape")

        err = raises(www_sync.sync_release, row)
        assert err is not None, \
            "sync_release accepted a version that escapes the vhost directory"
        assert fake.calls == [], \
            f"S3 was called for an escaping release path: {fake.calls}"

        failures = www_sync.fetch_webapps([row])
        assert opts.vhost.pk in failures, \
            "fetch_webapps let an escaping row raise instead of degrading it"
    finally:
        _exit(patches, root, www)
