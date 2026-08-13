#!/usr/bin/env python3
"""Converge the narrow Linux Audit policy used by MojoSec provenance.

This module is settings-free because it runs during node convergence and from
the root-owned audit health timer.  The sensor consumes only the resulting
sidecar; it never receives CAP_AUDIT_CONTROL.
"""

import argparse
import fcntl
import glob
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time


RULES_DIR = "/etc/audit/rules.d"
GENERATED_PATH = "/etc/audit/audit.rules"
MANAGED_PATH = "/etc/audit/rules.d/70-mojosec.rules"
STATE_PATH = "/etc/mojosec/audit-state.json"
HEALTH_PATH = "/run/mojosec/audit-health.json"
LOCK_PATH = "/run/mojosec-audit.lock"
MANAGED_MARKER = "# managed-by: django-mojo mojosec-audit-v1"
HEALTH_SCHEMA = "mojosec.audit-health"
MAX_HEALTH_AGE_SECONDS = 15
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_BOOT = re.compile(r"^[a-f0-9]{32}$")
_SEED_FORMS = {
    "-D\n-a task,never\n",
    "-D\n-a never,task\n",
}


class AuditError(RuntimeError):
    pass


def _sha256(payload):
    return hashlib.sha256(payload).hexdigest()


def render_policy(app_uid):
    if (not isinstance(app_uid, int) or isinstance(app_uid, bool) or
            not 1 <= app_uid < 4294967295):
        raise AuditError("application UID is invalid")
    lines = [
        MANAGED_MARKER,
        "-D",
        "-b 8192",
        "-f 1",
        "-r 0",
    ]
    for arch in ("b64", "b32"):
        lines.append(
            f"-a always,exit -F arch={arch} -S execve,execveat "
            f"-F euid=0 -F auid!={app_uid} -k mojosec-root-exec")
        lines.append(
            f"-a always,exit -F arch={arch} -S execve,execveat "
            f"-F auid={app_uid} -F auid!=4294967295 "
            "-F exe!=/usr/bin/sudo -k mojosec-app-exec")
    # A path watch catches sudo execution without authorizing a command-name
    # based disposition.  Audit can emit the same exec via another rule; the
    # compound identity de-duplicates it later.
    lines.append("-w /usr/bin/sudo -p x -k mojosec-sudo")
    return "\n".join(lines) + "\n"


def _read_regular(path, maximum=1024 * 1024):
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise AuditError(f"unsafe Audit file: {path}")
        data = b""
        while len(data) <= maximum:
            block = os.read(descriptor, min(65536, maximum + 1 - len(data)))
            if not block:
                break
            data += block
        if len(data) > maximum:
            raise AuditError(f"Audit file too large: {path}")
        return data, info
    finally:
        os.close(descriptor)


def _source_snapshot(rules_dir, generated_path):
    sources = []
    for path in sorted(glob.glob(os.path.join(rules_dir, "*.rules"))):
        data, info = _read_regular(path)
        sources.append({
            "path": path, "sha256": _sha256(data),
            "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid,
            "gid": info.st_gid, "content": data.decode("utf-8"),
        })
    generated = None
    if generated_path and os.path.exists(generated_path):
        data, info = _read_regular(generated_path)
        generated = {
            "path": generated_path, "sha256": _sha256(data),
            "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid,
            "gid": info.st_gid, "content": data.decode("utf-8"),
        }
    return sources, generated


