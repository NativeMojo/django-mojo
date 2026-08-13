#!/usr/bin/env python3
"""Root-owned semantic firewall broker used by packaged job workers.

The broker accepts no command-line arguments.  Its sole input is one bounded
JSON object on stdin.  It builds every target argv and ipset restore program;
raw argv, shell text, environment and restore stdin are not request fields.
"""

import argparse
import hashlib
import ipaddress
import json
import os
import pwd
import re
import resource
import selectors
import shlex
import stat
import subprocess
import sys
import syslog
import time
import uuid


BROKER_PATH = "/usr/local/sbin/mojo-firewall-broker"
SUDOERS_PATH = "/etc/sudoers.d/70-mojo-firewall-broker"
IPTABLES = "/sbin/iptables"
IPTABLES_SAVE = "/sbin/iptables-save"
IPSET = "/sbin/ipset"
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_CIDRS = 250000
MAX_RESTORE_BYTES = 24 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_RULES_OUTPUT_BYTES = 8 * 1024 * 1024
SCALAR_TIMEOUT_SECONDS = 15
BULK_TIMEOUT_SECONDS = 120
ADDRESS_SPACE_BYTES = 256 * 1024 * 1024
MAX_SET_NAME = 31
_SET_NAME = re.compile(r"^[A-Za-z0-9_-]{1,31}$")
_FUNCTION = re.compile(r"^mojo\.apps\.incident\.asyncjobs\.[A-Za-z0-9_]{1,96}$")
_CONTEXT_TOKEN = re.compile(r"^[A-Za-z0-9_.:@/+\-]{1,160}$")
_CONTEXT_FIELDS = {
    "execution_id", "job_id", "function", "attempt", "channel", "runner", "broadcast",
}
_COMMON_FIELDS = {"operation", "context"}
_OP_FIELDS = {
    "rules.contains": {"source"},
    "rule.insert": {"chain", "source"},
    "rule.delete": {"chain", "source"},
    "set.add": {"set_name", "source"},
    "set.delete": {"set_name", "source"},
    "set.replace": {"set_name", "cidrs"},
    "set.remove": {"set_name"},
    "set.rule_ensure": {"set_name"},
}
_FUNCTION_OPERATIONS = {
    "mojo.apps.incident.asyncjobs.broadcast_block_ip": {
        "rules.contains", "rule.insert"},
    "mojo.apps.incident.asyncjobs.broadcast_unblock_ip": {
        "rules.contains", "rule.delete"},
    "mojo.apps.incident.asyncjobs.broadcast_ipset_add_blocked": {
        "set.add", "set.rule_ensure"},
    "mojo.apps.incident.asyncjobs.broadcast_ipset_del_blocked": {"set.delete"},
    "mojo.apps.incident.asyncjobs.sync_firewall": {
        "set.replace", "set.rule_ensure"},
    "mojo.apps.incident.asyncjobs.broadcast_sync_ipset": {
        "set.replace", "set.rule_ensure"},
    "mojo.apps.incident.asyncjobs.broadcast_remove_ipset": {"set.remove"},
}


class BrokerError(RuntimeError):
    pass


class BrokerChildError(BrokerError):
    """A launched child failed before normal result handling."""

    def __init__(self, message, child, timeout=False):
        super().__init__(message)
        self.child = child
        self.timeout = timeout


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BrokerError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def parse_request(payload):
    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_REQUEST_BYTES:
        raise BrokerError("request size is invalid")
    try:
        value = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_strict_object,
            parse_constant=lambda child: (_ for _ in ()).throw(BrokerError("constant")),
        )
    except (UnicodeError, json.JSONDecodeError) as err:
        raise BrokerError("request is not strict JSON") from err
    if not isinstance(value, dict):
        raise BrokerError("request must be an object")
    operation = value.get("operation")
    allowed = _OP_FIELDS.get(operation)
    if allowed is None or set(value) != _COMMON_FIELDS | allowed:
        raise BrokerError("request has unknown, missing, or forbidden fields")
    context = value.get("context")
    if not isinstance(context, dict) or set(context) != _CONTEXT_FIELDS:
        raise BrokerError("execution context is malformed")
    _context(context)
    return value


def _network(value):
    if not isinstance(value, str) or len(value.encode("utf-8")) > 49:
        raise BrokerError("network is invalid")
    try:
        return str(ipaddress.ip_network(value, strict=False))
    except ValueError as err:
        raise BrokerError("network is invalid") from err


def _set_name(value, temporary=False):
    if not isinstance(value, str) or not _SET_NAME.fullmatch(value):
        raise BrokerError("set name is invalid")
    if temporary and len(value) + 4 > MAX_SET_NAME:
        raise BrokerError("set name has no room for temporary suffix")
    return value


