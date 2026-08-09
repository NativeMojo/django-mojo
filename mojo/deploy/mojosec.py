#!/usr/bin/env python3
"""Install and operate the privileged MojoSec service without Django settings.

This module is executed from the installed django-mojo package. It never
copies root-executed content from the project tree or ``var/deploy``.
"""

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile

from mojo.deploy.mojosec_nginx import (
    DEFAULT_LOG_PATH, render_http_log, render_receiver_location,
)


SERVICE = "mojosec.service"
SERVICE_PATH = "/etc/systemd/system/mojosec.service"
CONFIG_PATH = "/etc/mojosec/config.json"
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

MODES = ("off", "observe")
CRITICALITIES = ("best_effort", "required")
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
WorkingDirectory=/
ExecStartPre=/usr/bin/python3 -I -m mojo.mojosec --config /etc/mojosec/config.json check
ExecStart=/usr/bin/python3 -I -m mojo.mojosec --config /etc/mojosec/config.json run
Restart=on-failure
RestartSec=5s
TimeoutStopSec=30s
KillMode=mixed
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
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
AmbientCapabilities=CAP_DAC_READ_SEARCH
ReadWritePaths=/var/lib/mojosec /run/mojosec
RuntimeDirectory=mojosec
RuntimeDirectoryMode=0755
StateDirectory=mojosec
StateDirectoryMode=0700