def inventory_sources(rules_dir=RULES_DIR, generated_path=GENERATED_PATH,
                      active_rules=""):
    sources, generated = _source_snapshot(rules_dir, generated_path)
    combined = "".join(item["content"] for item in sources)
    managed = [item for item in sources if item["content"].startswith(MANAGED_MARKER)]
    unknown = [item for item in sources
               if not item["content"].startswith(MANAGED_MARKER) and
               item["content"] not in _SEED_FORMS]
    if unknown or len(managed) > 1:
        raise AuditError("unknown or conflicting Audit rule source exists")
    if managed:
        state = "managed"
    elif combined in _SEED_FORMS and active_rules.strip() in (
            "-a task,never", "-a never,task"):
        state = "seed"
    else:
        raise AuditError("Audit state is neither exact AL2023 seed nor Mojo-managed")
    digest_payload = json.dumps({
        "sources": [{key: item[key] for key in ("path", "sha256", "mode", "uid", "gid")}
                    for item in sources],
        "generated": ({key: generated[key] for key in
                       ("path", "sha256", "mode", "uid", "gid")}
                      if generated else None),
        "active": active_rules,
    }, sort_keys=True, separators=(",", ":")).encode()
    return {
        "state": state, "sources": sources, "generated": generated,
        "active_rules": active_rules, "inventory_sha256": _sha256(digest_payload),
    }


def parse_status(text):
    values = {}
    for line in str(text).splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2 or not re.fullmatch(r"[a-z_]+", parts[0]):
            continue
        try:
            values[parts[0]] = int(parts[1])
        except ValueError:
            continue
    required = {"enabled", "failure", "rate_limit", "backlog_limit", "backlog", "lost"}
    if not required.issubset(values):
        raise AuditError("auditctl status is incomplete")
    return values


def validate_health(value, now=None, previous=None):
    reason = ""
    now = time.time() if now is None else float(now)
    required = {
        "schema", "version", "boot_id", "generation", "rules_sha256",
        "sequence", "enabled", "failure", "rate_limit", "backlog_limit",
        "backlog", "lost", "updated_at",
    }
    if not isinstance(value, dict) or set(value) != required:
        return {"healthy": False, "reason": "malformed"}
    integers = ("sequence", "enabled", "failure", "rate_limit", "backlog_limit",
                "backlog", "lost")
    if (value.get("schema") != HEALTH_SCHEMA or value.get("version") != 1 or
            not _BOOT.fullmatch(str(value.get("boot_id") or "")) or
            not _DIGEST.fullmatch(str(value.get("generation") or "")) or
            not _DIGEST.fullmatch(str(value.get("rules_sha256") or "")) or
            any(not isinstance(value.get(key), int) or isinstance(value.get(key), bool) or
                value[key] < 0 for key in integers) or
            not isinstance(value.get("updated_at"), (int, float)) or
            isinstance(value.get("updated_at"), bool)):
        return {"healthy": False, "reason": "malformed"}
    if now < value["updated_at"] - 2 or now - value["updated_at"] > MAX_HEALTH_AGE_SECONDS:
        reason = "stale"
    elif (value["enabled"] != 1 or value["failure"] != 1 or
          value["rate_limit"] != 0 or value["backlog_limit"] < 8192 or
          value["backlog"] >= value["backlog_limit"] or
          value["lost"] != 0):
        reason = "audit_unhealthy"
    elif previous is not None:
        if value["boot_id"] == previous.get("boot_id"):
            repeated = (
                value["sequence"] == previous.get("sequence") and
                value["updated_at"] == previous.get("updated_at") and
                value["generation"] == previous.get("generation") and
                value["rules_sha256"] == previous.get("rules_sha256"))
            if (value["generation"] != previous.get("generation") or
                    value["rules_sha256"] != previous.get("rules_sha256")):
                reason = "generation_changed"
            elif value["lost"] < previous.get("lost", 0):
                reason = "loss_counter_regressed"
            elif not repeated and value["sequence"] != previous.get("sequence", -1) + 1:
                reason = "sequence_gap"
    return {"healthy": not reason, "reason": reason, "value": value}


def read_health(path=HEALTH_PATH, require_root=True):
    try:
        payload, info = _read_regular(path, maximum=4096)
    except (FileNotFoundError, OSError, AuditError):
        return None
    if (stat.S_IMODE(info.st_mode) != 0o600 or
            (require_root and (info.st_uid != 0 or info.st_gid != 0))):
        return None
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=lambda pairs: _health_object(pairs))
    except (UnicodeError, json.JSONDecodeError, AuditError):
        return None
    return value if isinstance(value, dict) else None


