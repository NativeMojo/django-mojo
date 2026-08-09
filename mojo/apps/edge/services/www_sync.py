"""
Pull promoted release bytes from S3 onto a node, verified against the manifest.

This closes the loop the release pipeline used to leave open: CI uploads to S3,
an admin promotes, and until now *something outside this codebase* had to put
the bytes on the node. Now `installer.install()` fetches them itself, so a
promote lands on the fleet with no operator action.

## The properties that matter

**Nothing is trusted, including S3.** Every object is downloaded to a temporary
file, re-hashed, and only then `os.replace`d into place. The manifest sha256 is
the anchor: a bucket that was swapped, a key that was overwritten, or a
truncated transfer all fail the same way — the temp file is unlinked and the
release directory keeps whatever it already had.

**A fetch is incremental.** A file whose on-disk sha256 already matches the
manifest is skipped, so the common case (a promote that changes three of four
hundred files, or a converge that changes no webapp at all) does almost no S3
work. That is what makes it safe to run inside every install.

**One unfetchable release degrades one vhost.** `fetch_webapps` never raises:
it returns a problem string per vhost, and the installer decides what that
means (serve the previous bytes where there are some, exclude the vhost where
there are none). A tenant's broken release must not stop a pool converging —
same rule as unreadable certificate material in `installer._exclude_or_abort`.

**Credentials are ambient.** `get_s3_client` passes NO access key, so boto3's
default chain resolves — on a node that is the instance role, which needs
`s3:GetObject` on the release prefix and nothing else. The platform's own
`S3Config` rows are deliberately not reachable from here: a node reads
releases, it does not hold the credentials that sign uploads.

Partial state is always safe: files that landed stay landed (the next converge
skips them by hash), and nothing serves a half-fetched release, because the
web-root symlink only moves once the installer is satisfied.
"""

import hashlib
import os
import shutil
import tempfile
import time

from mojo.helpers import logit
from mojo.helpers.settings import settings

from mojo.apps.edge import validators
from mojo.apps.edge.services import render


# Every in-flight download is named with this prefix, inside the release
# directory it is destined for, so a crashed fetch leaves an obvious,
# sweepable artifact and never a plausible-looking release file.
TMP_PREFIX = ".wwwsync-"

# Read chunk for hashing. Big enough that a 50MB bundle is not a syscall
# storm, small enough that a release file never sits in memory whole.
_HASH_CHUNK = 1024 * 1024

# How many problems a failure message names before it is truncated. Same shape
# as `releases.complete` — a CI job (or an operator) should learn WHICH files
# failed, not just that something did.
_MAX_REPORTED = 10

# Built once per process. Assign directly in tests.
_client = None


def fetch_timeout():
    """Per-attempt connect/read timeout, seconds."""
    return int(settings.get_static("EDGE_RELEASE_FETCH_TIMEOUT", 60))


def fetch_budget():
    """Wall-clock ceiling for one release's fetch, seconds.

    A converge runs on a timer. Without a ceiling, a big bundle over a slow
    link keeps the install running until the next one starts on top of it.
    Past the budget the remaining files are simply left for the next converge,
    which resumes by hash — the fetch is incremental, so nothing is redone.
    """
    return int(settings.get_static("EDGE_RELEASE_FETCH_BUDGET", 300))


def keep_releases():
    """Retained release directories per vhost. Parity with keep_generations."""
    return int(settings.get_static("EDGE_KEEP_RELEASES", 5))


def get_s3_client():
    """The S3 client this node reads releases with.

    **No access_key/secret_key on purpose.** Passing them would make a node
    read releases with the platform's upload-signing identity; omitting them
    lets boto3's default chain find the instance role, which is scoped to
    `s3:GetObject` on the release prefix. `get_static`, not `get`, for the
    region: a DB-backed Setting must not be able to point a node's fetches at
    another region's endpoint.
    """
    global _client

    if _client is None:
        from mojo.helpers.aws.client import get_client

        _client = get_client(
            "s3",
            region=settings.get_static("AWS_REGION", None),
            timeout=fetch_timeout())
    return _client


