"""Bounded Linux Audit compound and process-lineage handling."""

import hashlib
import json
import os
import re
import stat
import time


MAX_ARGUMENTS = 64
MAX_ARGUMENT_BYTES = 16 * 1024
MAX_PARENT_DEPTH = 32
MAX_EVENT_ANCESTORS = 8
COMPOUND_TIMEOUT_SECONDS = 2
_SERIAL = re.compile(r"audit\([^:()]+:(?P<serial>[0-9]{1,20})\)")
_AUDIT_TYPES = {"SYSCALL", "EXECVE", "PROCTITLE", "CWD", "EOE"}


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
    match = _SERIAL.search(str(record.get("MESSAGE") or ""))
    if not re.fullmatch(r"[a-f0-9]{32}", boot) or match is None:
        return None
    return boot, int(match.group("serial"))


def _kind(record):
    kind = str(record.get("_AUDIT_TYPE_NAME") or record.get("AUDIT_TYPE_NAME") or "").upper()
    return kind if kind in _AUDIT_TYPES else ""


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
                "boot_id": key[0], "audit_serial": key[1], "rows": {},
                "ambiguous": False, "updated_at": time.time(),
            })
            item["updated_at"] = time.time()
            current = item["rows"].get(kind)
            clean = {str(k): v for k, v in record.items()
                     if isinstance(k, str) and len(k) <= 64 and
                     isinstance(v, (str, int)) and len(str(v).encode()) <= 16384}
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
        ambiguous = item["ambiguous"] or not syscall
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
            "boot_id": item["boot_id"], "audit_serial": item["audit_serial"],
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
            "monotonic": _integer(syscall.get("__MONOTONIC_TIMESTAMP")),
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
        stat_fields = _read_proc_file(proc_root, pid, "stat", 4096).decode().split()
        if len(stat_fields) < 22:
            return None
        ppid = int(stat_fields[3])
        start_ticks = int(stat_fields[21])
        cmdline = _read_proc_file(proc_root, pid, "cmdline", 4096).rstrip(b"\0").split(b"\0")
        cgroup = _read_proc_file(proc_root, pid, "cgroup", 4096).decode(errors="replace")
        exe = os.readlink(os.path.join(proc_root, str(pid), "exe"))[:512]
    except (OSError, UnicodeError, ValueError):
        return None
    return {
        "pid": pid, "ppid": ppid, "start_ticks": start_ticks, "exe": exe,
        "cmdline": [value.decode("utf-8", errors="replace")[:512] for value in cmdline[:16]],
        "cgroup": cgroup[:1024],
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
            ambiguous = True
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
        "stdin_length", "count", "broker_pid", "broker_start_ticks", "monotonic_ns",
    }
    if not isinstance(value, dict) or not required.issubset(value):
        return None
    if (value.get("schema") != "mojosec.firewall-receipt" or value.get("version") != 1 or
            value.get("kind") not in ("begin", "result") or
            not re.fullmatch(r"[a-f0-9]{32}", str(value.get("operation_id") or "")) or
            _integer(value.get("broker_pid"), 2 ** 31 - 1) !=
            _integer(record.get("_PID"), 2 ** 31 - 1)):
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
