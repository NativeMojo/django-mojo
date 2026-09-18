"""Public, root-controlled request-service role bound to one installed config.

The application-writable installation receipt is progress information only.
This separate authority is readable by job processes but writable only by the
root config-sync service, so workers cannot opt themselves out of API proof.
"""

import json
import math
import os
import re
import stat
import time
import uuid


PATH = "/etc/mojo/fleet-config-role.json"
MAX_BYTES = 512
FIELDS = {"revision", "digest", "request_service_required", "installed_at"}
REVISION = re.compile(r"^[a-f0-9]{32,64}$")
DIGEST = re.compile(r"^[a-f0-9]{64}$")


def _valid_identity(revision, digest):
    return (isinstance(revision, str) and REVISION.fullmatch(revision)
            and isinstance(digest, str) and DIGEST.fullmatch(digest))


def _open_parent(path, required_uid):
    parent = os.open(os.path.dirname(os.path.abspath(path)),
                     os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        details = os.fstat(parent)
        if (not stat.S_ISDIR(details.st_mode) or details.st_uid != required_uid
                or details.st_mode & 0o022):
            raise ValueError("fleet role parent is not root-controlled")
    except (OSError, ValueError):
        os.close(parent)
        raise
    return parent


def write(revision, digest, required, path=PATH, *, required_uid=0):
    """Atomically publish sealed role evidence; return False on any failure.

    required_uid is a filesystem test seam. Production always requires uid 0.
    The role is non-secret and mode 0644 permits the job account to verify it.
    """
    if (os.geteuid() != required_uid or not _valid_identity(revision, digest)
            or type(required) is not bool):
        return False
    parent = None
    temporary = None
    try:
        directory = os.path.dirname(os.path.abspath(path))
        try:
            os.mkdir(directory, 0o755)
        except FileExistsError:
            pass
        parent = _open_parent(path, required_uid)
        previous = read(revision, digest, path, required_uid=required_uid)
        installed_at = time.time()
        if previous and previous["request_service_required"] is required:
            installed_at = previous["installed_at"]
        raw = json.dumps({"revision": revision, "digest": digest,
                          "request_service_required": required,
                          "installed_at": installed_at},
                         sort_keys=True, allow_nan=False).encode("ascii")
        temporary = ".fleet-role-" + uuid.uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o644)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, os.path.basename(path),
                   src_dir_fd=parent, dst_dir_fd=parent)
        temporary = None
        os.fsync(parent)
        return True
    except (OSError, ValueError, TypeError):
        return False
    finally:
        if parent is not None:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except OSError:
                    pass
            os.close(parent)


def read(revision, digest, path=PATH, *, required_uid=0):
    """Return the bound role and activation time, or None if unproved."""
    if not _valid_identity(revision, digest):
        return None
    parent = descriptor = None
    try:
        parent = _open_parent(path, required_uid)
        descriptor = os.open(os.path.basename(path), os.O_RDONLY
                             | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != required_uid
                or stat.S_IMODE(before.st_mode) != 0o644 or before.st_nlink != 1
                or before.st_size > MAX_BYTES):
            return None
        raw = os.read(descriptor, MAX_BYTES + 1)
        after = os.fstat(descriptor)
        if (len(raw) > MAX_BYTES or len(raw) != before.st_size
                or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
                or after.st_ctime_ns != before.st_ctime_ns):
            return None
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != FIELDS
                or value["revision"] != revision or value["digest"] != digest
                or type(value["request_service_required"]) is not bool):
            return None
        installed_at = value["installed_at"]
        if (type(installed_at) not in (int, float) or not math.isfinite(installed_at)
                or installed_at <= 0 or installed_at > time.time() + 5):
            return None
        return value
    except (OSError, ValueError, TypeError, OverflowError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            os.close(parent)


def clear(path=PATH, *, required_uid=0):
    """Remove prior authority when the current role cannot be certified."""
    if os.geteuid() != required_uid:
        return False
    parent = None
    try:
        try:
            parent = _open_parent(path, required_uid)
        except FileNotFoundError:
            return True
        try:
            # unlink removes a symlink itself, never its destination.
            os.unlink(os.path.basename(path), dir_fd=parent)
        except FileNotFoundError:
            return True
        os.fsync(parent)
        return True
    except (OSError, ValueError):
        return False
    finally:
        if parent is not None:
            os.close(parent)
