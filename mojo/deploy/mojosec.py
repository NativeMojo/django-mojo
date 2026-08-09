#!/usr/bin/env python3
"""Install and operate the privileged MojoSec service without Django settings.

This module is executed from the installed django-mojo package. It never
copies root-executed content from the project tree or ``var/deploy``.
"""

import argparse
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile

from mojo.deploy.mojosec_nginx import (
    DEFAULT_LOG_PATH, render_http_log, render_receiver_location,
    trusted_proxy_cidrs,
)


SERVICE = "mojosec.service"
SERVICE_PATH = "/etc/systemd/system/mojosec.service"
CONFIG_PATH = "/etc/mojosec/config.json"
DESIRED_CONFIG_PATH = "/opt/api/var/mojosec.json"
ENROLLMENT_PATH = "/etc/mojosec/enrollment.json"
STATE_DIR = "/var/lib/mojosec"
ETC_DIR = "/etc/mojosec"
CREDENTIAL_PATH = "/etc/mojosec/credential"
EXPECTED_CHANGES_PATH = "/etc/mojosec/expected_changes.json"
STATUS_DIR = "/run/mojosec"
STATUS_PATH = "/run/mojosec/status.json"
NGINX_FRAGMENT_PATH = "/etc/nginx/conf.d/00_mojosec.conf"
RECEIVER_SNIPPET_PATH = "/etc/nginx/snippets/mojosec_receiver.conf"
LOGROTATE_PATH = "/etc/logrotate.d/mojosec"
DEPLOY_STATE_PATH = "/etc/mojosec/deploy.json"
DJANGO_INCLUDE_PATH = "/etc/nginx/django.inc"
DJANGO_RECEIVER_INCLUDE = "include /etc/nginx/snippets/mojosec_receiver.conf;"
DJANGO_RECEIVER_COMMENT = "# MojoSec exact receiver cap"

MODES = ("off", "observe")
CRITICALITIES = ("best_effort", "required")
DESIRED_KEYS = {
    "version", "profile", "policy_revision", "poll_seconds", "collectors",
    "aggregation", "delivery",
}
HOME_BIND_PATHS = (
    "/root/.ssh", "/root/.config/systemd/user", "/root/.local/bin",
    "/root/.aws/config", "/root/.aws/credentials",
    "/root/.bashrc", "/root/.bash_profile", "/root/.profile",
    "/home/ec2-user/.ssh", "/home/ec2-user/.config/systemd/user",
    "/home/ec2-user/.local/bin", "/home/ec2-user/.aws/config",
    "/home/ec2-user/.aws/credentials",
    "/home/ec2-user/.bashrc", "/home/ec2-user/.bash_profile",
    "/home/ec2-user/.profile",
)
DEFAULT_FIM_ROOTS = (
    "/etc", "/usr", "/usr/local", "/boot", "/var/lib/cloud", "/var/spool",
) + HOME_BIND_PATHS
EDGE_LOG_DIR = "/opt/api/var/edge/log"
# Exact historical prerelease name only. Legacy OSSEC/Wazuh units are not on
# this list and are intentionally left alone.
RETIRED_UNITS = ("mojosec-agent.service",)

UNIT_TEXT = """[Unit]
Description=MojoSec host security sensor
Documentation=https://django-mojo.readthedocs.io/
After=network-online.target nginx.service
Wants=network-online.target
ConditionPathExists=/etc/mojosec/config.json

[Service]
Type=simple
User=root
Group=root
UMask=0077
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONHOME=
Environment=PYTHONPATH=
Environment=PYTHONUSERBASE=
Environment=PYTHONSTARTUP=
Environment=PYTHONINSPECT=
WorkingDirectory=/
ExecStartPre=/usr/bin/python3 -E -P -m mojo.mojosec --config /etc/mojosec/config.json check
ExecStart=/usr/bin/python3 -E -P -m mojo.mojosec --config /etc/mojosec/config.json run
Restart=on-failure
RestartSec=5s
TimeoutStopSec=30s
KillMode=mixed
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=tmpfs
ProtectSystem=strict
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
RestrictRealtime=true
RestrictNamespaces=true
SystemCallArchitectures=native
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=CAP_DAC_READ_SEARCH
ReadWritePaths=/var/lib/mojosec /run/mojosec
BindReadOnlyPaths=/root/.ssh /root/.config/systemd/user /root/.local/bin /root/.aws/config /root/.aws/credentials /root/.bashrc /root/.bash_profile /root/.profile /home/ec2-user/.ssh /home/ec2-user/.config/systemd/user /home/ec2-user/.local/bin /home/ec2-user/.aws/config /home/ec2-user/.aws/credentials /home/ec2-user/.bashrc /home/ec2-user/.bash_profile /home/ec2-user/.profile
RuntimeDirectory=mojosec
RuntimeDirectoryMode=0755
StateDirectory=mojosec
StateDirectoryMode=0700

[Install]
WantedBy=multi-user.target
"""