def _health_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AuditError("duplicate health field")
        result[key] = value
    return result


def _atomic_write(path, payload, mode):
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".mojosec-audit-", dir=parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _run(command):
    process = subprocess.run(command, capture_output=True, text=True, timeout=30)
    if process.returncode:
        raise AuditError(process.stderr.strip() or f"{' '.join(command)} failed")
    return process.stdout


def _active_rules():
    return _run(["/sbin/auditctl", "-l"])


def _load_state(path):
    try:
        data, info = _read_regular(path, maximum=16 * 1024 * 1024)
    except FileNotFoundError:
        return None, None
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
        raise AuditError("Audit rollback record is unsafe")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as err:
        raise AuditError("Audit rollback record is malformed") from err
    if (not isinstance(value, dict) or value.get("schema") != "mojosec.audit-state" or
            value.get("version") != 1 or not isinstance(value.get("prior"), dict)):
        raise AuditError("Audit rollback record is unsupported")
    return value, data


def _state_payload(prior, previous, generation, app_uid, rules_dir,
                   generated_path, managed_path):
    return {
        "schema": "mojosec.audit-state", "version": 1,
        "generation": generation, "app_uid": app_uid,
        "prior": prior, "previous": previous,
        "rules_dir": rules_dir, "generated_path": generated_path,
        "managed_path": managed_path, "installed_at": time.time(),
    }


def _restore_inventory(inventory, rules_dir, managed_path):
    expected_paths = {item["path"] for item in inventory["sources"]}
    for path in glob.glob(os.path.join(rules_dir, "*.rules")):
        if path != managed_path and path not in expected_paths:
            raise AuditError(f"unknown Audit source appeared during restore: {path}")
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise AuditError(f"unsafe Audit source during restore: {path}")
    for path in glob.glob(os.path.join(rules_dir, "*.rules")):
        os.unlink(path)
    for item in inventory["sources"]:
        _atomic_write(item["path"], item["content"].encode(), item["mode"])
        os.chown(item["path"], item["uid"], item["gid"], follow_symlinks=False)
    _run(["/sbin/augenrules", "--load"])
    active = _active_rules()
    expected = inventory["active_rules"].strip()
    if active.strip() != expected:
        raise AuditError("restored active Audit rules differ from snapshot")


def converge(app_uid, rules_dir=RULES_DIR, generated_path=GENERATED_PATH,
             managed_path=MANAGED_PATH, state_path=STATE_PATH):
    """Install and verify one managed generation, restoring exact prior state on error."""
    if os.geteuid() != 0:
        raise AuditError("Audit convergence must run as root")
    os.makedirs(os.path.dirname(LOCK_PATH), mode=0o755, exist_ok=True)
    lock = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before_active = _active_rules()
        inventory = inventory_sources(rules_dir, generated_path, before_active)
        policy = render_policy(app_uid)
        generation = _sha256(policy.encode())
        old_state, old_state_bytes = _load_state(state_path)
        if inventory["state"] == "managed":
            if old_state is None or old_state.get("generation") != _sha256(
                    inventory["sources"][0]["content"].encode()):
                raise AuditError("managed Audit generation has no matching rollback record")
            prior = old_state["prior"]
        else:
            if old_state is not None:
                raise AuditError("seed Audit state conflicts with existing rollback record")
            prior = inventory
        state = _state_payload(
            prior, inventory, generation, app_uid, rules_dir,
            generated_path, managed_path)
        os.makedirs(os.path.dirname(state_path), mode=0o700, exist_ok=True)
        # Re-hash every source immediately before mutation; an operator edit
        # between inventory and install is not silently overwritten.
        current = inventory_sources(rules_dir, generated_path, _active_rules())
        if current["inventory_sha256"] != inventory["inventory_sha256"]:
            raise AuditError("Audit state changed concurrently")
        _atomic_write(state_path, (json.dumps(
            state, sort_keys=True, separators=(",", ":")) + "\n").encode(), 0o600)
        try:
            for item in inventory["sources"]:
                if not item["content"].startswith(MANAGED_MARKER):
                    os.unlink(item["path"])
            _atomic_write(managed_path, policy.encode(), 0o600)
            _run(["/sbin/augenrules", "--load"])
            status = parse_status(_run(["/sbin/auditctl", "-s"]))
            active = _active_rules()
            if (status["failure"] != 1 or status["rate_limit"] != 0 or
                    status["backlog_limit"] < 8192 or status["lost"] != 0 or
                    "task,never" in active or "never,task" in active):
                raise AuditError("active Audit policy verification failed")
        except Exception:
            _restore_inventory(inventory, rules_dir, managed_path)
            if old_state_bytes is None:
                os.unlink(state_path)
            else:
                _atomic_write(state_path, old_state_bytes, 0o600)
            raise
        return {"generation": generation, "rules_sha256": _sha256(active.encode()),
                "state": state_path}
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)


