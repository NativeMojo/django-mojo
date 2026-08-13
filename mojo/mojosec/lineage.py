"""Bounded Linux Audit compound and process-lineage handling."""

import hashlib
import json
import os
import re
import shlex
import stat
import time


MAX_ARGUMENTS = 64
MAX_ARGUMENT_BYTES = 16 * 1024
MAX_PARENT_DEPTH = 32
MAX_EVENT_ANCESTORS = 8
COMPOUND_TIMEOUT_SECONDS = 2
# journald extracts the kernel Audit serial into _AUDIT_ID.  The timestamp
# remains in _SOURCE_REALTIME_TIMESTAMP and must never be reconstructed from
# MESSAGE text to form a compound identity.
_AUDIT_ID = re.compile(r"^[0-9]{1,20}$")
_AUDIT_TYPES = {"SYSCALL", "EXECVE", "PROCTITLE", "CWD", "EOE"}
CROND_SELINUX = "system_u:system_r:crond_t:s0-s0:c0.c1023"


def crond_launch(record, project_path, app_uid, app_gid):
    """Project one half of the exact AL2023 CROND/PAM launch attestation."""
    boot = str(record.get("_BOOT_ID") or "").replace("-", "").lower()
    session = _integer(record.get("_AUDIT_SESSION"), 4294967294)
    if not re.fullmatch(r"[a-f0-9]{32}", boot) or session is None:
        return None
    command = (f'{project_path}/bin/jobman start >> '
               f'{project_path}/var/logs/jobman.log 2>&1')
    common = bool(
        str(record.get("_UID") or "") == "0" and
        str(record.get("_GID") or "") == str(app_gid) and
        str(record.get("_AUDIT_LOGINUID") or "") == str(app_uid) and
        str(record.get("_SELINUX_CONTEXT") or "") == CROND_SELINUX)
    monotonic = _integer(record.get("__MONOTONIC_TIMESTAMP"))
    if not common or monotonic is None:
        return None
    if record.get("_TRANSPORT") == "syslog":
        expected_message = f"(ec2-user) CMD ({command})"
        expected_cmdline = f'/bin/bash -c "{command}"'
        pid = _integer(record.get("_PID"), 2 ** 31 - 1)
        scope = f"session-{session}.scope"
        cgroup = str(record.get("_SYSTEMD_CGROUP") or "")
        if (record.get("SYSLOG_IDENTIFIER") != "CROND" or
                record.get("_COMM") != "bash" or
                record.get("_EXE") != "/usr/bin/bash" or
                record.get("_CMDLINE") != expected_cmdline or
                record.get("MESSAGE") != expected_message or not pid or
                record.get("_SYSTEMD_UNIT") != scope or
                not cgroup.endswith("/" + scope)):
            return None
        return {"boot_id": boot, "audit_session": session, "half": "syslog",
                "bash_pid": pid, "monotonic": monotonic,
                "command_sha256": hashlib.sha256(command.encode()).hexdigest()}
    if (record.get("_TRANSPORT") == "audit" and
            str(record.get("_AUDIT_TYPE_NAME") or "") == "USER_START"):
        fields = _message_fields(record)
        if (str(_field(record, fields, "auid", "_AUDIT_LOGINUID")) != str(app_uid) or
                str(_field(record, fields, "ses", "_AUDIT_SESSION")) != str(session) or
                _field(record, fields, "exe") != "/usr/sbin/crond" or
                _field(record, fields, "acct") != "ec2-user" or
                _field(record, fields, "terminal") != "cron" or
                str(_field(record, fields, "res")).lower() not in ("success", "yes", "1")):
            return None
        return {"boot_id": boot, "audit_session": session, "half": "pam",
                "monotonic": monotonic,
                "command_sha256": hashlib.sha256(command.encode()).hexdigest()}
    return None


def _integer(value, maximum=2 ** 63 - 1):
    try:
        if isinstance(value, bool):
            raise ValueError
        found = int(value)
    except (TypeError, ValueError):
        return None
    return found if 0 <= found <= maximum else None


def compound_key(record):
    boot = str(record.get("_BOOT_ID") or "").replace("-", "").lower()
    audit_id = str(record.get("_AUDIT_ID") or "")
    if (record.get("_TRANSPORT") != "audit" or
            not re.fullmatch(r"[a-f0-9]{32}", boot) or
            not _AUDIT_ID.fullmatch(audit_id)):
        return None
    return boot, audit_id


