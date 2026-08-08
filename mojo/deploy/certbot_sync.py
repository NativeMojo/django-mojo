#!/usr/bin/env python3
"""Sync a Let's Encrypt lineage between load-balanced nodes via S3.

ONE node renews, every other node follows. Which one renews is decided by
hostname, so the same image can be booted N times and userdata's hostname
assignment picks the renewer — nothing else differs between nodes.

    primary  (hostname == PRIMARY_BALANCER_HOST)  certbot runs here -> uploads
    replica  (anything else)                      downloads when S3 is newer

Install (every node, from cron, as root — /etc/letsencrypt is root-only; this
is what the packaged 1_certbot / 4_certbot_sync cron templates render):

    * * * * * root python3 -m mojo.deploy.certbot_sync --config /opt/api/var/django.conf >> /opt/api/var/logs/certbot_sync.log 2>&1

Safe to install unconditionally: with AWS_CERT_BUCKET or LOAD_BALANCER_DOMAIN
absent from var/django.conf the script logs one line and exits 0, so a
single-node box runs it forever as a no-op.

Config, read straight out of ${PROJ_PATH}/var/django.conf (no Django import —
this has to work on a box whose app is broken, and it must be fast enough to
run every minute):

    AWS_REGION              required
    AWS_CERT_BUCKET         required  — bucket holding the shared lineage
    LOAD_BALANCER_DOMAIN    required  — the certbot lineage name
    PRIMARY_BALANCER_HOST   required  — hostname of the renewing node
    AWS_KEY / AWS_SECRET    OPTIONAL  — omit to use the instance role

    Prefer the instance role when the project's storage backend can use one.
    Static keys in a config file are material that leaks with any file-read
    bug or stray backup; a role scoped to this one bucket prefix is strictly
    less to lose.

WHY THIS SYNCS THE PRIVATE KEY, when older copies of this script only synced
fullchain.pem: nginx needs `ssl_certificate` AND `ssl_certificate_key`, and they
must be a matching pair. A node that pulls a fresh fullchain while holding its
own or a stale privkey fails every TLS handshake on the next reload. A
fullchain-only sync appears to work right up until the first renewal after a
second node exists, and then takes that node's TLS down.

  * All three files move together (fullchain, privkey, chain — chain is what
    `ssl_trusted_certificate` wants for OCSP stapling).
  * The downloaded key is checked against the downloaded cert BEFORE either is
    installed, by comparing public keys. A mismatched pair is discarded and the
    node keeps serving what it has.
  * Install is os.replace() per file — atomic per file, PROVIDED the staging
    directory shares a filesystem with /etc/letsencrypt, which is why staging
    happens there and never in /tmp (see pull()) — and privkey lands 0600.
  * nginx is config-tested before reload, so a bad state aborts instead of
    taking the node's TLS down.

THIS IS A PULL, so it belongs only between mutually-trusted nodes of the same
tier. Do not use it to move certificates to a box in a different trust zone (a
public content host, a tenant-operated node): a pull means that box holds a
credential that reads the shared lineage. Push from the trusted side instead, so
compromising the exposed box does not hand anyone a route back.

Typical topology this is written for — an NLB doing TCP passthrough, with the
ACME listener pointed at exactly one node:

    NLB :80  (TCP) -> target group containing ONLY the primary   <- challenges
    NLB :443 (TCP) -> target group containing every node          <- traffic

Because :80 reaches only the primary, every HTTP-01 challenge lands on the node
running certbot; this script then distributes the result. Keep the target-group
membership and PRIMARY_BALANCER_HOST naming the same node — if they drift, the
node receiving challenges will think it is a replica and never publish, and
renewals stop silently.

Longer term this whole file should give way to dnsman's Certificate model
(KMS-encrypted material in the shared DB, renew_certificates() on a schedule) —
no bucket, no direction, no second credential story.
"""

