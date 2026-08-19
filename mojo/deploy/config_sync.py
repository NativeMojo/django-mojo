#!/usr/bin/env python3
"""Pull this node's application config from S3.

Every node runs the same config, published once by an operator. This is a PULL
in one direction only — S3 is authoritative, no node ever writes back.

    s3://<AWS_CONFIG_BUCKET>/<AWS_CONFIG_PREFIX>/django.conf
        |
        v   python3 -m mojo.deploy.config_sync (systemd oneshot at boot + timer)
    /opt/api/var/django.conf   0640, owned app_user:web_user

WHY THIS EXISTS. The alternative is baking django.conf into the AMI, which works
and is simpler, but makes the image a secret-bearing artifact: every secret you
have, frozen into a snapshot, and rotating any one of them means re-baking or
touching every node by hand. A node launched from a three-month-old AMI boots
with three-month-old config.

WHAT THIS DOES NOT DO is remove the bootstrap secret. Reading from S3 needs
credentials, and without an instance profile those live on disk. What it buys is
that the ONE key left in the image can be scoped read-only to a single S3
prefix, so an image leak exposes one config file rather than the account — and
every other secret becomes rotatable with an upload. When nodes can carry an
instance profile, delete bootstrap.conf and this becomes genuinely zero-secret
with no other change.

CONFIG. Read from bootstrap.conf, deliberately NOT from django.conf: on a fresh
node django.conf is the thing we are fetching and does not exist yet.

    AWS_REGION               required
    AWS_CONFIG_BUCKET        required  — bucket holding the published config
    AWS_CONFIG_PREFIX        required  — e.g. "config/wmx/prod"
    AWS_CONFIG_FILENAME      optional  — object name under the prefix
    AWS_KEY / AWS_SECRET     OPTIONAL  — omit to use the instance role
    CONFIG_SYNC_RESTART      optional  — restart the app when config changes
    CONFIG_SYNC_OWNER        optional  — user:group for the installed file
    CONFIG_SYNC_SERVICE      optional  — systemd unit to restart
                                         (default: mojo-asgi.service)
    CONFIG_SYNC_REQUIRE_SHA  optional  — refuse to install a published object
                                         that carries no sha256 metadata.
                                         Off by default: turning it on by
                                         default would break every publisher
                                         that uses a plain `aws s3 cp`.
    CONFIG_SYNC_OVERRIDE_ALLOWED_KEYS
                              optional  — immutable deployment delegation for
                                         typed Admin fleet overrides. Empty
                                         disables the override object.
    CONFIG_SYNC_OVERRIDE_FILENAME
                              optional  — override object name under the same
                                         prefix (default django.override.json)

The path to bootstrap.conf is NOT itself configurable through bootstrap.conf —
it cannot be. Use --config when it lives somewhere else.

Install (every node):

    systemctl enable --now config-sync.service config-sync.timer

Unconfigured is not an error. With AWS_CONFIG_BUCKET absent it logs one debug
line and exits 0, so this can live in the base image and stay dormant on a box
that still bakes its config in.
"""

import argparse
import errno
import fcntl
import hashlib
import os
import pwd
import grp
import shutil
import socket
import subprocess
import sys
import tempfile
import time

# boto3 is imported lazily, past the config gate, so an unconfigured box pays
# nothing and a box missing boto3 fails with one clear line rather than a
# traceback on every timer tick.

BOOTSTRAP_PATH = "/opt/api/var/bootstrap.conf"
TARGET_PATH = "/opt/api/var/django.conf"

# The lock lives NEXT TO THE TARGET, not in /var/run. What it protects is
# concurrent writes to the config file, so the right permission to hold it is
# "can write the config" — which makes it work identically whether this runs as
# root from systemd or as the app user by hand. A /var/run lock would have made
# the script root-only for no reason connected to what it guards.
LOCK_SUFFIX = ".config_sync.lock"