def _context(value, function=None):
    if function is not None:
        found = str(function or "")
        if not _FUNCTION.fullmatch(found):
            raise BrokerError("function is invalid")
        return found
    if not isinstance(value, dict):
        raise BrokerError("execution context is missing")
    found = str(value.get("function") or "")
    if (set(value) != _CONTEXT_FIELDS or not _FUNCTION.fullmatch(found) or
            not _CONTEXT_TOKEN.fullmatch(str(value.get("execution_id") or "")) or
            not _CONTEXT_TOKEN.fullmatch(str(value.get("job_id") or "")) or
            not _CONTEXT_TOKEN.fullmatch(str(value.get("channel") or "")) or
            not _CONTEXT_TOKEN.fullmatch(str(value.get("runner") or "")) or
            not isinstance(value.get("attempt"), int) or
            isinstance(value.get("attempt"), bool) or
            not 0 <= value["attempt"] <= 1000000 or
            not isinstance(value.get("broadcast"), bool)):
        raise BrokerError("function is invalid")
    return found


def _digest_argv(argv):
    return hashlib.sha256(b"\0".join(item.encode() for item in argv)).hexdigest()


def build_operation(request, function=None):
    if not isinstance(request, dict):
        raise BrokerError("request is invalid")
    operation = request.get("operation")
    if operation not in _OP_FIELDS:
        raise BrokerError("operation is invalid")
    function = _context(request.get("context"), function=function)
    if operation not in _FUNCTION_OPERATIONS.get(function, set()):
        raise BrokerError("function is not permitted to perform operation")
    built = {"operation": operation, "function": function, "stdin": "", "cidrs": []}
    if operation == "rules.contains":
        source = _network(request.get("source"))
        argv = [IPTABLES_SAVE]
        built.update(source=source, argv=argv, semantic="rules contain source")
    elif operation in ("rule.insert", "rule.delete"):
        chain = request.get("chain")
        if chain not in ("INPUT", "FORWARD"):
            raise BrokerError("chain is invalid")
        source = _network(request.get("source"))
        action = "-I" if operation == "rule.insert" else "-D"
        argv = [IPTABLES, action, chain, "-s", source, "-j", "DROP"]
        built.update(chain=chain, source=source, argv=argv,
                     semantic=f"{action} {chain} source DROP")
    elif operation in ("set.add", "set.delete"):
        name = _set_name(request.get("set_name"))
        source = _network(request.get("source"))
        verb = "add" if operation == "set.add" else "del"
        argv = [IPSET, verb, name, source, "-exist"]
        built.update(set_name=name, source=source, argv=argv,
                     semantic=f"set {verb} network")
    elif operation == "set.replace":
        name = _set_name(request.get("set_name"), temporary=True)
        cidrs = request.get("cidrs")
        if (not isinstance(cidrs, list) or len(cidrs) > MAX_CIDRS or
                any(not isinstance(item, str) for item in cidrs)):
            raise BrokerError("CIDR collection is invalid")
        normalized = sorted(set(_network(item) for item in cidrs))
        temporary = name + "_tmp"
        lines = [f"create {name} hash:net -exist",
                 f"create {temporary} hash:net -exist", f"flush {temporary}"]
        lines.extend(f"add {temporary} {item}" for item in normalized)
        lines.extend((f"swap {name} {temporary}", f"destroy {temporary}"))
        stdin = "\n".join(lines) + "\n"
        if len(stdin.encode()) > MAX_RESTORE_BYTES:
            raise BrokerError("canonical restore program exceeds 24 MiB")
        argv = [IPSET, "restore"]
        built.update(set_name=name, cidrs=normalized, argv=argv, stdin=stdin,
                     semantic="replace hash:net atomically")
    elif operation == "set.remove":
        name = _set_name(request.get("set_name"))
        argv = [IPSET, "destroy", name]
        built.update(set_name=name, argv=argv, semantic="destroy hash:net")
    else:
        name = _set_name(request.get("set_name"))
        argv = [IPTABLES, "-C", "INPUT", "-m", "set", "--match-set", name,
                "src", "-j", "DROP"]
        built.update(set_name=name, argv=argv, semantic="ensure INPUT set DROP")
    built["argv_digest"] = _digest_argv(built["argv"])
    built["stdin_digest"] = hashlib.sha256(built["stdin"].encode()).hexdigest()
    built["stdin_length"] = len(built["stdin"].encode())
    built["count"] = len(built["cidrs"])
    return built