LOGROTATE_TEXT = """/var/log/nginx/mojosec.json.log {
    daily
    maxsize 50M
    rotate 14
    missingok
    notifempty
    compress
    delaycompress
    copytruncate
    su root root
}
"""


class DeployError(RuntimeError):
    pass


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DeployError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json_file(path, require_root=False, required_mode=None):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as err:
        raise DeployError(f"cannot safely open {path}: {err}") from err
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise DeployError(f"{path} must be a regular file")
        if require_root and info.st_uid != 0:
            raise DeployError(f"{path} must be owned by root")
        if required_mode is not None and stat.S_IMODE(info.st_mode) != required_mode:
            raise DeployError(f"{path} must have mode {required_mode:04o}")
        if info.st_size > 256 * 1024:
            raise DeployError(f"{path} is too large")
        payload = b""
        while len(payload) <= 256 * 1024:
            block = os.read(descriptor, min(65536, 256 * 1024 + 1 - len(payload)))
            if not block:
                break
            payload += block
        if len(payload) > 256 * 1024:
            raise DeployError(f"{path} is too large")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_strict_object,
                           parse_constant=lambda item: (_ for _ in ()).throw(
                               DeployError(f"non-finite JSON number: {item}")))
    except (UnicodeError, json.JSONDecodeError) as err:
        raise DeployError(f"cannot parse {path}: {err}") from err
    if not isinstance(value, dict):
        raise DeployError(f"{path} must contain a JSON object")
    return value, payload


def _run(argv):
    try:
        done = subprocess.run(argv, capture_output=True, text=True)
    except OSError as err:
        raise DeployError(f"cannot run {argv[0]}: {err}") from err
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip()[:500]
        raise DeployError(f"{' '.join(argv)} failed ({done.returncode}): {detail}")
    return done.stdout.strip()


def _lstat_regular(path, owner_uid=0, mode=None):
    try:
        info = os.lstat(path)
    except OSError as err:
        raise DeployError(f"cannot inspect {path}: {err}") from err
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise DeployError(f"{path} must be a regular file, not a symlink")
    if info.st_uid != owner_uid:
        raise DeployError(f"{path} must be owned by uid {owner_uid}")
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise DeployError(f"{path} must have mode {mode:04o}")
    return info


def _ensure_dir(path, mode):
    os.makedirs(path, mode=mode, exist_ok=True)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise DeployError(f"{path} must be a real directory")
    if info.st_uid != 0:
        raise DeployError(f"{path} must be owned by root")
    os.chown(path, 0, 0)
    os.chmod(path, mode)


def _prepare_home_binds(paths=None, users=None):
    """Precreate the exact non-optional paths exposed through ProtectHome=tmpfs."""
    paths = tuple(paths or HOME_BIND_PATHS)
    if users is None:
        users = {"/root": (0, 0)}
        try:
            account = pwd.getpwnam("ec2-user")
        except KeyError as err:
            raise DeployError("ec2-user is required for the standard MojoSec profile") from err
        users["/home/ec2-user"] = (account.pw_uid, account.pw_gid)
    # Bind AWS credentials and shell startup files exactly; every other target
    # is a directory subtree approved by the immutable profile.
    files = {path for path in paths
             if os.path.basename(path) in (
                 ".bashrc", ".bash_profile", ".profile", "config", "credentials")}
    directories = set(paths) - files

    def prepare_directory(home, path, owner):
        current = home
        for component in os.path.relpath(path, home).split(os.sep):
            current = os.path.join(current, component)
            try:
                child = os.lstat(current)
            except FileNotFoundError:
                os.mkdir(current, 0o700)
                os.chown(current, *owner)
                child = os.lstat(current)
            if not stat.S_ISDIR(child.st_mode) or stat.S_ISLNK(child.st_mode):
                raise DeployError(f"home bind ancestor is not a real directory: {current}")
            if child.st_uid != owner[0] or child.st_mode & 0o022:
                raise DeployError(f"home bind ancestor has unsafe ownership/mode: {current}")

    for home, owner in users.items():
        info = os.lstat(home)
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or
                info.st_uid != owner[0] or info.st_mode & 0o022):
            raise DeployError(f"standard home has unsafe ownership, mode, or type: {home}")
        for path in sorted(item for item in directories if item.startswith(home + "/")):
            prepare_directory(home, path, owner)
        for path in sorted(item for item in files if item.startswith(home + "/")):
            prepare_directory(home, os.path.dirname(path), owner)
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                descriptor = os.open(
                    path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                    getattr(os, "O_NOFOLLOW", 0), 0o600)
                os.fchown(descriptor, *owner)
                os.close(descriptor)
                info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise DeployError(f"home bind path is not a regular file: {path}")
            if info.st_uid != owner[0] or info.st_mode & 0o022:
                raise DeployError(f"home bind path has unsafe ownership/mode: {path}")


