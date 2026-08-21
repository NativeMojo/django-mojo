#!/usr/bin/env python3
"""Root-owned exact-path trusted-change journal for MojoSec deployments.

The ``run`` command is the stable lifecycle: this process records the exact
declared paths, starts one child argv, and completes or aborts the operation.
It never discovers a broad before/after scope and never suppresses FIM events.
"""

import argparse
import configparser
import csv
import datetime
import fcntl
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import zipfile


JOURNAL_PATH = "/var/lib/mojosec/expected-change-journal.json"
LOCK_PATH = "/var/lib/mojosec/expected-change-journal.lock"
MANIFEST_PATH = "/etc/mojosec/expected_changes.json"
# One operation may legitimately carry 4,096 exact paths plus a bounded
# before-change digest for each path. Keep only a generous corruption guard;
# the path count and approved-root checks are the meaningful security bounds.
MAX_BYTES = 20 * 1024 * 1024
MAX_OPERATIONS = 128
# Caller-declared config changes stay narrowly scoped. Package paths are
# derived from bounded pip reports and wheel RECORDs, so they get a much wider
# secondary corruption ceiling; the 20 MiB serialized-state bound normally
# binds first.
MAX_PATHS = 4096
MAX_PACKAGE_PATHS = 65536
# Active operations are held locally by the sensor for this whole ceiling,
# plus its bounded post-completion correlation window. Keep producer TTLs
# coherent with that delivery hold instead of promising day-long operations.
MAX_TTL_SECONDS = 15 * 60
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_GENERAL_ROOTS = (
    "/etc", "/usr/bin", "/usr/sbin", "/usr/local/bin", "/usr/local/sbin",
    "/usr/local/lib", "/boot", "/var/spool/cron", "/var/spool/at",
)
# Owned by the `mojo.deploy.mojosec converge` transaction, never by the outer
# journal — see split_control_state_paths and validate_paths.
_CONTROL_STATE_ROOTS = ("/etc/mojosec", "/var/lib/mojosec", "/run/mojosec")
_HOME_ROOTS = (
    "/root/.ssh", "/root/.config/systemd/user", "/root/.local/bin",
    "/root/.aws/config", "/root/.aws/credentials", "/root/.bashrc",
    "/root/.bash_profile", "/root/.profile",
    "/home/ec2-user/.ssh", "/home/ec2-user/.config/systemd/user",
    "/home/ec2-user/.local/bin", "/home/ec2-user/.aws/config",
    "/home/ec2-user/.aws/credentials", "/home/ec2-user/.bashrc",
    "/home/ec2-user/.bash_profile", "/home/ec2-user/.profile",
)


class ChangeError(RuntimeError):
    pass


def _distribution_name(value):
    return re.sub(r"[-_.]+", "-", value).lower()


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _timestamp(value):
    return value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value):
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as err:
        raise ChangeError("trusted-change operation timestamp is invalid") from err
    if parsed.tzinfo is None:
        raise ChangeError("trusted-change operation timestamp lacks a timezone")
    return parsed.astimezone(datetime.timezone.utc)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _contained(path, root):
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def validate_paths(paths, allowed_roots=None, max_paths=MAX_PATHS):
    roots = tuple(allowed_roots or (_GENERAL_ROOTS + _HOME_ROOTS))
    if (not isinstance(paths, (list, tuple)) or not paths or
            not isinstance(max_paths, int) or isinstance(max_paths, bool) or
            max_paths < 1):
        raise ChangeError(f"trusted change requires 1-{max_paths} exact paths")
    result = []
    seen = set()
    for path in paths:
        if (not isinstance(path, str) or not os.path.isabs(path) or "\x00" in path or
                os.path.normpath(path) != path or path == "/"):
            raise ChangeError(f"trusted-change path is not normalized: {path!r}")
        if path.startswith(("/opt/api", "/opt/www")):
            raise ChangeError("application release trees are never trusted-change scope")
        if path == "/etc/mojosec" or path.startswith("/etc/mojosec/"):
            raise ChangeError("MojoSec control state is never trusted-change scope")
        if path == "/var/lib/mojosec" or path.startswith("/var/lib/mojosec/"):
            raise ChangeError("MojoSec private state is never trusted-change scope")
        if path == "/run/mojosec" or path.startswith("/run/mojosec/"):
            raise ChangeError("MojoSec runtime state is never trusted-change scope")
        if not any(_contained(path, root) for root in roots):
            raise ChangeError(f"trusted-change path is outside an approved host destination: {path}")
        if path in seen:
            continue
        if len(result) >= max_paths:
            raise ChangeError(f"trusted change requires 1-{max_paths} exact paths")
        seen.add(path)
        result.append(path)
    return result