def _start_ticks(pid):
    descriptor = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(f"/proc/{pid}/stat", flags)
        value = os.read(descriptor, 4097)
        if len(value) > 4096:
            return 0
        return int(value.decode("ascii").split()[21])
    except (OSError, ValueError, IndexError):
        return 0
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _receipt(kind, operation_id, context, built, children=None, **values):
    broker_start_ticks = _start_ticks(os.getpid())
    if not broker_start_ticks:
        raise BrokerError("cannot prove broker PID generation")
    value = {
        "schema": "mojosec.firewall-receipt", "version": 1, "kind": kind,
        "operation_id": operation_id, "execution_id": context["execution_id"],
        "job_id": context["job_id"], "function": context["function"],
        "operation": built["operation"], "semantic": built["semantic"],
        "argv_digest": built["argv_digest"], "stdin_digest": built["stdin_digest"],
        "stdin_length": built["stdin_length"], "count": built["count"],
        "target_exe": built["argv"][0],
        "children": list(children or ()),
        "broker_pid": os.getpid(), "broker_start_ticks": broker_start_ticks,
        "monotonic_ns": time.monotonic_ns(), **values,
    }
    syslog.openlog(ident="mojo-firewall-broker", facility=syslog.LOG_AUTHPRIV)
    syslog.syslog(syslog.LOG_INFO, json.dumps(value, sort_keys=True, separators=(",", ":")))
    return value