def _require_root_install_dir(path, create=False):
    if create and not os.path.exists(path):
        os.mkdir(path, 0o755)
    try:
        info = os.lstat(path)
    except OSError as err:
        raise DeployError(f"root install directory unavailable: {path}: {err}") from err
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or
            info.st_uid != 0 or info.st_mode & 0o022):
        raise DeployError(
            f"root install directory must be root-owned and not group/world writable: {path}")


def _atomic_write(path, payload, mode):
    parent = os.path.dirname(path)
    # Callers prepare the exact owned directories they manage. Never chmod an
    # ambient parent such as /etc/systemd/system or /etc/nginx/conf.d.
    if not os.path.isdir(parent):
        raise DeployError(f"destination directory is absent: {parent}")
    descriptor, temporary = tempfile.mkstemp(prefix=".mojosec.", dir=parent)
    try:
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, 0, 0)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        try:
            parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except OSError as err:
            raise DeployError(f"cannot fsync destination directory {parent}: {err}") from err
        _lstat_regular(path, mode=mode)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _write_if_changed(path, text, mode):
    payload = text.encode("utf-8")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        current = None
    except OSError as err:
        raise DeployError(f"cannot safely read {path}: {err}") from err
    else:
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
                raise DeployError(f"existing {path} is not a root-owned regular file")
            current = os.read(descriptor, len(payload) + 1)
        finally:
            os.close(descriptor)
    if current == payload:
        os.chmod(path, mode)
        return False
    _atomic_write(path, payload, mode)
    return True


