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
FINALIZED_TTL_SECONDS = 10 * 60
COMPOUND_CAP = 8192
PROCTITLE_BOUNDARY = "audit-proctitle-v1"
# journald extracts the kernel Audit serial into _AUDIT_ID.  The timestamp
# remains in _SOURCE_REALTIME_TIMESTAMP and must never be reconstructed from
# MESSAGE text to form a compound identity.
_AUDIT_ID = re.compile(r"^[0-9]{1,20}$")
_AUDIT_TYPES = {"SYSCALL", "EXECVE", "PROCTITLE", "CWD", "EOE"}
CROND_SELINUX = "system_u:system_r:crond_t:s0-s0:c0.c1023"
CROND_PAM_GRANTORS = "pam_loginuid,pam_keyinit,pam_limits,pam_systemd"
_PAM_MESSAGE = re.compile(
    r'^USER_START(?: [A-Za-z][A-Za-z0-9_]{0,31}=[^ ]{1,256})* '
    r'msg=\'op=PAM:session_open grantors=('
    + re.escape(CROND_PAM_GRANTORS) +
    r') acct="([A-Za-z0-9_.-]{1,128})" '
    r'exe="(/usr/sbin/crond)" hostname=\? addr=\? terminal=(cron) '
    r'res=(success)\'(?: [A-Za-z][A-Za-z0-9_]{0,31}=[^ ]{1,256})*$')


def crond_launch(record, project_path, app_uid, app_gid):
    """Project one half of the exact AL2023 CROND/PAM launch attestation."""
    boot = str(record.get("_BOOT_ID") or "").replace("-", "").lower()
    session = _integer(record.get("_AUDIT_SESSION"), 4294967294)
    if not re.fullmatch(r"[a-f0-9]{32}", boot) or session is None:
        return None
    command = (f'{project_path}/bin/jobman start >> '
               f'{project_path}/var/logs/jobman.log 2>&1')
    common = bool(str(record.get("_UID") or "") == "0" and
                  str(record.get("_AUDIT_LOGINUID") or "") == str(app_uid) and
                  str(record.get("_SELINUX_CONTEXT") or "") == CROND_SELINUX)
    monotonic = _integer(record.get("__MONOTONIC_TIMESTAMP"))
    if not common or monotonic is None:
        return None
    if record.get("_TRANSPORT") == "syslog":
        expected_message = f"(ec2-user) CMD ({command})"
        pid = _integer(record.get("_PID"), 2 ** 31 - 1)
        scope = f"session-{session}.scope"
        cgroup = str(record.get("_SYSTEMD_CGROUP") or "")
        if (str(record.get("_GID") or "") != str(app_gid) or
                record.get("SYSLOG_IDENTIFIER") != "CROND" or
                record.get("_COMM") != "crond" or
                record.get("_EXE") != "/usr/sbin/crond" or
                record.get("_CMDLINE") != "/usr/sbin/CROND -n" or
                record.get("MESSAGE") != expected_message or not pid or
                record.get("_SYSTEMD_UNIT") != scope or
                not cgroup.endswith("/" + scope)):
            return None
        return {"boot_id": boot, "audit_session": session, "half": "syslog",
                "launch_pid": pid, "monotonic": monotonic,
                "command_sha256": hashlib.sha256(command.encode()).hexdigest()}
    if (record.get("_TRANSPORT") == "audit" and
            str(record.get("_AUDIT_TYPE_NAME") or "") == "USER_START"):
        message = str(record.get("MESSAGE") or "")
        if len(message.encode("utf-8", errors="replace")) > 4096:
            return None
        match = _PAM_MESSAGE.fullmatch(message)
        exposed_grantors = [record.get(key) for key in (
            "AUDIT_FIELD_GRANTORS", "_AUDIT_FIELD_GRANTORS")
                            if record.get(key) not in (None, "")]
        if (any(value != CROND_PAM_GRANTORS for value in exposed_grantors) or
                len(set(exposed_grantors)) > 1 or match is None or
                match.groups() != (CROND_PAM_GRANTORS, "ec2-user",
                                   "/usr/sbin/crond", "cron", "success")):
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
    kind = str(record.get("_AUDIT_TYPE_NAME") or "").upper()
    return kind if kind in _AUDIT_TYPES else ""