def _file_sha256(path):
    """Streamed hex digest of `path`, or None when it cannot be read."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(_HASH_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        # Absent, a directory, unreadable — all mean "not the file we want",
        # which is exactly what a None digest says to every caller here.
        return None
    return digest.hexdigest()


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _make_servable_dir(path):
    """Create a release directory nginx's workers can traverse.

    The mode is explicit for the same reason the file mode is: the fetching
    user is not the serving user. Under a hardened umask (0077 is ordinary in
    a systemd unit) an inherited-mode directory is 0700, and every file we
    carefully chmod 0644 sits behind a door nginx cannot open.
    """
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o755)
    except OSError:
        # Someone else owns it. The traversal check is nginx's to fail, not
        # ours to abort a converge over.
        pass


def _safe_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


class _SizeGuard:
    """Abort a download whose bytes exceed what the manifest declared.

    The sha256 check cannot run until the whole object is on disk, so without
    this a single overwritten key streams unbounded bytes into the release
    directory and fills the node's disk — taking down every vhost on it, which
    is exactly the pool-wide failure this module exists to prevent. boto3
    propagates an exception raised in the callback, which unlinks the temp.
    """

    def __init__(self, entry):
        self.entry = entry
        self.seen = 0

    def __call__(self, chunk):
        self.seen += chunk
        if self.seen > self.entry["size"]:
            raise ValueError(
                f"object is larger than the manifest declares "
                f"({self.entry['size']} bytes)")


def _sweep_tmp(target, declared, started):
    """Delete leftover `.wwwsync-*` files under `target`.

    Recursive: manifest paths nest, so a temp file lands beside its nested
    final, not at the release root. `declared` holds the manifest's own
    relative paths — a release is allowed to ship a file whose name happens to
    look like ours, and deleting it would corrupt the very release we just
    verified.

    `started` is when this fetch began: anything newer belongs to an install
    running concurrently with ours (the broadcast and the sweep can overlap),
    and unique temp names do not make deleting someone else's in-flight
    download safe. s3transfer's own inner temp carries our prefix too.
    """
    # os.walk descends into its root even when the root is itself a symlink, so
    # the per-entry containment check is not enough to bound what we delete.
    if not validators.contained_under(validators.www_base(), target):
        return
    for dirpath, _dirnames, filenames in os.walk(target):
        for name in filenames:
            if not name.startswith(TMP_PREFIX):
                continue
            full = os.path.join(dirpath, name)
            if os.path.relpath(full, target) in declared:
                continue
            try:
                if os.path.getmtime(full) >= started:
                    continue
            except OSError:
                continue
            _unlink(full)


def sync_release(row):
    """Fetch one `desired_webapps` row onto disk. None on success.

    Returns a problem string on failure — this is the degrade path, so it does
    not raise for anything a single release can do wrong. It DOES raise for
    the two things that are configuration errors rather than release errors:
    an undeclared bucket and a malformed manifest, both of which mean the row
    should never have reached a node.
    """
    from mojo.apps.edge.services import installer

    # Fail closed, and before a single S3 call: the bucket must still be one
    # this deployment declared. Same re-check at use time as `releases.register`
    # does at mint time — a bucket removed from the allowlist stops being read
    # as well as written.
    validators.validate_release_bucket(row["bucket"])
    cleaned = validators.validate_manifest(row["release"]["manifest"])

    version = row["release"]["version"]
    # release_dir validates the version, so the path cannot climb out of the
    # vhost's own tree even if a row reached us with something odd in it.
    target = installer.release_dir(row["vhost"], version)
    _make_servable_dir(target)

    bucket = row["bucket"]
    prefix = row["release"]["prefix"]
    client = get_s3_client()
    started = time.time()
    deadline = time.monotonic() + fetch_budget()

    # Built from the WHOLE manifest, not from the entries this pass reached:
    # a budget-truncated run must not sweep a declared file it never looked at.
    declared = {entry["path"] for entry in cleaned}
    problems = []
    fetched = 0
    skipped = 0

    for entry in cleaned:
        final = os.path.join(target, entry["path"])
        try:
            # Belt and braces over the manifest whitelist: this also catches a
            # symlink already inside the release tree that points elsewhere.
            if not validators.contained_under(target, final):
                problems.append(
                    f"{entry['path']}: resolves outside the release directory")
                continue

            if _file_sha256(final) == entry["sha256"]:
                skipped += 1
                continue

            # Checked here rather than at the top of the loop: the budget
            # bounds time spent waiting on S3, and hashing files that are
            # already correct is local work. Counting it would let a large
            # release that is fully on disk exhaust its own budget every
            # converge and never report success.
            if time.monotonic() >= deadline:
                problems.append(
                    f"fetch budget exceeded after {fetched + skipped} files")
                break

            parent = os.path.dirname(final)
            _make_servable_dir(parent)
            # Unique name in the destination directory, so two overlapping
            # installs cannot write the same temp file, and the final move is
            # a same-filesystem atomic replace.
            handle, tmp = tempfile.mkstemp(dir=parent, prefix=TMP_PREFIX)
            # Closed immediately: download_file opens the path itself, so
            # holding this descriptor leaks one per manifest entry.
            os.close(handle)
            try:
                client.download_file(bucket, f"{prefix}/{entry['path']}", tmp,
                                     Callback=_SizeGuard(entry))
                if _file_sha256(tmp) != entry["sha256"]:
                    problems.append(f"{entry['path']}: checksum mismatch")
                    _unlink(tmp)
                    continue
                # nginx WORKERS read these files, not the app user that
                # fetched them. Set explicitly: the mkstemp mode does not
                # survive (s3transfer renames its own file over ours), and
                # the runner's umask is not ours to assume.
                os.chmod(tmp, 0o644)
                os.replace(tmp, final)
            except Exception:
                _unlink(tmp)
                raise
            fetched += 1
        except Exception as err:
            problems.append(f"{entry['path']}: {err}")

    _sweep_tmp(target, declared, started)

    if problems:
        shown = "; ".join(problems[:_MAX_REPORTED])
        more = ("" if len(problems) <= _MAX_REPORTED
                else f" (+{len(problems) - _MAX_REPORTED} more)")
        message = f"{shown}{more}"
        logit.error(
            f"edge: release {version} for vhost {row['vhost']} did not "
            f"fetch cleanly: {message}")
        return message

    logit.info(
        f"edge: release {version} for vhost {row['vhost']} is on disk "
        f"({fetched} fetched, {skipped} already current)")
    return None


def fetch_webapps(webapps):
    """Fetch every desired release. Returns {vhost_id: problem message}.

    Never raises. A surprise from one row — an unreadable manifest, a disk
    error, a bucket dropped from the allowlist — degrades that vhost and
    leaves the rest of the pool converging.
    """
    failures = {}
    for row in webapps or []:
        try:
            problem = sync_release(row)
            if problem:
                # Inside the try on purpose: the installer's isolation rests on
                # this function returning rather than raising, so even a row
                # too malformed to describe must not escape.
                version = (row.get("release") or {}).get("version")
                failures[row["vhost"]] = (
                    f"{row.get('slug')} release {version} "
                    f"could not be fetched: {problem}")
        except Exception as err:
            logit.exception("edge: release fetch raised")
            vhost_id = row.get("vhost") if hasattr(row, "get") else None
            if vhost_id is not None:
                failures[vhost_id] = (
                    f"release could not be fetched: "
                    f"{err or err.__class__.__name__}")
    return failures


def _referenced_releases(vhost_id):
    """Realpaths of the releases any retained generation still points at.

    Exemption by REFERENCE, not by mtime, and that is the whole point of this
    function. Rolling back re-promotes an older version whose files are
    already on disk, so every file hash-skips and the directory's mtime stays
    the oldest one there. An mtime-only prune would delete the release the
    live generation is symlinked at — the documented "rollback is a symlink
    flip" property, deleted from under itself.
    """
    root = os.path.join(render.edge_root(), "generations")
    referenced = set()
    try:
        names = os.listdir(root)
    except OSError:
        return referenced
    for name in names:
        link = render.www_dir(name, vhost_id)
        if not os.path.islink(link):
            continue
        try:
            resolved = os.path.realpath(link)
        except OSError:
            continue
        if os.path.isdir(resolved):
            referenced.add(resolved)
    return referenced


def prune_releases(webapps, keep=None):
    """Drop old release directories per vhost. Returns the paths removed.

    Two exemptions, both unconditional: the version the desired state names,
    and every release a retained generation still references. `keep` counts
    the desired release, so it bounds how many *extra* unreferenced releases
    are retained for a quick rollback — which is why `EDGE_KEEP_RELEASES = 0`
    still cannot delete anything that is being served.
    """
    from mojo.apps.edge.services import installer

    if keep is None:
        keep = keep_releases()

    removed = []
    for row in webapps or []:
        vhost_id = row["vhost"]
        try:
            desired = installer.release_dir(vhost_id, row["release"]["version"])
        except Exception:
            # A row we cannot even name a directory for is not one we may
            # start deleting siblings of.
            continue
        base = os.path.dirname(desired)

        exempt = {os.path.realpath(desired)}
        exempt.update(_referenced_releases(vhost_id))

        try:
            entries = [os.path.join(base, name) for name in os.listdir(base)]
        except OSError:
            continue
        entries = [
            path for path in entries
            if os.path.isdir(path) and os.path.realpath(path) not in exempt
        ]
        # Guarded: prune runs AFTER the reload and after installed.json is
        # written, so a directory vanishing under us here would fail a job
        # whose install actually succeeded.
        entries.sort(key=_safe_mtime, reverse=True)
        for path in entries[max(keep - 1, 0):]:
            shutil.rmtree(path, ignore_errors=True)
            removed.append(path)

    if removed:
        logit.info(f"edge: pruned {len(removed)} retired release directories")
    return removed