def _owned_snapshot(path):
    """Return exact bytes/mode for one root-owned file, or None if absent."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as err:
        raise DeployError(f"cannot safely snapshot {path}: {err}") from err
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
            raise DeployError(f"existing {path} is not a root-owned regular file")
        chunks = []
        while True:
            block = os.read(descriptor, 65536)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks), stat.S_IMODE(info.st_mode)
    finally:
        os.close(descriptor)


def _remove_owned(path):
    snapshot = _owned_snapshot(path)
    if snapshot is None:
        return False
    os.unlink(path)
    return True


def _restore_snapshot(path, snapshot):
    if snapshot is None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return
    _atomic_write(path, snapshot[0], snapshot[1])


def _security_log_snapshot(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as err:
        raise DeployError(f"cannot inspect security log {path}: {err}") from err
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise DeployError(f"security log must be a regular file: {path}")
    return info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)


def _ensure_security_log(path):
    if path != DEFAULT_LOG_PATH:
        raise DeployError(f"standard MojoSec log path must be {DEFAULT_LOG_PATH}")
    _require_root_install_dir(os.path.dirname(path))
    prior = _security_log_snapshot(path)
    flags = (os.O_WRONLY | os.O_APPEND | os.O_CREAT |
             getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(path, flags, 0o640)
    except OSError as err:
        raise DeployError(f"cannot securely create security log {path}: {err}") from err
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise DeployError(f"security log must be a regular file: {path}")
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o640)
    finally:
        os.close(descriptor)
    return prior != (0, 0, 0o640)


def _restore_security_log(path, snapshot):
    if snapshot is None:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise DeployError(f"refusing to remove unsafe security log {path}")
        os.unlink(path)
        return
    flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise DeployError(f"security log changed type during rollback: {path}")
        os.fchown(descriptor, snapshot[0], snapshot[1])
        os.fchmod(descriptor, snapshot[2])
    finally:
        os.close(descriptor)


def _render_django_include(current, enabled):
    try:
        text = current.decode("utf-8")
    except UnicodeDecodeError as err:
        raise DeployError(f"{DJANGO_INCLUDE_PATH} must be UTF-8") from err
    source_lines = text.splitlines()
    has_marker = any(line.strip() in (DJANGO_RECEIVER_INCLUDE, DJANGO_RECEIVER_COMMENT)
                     for line in source_lines)
    if not enabled and not has_marker:
        return current
    lines = [line for line in source_lines
             if line.strip() not in (DJANGO_RECEIVER_INCLUDE, DJANGO_RECEIVER_COMMENT)]
    rendered = "\n".join(lines).rstrip()
    if enabled:
        rendered += "\n\n" + DJANGO_RECEIVER_COMMENT + "\n" + DJANGO_RECEIVER_INCLUDE
    return (rendered + "\n").encode("utf-8")


def _nginx_snapshot(paths, log_path, inspect_log=True):
    return {
        "files": {path: _owned_snapshot(path) for path in paths},
        "log": _security_log_snapshot(log_path) if inspect_log else None,
        "log_managed": inspect_log,
    }


def _restore_nginx(snapshot, log_path):
    for path, prior in snapshot["files"].items():
        _restore_snapshot(path, prior)
    if snapshot["log_managed"]:
        _restore_security_log(log_path, snapshot["log"])
    _run(["nginx", "-t"])
    _systemctl("reload", "nginx")


def _converge_nginx(enabled, log_path, proxy_cidrs, nginx_path,
                    receiver_snippet_path, logrotate_path=LOGROTATE_PATH,
                    django_include_path=DJANGO_INCLUDE_PATH, snapshot=None):
    """Install/remove the owned nginx graph as one validated transaction."""
    if log_path != DEFAULT_LOG_PATH:
        raise DeployError(f"standard MojoSec log path must be {DEFAULT_LOG_PATH}")
    paths = (nginx_path, receiver_snippet_path, logrotate_path,
             django_include_path)
    snapshot = snapshot or _nginx_snapshot(paths, log_path, inspect_log=enabled)
    changed = False
    try:
        if enabled:
            changed |= _ensure_security_log(log_path)
            changed |= _write_if_changed(
                nginx_path, render_http_log(log_path, proxy_cidrs), 0o644)
            changed |= _write_if_changed(
                receiver_snippet_path, render_receiver_location() + "\n", 0o644)
            changed |= _write_if_changed(logrotate_path, LOGROTATE_TEXT, 0o644)
        else:
            for path in (nginx_path, receiver_snippet_path, logrotate_path):
                changed |= _remove_owned(path)
        django_snapshot = snapshot["files"][django_include_path]
        if django_snapshot is None:
            if enabled:
                raise DeployError(f"standard nginx include is absent: {django_include_path}")
        else:
            rendered = _render_django_include(django_snapshot[0], enabled)
            if rendered != django_snapshot[0]:
                _atomic_write(django_include_path, rendered, django_snapshot[1])
                changed = True
        if not changed:
            return False
        _run(["nginx", "-t"])
        _systemctl("reload", "nginx")
        return True
    except Exception:
        try:
            _restore_nginx(snapshot, log_path)
        except (DeployError, OSError) as rollback_error:
            raise DeployError(f"nginx rollback failed: {rollback_error}")
        raise


def _audit_config(path=None):
    path = path or CONFIG_PATH
    _lstat_regular(path, mode=0o600)
    from mojo.mojosec.config import load_config
    config = load_config(path, require_root=True)
    if config["state_dir"] != STATE_DIR or config["credential_path"] != CREDENTIAL_PATH:
        raise DeployError("MojoSec config must use the deployment state/credential paths")
    if config["status_path"] != STATUS_PATH:
        raise DeployError("MojoSec config must use the public deployment status path")
    if config["expected_changes_path"] != EXPECTED_CHANGES_PATH:
        raise DeployError("MojoSec config must use the fixed expected-change path")
    return config


def _normal_path(path, label):
    if not isinstance(path, str) or not os.path.isabs(path) or "\x00" in path:
        raise DeployError(f"{label} must be an absolute path")
    normalized = os.path.normpath(path)
    if normalized != path or path == "/":
        raise DeployError(f"{label} must be normalized and narrower than /")
    return path


def _normal_root(path, label):
    path = _normal_path(path, label)
    if path == "/home" or (path.startswith("/home/") and
                            not _target_allowed(path, HOME_BIND_PATHS)):
        raise DeployError("only exact standard home persistence paths are selectable")
    if path == "/root" or (path.startswith("/root/") and
                            not _target_allowed(path, HOME_BIND_PATHS)):
        raise DeployError("only exact root persistence paths are selectable")
    if path == "/etc/mojosec" or path.startswith("/etc/mojosec/"):
        raise DeployError("MojoSec private state cannot be a FIM target")
    if path == "/var/lib/mojosec" or path.startswith("/var/lib/mojosec/"):
        raise DeployError("MojoSec private state cannot be a FIM target")
    if path == "/run/mojosec" or path.startswith("/run/mojosec/"):
        raise DeployError("MojoSec runtime state cannot be a FIM target")
    if path == "/opt/api" or path.startswith("/opt/api/") or \
            path == "/opt/www" or path.startswith("/opt/www/"):
        raise DeployError("application release trees cannot be FIM targets")
    return path


def _validate_enrollment(enrollment):
    enrollment = json.loads(json.dumps(enrollment))
    allowed = {"version", "sensor_id", "endpoint", "trusted_proxy_cidrs",
               "fim_allowed_roots", "nginx_plane", "edge_log_dir",
               "mode", "criticality"}
    unknown = set(enrollment) - allowed
    if unknown:
        raise DeployError("enrollment has unknown fields: " + ", ".join(sorted(unknown)))
    if enrollment.get("version") != 1:
        raise DeployError("enrollment version must be 1")
    roots = enrollment.get("fim_allowed_roots", list(DEFAULT_FIM_ROOTS))
    if not isinstance(roots, list) or not roots or len(roots) > 32:
        raise DeployError("enrollment fim_allowed_roots must contain 1-32 paths")
    enrollment["fim_allowed_roots"] = [
        _normal_root(path, "enrollment fim_allowed_roots[]") for path in roots
    ]
    enrollment["trusted_proxy_cidrs"] = trusted_proxy_cidrs(
        enrollment.get("trusted_proxy_cidrs", []))
    if enrollment.get("mode", "off") not in MODES:
        raise DeployError("enrollment mode must be off or observe")
    if enrollment.get("criticality", "best_effort") not in CRITICALITIES:
        raise DeployError("enrollment criticality must be best_effort or required")
    enrollment["mode"] = enrollment.get("mode", "off")
    enrollment["criticality"] = enrollment.get("criticality", "best_effort")
    plane = enrollment.get("nginx_plane", "standard")
    if plane not in ("standard", "edge"):
        raise DeployError("enrollment nginx_plane must be standard or edge")
    enrollment["nginx_plane"] = plane
    if plane == "standard":
        if "edge_log_dir" in enrollment:
            raise DeployError("edge_log_dir is only valid for nginx_plane=edge")
        enrollment["nginx_log_path"] = DEFAULT_LOG_PATH
    else:
        edge_root = _normal_path(enrollment.get("edge_log_dir", EDGE_LOG_DIR),
                                 "enrollment edge_log_dir")
        enrollment["edge_log_dir"] = edge_root
        enrollment["nginx_log_path"] = edge_root + "/mojosec.json.log"
    return enrollment


def _load_enrollment():
    enrollment, _ = _read_json_file(
        ENROLLMENT_PATH, require_root=True, required_mode=0o600)
    return _validate_enrollment(enrollment)


def _target_allowed(path, roots):
    return any(os.path.commonpath((path, root)) == root for root in roots)


def _prepare_effective_config():
    """Validate desired policy and merge only root-protected enrollment fields."""
    desired, source_payload = _read_json_file(DESIRED_CONFIG_PATH)
    unknown = set(desired) - DESIRED_KEYS
    if unknown:
        raise DeployError(
            "desired config may not set protected fields: " + ", ".join(sorted(unknown)))
    if desired.get("version", 1) != 1:
        raise DeployError("desired config version must be 1")
    enrollment = _load_enrollment()
    supplied = json.loads(json.dumps(desired))
    supplied["version"] = 1
    collectors = supplied.setdefault("collectors", {})
    if not isinstance(collectors, dict):
        raise DeployError("desired collectors must be an object")
    for core_name in ("journal", "nginx"):
        core = collectors.setdefault(core_name, {})
        if not isinstance(core, dict):
            raise DeployError(f"desired collectors.{core_name} must be an object")
        if core.get("enabled") is False:
            raise DeployError(f"desired config cannot disable core {core_name} monitoring")
        core["enabled"] = True
    nginx = collectors["nginx"]
    if "paths" in nginx and nginx["paths"] != [enrollment["nginx_log_path"]]:
        raise DeployError("desired config cannot override the dedicated nginx log path")
    nginx["paths"] = [enrollment["nginx_log_path"]]
    fim = collectors.setdefault("fim", {"enabled": False})
    if not isinstance(fim, dict):
        raise DeployError("desired collectors.fim must be an object")
    for target in fim.get("targets", []):
        if not isinstance(target, dict):
            raise DeployError("desired FIM targets must be objects")
        path = _normal_root(target.get("path"), "desired FIM target")
        if not _target_allowed(path, enrollment["fim_allowed_roots"]):
            raise DeployError(f"desired FIM target is outside enrolled roots: {path}")
    supplied.update({
        "sensor_id": enrollment.get("sensor_id"),
        "endpoint": enrollment.get("endpoint"),
        "state_dir": STATE_DIR,
        "status_path": STATUS_PATH,
        "credential_path": CREDENTIAL_PATH,
        "expected_changes_path": EXPECTED_CHANGES_PATH,
    })
    source_sha = hashlib.sha256(source_payload).hexdigest()
    supplied["config_provenance"] = {
        "source_revision": str(desired.get("policy_revision", ""))[:128],
        "source_sha256": source_sha,
        "canonical_revision": "v1:" + str(desired.get("policy_revision", ""))[:125],
        "effective_sha256": "",
        "nginx_plane": enrollment["nginx_plane"],
        "nginx_log_path": enrollment["nginx_log_path"],
        "edge_log_dir": enrollment.get("edge_log_dir", ""),
        "trusted_proxy_cidrs": enrollment["trusted_proxy_cidrs"],
        "deployment_mode": enrollment["mode"],
        "deployment_criticality": enrollment["criticality"],
    }
    from mojo.mojosec.config import build_config
    config = build_config(supplied)
    effective_payload = json.dumps(
        config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    config["config_provenance"]["effective_sha256"] = hashlib.sha256(
        effective_payload).hexdigest()
    config = build_config(config)
    payload = (json.dumps(config, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8")
    return config, payload, enrollment


def _systemctl(*args):
    return _run(["systemctl"] + list(args))


def _systemctl_is(verb, unit):
    try:
        done = subprocess.run(
            ["systemctl", verb, "--quiet", unit],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return done.returncode == 0


def _audit_active_nginx(log_path, proxy_cidrs):
    active = _run(["nginx", "-T"])
    required = (
        "log_format mojosec_v1 escape=json",
        f"access_log {log_path} mojosec_v1;",
    )
    missing = [item for item in required if item not in active]
    route_counts = []
    for route in ("/api/incident/mojosec/batch", "/api/incident/mojosec/batch/"):
        bodies = re.findall(
            r"location\s*=\s*" + re.escape(route) + r"\s*\{([^{}]*)\}", active)
        route_counts.append(len(bodies))
        if not bodies or any("client_max_body_size 512k;" not in body for body in bodies):
            missing.append(f"512k cap inside every exact {route} block")
    if route_counts[0] != route_counts[1]:
        missing.append("matching slash/unslashed exact receiver blocks")
    for network in trusted_proxy_cidrs(proxy_cidrs):
        directive = f"set_real_ip_from {network};"
        if directive not in active:
            missing.append(directive)
    if missing:
        raise DeployError("active nginx graph is missing MojoSec contract: " +
                          ", ".join(missing))


def _retired_unit_snapshot():
    result = {}
    for name in RETIRED_UNITS:
        path = os.path.join(os.path.dirname(SERVICE_PATH), name)
        result[name] = {
            "path": path,
            "file": _owned_snapshot(path),
            "enabled": _systemctl_is("is-enabled", name),
            "active": _systemctl_is("is-active", name),
        }
        result[name]["present"] = bool(
            result[name]["file"] is not None or result[name]["enabled"] or
            result[name]["active"])
    return result


def _retire_stale_units(snapshot):
    changed = False
    for name, prior in snapshot.items():
        if prior["enabled"] or prior["active"]:
            _systemctl("disable", "--now", name)
            changed = True
            if (_systemctl_is("is-enabled", name) or
                    _systemctl_is("is-active", name)):
                raise DeployError(f"retired unit did not stop and disable: {name}")
        if prior["file"] is not None:
            os.unlink(prior["path"])
            changed = True
    return changed


def _restore_unit_set(service_path, unit_snapshot, service_state,
                      retired_snapshot):
    current_present = _owned_snapshot(service_path) is not None
    current_enabled = _systemctl_is("is-enabled", SERVICE)
    current_active = _systemctl_is("is-active", SERVICE)
    if current_present or current_enabled or current_active:
        _systemctl("disable", "--now", SERVICE)
    _restore_snapshot(service_path, unit_snapshot)
    for prior in (retired_snapshot or {}).values():
        _restore_snapshot(prior["path"], prior["file"])
    _systemctl("daemon-reload")
    if service_state is not None and service_state["present"]:
        if service_state["enabled"]:
            _systemctl("enable", SERVICE)
        else:
            _systemctl("disable", SERVICE)
        if service_state["active"]:
            _systemctl("start", SERVICE)
        else:
            _systemctl("stop", SERVICE)
    for name, prior in (retired_snapshot or {}).items():
        if not prior["present"]:
            continue
        if prior["enabled"]:
            _systemctl("enable", name)
        else:
            _systemctl("disable", name)
        if prior["active"]:
            _systemctl("start", name)
        else:
            _systemctl("stop", name)


def converge(mode, criticality, proxy_cidrs=None, log_path=DEFAULT_LOG_PATH,
             service_path=SERVICE_PATH, nginx_path=NGINX_FRAGMENT_PATH,
             receiver_snippet_path=RECEIVER_SNIPPET_PATH,
             django_include_path=DJANGO_INCLUDE_PATH):
    """Converge one exact service. `off` preserves spool and credentials."""
    if mode not in MODES or criticality not in CRITICALITIES:
        raise DeployError("unsupported MojoSec deployment mode or criticality")
    if log_path != DEFAULT_LOG_PATH:
        raise DeployError("nginx log path is protected by root enrollment")
    if proxy_cidrs:
        raise DeployError("trusted proxy CIDRs are protected by root enrollment")
    unit_snapshot = service_state = retired_snapshot = None
    nginx_snapshot = deploy_state_snapshot = config_snapshot = None
    unit_changed = nginx_changed = config_changed = retired_changed = False
    mutation_started = False
    prepared = None
    try:
        if mode == "observe" and sys.version_info < (3, 11):
            raise DeployError(
                "MojoSec observe requires Python 3.11+ safe-path support")
        if os.geteuid() != 0:
            raise DeployError("MojoSec deployment must run as root")
        # Enrollment is a precondition, not something discovered after nginx
        # has already started producing a new log. best_effort failure must
        # leave an unenrolled legacy node operationally unchanged.
        if mode == "observe":
            prepared = _prepare_effective_config()
            _lstat_regular(CREDENTIAL_PATH, mode=0o600)
            proxy_cidrs = prepared[2]["trusted_proxy_cidrs"]
        _ensure_dir(STATE_DIR, 0o700)
        _ensure_dir(ETC_DIR, 0o700)
        _ensure_dir(STATUS_DIR, 0o755)
        if mode == "observe" and service_path == SERVICE_PATH:
            _prepare_home_binds()
        _require_root_install_dir(os.path.dirname(service_path))
        _require_root_install_dir(os.path.dirname(nginx_path))
        _require_root_install_dir(os.path.dirname(receiver_snippet_path), create=True)
        _require_root_install_dir(os.path.dirname(LOGROTATE_PATH))
        _require_root_install_dir(os.path.dirname(django_include_path))
        unit_snapshot = _owned_snapshot(service_path)
        deploy_state_snapshot = _owned_snapshot(DEPLOY_STATE_PATH)
        config_snapshot = _owned_snapshot(CONFIG_PATH)
        service_state = {
            "enabled": _systemctl_is("is-enabled", SERVICE),
            "active": _systemctl_is("is-active", SERVICE),
        }
        service_state["present"] = bool(
            unit_snapshot is not None or service_state["enabled"] or
            service_state["active"])
        retired_snapshot = _retired_unit_snapshot()
        nginx_paths = (nginx_path, receiver_snippet_path, LOGROTATE_PATH,
                       django_include_path)
        nginx_plane = prepared[2]["nginx_plane"] if prepared is not None else "standard"
        runtime_log_path = (prepared[2]["nginx_log_path"] if prepared is not None
                            else DEFAULT_LOG_PATH)
        standard_observe = mode == "observe" and nginx_plane == "standard"
        nginx_snapshot = _nginx_snapshot(
            nginx_paths, DEFAULT_LOG_PATH, inspect_log=standard_observe)
        mutation_started = True
        retired_changed = _retire_stale_units(retired_snapshot)
        if prepared is not None:
            config_changed = _write_if_changed(
                CONFIG_PATH, prepared[1].decode("utf-8"), 0o600)
            _audit_config()
        unit_changed = _write_if_changed(service_path, UNIT_TEXT, 0o644)
        nginx_changed = _converge_nginx(
            standard_observe, DEFAULT_LOG_PATH, proxy_cidrs,
            nginx_path, receiver_snippet_path,
            django_include_path=django_include_path, snapshot=nginx_snapshot)
        if mode == "observe":
            _audit_active_nginx(runtime_log_path, proxy_cidrs)
        if unit_changed or retired_changed:
            _systemctl("daemon-reload")
        if mode == "off":
            _systemctl("disable", "--now", SERVICE)
            if (_systemctl_is("is-enabled", SERVICE) or
                    _systemctl_is("is-active", SERVICE)):
                raise DeployError("MojoSec off convergence left service enabled or active")
        else:
            _systemctl("enable", "--now", SERVICE)
            if unit_changed or config_changed:
                _systemctl("restart", SERVICE)
            if (not _systemctl_is("is-enabled", SERVICE) or
                    not _systemctl_is("is-active", SERVICE)):
                raise DeployError("MojoSec observe convergence did not enable and start service")
        state = {
            "schema": "mojosec.deploy", "version": 1,
            "mode": mode, "criticality": criticality,
            "service": SERVICE, "spool_preserved": True,
        }
        if prepared is not None:
            state["sensor_id"] = prepared[0]["sensor_id"]
            state["config"] = prepared[0]["config_provenance"]
            state["nginx_plane"] = nginx_plane
        _write_if_changed(DEPLOY_STATE_PATH,
                          json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n",
                          0o600)
        return {"changed": unit_changed or nginx_changed or config_changed or retired_changed,
                **state}
    except (DeployError, OSError, ValueError) as err:
        rollback_errors = []
        if mutation_started:
            try:
                _restore_snapshot(CONFIG_PATH, config_snapshot)
            except (DeployError, OSError) as rollback_error:
                rollback_errors.append(f"runtime config: {rollback_error}")
            try:
                _restore_snapshot(DEPLOY_STATE_PATH, deploy_state_snapshot)
            except (DeployError, OSError) as rollback_error:
                rollback_errors.append(f"deploy state: {rollback_error}")
            try:
                _restore_nginx(nginx_snapshot, DEFAULT_LOG_PATH)
            except (DeployError, OSError) as rollback_error:
                rollback_errors.append(f"nginx: {rollback_error}")
            try:
                _restore_unit_set(service_path, unit_snapshot, service_state,
                                  retired_snapshot)
            except (DeployError, OSError) as rollback_error:
                rollback_errors.append(f"systemd: {rollback_error}")
        message = str(err)
        if rollback_errors:
            message += "; rollback incomplete: " + "; ".join(rollback_errors)
        if criticality == "best_effort":
            return {"schema": "mojosec.deploy", "version": 1,
                    "mode": mode, "criticality": criticality,
                    "ok": False, "warning": message[:500]}
        if rollback_errors:
            raise DeployError(message) from err
        raise


def _read_stdin(stream, maximum, label):
    payload = stream.buffer.read(maximum + 1) if hasattr(stream, "buffer") else stream.read(
        maximum + 1)
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    if len(payload) > maximum:
        raise DeployError(f"{label} input is too large")
    return payload


def install_enrollment(stream):
    """Install nonsecret, root-protected host identity and receiver enrollment."""
    if os.geteuid() != 0:
        raise DeployError("enrollment installation must run as root")
    payload = _read_stdin(stream, 256 * 1024, "enrollment")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeError, json.JSONDecodeError) as err:
        raise DeployError(f"cannot parse enrollment: {err}") from err
    if not isinstance(value, dict):
        raise DeployError("enrollment must be a JSON object")
    enrollment = _validate_enrollment(value)
    from mojo.mojosec.config import build_config
    build_config({
        "version": 1,
        "sensor_id": enrollment.get("sensor_id"),
        "endpoint": enrollment.get("endpoint"),
        "collectors": {"fim": {"enabled": False}},
    })
    _ensure_dir(ETC_DIR, 0o700)
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    _write_if_changed(ENROLLMENT_PATH, rendered, 0o600)
    return enrollment


def rotate_credential(stream, restart=True):
    """Read a bearer secret from stdin and roll back a failed live rotation."""
    if os.geteuid() != 0:
        raise DeployError("credential rotation must run as root")
    payload = _read_stdin(stream, 16384, "credential")
    token = payload.decode("utf-8").strip()
    if (not token or len(token) > 8192 or
            any(ord(character) < 33 or ord(character) == 127 for character in token)):
        raise DeployError("credential must be one non-empty API key")
    _ensure_dir(ETC_DIR, 0o700)
    prior = _owned_snapshot(CREDENTIAL_PATH)
    was_active = restart and _systemctl_is("is-active", SERVICE)
    _atomic_write(CREDENTIAL_PATH, (token + "\n").encode("utf-8"), 0o600)
    if was_active:
        try:
            _systemctl("restart", SERVICE)
            if not _systemctl_is("is-active", SERVICE):
                raise DeployError("service is not active after credential rotation")
        except DeployError as err:
            try:
                _restore_snapshot(CREDENTIAL_PATH, prior)
                _systemctl("restart", SERVICE)
                if not _systemctl_is("is-active", SERVICE):
                    raise DeployError("service is not active after restoring old credential")
            except (DeployError, OSError) as rollback_error:
                raise DeployError(
                    f"credential rotation failed ({err}); rollback failed ({rollback_error})"
                ) from err
            raise DeployError(f"credential rotation failed; old credential restored: {err}") from err


def resolve_lifecycle(mode, criticality):
    """Resolve persistent root enrollment, defaulting an unenrolled node off."""
    if mode != "enrolled" and criticality != "enrolled":
        return mode, criticality
    try:
        os.lstat(ENROLLMENT_PATH)
    except FileNotFoundError:
        enrollment = {"mode": "off", "criticality": "best_effort"}
    else:
        enrollment = _load_enrollment()
    return (
        enrollment["mode"] if mode == "enrolled" else mode,
        enrollment["criticality"] if criticality == "enrolled" else criticality,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="/usr/bin/python3 -E -P -m mojo.deploy.mojosec")
    sub = parser.add_subparsers(dest="command", required=True)
    install = sub.add_parser("converge")
    install.add_argument("--mode", choices=("enrolled",) + MODES, default="enrolled")
    install.add_argument("--criticality", choices=("enrolled",) + CRITICALITIES,
                         default="enrolled")
    rotate = sub.add_parser("rotate-credential")
    rotate.add_argument("--no-restart", action="store_true")
    sub.add_parser("install-enrollment")
    args = parser.parse_args(argv)
    try:
        if args.command == "rotate-credential":
            rotate_credential(sys.stdin, restart=not args.no_restart)
            result = {"ok": True, "credential": "rotated"}
        elif args.command == "install-enrollment":
            enrollment = install_enrollment(sys.stdin)
            result = {"ok": True, "enrollment": "installed",
                      "sensor_id": enrollment["sensor_id"]}
        else:
            mode, criticality = resolve_lifecycle(args.mode, args.criticality)
            result = converge(mode, criticality)
            result.setdefault("ok", True)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except (DeployError, OSError, ValueError) as err:
        print(f"mojosec deploy: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