[Install]
WantedBy=multi-user.target
"""

LOGROTATE_TEXT = """/var/log/nginx/mojosec.json.log {
    daily
    rotate 14
    missingok
    notifempty
    compress
    delaycompress
    create 0640 root root
    sharedscripts
    postrotate
        /bin/systemctl kill -s USR1 nginx.service >/dev/null 2>&1 || true
    endscript
}
"""


class DeployError(RuntimeError):
    pass


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


def _converge_nginx(enabled, log_path, proxy_cidrs, nginx_path,
                    receiver_snippet_path, logrotate_path=LOGROTATE_PATH):
    """Install/remove the two owned fragments as one validated transaction."""
    paths = (nginx_path, receiver_snippet_path, logrotate_path)
    snapshots = {path: _owned_snapshot(path) for path in paths}
    changed = False
    try:
        if enabled:
            changed |= _write_if_changed(
                nginx_path, render_http_log(log_path, proxy_cidrs), 0o644)
            changed |= _write_if_changed(
                receiver_snippet_path, render_receiver_location() + "\n", 0o644)
            rotate_text = LOGROTATE_TEXT.replace(
                "/var/log/nginx/mojosec.json.log", log_path)
            changed |= _write_if_changed(logrotate_path, rotate_text, 0o644)
        else:
            for path in paths:
                changed |= _remove_owned(path)
        if not changed:
            return False
        _run(["nginx", "-t"])
        _systemctl("reload", "nginx")
        return True
    except Exception:
        for path in paths:
            _restore_snapshot(path, snapshots[path])
        # Confirm the restored graph before returning the original failure.
        # Reloading is unnecessary: nginx never accepted the rejected graph.
        try:
            _run(["nginx", "-t"])
        except DeployError:
            pass
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


def _retire_stale_units():
    for name in RETIRED_UNITS:
        try:
            _systemctl("disable", "--now", name)
        except DeployError:
            pass
        path = os.path.join(os.path.dirname(SERVICE_PATH), name)
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_uid == 0:
            os.unlink(path)


def converge(mode, criticality, proxy_cidrs=None, log_path=DEFAULT_LOG_PATH,
             service_path=SERVICE_PATH, nginx_path=NGINX_FRAGMENT_PATH,
             receiver_snippet_path=RECEIVER_SNIPPET_PATH):
    """Converge one exact service. `off` preserves spool and credentials."""
    if mode not in MODES or criticality not in CRITICALITIES:
        raise DeployError("unsupported MojoSec deployment mode or criticality")
    unit_snapshot = None
    unit_changed = False
    lifecycle_started = False
    previous_enabled = False
    previous_active = False
    try:
        if os.geteuid() != 0:
            raise DeployError("MojoSec deployment must run as root")
        # Enrollment is a precondition, not something discovered after nginx
        # has already started producing a new log. best_effort failure must
        # leave an unenrolled legacy node operationally unchanged.
        if mode == "observe":
            _audit_config()
            _lstat_regular(CREDENTIAL_PATH, mode=0o600)
        _ensure_dir(STATE_DIR, 0o700)
        _ensure_dir(ETC_DIR, 0o700)
        _ensure_dir(STATUS_DIR, 0o755)
        _require_root_install_dir(os.path.dirname(service_path))
        _require_root_install_dir(os.path.dirname(nginx_path))
        _require_root_install_dir(os.path.dirname(receiver_snippet_path), create=True)
        _require_root_install_dir(os.path.dirname(LOGROTATE_PATH))
        _retire_stale_units()
        unit_snapshot = _owned_snapshot(service_path)
        previous_enabled = _systemctl_is("is-enabled", SERVICE)
        previous_active = _systemctl_is("is-active", SERVICE)
        unit_changed = _write_if_changed(service_path, UNIT_TEXT, 0o644)
        nginx_changed = _converge_nginx(
            mode == "observe", log_path, proxy_cidrs,
            nginx_path, receiver_snippet_path)
        if unit_changed:
            _systemctl("daemon-reload")
        lifecycle_started = True
        if mode == "off":
            try:
                _systemctl("disable", "--now", SERVICE)
            except DeployError:
                # An absent/not-loaded unit is already off. Verify below.
                pass
        else:
            _systemctl("enable", "--now", SERVICE)
            if unit_changed:
                _systemctl("restart", SERVICE)
        state = {
            "schema": "mojosec.deploy", "version": 1,
            "mode": mode, "criticality": criticality,
            "service": SERVICE, "spool_preserved": True,
        }
        _write_if_changed(DEPLOY_STATE_PATH,
                          json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n",
                          0o600)
        return {"changed": unit_changed or nginx_changed, **state}
    except (DeployError, OSError, ValueError) as err:
        if unit_changed:
            try:
                _restore_snapshot(service_path, unit_snapshot)
                _systemctl("daemon-reload")
            except (DeployError, OSError):
                pass
        if lifecycle_started:
            try:
                if previous_enabled:
                    _systemctl("enable", SERVICE)
                else:
                    _systemctl("disable", SERVICE)
                if previous_active:
                    _systemctl("start", SERVICE)
                else:
                    _systemctl("stop", SERVICE)
            except DeployError:
                pass
        if criticality == "best_effort":
            return {"schema": "mojosec.deploy", "version": 1,
                    "mode": mode, "criticality": criticality,
                    "ok": False, "warning": str(err)[:500]}
        raise


def rotate_credential(stream, restart=True):
    """Read a bearer secret from stdin, never argv, and atomically rotate it."""
    if os.geteuid() != 0:
        raise DeployError("credential rotation must run as root")
    payload = stream.buffer.read(16385) if hasattr(stream, "buffer") else stream.read(16385)
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    if len(payload) > 16384:
        raise DeployError("credential input is too large")
    token = payload.decode("utf-8").strip()
    if (not token or len(token) > 8192 or
            any(ord(character) < 33 or ord(character) == 127 for character in token)):
        raise DeployError("credential must be one non-empty API key")
    _atomic_write(CREDENTIAL_PATH, (token + "\n").encode("utf-8"), 0o600)
    if restart:
        try:
            if _run(["systemctl", "is-active", "--quiet", SERVICE]) == "":
                _systemctl("restart", SERVICE)
        except DeployError:
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -I -m mojo.deploy.mojosec")
    sub = parser.add_subparsers(dest="command", required=True)
    install = sub.add_parser("converge")
    install.add_argument("--mode", choices=MODES, default="off")
    install.add_argument("--criticality", choices=CRITICALITIES, default="best_effort")
    install.add_argument("--trusted-proxy-cidrs", default="")
    install.add_argument("--nginx-log-path", default=DEFAULT_LOG_PATH)
    rotate = sub.add_parser("rotate-credential")
    rotate.add_argument("--no-restart", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "rotate-credential":
            rotate_credential(sys.stdin, restart=not args.no_restart)
            result = {"ok": True, "credential": "rotated"}
        else:
            result = converge(args.mode, args.criticality,
                              args.trusted_proxy_cidrs, args.nginx_log_path)
            result.setdefault("ok", True)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except (DeployError, OSError, ValueError) as err:
        print(f"mojosec deploy: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