def split_control_state_paths(paths):
    """Partition caller-declared paths into (journalable, mojosec-control).

    MojoSec control state is owned by the converge transaction and is excluded
    from the integrity profile, so a declaration naming it explains nothing the
    journal could annotate. The 1.11.9 and 1.11.10 post-deploy bodies both
    declare /etc/mojosec/audit-state.json, and the body that drives an upgrade
    is the previously installed generation's — so refusing it outright wedges
    every enrolled node (item 2014). Drop it with a warning instead; it is
    never journaled either way.

    Only the caller-declared `run` intake uses this. validate_paths keeps
    rejecting the same prefixes for pip-derived paths and in-process callers,
    which ship in the same wheel and cannot skew.
    """
    keep, dropped = [], []
    for path in paths:
        if isinstance(path, str) and any(
                path == root or path.startswith(root + "/")
                for root in _CONTROL_STATE_ROOTS):
            dropped.append(path)
        else:
            keep.append(path)
    return keep, dropped


def _system_install_scheme():
    import sysconfig

    scheme = sysconfig.get_paths()
    required = ("purelib", "platlib", "scripts", "data")
    if any(not os.path.isabs(scheme.get(name, "")) for name in required):
        raise ChangeError("system Python install scheme is not absolute")
    roots = {os.path.realpath(scheme[name]) for name in required}
    if not any(root.startswith(("/usr/lib/", "/usr/lib64/", "/usr/local/lib/"))
               for root in (scheme["purelib"], scheme["platlib"])):
        raise ChangeError("pip trusted changes require system Python site-packages")
    return {name: os.path.realpath(path) for name, path in scheme.items()
            if isinstance(path, str) and os.path.isabs(path)}, roots


def _approved_monitored_path(path):
    return any(_contained(path, root) for root in _GENERAL_ROOTS + _HOME_ROOTS)


def _with_monitored_parents(paths):
    result = list(paths)
    for destination in list(result):
        parent = os.path.dirname(destination)
        while parent and parent != "/" and _approved_monitored_path(parent):
            result.append(parent)
            parent = os.path.dirname(parent)
    return result


def _with_immediate_parents(paths, allowed_roots=None, max_paths=MAX_PATHS):
    """Add each declared path's immediate parent directory when monitored.

    Writing a file perturbs its directory's metadata, and FIM reports that
    change separately — without this the exact-match manifest leaves routine
    deploy-owned directory metadata unexplained (/etc/cron.d, /etc/logrotate.d,
    /etc/nginx/conf.d). Only the immediate parent, and never an approved root
    itself: auto-journaling /etc outright would attribute concurrent
    non-deploy writes to the deploy.
    """
    roots = tuple(allowed_roots or (_GENERAL_ROOTS + _HOME_ROOTS))
    result = list(paths)
    seen = set(paths)
    for path in paths:
        parent = os.path.dirname(path)
        if (not parent or parent == "/" or parent in seen or parent in roots or
                not any(_contained(parent, root) for root in roots)):
            continue
        if len(result) >= max_paths:
            break
        seen.add(parent)
        result.append(parent)
    return result