# The file lands 0640, owned CONFIG_SYNC_OWNER (app_user:web_user). It holds
# every downstream credential the app has, so it stays unreadable to the rest
# of the box — but the writer (app_user) and the reader (the web app, running
# as web_user) are different processes, so the group-read bit is load-bearing:
# 0600 would strand whichever of the two is not the owner. See check_node.py's
# check_config_plane, which audits nodes against this same contract.
FILE_MODE = 0o640

# Staging directories are created as <target dir>/config_sync.XXXXXX by mkdtemp.
STAGING_PREFIX = "config_sync."

DEFAULT_SERVICE = "mojo-asgi.service"

# Logging: the repo's standard graceful-fallback idiom. On a bootstrap node the
# except branch ALWAYS fires — mojo.helpers.logit reads paths.LOG_ROOT at module
# level and that attribute only exists once Django settings have configured the
# paths, which by definition has not happened yet when this runs. See
# mojo/deploy/__init__.py for the full contract.
try:
    from mojo.helpers import logit
    log = logit.get_logger("config_sync", "config_sync.log")
except Exception:
    import logging
    log = logging.getLogger("config_sync")


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def read_config(path):
    """Parse a simple key=value file. Malformed lines are skipped rather than
    raised — this runs on a timer and one bad line should not stop config
    distribution."""
    config = {}
    try:
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        return {}
    except OSError as err:
        log.error("cannot read %s: %s", path, err)
    return config