import argparse
import errno
import fcntl
import hashlib
import os
import socket
import subprocess
import sys
import tempfile

# boto3 is imported LAZILY, inside the functions that touch S3. It costs a few
# hundred ms to import and this runs every minute on every node — an
# unconfigured single-node box should pay nothing at all, and should exit 0
# rather than tracebacking every minute if boto3 is not installed.

CONFIG_PATH = os.path.join(
    os.environ.get("PROJ_PATH", "/opt/api"), "var", "django.conf")
LETSENCRYPT_DIR = "/etc/letsencrypt"

# Overridable only so the test harness can point the lock somewhere it can
# actually write — acquire_lock() opens this for writing, which needs root.
# The production default is unchanged.
LOCK_PATH = os.environ.get("CERTBOT_SYNC_LOCK", "/var/run/certbot_sync.lock")

# EVERYTHING in the lineage, not a chosen subset. Different nginx configs
# reference different members of it — `ssl_certificate` may point at
# fullchain.pem or at cert.pem, and `ssl_trusted_certificate` wants chain.pem —
# so syncing a subset means "works on the boxes we happened to check". Sync it
# all and any config keeps working.
#
# There is no crash-safe install ORDER: stop between cert and key and the pair
# is mismatched whichever way round it went. What actually protects the node is
# that nginx is not reloaded until every file has landed, and nginx only reads
# this material at start/reload — so a half-finished sync leaves the running
# process on the old pair, and the next run repairs the disk. `is_current()`
# checks the on-disk pair for exactly that reason.
LINEAGE_FILES = ("cert.pem", "chain.pem", "fullchain.pem", "privkey.pem")

# Private key is 0600. The rest are world-readable — they are public material
# and nginx workers drop privileges.
FILE_MODES = {
    "privkey.pem": 0o600,
    "fullchain.pem": 0o644,
    "chain.pem": 0o644,
    "cert.pem": 0o644,
}

# nginx cannot serve without these two. cert.pem/chain.pem are best-effort so a
# bucket still holding a lineage published by an older, fullchain-only version
# of this script does not hard-fail every replica until the next renewal.
REQUIRED_FILES = ("fullchain.pem", "privkey.pem")

# Logging: the repo's standard graceful-fallback idiom. On a node the except
# branch ALWAYS fires — mojo.helpers.logit reads paths.LOG_ROOT at module
# level and that attribute only exists once Django settings have configured
# the paths, which never happens on this pre-Django path. See
# mojo/deploy/__init__.py for the full contract.
try:
    from mojo.helpers import logit
    log = logit.get_logger("certbot_sync", "certbot_sync.log")
except Exception:
    import logging
    log = logging.getLogger("certbot_sync")


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def read_config(path):
    """Parse a simple key=value config file.

    Skips comments, which the mojo settings parser notably does not — a `#`
    line in django.conf is fine here and is not fine there. Nothing is evaluated
    as Python; a malformed line is skipped rather than raising, because this
    runs every minute and one bad line should not stop cert distribution.
    """
    config = {}
    try:
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip().strip('"').strip("'")
    except OSError as err:
        log.error("cannot read %s: %s", path, err)
    return config


def lineage_dir(domain):
    # Derived from LETSENCRYPT_DIR, not spelled out again, so the destination
    # and the staging directory below cannot drift onto different filesystems.
    return os.path.join(LETSENCRYPT_DIR, "live", domain)


def s3_key_for(domain, filename):
    # No leading slash. The original used "/{domain}/fullchain.pem", which
    # creates a key whose first character is "/" — legal in S3 but it renders
    # as an empty top-level folder and is easy to mistype against. New layout;
    # a primary re-uploads on first run, so replicas converge on their own.
    return f"{domain}/{filename}"


# ---------------------------------------------------------------------------
# aws
# ---------------------------------------------------------------------------