def _wheel_identity(path):
    with zipfile.ZipFile(path) as archive:
        metadata = [name for name in archive.namelist()
                    if name.count("/") == 1 and name.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            raise ChangeError(f"wheel has no unique dist-info metadata: {path}")
        name = version = ""
        for line in archive.read(metadata[0]).decode("utf-8", "replace").splitlines():
            if line.startswith("Name: "):
                name = line[6:].strip()
            elif line.startswith("Version: "):
                version = line[9:].strip()
            if name and version:
                break
        if not name or not version:
            raise ChangeError(f"wheel metadata lacks name/version: {path}")
        return _distribution_name(name), version


def _wheel_record_paths(path, scheme):
    """Map one wheel RECORD to the exact system install destinations."""
    with zipfile.ZipFile(path) as archive:
        records = [name for name in archive.namelist()
                   if name.count("/") == 1 and name.endswith(".dist-info/RECORD")]
        wheels = [name for name in archive.namelist()
                  if name.count("/") == 1 and name.endswith(".dist-info/WHEEL")]
        if len(records) != 1 or len(wheels) != 1:
            raise ChangeError(f"wheel has no unique RECORD/WHEEL: {path}")
        pure = None
        for line in archive.read(wheels[0]).decode("utf-8", "replace").splitlines():
            if line.lower().startswith("root-is-purelib:"):
                pure = line.split(":", 1)[1].strip().lower() == "true"
        if pure is None:
            raise ChangeError(f"wheel lacks Root-Is-Purelib: {path}")
        default_root = scheme["purelib" if pure else "platlib"]
        paths = []
        reader = csv.reader(io.StringIO(
            archive.read(records[0]).decode("utf-8", "strict")))
        for row in reader:
            if not row or not row[0] or "\x00" in row[0]:
                raise ChangeError(f"wheel RECORD contains an invalid path: {path}")
            relative = row[0].replace("/", os.sep)
            if os.path.isabs(relative) or ".." in relative.split(os.sep):
                raise ChangeError(f"wheel RECORD path escapes its install scheme: {row[0]}")
            parts = relative.split(os.sep)
            data_schemes = {
                "purelib": "purelib", "platlib": "platlib", "scripts": "scripts",
                "data": "data", "headers": "include",
            }
            if (len(parts) >= 3 and parts[0].endswith(".data") and
                    parts[1] in data_schemes and data_schemes[parts[1]] in scheme):
                destination = os.path.join(
                    scheme[data_schemes[parts[1]]], *parts[2:])
            else:
                destination = os.path.join(default_root, relative)
            destination = os.path.realpath(destination)
            if _approved_monitored_path(destination):
                paths.append(destination)
                if destination.endswith(".py"):
                    paths.append(importlib.util.cache_from_source(destination))
        dist_info = os.path.dirname(os.path.join(default_root, records[0]))
        for name in ("INSTALLER", "REQUESTED", "direct_url.json"):
            paths.append(os.path.join(dist_info, name))
        entry_points = os.path.join(
            os.path.dirname(records[0]), "entry_points.txt").replace(os.sep, "/")
        if entry_points in archive.namelist():
            parser = configparser.ConfigParser(interpolation=None)
            parser.optionxform = str
            parser.read_string(archive.read(entry_points).decode("utf-8", "strict"))
            for section in ("console_scripts", "gui_scripts"):
                for name in parser[section] if parser.has_section(section) else ():
                    if not name or "/" in name or "\x00" in name:
                        raise ChangeError(f"wheel has an invalid entry point: {name!r}")
                    paths.append(os.path.join(scheme["scripts"], name))
        return _with_monitored_parents(paths)


def _installed_record_paths(names, scheme):
    paths = []
    search = sorted({scheme["purelib"], scheme["platlib"]})
    for distribution in importlib.metadata.distributions(path=search):
        name = distribution.metadata.get("Name", "")
        if _distribution_name(name) not in names:
            continue
        for entry in distribution.files or ():
            destination = os.path.abspath(os.path.normpath(
                str(distribution.locate_file(entry))))
            if _approved_monitored_path(destination):
                paths.append(destination)
    return _with_monitored_parents(paths)


def _prepare_pip_change(child):
    """Resolve/build wheels without mutation, then return exact paths + argv."""
    if len(child) < 2 or child[1] != "install":
        raise ChangeError("pip trusted changes require a `pip install` child")
    forbidden = {"--target", "-t", "--root", "--prefix", "--user", "-e", "--editable"}
    if any(arg.split("=", 1)[0] in forbidden for arg in child[2:]):
        raise ChangeError("pip trusted changes refuse alternate or editable install schemes")
    temporary = tempfile.TemporaryDirectory(prefix="mojosec-pip-")
    report_path = os.path.join(temporary.name, "report.json")
    resolve = child[:2] + ["--dry-run", "--report", report_path] + child[2:]
    done = subprocess.run(resolve, check=False)
    if done.returncode != 0:
        temporary.cleanup()
        raise ChangeError(f"pip dry-run resolution failed with exit {done.returncode}")
    try:
        with open(report_path, encoding="utf-8") as handle:
            report = json.load(handle)
    except (OSError, json.JSONDecodeError) as err:
        temporary.cleanup()
        raise ChangeError("pip dry-run report is unavailable or invalid") from err
    installs = report.get("install")
    if not isinstance(installs, list) or len(installs) > 256:
        temporary.cleanup()
        raise ChangeError("pip dry-run report has no bounded install list")
    wheel_dir = os.path.join(temporary.name, "wheels")
    os.mkdir(wheel_dir, 0o700)
    expected = {}
    pip = child[0]
    for item in installs:
        try:
            name = _distribution_name(item["metadata"]["name"])
            version = str(item["metadata"]["version"])
            url = item["download_info"]["url"]
        except (KeyError, TypeError) as err:
            temporary.cleanup()
            raise ChangeError("pip report entry lacks name/version/download URL") from err
        if not _IDENTITY.fullmatch(name) or not version or not isinstance(url, str):
            temporary.cleanup()
            raise ChangeError("pip report entry has an invalid package identity")
        done = subprocess.run(
            [pip, "wheel", "--no-deps", "--wheel-dir", wheel_dir, url], check=False)
        if done.returncode != 0:
            temporary.cleanup()
            raise ChangeError(f"pip could not build the resolved wheel for {name}=={version}")
        expected[(name, version)] = None
    wheels = []
    for filename in sorted(os.listdir(wheel_dir)):
        path = os.path.join(wheel_dir, filename)
        if not filename.endswith(".whl") or not os.path.isfile(path):
            continue
        identity = _wheel_identity(path)
        if identity in expected:
            if expected[identity] is not None:
                temporary.cleanup()
                raise ChangeError(f"pip produced duplicate wheels for {identity[0]}")
            expected[identity] = path
            wheels.append(path)
    missing = [f"{name}=={version}" for (name, version), path in expected.items()
               if path is None]
    if missing:
        temporary.cleanup()
        raise ChangeError("pip did not produce resolved wheels: " + ", ".join(missing))
    scheme, _roots = _system_install_scheme()
    paths = _installed_record_paths({name for name, _version in expected}, scheme)
    for wheel in wheels:
        paths.extend(_wheel_record_paths(wheel, scheme))
    paths = validate_paths(paths, max_paths=MAX_PACKAGE_PATHS) if paths else []
    install = [pip, "install", "--no-deps"] + wheels
    return temporary, paths, install


def _snapshot(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISREG(info.st_mode):
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if opened.st_dev != info.st_dev or opened.st_ino != info.st_ino:
                raise ChangeError(f"trusted-change path raced while opening: {path}")
            digest = hashlib.sha256()
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                digest.update(block)
            finished = os.fstat(descriptor)
            if (finished.st_dev != info.st_dev or finished.st_ino != info.st_ino or
                    finished.st_size != info.st_size or
                    finished.st_mtime_ns != info.st_mtime_ns):
                raise ChangeError(f"trusted-change path raced while hashing: {path}")
        finally:
            os.close(descriptor)
        return {
            "kind": "file", "mode": stat.S_IMODE(finished.st_mode),
            "uid": finished.st_uid, "gid": finished.st_gid,
            "size": finished.st_size, "mtime_ns": finished.st_mtime_ns,
            "ctime_ns": finished.st_ctime_ns, "device": finished.st_dev,
            "inode": finished.st_ino, "sha256": digest.hexdigest(),
        }
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(path)
        return {
            "kind": "symlink", "mode": stat.S_IMODE(info.st_mode),
            "uid": info.st_uid, "gid": info.st_gid, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns,
            "device": info.st_dev, "inode": info.st_ino,
            "target_sha256": hashlib.sha256(target.encode("utf-8")).hexdigest(),
        }
    return {
        "kind": "directory" if stat.S_ISDIR(info.st_mode) else "other",
        "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid,
        "gid": info.st_gid, "size": info.st_size,
        "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns,
        "device": info.st_dev, "inode": info.st_ino,
    }


def _evidence_digest(value):
    kind = value.get("kind") if isinstance(value, dict) else None
    if kind not in ("file", "symlink", "directory", "other"):
        return None, None
    material = {field: value.get(field) for field in (
        "kind", "mode", "uid", "gid", "size", "mtime_ns", "ctime_ns",
        "device", "inode",
    )}
    if kind == "file":
        material["content_sha256"] = value.get("sha256")
    elif kind == "symlink":
        material["target_sha256"] = value.get("target_sha256")
    return kind, hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()


def _read(path, empty):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return json.loads(json.dumps(empty))
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES or
                info.st_mode & 0o077 or info.st_uid != os.geteuid()):
            raise ChangeError(f"trusted-change state is unsafe or too large: {path}")
        payload = os.read(descriptor, MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as err:
        raise ChangeError(f"trusted-change state is invalid: {path}") from err
    if not isinstance(value, dict):
        raise ChangeError(f"trusted-change state is not an object: {path}")
    return value


def _atomic_write(path, value, mode=0o600):
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    payload = (_canonical(value) + "\n").encode("utf-8")
    if len(payload) > MAX_BYTES:
        raise ChangeError("trusted-change state exceeds its byte bound")
    descriptor, temporary = tempfile.mkstemp(prefix=".mojosec-change.", dir=parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


class ChangeJournal:
    def __init__(self, journal_path=JOURNAL_PATH, lock_path=LOCK_PATH,
                 manifest_path=MANIFEST_PATH, allowed_roots=None):
        self.journal_path = journal_path
        self.lock_path = lock_path
        self.manifest_path = manifest_path
        self.allowed_roots = allowed_roots

    def _locked(self):
        os.makedirs(os.path.dirname(self.lock_path), mode=0o700, exist_ok=True)
        descriptor = os.open(
            self.lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
                info.st_mode & 0o077):
            os.close(descriptor)
            raise ChangeError("trusted-change lock has unsafe ownership or mode")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor

    def _load_journal(self):
        value = _read(self.journal_path, {
            "schema": "mojosec.change_journal", "version": 1, "operations": {},
        })
        if (set(value) != {"schema", "version", "operations"} or
                value["schema"] != "mojosec.change_journal" or value["version"] != 1 or
                not isinstance(value["operations"], dict) or
                len(value["operations"]) > MAX_OPERATIONS):
            raise ChangeError("trusted-change journal envelope is invalid")
        return value

    def _load_manifest(self):
        value = _read(self.manifest_path, {
            "schema": "mojosec.expected_changes", "version": 2, "entries": [],
        })
        if (set(value) != {"schema", "version", "entries"} or
                value["schema"] != "mojosec.expected_changes" or
                value["version"] not in (1, 2) or
                not isinstance(value["entries"], list) or
                len(value["entries"]) > MAX_PACKAGE_PATHS):
            raise ChangeError("expected-change v2 envelope is invalid")
        now = _timestamp(_now())
        value["entries"] = [entry for entry in value["entries"]
                            if isinstance(entry, dict) and entry.get("expires_at", "") >= now]
        required = {"path", "change", "sha256", "expires_at", "deployment_id"}
        if any(not required.issubset(entry) for entry in value["entries"]):
            raise ChangeError("expected-change manifest contains an invalid entry")
        value["version"] = 2
        return value

    def _reap_expired(self, journal, now=None):
        now = now or _now()
        expired = []
        for operation_id, operation in journal["operations"].items():
            if not isinstance(operation, dict):
                raise ChangeError("trusted-change journal operation is invalid")
            ttl = operation.get("ttl_seconds")
            if not isinstance(ttl, int) or isinstance(ttl, bool):
                raise ChangeError("trusted-change journal TTL is invalid")
            if _parse_timestamp(operation.get("started_at")) + \
                    datetime.timedelta(seconds=ttl) <= now:
                expired.append(operation_id)
        for operation_id in expired:
            del journal["operations"][operation_id]
        return len(expired)

    def begin(self, operation_id, operation_kind, paths, deployment_id=None,
              ttl_seconds=900, max_paths=MAX_PATHS):
        if (not isinstance(operation_id, str) or not _IDENTITY.fullmatch(operation_id) or
                not isinstance(operation_kind, str) or not _IDENTITY.fullmatch(operation_kind)):
            raise ChangeError("trusted-change operation identity is invalid")
        deployment_id = deployment_id or operation_id
        if not isinstance(deployment_id, str) or not _IDENTITY.fullmatch(deployment_id):
            raise ChangeError("trusted-change deployment identity is invalid")
        if (not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or
                not 1 <= ttl_seconds <= MAX_TTL_SECONDS):
            raise ChangeError("trusted-change TTL is out of bounds")
        exact = validate_paths(paths, self.allowed_roots, max_paths=max_paths)
        exact = _with_immediate_parents(
            exact, allowed_roots=self.allowed_roots, max_paths=max_paths)
        before = {path: _snapshot(path) for path in exact}
        descriptor = self._locked()
        try:
            journal = self._load_journal()
            reaped = self._reap_expired(journal)
            existing = journal["operations"].get(operation_id)
            requested = {
                "operation_id": operation_id, "operation_kind": operation_kind,
                "deployment_id": deployment_id, "paths": exact, "before": before,
                "started_at": _timestamp(_now()), "ttl_seconds": ttl_seconds,
            }
            if existing is not None:
                # Compare the DECLARED identity only. `before` snapshots of
                # auto-added parent directories are volatile (any concurrent
                # write moves their mtime), and a legitimate retry must not
                # read as a different operation because a hot directory moved.
                identity = (
                    "operation_id", "operation_kind", "deployment_id",
                    "paths", "ttl_seconds",
                )
                if any(existing.get(field) != requested[field]
                       for field in identity):
                    raise ChangeError("operation id is already active with different scope")
                if reaped:
                    _atomic_write(self.journal_path, journal)
                return existing
            if len(journal["operations"]) >= MAX_OPERATIONS:
                raise ChangeError("trusted-change operation bound was exceeded")
            journal["operations"][operation_id] = requested
            _atomic_write(self.journal_path, journal)
            return requested
        finally:
            os.close(descriptor)

    def complete(self, operation_id):
        descriptor = self._locked()
        try:
            journal = self._load_journal()
            operation = journal["operations"].get(operation_id)
            if operation is None:
                raise ChangeError("trusted-change operation is not active")
            completed = _now()
            expiry = completed + datetime.timedelta(seconds=operation["ttl_seconds"])
            entries = []
            for path in operation["paths"]:
                before = operation["before"].get(path)
                after = _snapshot(path)
                if before == after:
                    continue
                if before is None:
                    change, evidence = "created", after
                elif after is None:
                    change, evidence = "deleted", before
                else:
                    change, evidence = "modified", after
                evidence_kind, evidence_sha256 = _evidence_digest(evidence)
                if not evidence_kind or not evidence_sha256:
                    raise ChangeError(f"trusted-change path lacks bounded digest evidence: {path}")
                entries.append({
                    "path": path, "change": change, "sha256": evidence_sha256,
                    "evidence_kind": evidence_kind,
                    "expires_at": _timestamp(expiry),
                    "deployment_id": operation["deployment_id"],
                    "operation_id": operation["operation_id"],
                    "operation_kind": operation["operation_kind"],
                    "started_at": operation["started_at"],
                    "completed_at": _timestamp(completed),
                })
            manifest = self._load_manifest()
            keys = {(item["path"], item["change"], item["sha256"])
                    for item in manifest["entries"]}
            for entry in entries:
                key = (entry["path"], entry["change"], entry["sha256"])
                if key not in keys:
                    manifest["entries"].append(entry)
                    keys.add(key)
            if len(manifest["entries"]) > MAX_PACKAGE_PATHS:
                raise ChangeError("expected-change entry bound was exceeded")
            del journal["operations"][operation_id]
            _atomic_write(self.manifest_path, manifest)
            _atomic_write(self.journal_path, journal)
            return entries
        finally:
            os.close(descriptor)

    def abort(self, operation_id):
        descriptor = self._locked()
        try:
            journal = self._load_journal()
            removed = journal["operations"].pop(operation_id, None)
            if removed is not None:
                _atomic_write(self.journal_path, journal)
            return removed is not None
        finally:
            os.close(descriptor)

    def status(self):
        descriptor = self._locked()
        try:
            journal = self._load_journal()
            reaped = self._reap_expired(journal)
            if reaped:
                _atomic_write(self.journal_path, journal)
            manifest = self._load_manifest()
            return {
                "ok": True, "active_operations": len(journal["operations"]),
                "manifest_entries": len(manifest["entries"]), "version": 2,
                "reaped_operations": reaped,
            }
        finally:
            os.close(descriptor)

    def active_paths(self):
        descriptor = self._locked()
        try:
            journal = self._load_journal()
            reaped = self._reap_expired(journal)
            if reaped:
                _atomic_write(self.journal_path, journal)
            return sorted({path for operation in journal["operations"].values()
                           for path in operation.get("paths", [])})
        finally:
            os.close(descriptor)

    def reap(self):
        descriptor = self._locked()
        try:
            journal = self._load_journal()
            reaped = self._reap_expired(journal)
            if reaped:
                _atomic_write(self.journal_path, journal)
            return reaped
        finally:
            os.close(descriptor)


def run_trusted_change(operation_id, operation_kind, paths, mutation,
                       deployment_id=None, ttl_seconds=900, journal=None):
    """Run one in-process mutation with complete-or-abort journal semantics."""
    journal = journal or ChangeJournal()
    journal.begin(
        operation_id, operation_kind, paths, deployment_id=deployment_id,
        ttl_seconds=ttl_seconds)
    try:
        result = mutation()
        journal.complete(operation_id)
    except Exception:
        journal.abort(operation_id)
        raise
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -E -P -m mojo.deploy.mojosec_changes")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--operation-id", required=True)
    run.add_argument("--kind", required=True)
    run.add_argument("--deployment-id", default="")
    run.add_argument("--ttl", type=int, default=900)
    run.add_argument("--path", action="append", dest="paths", default=[])
    run.add_argument("child", nargs=argparse.REMAINDER)
    pip_run = sub.add_parser("pip-run")
    pip_run.add_argument("--operation-id", required=True)
    pip_run.add_argument("--deployment-id", default="")
    pip_run.add_argument("--ttl", type=int, default=900)
    pip_run.add_argument("child", nargs=argparse.REMAINDER)
    sub.add_parser("status")
    sub.add_parser("reap")
    args = parser.parse_args(argv)
    journal = ChangeJournal()
    try:
        if args.command == "status":
            print(_canonical(journal.status()))
            return 0
        if args.command == "reap":
            print(_canonical({"ok": True, "reaped_operations": journal.reap()}))
            return 0
        child = list(args.child)
        if child and child[0] == "--":
            child = child[1:]
        if not child:
            raise ChangeError("trusted-change run requires one child argv")
        if args.command == "pip-run":
            prepared, paths, install = _prepare_pip_change(child)
            try:
                if not paths:
                    return 0
                journal.begin(
                    args.operation_id, "system-python-packages", paths,
                    deployment_id=args.deployment_id or None,
                    ttl_seconds=args.ttl, max_paths=MAX_PACKAGE_PATHS)
                try:
                    done = subprocess.run(install, check=False)
                    if done.returncode != 0:
                        journal.abort(args.operation_id)
                        return done.returncode
                    entries = journal.complete(args.operation_id)
                except Exception:
                    journal.abort(args.operation_id)
                    raise
                print(_canonical({
                    "ok": True, "operation_id": args.operation_id,
                    "annotated_paths": len(entries), "packages": len(install) - 3,
                }))
                return 0
            finally:
                prepared.cleanup()
        declared, control_state = split_control_state_paths(args.paths)
        for path in control_state:
            print("mojosec changes: ignoring declared MojoSec control-state "
                  f"path (converge owns it): {path}", file=sys.stderr)
        journal.begin(
            args.operation_id, args.kind, declared,
            deployment_id=args.deployment_id or None, ttl_seconds=args.ttl)
        try:
            done = subprocess.run(child, check=False)
            if done.returncode != 0:
                journal.abort(args.operation_id)
                return done.returncode
            entries = journal.complete(args.operation_id)
        except Exception:
            journal.abort(args.operation_id)
            raise
        print(_canonical({"ok": True, "operation_id": args.operation_id,
                          "annotated_paths": len(entries)}))
        return 0
    except (ChangeError, OSError, ValueError) as err:
        print(f"mojosec changes: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