def as_bool(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# aws
# ---------------------------------------------------------------------------

def build_s3_client(config):
    """S3 client, preferring the instance role.

    Static credentials are used only when BOTH are present, so a box that has
    had them scrubbed falls through to the role rather than failing.

    NOTE the deliberate divergence from mojo/helpers/aws/client.py, which raises
    PartialCredentialsError when exactly one of the pair is set. A deploy script
    that refuses to run is worse than one that falls back — so half a pair is a
    loud warning here, not a failure."""
    import boto3

    region = config.get("AWS_REGION")
    key = config.get("AWS_KEY")
    secret = config.get("AWS_SECRET")
    if key and secret:
        log.debug("using static credentials from %s", BOOTSTRAP_PATH)
        return boto3.client(
            "s3", region_name=region,
            aws_access_key_id=key, aws_secret_access_key=secret)
    if key or secret:
        log.warning("only one of AWS_KEY/AWS_SECRET is set — ignoring it and "
                    "falling back to the ambient credential chain")
    log.debug("no AWS_KEY/AWS_SECRET — using the instance role")
    return boto3.client("s3", region_name=region)


def remote_details(s3, bucket, key, missing_ok=False):
    """(etag, sha256-from-metadata, content-length) for one object.

    A missing object is NOT normal here — a missing config means someone
    pointed this node at the wrong prefix. Reported as an error, and the caller
    keeps what it has. (None, None) means "no such object"; an etag with no
    sha256 means "published, but unverifiable".
    """
    from botocore.exceptions import ClientError

    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as err:
        code = err.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            if not missing_ok:
                log.error("nothing published at s3://%s/%s", bucket, key)
            return None, None, None
        raise
    return (head.get("ETag", "").strip('"'),
            head.get("Metadata", {}).get("sha256"),
            head.get("ContentLength"))


def remote_digest(s3, bucket, key, missing_ok=False):
    """Compatibility pair for callers that do not need the object size."""
    etag, digest, _ = remote_details(s3, bucket, key, missing_ok=missing_ok)
    return etag, digest


# ---------------------------------------------------------------------------
# local
# ---------------------------------------------------------------------------

def file_sha256(path):
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def resolve_owner(spec):
    """"user:group" -> (uid, gid), or (None, None) if unset/unresolvable.

    Unresolvable is a warning, not a failure: a wrong owner is recoverable, and
    refusing to install config over it is not.
    """
    if not spec:
        return None, None
    user, _, group = spec.partition(":")
    try:
        uid = pwd.getpwnam(user).pw_uid
        gid = grp.getgrnam(group).gr_gid if group else pwd.getpwnam(user).pw_gid
        return uid, gid
    except KeyError as err:
        log.warning("cannot resolve CONFIG_SYNC_OWNER %r (%s) — leaving as root",
                    spec, err)
        return None, None


def install(temp_path, dest_path, owner_spec):
    """Move the staged file into place atomically, mode and owner first.

    Order matters: chmod/chown BEFORE os.replace, so the file is never visible
    at the destination with the wrong permissions, even briefly. This ordering
    is the load-bearing security property of this module — do not reorder it,
    and do not replace os.replace with an open()+write().
    """
    os.chmod(temp_path, FILE_MODE)
    uid, gid = resolve_owner(owner_spec)
    if uid is not None:
        os.chown(temp_path, uid, gid)
    os.replace(temp_path, dest_path)
    log.info("installed %s (mode %o, owner %s)", dest_path, FILE_MODE,
             owner_spec or "root")


def restart_app(config, dry_run):
    """Restart the app so it picks up the new config.

    JITTERED BY HOSTNAME. Every node polls the same bucket on the same timer, so
    an un-jittered restart takes the whole fleet out simultaneously the moment a
    config lands — the load balancer health-checks them all out at once and the
    site is down for the length of a startup. The delay is derived from the
    hostname rather than randomly, so a given node's slot is stable across runs
    and reproducible when you are trying to work out what happened.
    """
    service = config.get("CONFIG_SYNC_SERVICE") or DEFAULT_SERVICE
    host = socket.gethostname().encode("utf-8", "replace")
    delay = int(hashlib.sha256(host).hexdigest()[:4], 16) % 60

    if dry_run:
        log.info("[dry-run] would sleep %ds then restart %s", delay, service)
        return True

    log.info("config changed — restarting %s in %ds (hostname jitter)",
             service, delay)
    time.sleep(delay)
    done = subprocess.run(
        ["systemctl", "--no-block", "restart", service],
        capture_output=True, check=False)
    if done.returncode != 0:
        log.error("%s restart FAILED (%d): %s", service, done.returncode,
                  done.stderr.decode("utf-8", "replace").strip())
        return False
    log.info("%s restart enqueued", service)
    return True


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------

def acquire_lock(target):
    """Single instance. The timer and the boot-time oneshot can overlap, and two
    processes writing the same destination is exactly the race that produces a
    truncated config.

    Returns None if another run holds it. Raises nothing the caller has to
    handle beyond that — an unwritable lock directory is reported as an error
    and treated as "cannot run", not as a traceback on every timer tick.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(target)), LOCK_SUFFIX)
    try:
        handle = open(path, "w")
    except OSError as err:
        log.error("cannot open lock %s: %s", path, err)
        return None
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as err:
        handle.close()
        if err.errno in (errno.EACCES, errno.EAGAIN):
            log.info("another config_sync is running — skipping this tick")
            return None
        raise
    return handle


def sweep_stale_staging(directory):
    """Remove staging directories left by a run that never unwound.

    rmtree in a `finally` only covers the paths where Python gets to unwind. A
    SIGKILL, an OOM kill, or a power loss mid-transfer runs no finally at all
    and leaves a config_sync.XXXXXX directory behind forever. Safe to do here
    because acquire_lock is already held, so no concurrent run owns one.
    """
    try:
        names = os.listdir(directory)
    except OSError as err:
        log.debug("cannot sweep staging directories in %s: %s", directory, err)
        return
    for name in names:
        if not name.startswith(STAGING_PREFIX):
            continue
        path = os.path.join(directory, name)
        if not os.path.isdir(path):
            continue
        log.info("removing stale staging directory %s", path)
        shutil.rmtree(path, ignore_errors=True)


def sync(s3, config, target, remote_name, dry_run):
    from botocore.exceptions import ClientError
    from mojo.deploy import config_override

    bucket = config["AWS_CONFIG_BUCKET"]
    # The published object's name is its own setting, NOT basename(target).
    # Deriving it from the local path means changing where the file installs
    # silently changes which object is fetched, which is a surprising way to
    # get a 404 on a bucket that plainly has the config in it.
    key = "%s/%s" % (config["AWS_CONFIG_PREFIX"].strip("/"), remote_name)

    # A config-sync unit ordered Before=mojo-asgi.service runs before
    # mojo-asgi's ExecStartPre has created /opt/api/var, so mkdtemp below would
    # fail on a genuinely fresh node for a reason that has nothing to do with S3.
    target_dir = os.path.dirname(os.path.abspath(target))
    os.makedirs(target_dir, exist_ok=True)
    # The sweep deletes directories as root, so it must not run under --dry-run:
    # an operator reaching for --dry-run to inspect a node without touching it
    # gets exactly that. (makedirs stays -- it is idempotent, and the dry-run
    # still stages a download below in order to report the sha it would install.)
    if dry_run:
        log.info("[dry-run] would sweep stale staging directories under %s",
                 target_dir)
    else:
        sweep_stale_staging(target_dir)

    allowed = config_override.normalize_allowed(
        config.get("CONFIG_SYNC_OVERRIDE_ALLOWED_KEYS", ""))
    override_key = None
    override_etag = override_sha = override_size = None
    if allowed:
        override_name = config.get("CONFIG_SYNC_OVERRIDE_FILENAME") or "django.override.json"
        override_key = "%s/%s" % (
            config["AWS_CONFIG_PREFIX"].strip("/"), override_name)
        override_etag, override_sha, override_size = remote_details(
            s3, bucket, override_key, missing_ok=True)
        if override_etag is not None and not override_sha:
            log.error("fleet override carries no sha256 metadata — refusing it")
            return 1
        if (override_size is not None and
                override_size > config_override.MAX_DOCUMENT_BYTES):
            log.error("fleet override exceeds the maximum document size — refusing it")
            return 1

    etag, published_sha = remote_digest(s3, bucket, key)
    local_sha = file_sha256(target)

    if published_sha and published_sha == local_sha and override_etag is None:
        log.debug("already holding the published config (%s)", published_sha[:12])
        return 0

    if etag is not None and not published_sha:
        # The object exists but the publisher set no sha256 metadata, which is
        # what a hand-run `aws s3 cp` produces. The integrity check below then
        # silently no-ops, so say so out loud.
        if as_bool(config.get("CONFIG_SYNC_REQUIRE_SHA")):
            log.error("published object carries no sha256 metadata and "
                      "CONFIG_SYNC_REQUIRE_SHA is set — refusing to install")
            return 1
        log.warning("published object carries no sha256 metadata — "
                    "installing unverified")

    staging_dir = tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=target_dir)
    temp_path = os.path.join(staging_dir, "download")
    try:
        try:
            s3.download_file(bucket, key, temp_path)
        except ClientError as err:
            # THE important failure. Leaving the existing config alone is always
            # better than replacing it with nothing: a node with stale config
            # serves, a node with no config does not start.
            log.error("download of s3://%s/%s failed (%s) — keeping current config",
                      bucket, key, err.response.get("Error", {}).get("Code", "?"))
            return 1

        downloaded_sha = file_sha256(temp_path)
        if not os.path.getsize(temp_path):
            log.error("published config is empty — refusing to install")
            return 1
        if published_sha and downloaded_sha != published_sha:
            log.error("downloaded config does not match its published sha256 "
                      "— refusing to install")
            return 1
        if override_etag is not None:
            try:
                response = s3.get_object(
                    Bucket=bucket, Key=override_key,
                    IfMatch=f'"{override_etag}"')
            except ClientError as err:
                log.error("download of fleet override failed (%s) — keeping current config",
                          err.response.get("Error", {}).get("Code", "?"))
                return 1
            if response.get("ContentLength", 0) > config_override.MAX_DOCUMENT_BYTES:
                log.error("fleet override exceeds the maximum document size — refusing it")
                return 1
            body = response["Body"]
            try:
                override_payload = body.read(config_override.MAX_DOCUMENT_BYTES + 1)
            finally:
                close = getattr(body, "close", None)
                if close:
                    close()
            if len(override_payload) > config_override.MAX_DOCUMENT_BYTES:
                log.error("fleet override exceeds the maximum document size — refusing it")
                return 1
            if config_override.sha256(override_payload) != override_sha:
                log.error("fleet override does not match its published sha256 — refusing it")
                return 1
            try:
                document = config_override.decode_document(override_payload, allowed)
                with open(temp_path, "rb") as handle:
                    base_payload = handle.read()
                combined = config_override.compose(base_payload, document)
            except ValueError as err:
                log.error("fleet override is invalid (%s) — keeping current config", err)
                return 1
            with open(temp_path, "wb") as handle:
                handle.write(combined)
            downloaded_sha = config_override.sha256(combined)
        if downloaded_sha == local_sha:
            # Reachable when the publisher did not set the sha256 metadata, so
            # the cheap head_object comparison above could not short-circuit.
            log.debug("downloaded config is identical to the local one")
            return 0

        if dry_run:
            log.info("[dry-run] would install %s (sha %s)", target,
                     (downloaded_sha or "?")[:12])
            return 0

        install(temp_path, target, config.get("CONFIG_SYNC_OWNER"))
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    log.info("config updated from s3://%s/%s", bucket, key)

    if as_bool(config.get("CONFIG_SYNC_RESTART")):
        return 0 if restart_app(config, dry_run) else 1
    log.info("CONFIG_SYNC_RESTART not set — the app is still running the old "
             "config until it is restarted")
    return 0


def main(argv):
    # prog is set explicitly: under `-m` argparse derives it from sys.argv[0]
    # and prints "config_sync.py", which is not an invocation an operator can
    # copy and run.
    parser = argparse.ArgumentParser(
        prog="python3 -m mojo.deploy.config_sync",
        description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would happen; change nothing")
    parser.add_argument("--verbose", action="store_true",
                        help="log the no-op path too (default logs only changes)")
    parser.add_argument("--config", default=BOOTSTRAP_PATH,
                        help="bootstrap config holding the bucket and credentials")
    parser.add_argument("--target", default=TARGET_PATH,
                        help="where the fetched config is installed")
    parser.add_argument("--remote-name", default=None,
                        help="object name under the prefix "
                             "(default: AWS_CONFIG_FILENAME, else django.conf)")
    args = parser.parse_args(argv)

    import logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [config_sync] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    # --verbose is for OUR no-op path, not for boto's wire logging. Without
    # this, DEBUG buries the one line you turned it on to read.
    for noisy in ("boto3", "botocore", "urllib3", "s3transfer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Which django-mojo published this run. django-mojo is upgraded unpinned on
    # every deploy, so a bad release has to be identifiable from the journal
    # without SSHing in to ask pip.
    try:
        import mojo
        version = mojo.__version__
    except Exception:
        version = "unknown"
    log.info("config_sync from django-mojo %s", version)

    config = read_config(args.config)

    # Unconfigured is a box that still bakes its config into the image. Exit 0
    # so this can ship in the base image and stay dormant.
    if not config.get("AWS_CONFIG_BUCKET"):
        log.debug("AWS_CONFIG_BUCKET unset — nothing to sync")
        return 0
    if not config.get("AWS_CONFIG_PREFIX"):
        log.error("AWS_CONFIG_BUCKET is set but AWS_CONFIG_PREFIX is not — "
                  "refusing to guess which environment's config to pull")
        return 1

    try:
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as err:
        log.error("boto3/botocore not installed, cannot sync config: %s", err)
        return 1

    lock = acquire_lock(args.target)
    if lock is None:
        return 0

    try:
        remote_name = (args.remote_name
                       or config.get("AWS_CONFIG_FILENAME")
                       or os.path.basename(TARGET_PATH))
        return sync(build_s3_client(config), config, args.target,
                    remote_name, args.dry_run)
    except (BotoCoreError, ClientError) as err:
        log.error("S3 operation failed: %s", err)
        return 1
    except OSError as err:
        log.error("filesystem error: %s", err)
        return 1
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