def _message_fields(record):
    message = str(record.get("MESSAGE") or "")
    if len(message.encode("utf-8", errors="replace")) > MAX_ARGUMENT_BYTES * 3:
        raise ValueError("oversized Audit message")
    fields = {}
    # Keep quotes: unquoted hex is Audit encoding, quoted hex is literal text.
    tokens = re.findall(r'[^\s="\']+="[^"\n]*"(?=\s|$)|[^\s]+', message)
    for token in tokens:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        key = key.lower()
        if key in fields and fields[key] != value:
            raise ValueError("conflicting Audit message field")
        fields[key] = value
    return fields


def _audit_string(value):
    if not isinstance(value, str) or len(value) > MAX_ARGUMENT_BYTES * 2:
        raise ValueError("invalid Audit string")
    if value.startswith('"'):
        if len(value) < 2 or not value.endswith('"') or '"' in value[1:-1]:
            raise ValueError("invalid quoted Audit string")
        value = value[1:-1]
    elif re.fullmatch(r"[a-fA-F0-9]+", value):
        if len(value) % 2:
            raise ValueError("invalid Audit hex")
        value = bytes.fromhex(value).decode("utf-8", errors="strict")
    elif '"' in value or any(char.isspace() for char in value):
        raise ValueError("invalid unquoted Audit string")
    if "\0" in value or len(value.encode("utf-8")) > MAX_ARGUMENT_BYTES:
        raise ValueError("invalid Audit argument bytes")
    return value


def _field(record, message, name, *fallbacks):
    decode = _audit_string if name == "exe" or re.fullmatch(r"a[0-9]+", name) else str
    values = [decode(value) for value in (
        record.get(f"_AUDIT_FIELD_{name.upper()}"), message.get(name.lower()))
        if value is not None]
    if values:
        if any(value != values[0] for value in values):
            raise ValueError("Audit representations disagree")
        return values[0]
    for key in fallbacks:
        if record.get(key) not in (None, ""):
            return record[key]
    return None


def _normalize_record(record, kind):
    message = _message_fields(record)
    prefix = str(record.get("MESSAGE") or "").split(" ", 1)[0]
    if prefix.startswith("type="):
        prefix = prefix[5:]
    if prefix in _AUDIT_TYPES and prefix != kind:
        raise ValueError("Audit record types disagree")
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
        names = set(message) | {key[len("_AUDIT_FIELD_"):].lower()
                                for key in record if key.startswith("_AUDIT_FIELD_")}
        for name in names:
            match = re.fullmatch(r"a([0-9]+)(.*)", name)
            if match and (int(match[1]) >= MAX_ARGUMENTS or match[2]):
                raise ValueError("unsupported or oversized Audit argument index")
        result = {"argc": _field(record, message, "argc")}
        for index in range(MAX_ARGUMENTS):
            value = _field(record, message, f"a{index}")
            if value is not None:
                result[f"a{index}"] = value
    elif kind == "PROCTITLE":
        result = {"proctitle": _field(record, message, "proctitle")}
    else:
        result = {}
    return {key: value for key, value in result.items() if value is not None}