def _kind(record):
    kind = str(record.get("_AUDIT_TYPE_NAME") or record.get("AUDIT_TYPE_NAME") or "").upper()
    return kind if kind in _AUDIT_TYPES else ""


def _message_fields(record):
    message = str(record.get("MESSAGE") or "")
    if len(message.encode("utf-8", errors="replace")) > 16384:
        return {}
    try:
        parts = shlex.split(message, posix=True)
    except ValueError:
        return {}
    fields = {}
    for part in parts:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_\[\]]{0,63}", key) and len(value) <= 4096:
            fields[key.lower()] = value
    return fields


def _field(record, message, name, *fallbacks):
    for key in (f"_AUDIT_FIELD_{name.upper()}",
                f"AUDIT_FIELD_{name.upper()}"):
        if record.get(key) not in (None, ""):
            return record[key]
    if message.get(name.lower()) not in (None, ""):
        return message[name.lower()]
    for key in fallbacks:
        if record.get(key) not in (None, ""):
            return record[key]
    return None


def _normalize_record(record, kind):
    message = _message_fields(record)
    if kind == "SYSCALL":
        result = {
            "pid": _field(record, message, "pid", "_PID"),
            "ppid": _field(record, message, "ppid"),
            "uid": _field(record, message, "uid", "_UID"),
            "euid": _field(record, message, "euid"),
            "auid": _field(record, message, "auid", "_AUDIT_LOGINUID"),
            "ses": _field(record, message, "ses", "_AUDIT_SESSION"),
            "tty": _field(record, message, "tty", "_TTY"),
            "exe": _field(record, message, "exe", "_EXE"),
            "subj": _field(record, message, "subj", "_SELINUX_CONTEXT"),
            "success": _field(record, message, "success"),
            "exit": _field(record, message, "exit"),
            "monotonic": record.get("__MONOTONIC_TIMESTAMP"),
        }
    elif kind == "EXECVE":
        result = {"argc": _field(record, message, "argc")}
        for index in range(MAX_ARGUMENTS):
            value = _field(record, message, f"a{index}")
            if value is not None:
                result[f"a{index}"] = value
    else:
        result = {}
    return {key: value for key, value in result.items() if value is not None}


class CompoundAssembler:
    def __init__(self, fragments=None):
        self.fragments = fragments or {}

    def ingest(self, records):
        completed = []
        changed = []
        for record in records:
            key = compound_key(record)
            kind = _kind(record)
            if key is None or not kind:
                continue
            item = self.fragments.setdefault(key, {
                "boot_id": key[0], "audit_id": key[1], "rows": {},
                "ambiguous": False, "updated_at": time.time(),
            })
            item["updated_at"] = time.time()
            current = item["rows"].get(kind)
            clean = _normalize_record(record, kind)
            if current is not None and current != clean:
                # Audit may legitimately emit multiple EXECVE rows only when
                # split arguments agree.  All other same-type disagreement is
                # ambiguous rather than last-writer-wins.
                if kind == "EXECVE":
                    merged = dict(current)
                    for field, value in clean.items():
                        if field in merged and merged[field] != value:
                            item["ambiguous"] = True
                        else:
                            merged[field] = value
                    item["rows"][kind] = merged
                else:
                    item["ambiguous"] = True
            else:
                item["rows"][kind] = clean
            changed.append(item)
            if kind == "EOE":
                completed.append(self._finish(item))
                self.fragments.pop(key, None)
        cutoff = time.time() - COMPOUND_TIMEOUT_SECONDS
        for key, item in list(self.fragments.items()):
            if float(item.get("updated_at", 0)) <= cutoff:
                item["ambiguous"] = True
                item["incomplete"] = True
                completed.append(self._finish(item))
                self.fragments.pop(key, None)
        return {"complete": completed, "fragments": list(self.fragments.values()),
                "changed": changed}

    def _finish(self, item):
        syscall = item["rows"].get("SYSCALL", {})
        execve = item["rows"].get("EXECVE", {})
        argc = _integer(execve.get("argc"), MAX_ARGUMENTS)
        argv = []
        used = 0
        success = str(syscall.get("success") or "").lower() in ("yes", "1")
        exit_code = _integer(syscall.get("exit"))
        ambiguous = item["ambiguous"] or not syscall or not execve or argc is None
        if argc is not None:
            for index in range(argc):
                value = execve.get(f"a{index}")
                if not isinstance(value, str):
                    ambiguous = True
                    break
                used += len(value.encode("utf-8", errors="replace"))
                if used > MAX_ARGUMENT_BYTES:
                    ambiguous = True
                    break
                argv.append(value)
        return {
            "boot_id": item["boot_id"], "audit_id": item["audit_id"],
            "pid": _integer(syscall.get("pid"), 2 ** 31 - 1),
            "ppid": _integer(syscall.get("ppid"), 2 ** 31 - 1),
            "uid": _integer(syscall.get("uid"), 4294967294),
            "euid": _integer(syscall.get("euid"), 4294967294),
            "auid": _integer(syscall.get("auid"), 4294967294),
            "audit_session": _integer(syscall.get("ses"), 4294967294),
            "tty": str(syscall.get("tty") or "")[:96],
            "exe": str(syscall.get("exe") or "")[:512],
            "argv": argv, "argv_sha256": hashlib.sha256(
                b"\0".join(value.encode() for value in argv)).hexdigest(),
            "selinux": str(syscall.get("subj") or "")[:256],
            "monotonic": _integer(syscall.get("monotonic")),
            "success": bool(success and exit_code == 0),
            "eoe": "EOE" in item["rows"],
            "ambiguous": ambiguous,
            "incomplete": bool(item.get("incomplete")),
        }