def restore_prior(state_path=STATE_PATH):
    state, unused = _load_state(state_path)
    if state is None:
        raise AuditError("Audit rollback record is absent")
    _restore_inventory(
        state["prior"], state.get("rules_dir", RULES_DIR),
        state.get("managed_path", MANAGED_PATH))
    return state


def publish_health(generation, rules_sha256, sequence, status_text=None,
                   path=HEALTH_PATH, boot_id=None, active_rules_text=None):
    if status_text is None:
        process = subprocess.run(
            ["/sbin/auditctl", "-s"], capture_output=True, text=True, timeout=5)
        if process.returncode:
            raise AuditError(process.stderr.strip() or "auditctl -s failed")
        status_text = process.stdout
    status = parse_status(status_text)
    if active_rules_text is None:
        process = subprocess.run(
            ["/sbin/auditctl", "-l"], capture_output=True, text=True, timeout=5)
        if process.returncode:
            raise AuditError(process.stderr.strip() or "auditctl -l failed")
        active_rules_text = process.stdout
    active_digest = _sha256(active_rules_text.encode())
    if (active_digest != rules_sha256 or
            any(value not in active_rules_text for value in (
                "mojosec-root-exec", "mojosec-app-exec", "mojosec-sudo")) or
            "task,never" in active_rules_text or "never,task" in active_rules_text):
        # Preserve a valid bounded sidecar while making it ineligible for
        # suppression. The actual digest proves what drifted.
        status["enabled"] = 0
    if boot_id is None:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as handle:
            boot_id = handle.read().strip().replace("-", "")
    if sequence is None:
        prior = read_health(path=path, require_root=True)
        sequence = (int(prior.get("sequence", -1)) + 1
                    if prior and prior.get("boot_id") == boot_id else 0)
    payload = {
        "schema": HEALTH_SCHEMA, "version": 1, "boot_id": boot_id,
        "generation": generation, "rules_sha256": active_digest,
        "sequence": sequence, "updated_at": time.time(), **status,
    }
    _atomic_write(path, (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(), 0o600)
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m mojo.deploy.audit")
    parser.add_argument("command", choices=("render", "converge", "restore", "publish-health"))
    parser.add_argument("--app-uid", type=int)
    parser.add_argument("--generation", default="")
    parser.add_argument("--rules-sha256", default="")
    parser.add_argument("--sequence", type=int)
    args = parser.parse_args(argv)
    if args.command == "render":
        print(render_policy(args.app_uid), end="")
    elif args.command == "converge":
        print(json.dumps(converge(args.app_uid), sort_keys=True))
    elif args.command == "restore":
        restore_prior()
    else:
        publish_health(args.generation, args.rules_sha256, args.sequence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