def build_s3_client(config):
    """S3 client, preferring the instance role.

    Static credentials are used only when BOTH are present, so a box that has
    had them scrubbed falls through to the role rather than failing.
    """
    import boto3

    region = config.get("AWS_REGION")
    key = config.get("AWS_KEY")
    secret = config.get("AWS_SECRET")
    if key and secret:
        log.debug("using static credentials from the config file")
        return boto3.client(
            "s3", region_name=region,
            aws_access_key_id=key, aws_secret_access_key=secret)
    log.debug("no AWS_KEY/AWS_SECRET — using the instance role")
    return boto3.client("s3", region_name=region)


def remote_metadata(s3, bucket, key):
    """(mtime, sha256) for an S3 object, or (0, None) if it is not there.

    A missing object is a normal state (nothing uploaded yet) and returns
    zeroes. Every OTHER failure — credentials, network, permissions — is
    re-raised, because the original swallowed them with a bare `except` and
    reported "S3 has nothing", which silently skipped the sync forever.
    """
    from botocore.exceptions import ClientError

    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as err:
        code = err.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return 0, None
        raise
    meta = head.get("Metadata", {})
    try:
        mtime = int(meta.get("created", 0))
    except (TypeError, ValueError):
        mtime = 0
    return mtime, meta.get("sha256")


# ---------------------------------------------------------------------------
# local files
# ---------------------------------------------------------------------------

def file_mtime(path):
    try:
        return int(os.path.getmtime(path))
    except OSError:
        return 0


def file_sha256(path):
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def public_key_of(args):
    """Run an openssl command that emits a PEM public key, or None."""
    try:
        done = subprocess.run(
            args, capture_output=True, check=False, timeout=15)
    except (OSError, subprocess.SubprocessError) as err:
        log.error("openssl failed (%s): %s", " ".join(args), err)
        return None
    if done.returncode != 0:
        log.error("openssl exited %d: %s", done.returncode,
                  done.stderr.decode("utf-8", "replace").strip())
        return None
    return done.stdout.strip() or None


def pair_matches(cert_path, key_path):
    """Does this private key belong to this certificate?

    Compares PUBLIC KEYS rather than RSA moduli: `openssl rsa -modulus` is the
    common recipe and it simply fails on an EC lineage, which would reject
    perfectly good ECDSA certs. `x509 -pubkey` / `pkey -pubout` are uniform
    across key types.
    """
    cert_pub = public_key_of(["openssl", "x509", "-in", cert_path, "-noout", "-pubkey"])
    key_pub = public_key_of(["openssl", "pkey", "-in", key_path, "-pubout"])
    if not cert_pub or not key_pub:
        return False
    return cert_pub == key_pub


def pair_on_disk_ok(directory):
    """Is the material currently installed a usable pair?"""
    return pair_matches(os.path.join(directory, "fullchain.pem"),
                        os.path.join(directory, "privkey.pem"))


def is_current(directory, remote_hash):
    """Are we already holding the published material, AND is it usable?

    The hash alone is not enough, and this is the subtle one. Suppose a previous
    run installed fullchain.pem and then died before privkey.pem: the local
    fullchain now matches S3 exactly, so a hash-only check reports "current" and
    returns early — forever. The stale key is never repaired, and nothing looks
    wrong until the next nginx reload, which could be days later and will be
    blamed on whatever triggered it.

    So being current requires both: the published cert IS what we hold, and what
    we hold is a matching pair. A half-finished sync fails the second test and
    gets redone on the next tick.
    """
    if remote_hash is None:
        return False
    if file_sha256(os.path.join(directory, "fullchain.pem")) != remote_hash:
        return False
    if not pair_on_disk_ok(directory):
        log.warning("local fullchain matches S3 but the on-disk key does not "
                    "match it — repairing (likely an interrupted sync)")
        return False
    return True


def install_file(temp_path, dest_path, mode):
    """Move a staged file into place atomically, with the right mode."""
    os.chmod(temp_path, mode)
    os.replace(temp_path, dest_path)
    log.info("installed %s (mode %o)", dest_path, mode)