def _read_proc_file(root, pid, name, maximum):
    directory = os.path.join(root, str(pid))
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(os.path.join(directory, name), flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not regular")
        data = os.read(descriptor, maximum + 1)
        if len(data) > maximum:
            raise OSError("too large")
        return data
    finally:
        os.close(descriptor)


def enrich_process(pid, proc_root="/proc"):
    """Read a compact identity from one live process; absence is expected."""
    try:
        first_stat = _read_proc_file(proc_root, pid, "stat", 4096).decode().split()
        if len(first_stat) < 22:
            return None
        ppid = int(first_stat[3])
        start_ticks = int(first_stat[21])
        cmdline = _read_proc_file(proc_root, pid, "cmdline", 4096).rstrip(b"\0").split(b"\0")
        cgroup = _read_proc_file(proc_root, pid, "cgroup", 4096).decode(errors="replace")
        try:
            selinux = _read_proc_file(
                proc_root, pid, os.path.join("attr", "current"), 512
            ).decode(errors="replace").strip()
        except OSError:
            selinux = ""
        exe = os.readlink(os.path.join(proc_root, str(pid), "exe"))[:512]
        namespaces = {}
        for name in ("mnt", "pid", "user", "net"):
            try:
                target = os.readlink(os.path.join(proc_root, str(pid), "ns", name))
            except OSError:
                continue
            if re.fullmatch(r"[a-z]+:\[[0-9]{1,20}\]", target):
                namespaces[name] = target
        second_stat = _read_proc_file(proc_root, pid, "stat", 4096).decode().split()
        if len(second_stat) < 22 or int(second_stat[21]) != start_ticks:
            return None
    except (OSError, UnicodeError, ValueError):
        return None
    unit = ""
    for component in re.split(r"[/\n]", cgroup):
        if component.endswith((".service", ".scope")) and len(component) <= 256:
            unit = component
    return {
        "pid": pid, "ppid": ppid, "start_ticks": start_ticks, "exe": exe,
        "cmdline": [value.decode("utf-8", errors="replace")[:512] for value in cmdline[:16]],
        "cgroup": cgroup[:1024], "unit": unit, "selinux": selinux[:256],
        "namespaces": namespaces,
    }


def walk_parents(pid, proc_root="/proc", maximum=MAX_PARENT_DEPTH):
    found = []
    seen = set()
    current = pid
    ambiguous = False
    for _ in range(min(maximum, MAX_PARENT_DEPTH)):
        if current in seen or current <= 0:
            ambiguous = current in seen
            break
        seen.add(current)
        node = enrich_process(current, proc_root=proc_root)
        if node is None:
            # Short-lived processes commonly exit before enrichment. Audit
            # edges remain complete truth; absence is not a contradiction.
            break
        found.append(node)
        if node["ppid"] in (0, current):
            break
        current = node["ppid"]
    return {"nodes": found, "ambiguous": ambiguous}


def project_ancestors(ancestors):
    projected = []
    for node in ancestors[:MAX_EVENT_ANCESTORS]:
        clean = {}
        for key in ("pid", "ppid", "start_ticks", "exe", "unit", "cgroup", "selinux"):
            value = node.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                clean[key] = value
            elif isinstance(value, str) and value:
                clean[key] = value[:512]
        projected.append(clean)
    return projected


def firewall_receipt(record):
    if (str(record.get("SYSLOG_IDENTIFIER") or "") != "mojo-firewall-broker" or
            str(record.get("_UID") or "") != "0"):
        return None
    try:
        value = json.loads(str(record.get("MESSAGE") or ""))
    except json.JSONDecodeError:
        return None
    required = {
        "schema", "version", "kind", "operation_id", "execution_id", "job_id",
        "function", "operation", "semantic", "argv_digest", "stdin_digest",
        "stdin_length", "count", "broker_pid", "broker_start_ticks", "target_exe",
        "monotonic_ns", "children",
    }
    optional = {"target_pid", "target_start_ticks", "returncode", "duration_ms",
                "ok", "error"}
    if (not isinstance(value, dict) or not required.issubset(value) or
            not set(value).issubset(required | optional)):
        return None
    if (value.get("schema") != "mojosec.firewall-receipt" or value.get("version") != 1 or
            value.get("kind") not in ("begin", "result") or
            not re.fullmatch(r"[a-f0-9]{32}", str(value.get("operation_id") or "")) or
            not re.fullmatch(r"[A-Za-z0-9_.:@+-]{1,160}",
                             str(value.get("execution_id") or "")) or
            not re.fullmatch(r"[A-Za-z0-9_.:@+-]{1,160}", str(value.get("job_id") or "")) or
            not re.fullmatch(r"mojo\.apps\.incident\.asyncjobs\.[A-Za-z0-9_]{1,96}",
                             str(value.get("function") or "")) or
            not re.fullmatch(r"[a-f0-9]{64}", str(value.get("argv_digest") or "")) or
            not re.fullmatch(r"[a-f0-9]{64}", str(value.get("stdin_digest") or "")) or
            value.get("operation") not in {
                "rules.contains", "rule.insert", "rule.delete", "set.add", "set.delete",
                "set.replace", "set.remove", "set.rule_ensure"} or
            not isinstance(value.get("semantic"), str) or
            not 1 <= len(value["semantic"]) <= 160 or
            any(ord(char) < 32 for char in value["semantic"]) or
            _integer(value.get("stdin_length"), 24 * 1024 * 1024) is None or
            _integer(value.get("count"), 250000) is None or
            value.get("target_exe") not in (
                "/sbin/iptables", "/sbin/iptables-save", "/sbin/ipset") or
            not _integer(value.get("broker_start_ticks")) or
            _integer(value.get("monotonic_ns")) is None or
            _integer(value.get("broker_pid"), 2 ** 31 - 1) !=
            _integer(record.get("_PID"), 2 ** 31 - 1)):
        return None
    children = value.get("children")
    if not isinstance(children, list) or len(children) > 8:
        return None
    for child in children:
        if (not isinstance(child, dict) or set(child) != {
                "pid", "start_ticks", "exe", "argv_digest", "returncode", "ok"} or
                not _integer(child.get("pid"), 2 ** 31 - 1) or
                not _integer(child.get("start_ticks")) or
                child.get("exe") not in (
                    "/sbin/iptables", "/sbin/iptables-save", "/sbin/ipset") or
                not re.fullmatch(r"[a-f0-9]{64}", str(child.get("argv_digest") or "")) or
                not isinstance(child.get("returncode"), int) or
                isinstance(child.get("returncode"), bool) or
                not isinstance(child.get("ok"), bool)):
            return None
    if value["kind"] == "result" and (
            not isinstance(value.get("ok"), bool) or
            _integer(value.get("target_pid"), 2 ** 31 - 1) is None or
            _integer(value.get("target_start_ticks")) is None):
        return None
    boot_id = str(record.get("_BOOT_ID") or "").replace("-", "").lower()
    session = _integer(record.get("_AUDIT_SESSION"), 4294967294)
    tty = str(record.get("_TTY") or "")
    exe = str(record.get("_EXE") or "")
    if (not re.fullmatch(r"[a-f0-9]{32}", boot_id) or session is None or tty or
            exe not in ("/usr/bin/python3", "/usr/bin/python3.11", "/usr/bin/python3.12")):
        return None
    value["boot_id"] = boot_id
    value["audit_session"] = session
    value["producer_exe"] = exe
    return value
