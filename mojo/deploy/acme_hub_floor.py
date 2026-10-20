#!/usr/bin/python3 -I
"""Fixed local root ACME cutover helper; no Django, configuration or transport.

Enrollment copies this reviewed file to /usr/local/libexec/mojo-acme-hub-upgrade.
It is not installed or granted sudo rights by importing this module. Operator
frames use one process/lock for prepare_hold, confirm_hold and release. The
single fixed --updater-check entry is private to the existing root transaction.
"""
import fcntl
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import select
import stat
import sys
import time
import uuid

from packaging.version import Version, InvalidVersion

STATE_ROOT = Path("/var/lib/django-mojo-deploy")
ENTRY = "/usr/local/libexec/mojo-acme-hub-upgrade"
FLOOR_NAME = "acme-hub-floor.json"
MAX_BYTES = 65536
MAX_CODE_BYTES = 1024 * 1024
MAX_SESSION_SECONDS = 600
HEARTBEAT_SECONDS = 30
EPOCH = 1
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
NONCE = re.compile(r"^[0-9a-f]{32}$")
CRITICAL_FILES = (
    "apps/dnsman/acme_hub_feature.py", "apps/dnsman/models/acme_hub_installation.py",
    "apps/dnsman/services/acme_hub.py", "apps/dnsman/services/acme_hub_upgrade.py",
    "apps/dnsman/services/acme_hub_legacy.py", "apps/edge/services/acme_hub_deploy.py",
    "apps/edge/asyncjobs.py", "apps/edge/services/deploy.py",
    "deploy/acme_hub_floor.py", "deploy/project_scripts/update.sh",
    "deploy/project_scripts/post_deploy.sh",
    "apps/dnsman/migrations/0005_acmehubparticipant_and_more.py",
    "apps/dnsman/migrations/0007_acmehubinstallation.py",
    "apps/dnsman/management/commands/acme_hub_upgrade.py",
    "apps/aws/services/capacity.py",
    "apps/dnsman/services/acme_hub_client.py",
    "apps/dnsman/services/delegation_setup.py",
)
FLOOR_FIELDS = {"schema", "installation_uuid", "activation_uuid", "generation", "artifact", "roster_digest"}
ARTIFACT_FIELDS = {"epoch", "source_sha", "manifest_sha256", "release", "wheel_sha256"}


class FloorError(RuntimeError):
    pass


def _fail(reason="floor_unavailable"):
    raise FloorError(reason)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("invalid_frame")
        result[key] = value
    return result


def decode(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_BYTES:
        _fail("invalid_frame")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                           parse_constant=lambda value: _fail("invalid_frame"))
    except (ValueError, UnicodeError):
        _fail("invalid_frame")
    if not isinstance(value, dict):
        _fail("invalid_frame")
    return value


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def release(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        _fail("artifact_unavailable")
    try:
        parsed = Version(value)
    except (InvalidVersion, TypeError):
        _fail("artifact_unavailable")
    if (not isinstance(value, str) or str(parsed) != value or parsed.local is not None
            or parsed.is_prerelease or parsed.is_devrelease):
        _fail("artifact_unavailable")
    return parsed


def _uuid(value):
    try:
        if str(uuid.UUID(value)) != value:
            _fail("invalid_frame")
    except (ValueError, TypeError, AttributeError):
        _fail("invalid_frame")


def artifact(value):
    if (not isinstance(value, dict) or set(value) != ARTIFACT_FIELDS
            or type(value["epoch"]) is not int or value["epoch"] != EPOCH
            or not isinstance(value["source_sha"], str) or not HEX40.fullmatch(value["source_sha"])
            or any(not isinstance(value[key], str) or not HEX64.fullmatch(value[key])
                   for key in ("manifest_sha256", "wheel_sha256"))):
        _fail("artifact_unavailable")
    release(value["release"])
    return value


def validate_floor(value):
    if (not isinstance(value, dict) or set(value) != FLOOR_FIELDS or type(value["schema"]) is not int or value["schema"] != 1
            or type(value["generation"]) is not int or value["generation"] != 1
            or not isinstance(value["roster_digest"], str) or not HEX64.fullmatch(value["roster_digest"])):
        _fail()
    _uuid(value["installation_uuid"])
    _uuid(value["activation_uuid"])
    artifact(value["artifact"])
    return value


def _parent(root, uid):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if info.st_uid != uid or info.st_mode & 0o022:
        os.close(fd)
        _fail()
    return fd


def _read(parent, name, uid, *, missing=False, mode=None, maximum=MAX_BYTES):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        if missing:
            return None
        _fail()
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_nlink != 1
                or info.st_mode & 0o022 or info.st_size > maximum
                or (mode is not None and stat.S_IMODE(info.st_mode) != mode)):
            _fail()
        raw = os.read(fd, maximum + 1)
        after = os.fstat(fd)
        if len(raw) != info.st_size or (after.st_mtime_ns, after.st_ctime_ns) != (info.st_mtime_ns, info.st_ctime_ns):
            _fail()
        return raw
    finally:
        os.close(fd)