def reload_nginx(dry_run):
    """Config-test, then reload. Both checked.

    The original called os.system() and ignored the result, so a failed reload
    left the node serving the old certificate with nothing to show for it.
    """
    if dry_run:
        log.info("[dry-run] would run: nginx -t && systemctl reload nginx")
        return True
    test = subprocess.run(["nginx", "-t"], capture_output=True, check=False)
    if test.returncode != 0:
        log.error("nginx -t FAILED, not reloading: %s",
                  test.stderr.decode("utf-8", "replace").strip())
        return False
    done = subprocess.run(
        ["systemctl", "reload", "nginx.service"], capture_output=True, check=False)
    if done.returncode != 0:
        log.error("nginx reload FAILED (%d): %s", done.returncode,
                  done.stderr.decode("utf-8", "replace").strip())
        return False
    log.info("nginx reloaded")
    return True


# ---------------------------------------------------------------------------
# the two directions
# ---------------------------------------------------------------------------

def push(s3, bucket, domain, dry_run):
    """Primary: upload the lineage when the local copy is newer than S3."""
    directory = lineage_dir(domain)
    cert_path = os.path.join(directory, "fullchain.pem")
    local_mtime = file_mtime(cert_path)
    if not local_mtime:
        log.warning("no certificate at %s — nothing to publish yet", cert_path)
        return 0

    remote_mtime, remote_hash = remote_metadata(
        s3, bucket, s3_key_for(domain, "fullchain.pem"))
    local_hash = file_sha256(cert_path)
    if remote_mtime >= local_mtime and remote_hash == local_hash:
        log.debug("S3 is current (mtime %d, sha %s) — nothing to do",
                  remote_mtime, (remote_hash or "?")[:12])
        return 0

    for name in LINEAGE_FILES:
        path = os.path.join(directory, name)
        if not os.path.exists(path):
            log.warning("%s missing from the lineage — skipping it", path)
            continue
        if dry_run:
            log.info("[dry-run] would upload %s", path)
            continue
        s3.upload_file(
            path, bucket, s3_key_for(domain, name),
            ExtraArgs={"Metadata": {
                "created": str(local_mtime),
                "sha256": file_sha256(path) or "",
            }})
        log.info("uploaded %s", name)
    log.info("published lineage %s (mtime %d)", domain, local_mtime)
    return 0