def _run_child(argv, stdin_text="", timeout=SCALAR_TIMEOUT_SECONDS,
               stdout_limit=MAX_OUTPUT_BYTES):
    """Run one exact child while retaining at most the reviewed output bounds."""
    process = subprocess.Popen(
        argv, stdin=subprocess.PIPE if stdin_text else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"})
    ticks = _start_ticks(process.pid)
    if not ticks:
        process.kill()
        process.wait()
        raise BrokerError("cannot prove child PID generation")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    stdin_bytes = stdin_text.encode()
    stdin_offset = 0
    if stdin_text:
        os.set_blocking(process.stdin.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
    stdout = bytearray()
    stderr = bytearray()
    deadline = time.monotonic() + timeout
    failure = None
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            ready = selector.select(min(remaining, 0.1))
            for key, unused in ready:
                if key.data == "stdin":
                    try:
                        written = os.write(
                            key.fileobj.fileno(), stdin_bytes[stdin_offset:stdin_offset + 65536])
                    except BrokenPipeError:
                        written = 0
                    stdin_offset += written
                    if written == 0 or stdin_offset >= len(stdin_bytes):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target = stdout if key.data == "stdout" else stderr
                limit = stdout_limit if key.data == "stdout" else MAX_OUTPUT_BYTES
                if len(target) + len(chunk) > limit:
                    raise BrokerError("target output exceeded semantic decision bound")
                target.extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(argv, timeout)
        returncode = process.wait(timeout=remaining)
    except Exception as err:
        failure = err
        process.kill()
        process.wait()
    finally:
        selector.close()
    child = {
        "pid": process.pid, "start_ticks": ticks, "exe": argv[0],
        "argv_digest": _digest_argv(argv),
        "returncode": process.returncode if failure is not None else returncode,
        "ok": failure is None and returncode == 0,
    }
    if failure is not None:
        raise BrokerChildError(
            "target timed out" if isinstance(failure, subprocess.TimeoutExpired)
            else "target output or I/O failed",
            child, timeout=isinstance(failure, subprocess.TimeoutExpired)) from failure
    return child, stdout.decode("utf-8", errors="replace"), stderr.decode(
        "utf-8", errors="replace")


def _rules_contain_source(payload, source):
    for line in payload.splitlines():
        if not line.startswith("-A "):
            continue
        try:
            parts = shlex.split(line, posix=True)
        except ValueError as err:
            raise BrokerError("iptables-save returned malformed syntax") from err
        if "-s" not in parts or "-j" not in parts:
            continue
        try:
            found = _network(parts[parts.index("-s") + 1])
            target = parts[parts.index("-j") + 1]
        except (IndexError, BrokerError):
            continue
        if found == source and target == "DROP":
            return True
    return False


def _expected_absence(stderr, target):
    message = str(stderr or "").lower()
    if target == "iptables":
        return "does a matching rule exist in that chain" in message
    return "does not exist" in message or "set with the given name does not exist" in message


def execute(request):
    context = request["context"]
    built = build_operation(request)
    operation_id = uuid.uuid4().hex
    _receipt("begin", operation_id, context, built, children=[])
    started = time.monotonic()
    children = []
    try:
        if built["operation"] == "set.add":
            child, unused_stdout, unused_stderr = _run_child(
                [IPSET, "create", built["set_name"], "hash:net", "-exist"])
            children.append(child)
            if not child["ok"]:
                raise BrokerError("cannot create hash:net set")
        if built["operation"] == "set.remove":
            child, unused_stdout, unused_stderr = _run_child(
                [IPTABLES, "-D", "INPUT", "-m", "set", "--match-set",
                 built["set_name"], "src", "-j", "DROP"])
            children.append(child)
            if child["returncode"] == 1 and _expected_absence(
                    unused_stderr, "iptables"):
                child["ok"] = True
            elif not child["ok"]:
                raise BrokerError("cannot remove set rule")
            child, unused_stdout, unused_stderr = _run_child(
                [IPSET, "flush", built["set_name"]])
            children.append(child)
            if child["returncode"] == 1 and _expected_absence(
                    unused_stderr, "ipset"):
                child["ok"] = True
            elif not child["ok"]:
                raise BrokerError("cannot flush set")
        stdout_limit = (MAX_RULES_OUTPUT_BYTES if built["operation"] == "rules.contains"
                        else MAX_OUTPUT_BYTES)
        child, stdout, stderr = _run_child(
            built["argv"], built["stdin"],
            BULK_TIMEOUT_SECONDS if built["operation"] == "set.replace"
            else SCALAR_TIMEOUT_SECONDS, stdout_limit=stdout_limit)
        children.append(child)
        ok = child["ok"]
        if built["operation"] == "rules.contains":
            if not ok:
                raise BrokerError("cannot read firewall rules")
            result = {"ok": True, "present": _rules_contain_source(
                stdout, built["source"])}
        elif built["operation"] == "set.rule_ensure" and child["returncode"] == 1:
            child["ok"] = True
            insert = [IPTABLES, "-I", "INPUT", "-m", "set", "--match-set",
                      built["set_name"], "src", "-j", "DROP"]
            child, stdout, stderr = _run_child(insert)
            children.append(child)
            ok = child["ok"]
            result = {"ok": ok}
        elif (built["operation"] == "set.remove" and child["returncode"] == 1 and
              _expected_absence(stderr, "ipset")):
            child["ok"] = True
            ok = True
            result = {"ok": True}
        else:
            result = {"ok": ok}
        receipt = _receipt(
            "result", operation_id, context, built, children=children,
            target_pid=child["pid"], target_start_ticks=child["start_ticks"],
            returncode=child["returncode"],
            duration_ms=int((time.monotonic() - started) * 1000), ok=ok)
        result["operation_id"] = operation_id
        result["receipt"] = receipt
        return result
    except BrokerChildError as err:
        children.append(err.child)
        _receipt(
            "result", operation_id, context, built, children=children,
            target_pid=err.child["pid"], target_start_ticks=err.child["start_ticks"],
            returncode=err.child["returncode"], ok=False,
            error="timeout" if err.timeout else "target_failure")
        raise BrokerError("target timed out" if err.timeout else
                          "target execution failed") from err
    except subprocess.TimeoutExpired as err:
        _receipt("result", operation_id, context, built, children=children,
                 target_pid=0, target_start_ticks=0, returncode=-1, ok=False, error="timeout")
        raise BrokerError("target timed out") from err
    except (BrokerError, OSError, subprocess.SubprocessError) as err:
        _receipt("result", operation_id, context, built, children=children,
                 target_pid=0, target_start_ticks=0, returncode=-1, ok=False,
                 error="target_failure")
        raise BrokerError("target execution failed") from err


def _verify_caller():
    if os.geteuid() != 0:
        raise BrokerError("broker must run as root")
    raw_uid = os.environ.get("SUDO_UID", "")
    if not raw_uid.isdigit() or int(raw_uid) <= 0:
        raise BrokerError("SUDO_UID is missing or invalid")
    try:
        expected = pwd.getpwnam("ec2-user").pw_uid
    except KeyError as err:
        raise BrokerError("application account is missing") from err
    if int(raw_uid) != expected:
        raise BrokerError("caller is not the application account")
    info = os.stat(BROKER_PATH, follow_symlinks=False)
    if (info.st_uid != 0 or info.st_gid != 0 or info.st_mode & 0o022 or
            not stat.S_ISREG(info.st_mode)):
        raise BrokerError("installed broker metadata is unsafe")


def render_sudoers(user="ec2-user"):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", user):
        raise BrokerError("sudoers user is invalid")
    # An empty quoted argument is sudoers' exact no-arguments grammar.
    return f"{user} ALL=(root) NOPASSWD: {BROKER_PATH} \"\"\n"


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        print("firewall broker accepts no arguments", file=sys.stderr)
        return 2
    try:
        _verify_caller()
        resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE_BYTES, ADDRESS_SPACE_BYTES))
        payload = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        request = parse_request(payload)
        print(json.dumps(execute(request), sort_keys=True, separators=(",", ":")))
        return 0
    except (BrokerError, OSError, ValueError) as err:
        print(f"firewall broker: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