def _write_bytes(parent, name, raw):
    temporary = ".acme-floor-" + uuid.uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass


def _write(parent, name, value):
    _write_bytes(parent, name, encoded(value))


def read_floor(root=STATE_ROOT, *, uid=0):
    parent = _parent(root, uid)
    try:
        raw = _read(parent, FLOOR_NAME, uid, missing=True, mode=0o600)
        return validate_floor(decode(raw)) if raw is not None else None
    finally:
        os.close(parent)


def installed_identity(*, uid=0):
    """Inspect distribution bytes without importing the candidate package."""
    try:
        distribution = importlib.metadata.distribution("django-mojo")
        root = Path(distribution.locate_file("mojo"))
        fd = _parent(root / "apps/dnsman", uid)
        try:
            value = decode(_read(fd, "_acme_hub_build.json", uid))
        finally:
            os.close(fd)
        if set(value) != {"epoch", "source_sha", "manifest_sha256", "release"} or value["epoch"] != EPOCH:
            _fail("artifact_unavailable")
        if release(value["release"]) != release(distribution.version):
            _fail("artifact_unavailable")
        if not HEX40.fullmatch(value["source_sha"]) or not HEX64.fullmatch(value["manifest_sha256"]):
            _fail("artifact_unavailable")
        digest = hashlib.sha256()
        for name in CRITICAL_FILES:
            parent = _parent((root / name).parent, uid)
            try:
                raw = _read(parent, (root / name).name, uid, maximum=MAX_CODE_BYTES)
            finally:
                os.close(parent)
            digest.update(name.encode("ascii") + b"\0")
            digest.update(hashlib.sha256(raw).digest())
        if digest.hexdigest() != value["manifest_sha256"]:
            _fail("artifact_unavailable")
        return value
    except (OSError, ValueError, TypeError, KeyError, importlib.metadata.PackageNotFoundError):
        _fail("artifact_unavailable")


def _qualifies(identity, floor):
    if (not isinstance(identity, dict) or set(identity) != {"epoch", "source_sha", "manifest_sha256", "release"}
            or type(identity["epoch"]) is not int or identity["epoch"] < floor["artifact"]["epoch"]
            or not isinstance(identity["source_sha"], str) or not HEX40.fullmatch(identity["source_sha"])
            or not isinstance(identity["manifest_sha256"], str) or not HEX64.fullmatch(identity["manifest_sha256"])
            or release(identity["release"]) < release(floor["artifact"]["release"])):
        _fail("incompatible_framework")


def _transaction(root, uid):
    parent = _parent(root, uid)
    try:
        names = os.listdir(parent)
        active = "active" in names
        preparing = any(name.startswith("preparing.") for name in names)
        return {"active": active, "preparing": preparing}
    finally:
        os.close(parent)