class CompoundAssembler:
    def __init__(self, fragments=None):
        self.fragments = fragments or {}

    def ingest(self, records):
        completed = []
        changed = {}
        now = time.time()
        self.fragments = {key: item for key, item in self.fragments.items()
                          if not item.get("finalized") or
                          float(item.get("updated_at", 0)) > now - FINALIZED_TTL_SECONDS}
        for record in records:
            key = compound_key(record)
            kind = _kind(record)
            if key is None or not kind:
                continue
            item = self.fragments.setdefault(key, {
                "boot_id": key[0], "audit_id": key[1], "rows": {},
                "ambiguous": False, "updated_at": time.time(),
            })
            item["updated_at"] = now
            current = item["rows"].get(kind)
            try:
                clean = _normalize_record(record, kind)
            except (ValueError, UnicodeError):
                item["ambiguous"] = True
                clean = {}
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
            changed[key] = item
        cutoff = now - COMPOUND_TIMEOUT_SECONDS
        for key, item in list(self.fragments.items()):
            terminal = "EOE" in item["rows"] or "PROCTITLE" in item["rows"]
            if not item.get("finalized") and not terminal and float(item.get("updated_at", 0)) <= cutoff:
                item["ambiguous"] = True
                item["incomplete"] = True
                changed[key] = item
            if key in changed and (terminal or item.get("incomplete") or item.get("finalized")):
                node = self._finish(item)
                item["ambiguous"] = node["ambiguous"]
                digest = hashlib.sha256(json.dumps(node, sort_keys=True).encode()).hexdigest()
                if digest != item.get("final_digest"):
                    completed.append(node)
                item["finalized"] = True
                item["final_digest"] = digest
        # Limits retire evidence, never manufacture complete nodes.
        self.fragments = dict(sorted(self.fragments.items(), key=lambda pair:
                                     pair[1]["updated_at"], reverse=True)[:COMPOUND_CAP])
        return {"complete": completed, "fragments": list(self.fragments.values()),
                "changed": list(changed.values())}

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
            if any(f"a{index}" in execve for index in range(argc, MAX_ARGUMENTS)):
                ambiguous = True
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
            "_completion": PROCTITLE_BOUNDARY if "PROCTITLE" in item["rows"] else "",
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


def enrich_process(pid, proc_root="/proc", exe_resolver=None):
    """Read a compact identity from one live process; absence is expected."""
    try:
        with open(os.path.join(proc_root, "sys/kernel/random/boot_id"), encoding="ascii") as handle:
            boot_id = handle.read(64).strip().replace("-", "").lower()
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
        if exe_resolver is not None:
            exe = exe_resolver(pid, start_ticks)
        else:
            try:
                exe = os.readlink(os.path.join(proc_root, str(pid), "exe"))
            except PermissionError:
                if proc_root != "/proc":
                    raise
                from .proc_identity import resolve_executable
                try:
                    exe = resolve_executable(pid, start_ticks)
                except (OSError, RuntimeError, UnicodeError, ValueError) as err:
                    raise OSError("process executable identity is unavailable") from err
        if (not isinstance(exe, str) or not exe.startswith("/") or "\0" in exe or
                len(exe.encode("utf-8", errors="strict")) > 512):
            return None
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
        "pid": pid, "ppid": ppid, "start_ticks": start_ticks, "exe": exe, "boot_id": boot_id,
        "cmdline": [value.decode("utf-8", errors="replace")[:512] for value in cmdline[:16]],
        "cgroup": cgroup[:1024], "unit": unit, "selinux": selinux[:256],
        "namespaces": namespaces,
    }


def eligible_process_node(node):
    argv = node.get("argv") if isinstance(node, dict) else None
    return bool(
        isinstance(node, dict) and node.get("success") is True and
        (node.get("eoe") is True or node.get("_completion") == PROCTITLE_BOUNDARY) and
        not node.get("ambiguous") and not node.get("incomplete") and
        isinstance(argv, list) and 1 <= len(argv) <= MAX_ARGUMENTS and
        all(isinstance(part, str) for part in argv) and
        sum(len(part.encode("utf-8", errors="replace")) for part in argv) <= MAX_ARGUMENT_BYTES)


def live_engine_identity(node, live):
    """The same complete Audit and live generation contract at pin and refresh."""
    argv = node.get("argv", [])
    return bool(eligible_process_node(node) and live and
                node.get("boot_id") == live.get("boot_id") and
                node.get("pid") == live.get("pid") and
                node.get("start_ticks") and node["start_ticks"] == live.get("start_ticks") and
                node.get("exe") == live.get("exe") and
                os.path.basename(node.get("exe", "")).startswith("python3") and
                argv == live.get("cmdline") and len(argv) >= 3 and
                argv[-2:] == ["engine", "foreground"] and
                (argv[-3].endswith("/bin/jobs.py") or argv[-3] == "bin/jobs.py"))


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
                "rules.contains", "rule.insert", "rule.delete",
                "permanent.add", "permanent.delete", "permanent.rule_ensure",
                "permanent.normalize", "set.replace", "set.remove",
                "set.rule_ensure", "set.status", "set.normalize",
                "ip.status", "ip.normalize", "geolocated.normalize"} or
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
    if not isinstance(children, list) or len(children) > 64:
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