def pull(s3, bucket, domain, dry_run):
    """Replica: download the lineage when S3 is newer, then reload nginx."""
    from botocore.exceptions import ClientError

    directory = lineage_dir(domain)
    cert_path = os.path.join(directory, "fullchain.pem")

    remote_mtime, remote_hash = remote_metadata(
        s3, bucket, s3_key_for(domain, "fullchain.pem"))
    if not remote_mtime:
        log.info("nothing published for %s yet — waiting on the primary", domain)
        return 0

    if is_current(directory, remote_hash):
        log.debug("already holding the published certificate")
        return 0
    if remote_mtime <= file_mtime(cert_path) and pair_on_disk_ok(directory):
        log.debug("local copy is newer than S3 — not overwriting")
        return 0

    os.makedirs(directory, mode=0o700, exist_ok=True)
    staged = {}
    # Stage NEXT TO the destination, never in /tmp: AL2023 mounts /tmp as
    # tmpfs, so os.replace() into /etc/letsencrypt crosses a filesystem and
    # raises EXDEV — which made every pull fail forever while logging only
    # "filesystem error". Same filesystem = the rename really is atomic. The
    # leading dot also keeps a stray staging dir from reading like a lineage,
    # and keeps the downloaded privkey out of world-traversable /tmp until
    # install_file() chmods it 0600.
    staging_dir = tempfile.mkdtemp(prefix=".certbot_sync.", dir=LETSENCRYPT_DIR)
    try:
        for name in LINEAGE_FILES:
            temp_path = os.path.join(staging_dir, name)
            try:
                s3.download_file(bucket, s3_key_for(domain, name), temp_path)
            except ClientError as err:
                code = err.response.get("Error", {}).get("Code", "")
                if code in ("404", "NoSuchKey", "NotFound") and name not in REQUIRED_FILES:
                    log.warning("no %s published — continuing without it", name)
                    continue
                raise
            staged[name] = temp_path

        missing = [name for name in REQUIRED_FILES if name not in staged]
        if missing:
            log.error("published lineage is missing %s — refusing to install",
                      ", ".join(missing))
            return 1

        # THE check. An unmatched pair installed here breaks TLS on this node
        # until someone notices; keeping the current material costs nothing.
        if not pair_matches(staged["fullchain.pem"], staged["privkey.pem"]):
            log.error("downloaded privkey does not match downloaded fullchain "
                      "— refusing to install, keeping current material")
            return 1

        if dry_run:
            log.info("[dry-run] would install %s and reload nginx",
                     ", ".join(sorted(staged)))
            return 0

        for name in LINEAGE_FILES:
            if name in staged:
                install_file(staged.pop(name),
                             os.path.join(directory, name),
                             FILE_MODES[name])
    finally:
        for leftover in staged.values():
            try:
                os.unlink(leftover)
            except OSError:
                pass
        try:
            os.rmdir(staging_dir)
        except OSError:
            pass

    log.info("synced lineage %s from S3 (mtime %d)", domain, remote_mtime)
    return 0 if reload_nginx(dry_run) else 1


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------

def is_primary(configured):
    """Hostname match, tolerant of short-vs-FQDN.

    socket.gethostname() returns whichever form the box was configured with, so
    comparing raw strings makes the role depend on how userdata happened to set
    it. Short names are compared case-insensitively.
    """
    if not configured:
        return False
    mine = socket.gethostname().strip().lower()
    theirs = configured.strip().lower()
    return mine == theirs or mine.split(".")[0] == theirs.split(".")[0]