class Session:
    """One local lock-owning session. root/uid/clock/loader are library seams."""
    def __init__(self, root=STATE_ROOT, *, uid=0, clock=time.monotonic, loader=None):
        self.root, self.uid, self.clock = Path(root), uid, clock
        self.loader = loader or (lambda: installed_identity(uid=uid))
        self.lock_fd = None
        self.floor = None
        self.nonce = None
        self.session = uuid.uuid4().hex
        self.started = clock()
        self.deadline = self.started + MAX_SESSION_SECONDS
        self.heartbeat = self.started + HEARTBEAT_SECONDS

    def _lock(self):
        parent = _parent(self.root, self.uid)
        try:
            # Enrollment/updater creates this lock. Status never creates it.
            fd = os.open("update.lock", os.O_RDWR | os.O_NOFOLLOW, dir_fd=parent)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.uid
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                os.close(fd)
                _fail()
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                _fail("update_busy")
            self.lock_fd = fd
        finally:
            os.close(parent)

    def close(self):
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None

    def _receipt(self):
        try:
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            start = int(Path(f"/proc/{os.getpid()}/stat").read_text().split(") ", 1)[1].split()[19])
        except (OSError, ValueError, IndexError):
            # Unsupported systems can unit-test the library, but their result
            # cannot serve as an enrolled Linux production process identity.
            boot, start = "unavailable", 0
        return dict(floor=self.floor, nonce=self.nonce, helper_pid=os.getpid(),
                    helper_start=start, boot_id=boot, helper_session=self.session,
                    confirmed_at=time.time(), held=self.lock_fd is not None)

    def handle(self, request):
        if self.clock() >= min(self.deadline, self.heartbeat):
            self.close()
            _fail("session_expired")
        if not isinstance(request, dict):
            _fail("invalid_frame")
        operation = request.get("operation")
        if operation == "status" and set(request) == {"operation"} and self.lock_fd is None:
            self._lock()
            try:
                return dict(floor=read_floor(self.root, uid=self.uid), **_transaction(self.root, self.uid))
            finally:
                self.close()
        if operation == "prepare_hold" and set(request) == {"operation", "floor", "nonce"} and self.lock_fd is None:
            floor = validate_floor(request["floor"])
            if not isinstance(request["nonce"], str) or not NONCE.fullmatch(request["nonce"]):
                _fail("invalid_frame")
            self._lock()
            try:
                if any(_transaction(self.root, self.uid).values()):
                    _fail("transaction_not_idle")
                previous = read_floor(self.root, uid=self.uid)
                if previous is not None and previous != floor:
                    _fail("activation_conflict")
                identity = self.loader()
                expected = {key: value for key, value in floor["artifact"].items() if key != "wheel_sha256"}
                if identity != expected:
                    _fail("artifact_unavailable")
                parent = _parent(self.root, self.uid)
                try:
                    if previous is None:
                        _write(parent, FLOOR_NAME, floor)
                finally:
                    os.close(parent)
                self.floor, self.nonce = floor, request["nonce"]
                self.heartbeat = self.clock() + HEARTBEAT_SECONDS
                return self._receipt()
            except BaseException:
                self.close()
                raise
        if operation in {"confirm_hold", "release"} and set(request) == {"operation", "nonce", "helper_session"}:
            if (self.lock_fd is None or request["nonce"] != self.nonce
                    or request["helper_session"] != self.session or read_floor(self.root, uid=self.uid) != self.floor):
                _fail("session_unavailable")
            self.heartbeat = self.clock() + HEARTBEAT_SECONDS
            result = self._receipt()
            if operation == "release":
                self.close()
                result["held"] = False
            return result
        _fail("invalid_frame")


def _active(root, uid):
    return _parent(Path(root) / "active", uid)


def _text(fd, name, uid):
    return _read(fd, name, uid).decode("ascii").strip()


def _saved_files(fd, uid):
    result = {}
    for name in ("previous_post.sh", "previous_requirements.txt", "previous_profile.sh"):
        raw = _read(fd, name, uid, missing=True)
        result[name] = hashlib.sha256(raw).hexdigest() if raw is not None else None
    return result