def acquire_lock():
    """Single instance. Cron fires every minute; a slow download must not
    overlap with the next run and race on the same files."""
    handle = open(LOCK_PATH, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as err:
        if err.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise
    return handle


def find_certbot():
    """Where certbot lives.

    The provisioning scripts install certbot in its own venv (/opt/certbot,
    symlinked to /usr/local/bin/certbot) precisely so the app's dependency
    pins cannot break it — co-installed, a project pinning a newer
    cryptography breaks certbot's pyOpenSSL and `renew --quiet` then fails
    silently until the certificate expires. PATH first, so a box that
    installed it elsewhere still works.
    """
    import shutil
    return shutil.which("certbot") or "/usr/local/bin/certbot"


def renew(config, dry_run):
    """`--renew` mode: the daily renewal attempt, role-aware.

    Unconfigured box (no cert plane): plain renew, no push — single-node
    behavior, every node renews itself, exactly what the pre-fleet cron did.
    Configured replica: skip. Renewal is the primary's job; replicas only
    ever pull. A replica running certbot against a synced lineage would
    corrupt it (the pull installs regular files, not certbot's symlinks
    into archive/).
    Configured primary: renew, then push immediately so a fresh certificate
    reaches the fleet bucket in the same run instead of waiting for a tick.

    EVERY path logs exactly one line, success included. The old weekly cron
    ran `certbot renew --quiet` and said nothing on success OR failure, so a
    broken certbot looked identical to a healthy one for the forty days it
    took the certificate to expire. This log is a positive heartbeat —
    cron mail on these boxes goes nowhere.
    """
    bucket = config.get("AWS_CERT_BUCKET")
    domain = config.get("LOAD_BALANCER_DOMAIN")
    primary = config.get("PRIMARY_BALANCER_HOST")
    configured = bool(bucket and domain)

    if configured and not is_primary(primary):
        log.info("replica node — renewal is the primary's job, skipping")
        return 0

    argv = [find_certbot(), "renew",
            "--post-hook", "systemctl reload nginx", "--quiet"]
    if dry_run:
        log.info("[dry-run] would run: %s", " ".join(argv))
    else:
        done = subprocess.run(argv, capture_output=True)
        if done.returncode != 0:
            log.error("renew failed rc %d: %s", done.returncode,
                      (done.stderr or done.stdout or b"").decode(
                          "utf-8", "replace").strip()[-800:])
            return 1
        if configured:
            log.info("renew ok (this node is the primary) — pushing to S3")
        else:
            log.info("renew ok (single-node box — nothing to publish)")

    # Returns before build_s3_client() on a single-node box, so boto3 is
    # never imported there — same lazy-import property as the sync path.
    if not configured:
        return 0
    s3 = build_s3_client(config)
    return push(s3, bucket, domain, dry_run)


def main(argv):
    # prog is set explicitly: under `-m` argparse derives it from sys.argv[0]
    # and prints "certbot_sync.py", which is not an invocation an operator can
    # copy and run.
    parser = argparse.ArgumentParser(
        prog="python3 -m mojo.deploy.certbot_sync",
        description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would happen; change nothing")
    parser.add_argument("--verbose", action="store_true",
                        help="log the no-op path too (default logs only changes)")
    parser.add_argument("--config", default=CONFIG_PATH)
    parser.add_argument("--renew", action="store_true",
                        help="run the daily certbot renew (primary-gated when "
                             "the cert plane is configured), then push")
    args = parser.parse_args(argv)

    import logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [certbot_sync] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    config = read_config(args.config)
    bucket = config.get("AWS_CERT_BUCKET")
    domain = config.get("LOAD_BALANCER_DOMAIN")
    primary = config.get("PRIMARY_BALANCER_HOST")

    # BEFORE the dormant gate below: renew has its own gating, because an
    # unconfigured box must still renew its own certificate.
    if args.renew:
        if bucket and domain and not primary:
            log.error("PRIMARY_BALANCER_HOST unset but AWS_CERT_BUCKET is set — "
                      "refusing to guess which node renews")
            return 1
        lock = acquire_lock()
        if lock is None:
            log.info("another certbot_sync is running — skipping this renew")
            return 0
        try:
            return renew(config, args.dry_run)
        except OSError as err:
            log.error("renew failed: %s", err)
            return 1
        except Exception as err:
            # The push leg's botocore exceptions are imported lazily past the
            # config gate, so they cannot be named in this except clause. A
            # cron must log one line, never traceback.
            log.error("renew/push failed: %s: %s", type(err).__name__, err)
            return 1
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()

    # Unconfigured is not an error — it is a single-node box. Exit 0 so this can
    # be installed in the base image and stay dormant until a fleet exists.
    if not bucket or not domain:
        log.debug("AWS_CERT_BUCKET/LOAD_BALANCER_DOMAIN unset — nothing to sync")
        return 0
    if not primary:
        log.error("PRIMARY_BALANCER_HOST unset but AWS_CERT_BUCKET is set — "
                  "refusing to guess which node renews")
        return 1

    # Imported only once past the config gate, so an unconfigured box never
    # loads boto3 at all — and a box missing it fails here with one clear line
    # instead of a traceback every minute.
    try:
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as err:
        log.error("boto3/botocore not installed, cannot sync certificates: %s", err)
        return 1

    lock = acquire_lock()
    if lock is None:
        log.info("another certbot_sync is running — skipping this minute")
        return 0

    try:
        s3 = build_s3_client(config)
        if is_primary(primary):
            return push(s3, bucket, domain, args.dry_run)
        return pull(s3, bucket, domain, args.dry_run)
    except (BotoCoreError, ClientError) as err:
        # Loudly. The original returned 0 here and looked identical to success.
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