def check_update(request, root=STATE_ROOT, *, uid=0, loader=None):
    """Caller owns update.lock; this helper never releases or reacquires it."""
    if not isinstance(request, dict) or set(request) != {"phase", "framework"}:
        _fail("invalid_frame")
    phase = request["phase"]
    if phase not in {"admit", "snapshot", "candidate", "rollback", "publication"}:
        _fail("invalid_frame")
    floor = read_floor(root, uid=uid)
    if floor is None:
        return {"floor": False}
    if phase == "admit":
        if release(request["framework"]) < release(floor["artifact"]["release"]):
            _fail("incompatible_framework")
        if _transaction(root, uid)["active"]:
            active = _active(root, uid)
            try:
                committed = _read(active, "activation_succeeded", uid, missing=True) is not None
            finally:
                os.close(active)
            check_update(dict(phase="publication" if committed else "rollback", framework=request["framework"]),
                         root, uid=uid, loader=loader)
        return {"floor": True}
    identity_loader = loader or (lambda: installed_identity(uid=uid))
    fd = _active(root, uid)
    try:
        if phase == "snapshot":
            identity = identity_loader()
            _qualifies(identity, floor)
            if release(_text(fd, "previous_framework", uid)) != release(identity["release"]):
                _fail("artifact_unavailable")
            sha = _text(fd, "previous_sha", uid)
            if not HEX40.fullmatch(sha):
                _fail("artifact_unavailable")
            release(request["framework"])
            _write_bytes(fd, "acme_framework_constraint.txt",
                         ("django-mojo==" + request["framework"] + "\n").encode("ascii"))
            _write_bytes(fd, "acme_previous_framework_constraint.txt",
                         ("django-mojo==" + identity["release"] + "\n").encode("ascii"))
            _write(fd, "acme_previous_identity.json", dict(identity=identity, sha=sha, files=_saved_files(fd, uid)))
        elif phase in {"candidate", "publication"}:
            identity = identity_loader()
            _qualifies(identity, floor)
            if release(_text(fd, "candidate_framework", uid)) != release(identity["release"]):
                _fail("artifact_unavailable")
        elif phase == "rollback":
            saved = decode(_read(fd, "acme_previous_identity.json", uid, mode=0o600))
            if (set(saved) != {"identity", "sha", "files"}
                    or saved["sha"] != _text(fd, "previous_sha", uid)
                    or saved["files"] != _saved_files(fd, uid)
                    or release(saved["identity"]["release"]) != release(_text(fd, "previous_framework", uid))):
                _fail("recovery_unavailable")
            _qualifies(saved["identity"], floor)
            expected = "django-mojo==" + saved["identity"]["release"]
            if _text(fd, "acme_previous_framework_constraint.txt", uid) != expected:
                _fail("recovery_unavailable")
        return {"floor": True}
    finally:
        os.close(fd)


def serve(stream, output, session):
    """Bounded newline frames. Only this process can confirm its held flock."""
    pending = bytearray()
    try:
        while True:
            remaining = min(session.deadline, session.heartbeat) - session.clock()
            if remaining <= 0:
                _fail("session_expired")
            if b"\n" not in pending:
                if not select.select([stream], [], [], remaining)[0]:
                    _fail("session_expired")
                block = os.read(stream.fileno(), 4096)
                if not block:
                    if pending:
                        _fail("invalid_frame")
                    return 0
                pending.extend(block)
                if len(pending) > MAX_BYTES and b"\n" not in pending[:MAX_BYTES]:
                    _fail("invalid_frame")
                if b"\n" not in pending:
                    continue
            raw, unused, rest = pending.partition(b"\n")
            pending = bytearray(rest)
            request = decode(bytes(raw))
            result = session.handle(request)
            output.write(encoded(dict(ok=True, **result)) + b"\n")
            output.flush()
            if request["operation"] in {"status", "release"}:
                return 0
    finally:
        session.close()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if os.geteuid() != 0:
            _fail("root_required")
        if argv == ["--updater-check"]:
            result = check_update(decode(sys.stdin.buffer.readline(MAX_BYTES + 1)))
            sys.stdout.buffer.write(encoded(dict(ok=True, **result)) + b"\n")
            return 0
        if argv:
            _fail("invalid_frame")
        return serve(sys.stdin.buffer, sys.stdout.buffer, Session())
    except (FloorError, OSError, ValueError, KeyError, TypeError):
        # Never expose paths, config values or arbitrary provider exceptions.
        sys.stderr.write("shared ACME floor refused: evidence_unavailable\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
