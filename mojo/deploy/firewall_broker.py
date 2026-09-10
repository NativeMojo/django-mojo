#!/usr/bin/env python3
"""Root-owned semantic firewall broker used by packaged job workers.

The broker accepts no command-line arguments.  Its sole input is one bounded
JSON object on stdin.  It builds every target argv and ipset restore program;
raw argv, shell text, environment and restore stdin are not request fields.
"""

import argparse
import fcntl
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

from mojo.apps.incident.services.firewall_truth import (
    FirewallTruthError,
    canonical_ipv4,
    canonical_ipv4_networks,
    network_digest,
)


BROKER_PATH = "/usr/local/sbin/mojo-firewall-broker"
SUDOERS_PATH = "/etc/sudoers.d/70-mojo-firewall-broker"
CONFIG_PATH = "/etc/mojo-firewall-broker.json"
DEFAULT_PERMANENT_SET_NAME = "mojo_blocked"
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
ADDRESS_SPACE_GROWTH_BYTES = 256 * 1024 * 1024
MAX_ADDRESS_SPACE_BYTES = 768 * 1024 * 1024
MAX_STATM_BYTES = 128
_RESOURCE_EXHAUSTED_RESPONSE = (
    b'{"error":{"code":"broker_resource_exhausted",'
    b'"message":"broker memory exhausted"},"ok":false}\n')
_RESOURCE_LIMIT_UNAVAILABLE_RESPONSE = (
    b'{"error":{"code":"broker_resource_limit_unavailable",'
    b'"message":"broker resource limit unavailable"},"ok":false}\n')
MAX_SET_NAME = 31
MAX_CONFIG_BYTES = 4096
_SET_NAME = re.compile(r"^[A-Za-z0-9_-]{1,31}$")
READINESS_FUNCTION = "mojo.apps.incident.services.firewall_readiness.probe"
_FUNCTION = re.compile(
    r"^(?:mojo\.apps\.incident\.asyncjobs\.[A-Za-z0-9_]{1,96}|"
    r"mojo\.apps\.incident\.services\.firewall_readiness\.probe)$")
_CONTEXT_TOKEN = re.compile(r"^[A-Za-z0-9_.:@/+\-]{1,160}$")
_CONTEXT_FIELDS = {
    "execution_id", "job_id", "function", "attempt", "channel", "runner", "broadcast",
}
_COMMON_FIELDS = {"operation", "context"}
_OP_FIELDS = {
    "broker.status": set(),
    "rules.contains": {"source"},
    "rule.insert": {"chain", "source"},
    "rule.delete": {"chain", "source"},
    "permanent.add": {"source", "expected_permanent_set"},
    "permanent.delete": {"source", "expected_permanent_set"},
    "permanent.rule_ensure": {"expected_permanent_set"},
    "permanent.normalize": {"cidrs", "expected_permanent_set"},
    "set.replace": {"set_name", "cidrs", "expected_permanent_set"},
    "set.remove": {"set_name", "expected_permanent_set"},
    "set.rule_ensure": {"set_name", "expected_permanent_set"},
    "ip.status": {"source"},
    "ip.normalize": {"source", "present"},
    "set.status": {"set_name", "expected_permanent_set"},
    "set.normalize": {
        "set_name", "cidrs", "present", "expected_permanent_set"},
    "geolocated.normalize": {
        "source", "cidrs", "temporary_present", "expected_permanent_set"},
}
_FUNCTION_OPERATIONS = {
    READINESS_FUNCTION: {"broker.status"},
    "mojo.apps.incident.asyncjobs.broadcast_block_ip": {
        "rules.contains", "rule.insert", "ip.status", "ip.normalize"},
    "mojo.apps.incident.asyncjobs.broadcast_unblock_ip": {
        "rules.contains", "rule.delete", "ip.status", "ip.normalize"},
    "mojo.apps.incident.asyncjobs.broadcast_ipset_add_blocked": {
        "permanent.add", "permanent.rule_ensure"},
    "mojo.apps.incident.asyncjobs.broadcast_ipset_del_blocked": {
        "permanent.delete"},
    "mojo.apps.incident.asyncjobs.sync_firewall": {
        "permanent.normalize", "set.normalize", "ip.normalize"},
    "mojo.apps.incident.asyncjobs.broadcast_sync_ipset": {
        "set.replace", "set.rule_ensure", "set.status", "set.normalize"},
    "mojo.apps.incident.asyncjobs.broadcast_remove_ipset": {
        "set.remove", "set.status", "set.normalize"},
    "mojo.apps.incident.asyncjobs.broadcast_reconcile_firewall_ip": {
        "ip.status", "ip.normalize"},
    "mojo.apps.incident.asyncjobs.broadcast_reconcile_firewall_set": {
        "set.status", "set.normalize"},
    "mojo.apps.incident.asyncjobs.broadcast_reconcile_geolocated_ip": {
        "geolocated.normalize"},
}


class BrokerError(RuntimeError):
    def __init__(self, message, code="invalid_request"):
        super().__init__(message)
        self.code = code


class BrokerChildError(BrokerError):
    """A launched child failed before normal result handling."""

    def __init__(self, message, child, timeout=False):
        super().__init__(
            message,
            code="broker_target_timeout" if timeout else "broker_target_failure")
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
    if operation == "broker.status":
        if set(value) != {"operation"}:
            raise BrokerError("broker status accepts only operation")
        return value
    if not isinstance(operation, str):
        raise BrokerError("operation must be a string")
    allowed = _OP_FIELDS.get(operation)
    if allowed is None or set(value) != _COMMON_FIELDS | allowed:
        raise BrokerError("request has unknown, missing, or forbidden fields")
    context = value.get("context")
    if not isinstance(context, dict) or set(context) != _CONTEXT_FIELDS:
        raise BrokerError("execution context is malformed")
    _context(context)
    return value


def _network(value):
    try:
        return canonical_ipv4(value)
    except FirewallTruthError as err:
        raise BrokerError(str(err), code=err.code) from err


def _set_name(value, temporary=False):
    if not isinstance(value, str) or not _SET_NAME.fullmatch(value):
        raise BrokerError("set name is invalid")
    if value.endswith("_tmp"):
        raise BrokerError("temporary set namespace is reserved",
                          code="reserved_set_name")
    if temporary and len(value) + 4 > MAX_SET_NAME:
        raise BrokerError("set name has no room for temporary suffix")
    return value


def _root_permanent_set_name(path=CONFIG_PATH):
    """Read the root-owned broker namespace, or the secure legacy default."""
    descriptor = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return DEFAULT_PERMANENT_SET_NAME
    try:
        info = os.fstat(descriptor)
        if (info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or
                info.st_mode & 0o077):
            raise BrokerError(
                "firewall broker config metadata is unsafe",
                code="broker_config_unsafe")
        payload = os.read(descriptor, MAX_CONFIG_BYTES + 1)
        if not payload or len(payload) > MAX_CONFIG_BYTES:
            raise BrokerError(
                "firewall broker config size is invalid",
                code="broker_config_invalid")
        value = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_strict_object,
            parse_constant=lambda unused: (_ for _ in ()).throw(
                BrokerError("constant")))
        if not isinstance(value, dict) or set(value) != {"permanent_set_name"}:
            raise BrokerError(
                "firewall broker config is invalid",
                code="broker_config_invalid")
        return _set_name(value["permanent_set_name"], temporary=True)
    except (UnicodeError, json.JSONDecodeError) as err:
        raise BrokerError(
            "firewall broker config is invalid",
            code="broker_config_invalid") from err
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _permanent_authority(request):
    """Return root authority only when application configuration agrees."""
    actual = _root_permanent_set_name()
    expected = _set_name(request.get("expected_permanent_set"), temporary=True)
    if expected != actual:
        raise BrokerError(
            "application and root firewall set configuration differ",
            code="permanent_set_config_mismatch")
    return actual


def _operator_set_target(request, temporary=False):
    aggregate = _permanent_authority(request)
    name = _set_name(request.get("set_name"), temporary=True)
    if name == aggregate or name.startswith("mojo_"):
        raise BrokerError(
            "framework firewall set namespace is reserved",
            code="reserved_set_name")
    return name


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
    if operation == "broker.status":
        built.update(argv=[BROKER_PATH], semantic="read broker enrollment status")
    elif operation == "rules.contains":
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
    elif operation in ("permanent.add", "permanent.delete"):
        name = _permanent_authority(request)
        source = _network(request.get("source"))
        verb = "add" if operation == "permanent.add" else "del"
        argv = [IPSET, verb, name, source, "-exist"]
        built.update(set_name=name, source=source, argv=argv,
                     semantic=f"set {verb} network")
    elif operation == "set.replace":
        name = _operator_set_target(request, temporary=True)
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
        name = _operator_set_target(request)
        argv = [IPSET, "destroy", name]
        built.update(set_name=name, argv=argv, semantic="destroy hash:net")
    elif operation in ("set.rule_ensure", "permanent.rule_ensure"):
        name = (_permanent_authority(request)
                if operation == "permanent.rule_ensure"
                else _operator_set_target(request))
        argv = [IPTABLES, "-C", "INPUT", "-m", "set", "--match-set", name,
                "src", "-j", "DROP"]
        built.update(set_name=name, argv=argv, semantic="ensure INPUT set DROP")
    elif operation in ("ip.status", "ip.normalize"):
        source = _network(request.get("source"))
        if not isinstance(request.get("present", False), bool):
            raise BrokerError("present must be a boolean")
        built.update(
            source=source, present=request.get("present", False),
            argv=[IPTABLES_SAVE], semantic="normalize exact IPv4 DROP rules")
    elif operation in ("set.status", "set.normalize", "permanent.normalize"):
        present = (True if operation == "permanent.normalize"
                   else request.get("present", False))
        if not isinstance(present, bool):
            raise BrokerError("present must be a boolean")
        name = (_permanent_authority(request)
                if operation == "permanent.normalize"
                else _operator_set_target(
                    request, temporary=(
                        operation == "set.normalize" and present)))
        cidrs = request.get("cidrs", [])
        try:
            cidrs = canonical_ipv4_networks(cidrs, limit=MAX_CIDRS) if present else []
        except FirewallTruthError as err:
            raise BrokerError(str(err), code=err.code) from err
        built.update(
            set_name=name, present=present, cidrs=cidrs,
            argv=[IPSET], semantic="normalize exact IPv4 hash:net set and rules")
    else:
        source = _network(request.get("source"))
        name = _permanent_authority(request)
        temporary_present = request.get("temporary_present")
        if not isinstance(temporary_present, bool):
            raise BrokerError("temporary_present must be a boolean")
        try:
            cidrs = canonical_ipv4_networks(
                request.get("cidrs"), limit=MAX_CIDRS)
        except FirewallTruthError as err:
            raise BrokerError(str(err), code=err.code) from err
        built.update(
            source=source, set_name=name, cidrs=cidrs,
            temporary_present=temporary_present, argv=[IPTABLES, IPSET],
            semantic="normalize one IP and the permanent IPv4 set atomically")
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
        raise BrokerError(
            "cannot prove broker PID generation",
            code="broker_identity_unavailable")
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
        raise BrokerError(
            "cannot prove child PID generation",
            code="broker_identity_unavailable")
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


def _forwarding_required():
    try:
        with open("/proc/sys/net/ipv4/ip_forward", encoding="ascii") as value:
            return value.read(8).strip() == "1"
    except (OSError, UnicodeError):
        raise BrokerError("cannot read IPv4 forwarding state", code="status_unavailable")


def _rule_counts(payload, source=None, set_name=None):
    counts = {"INPUT": 0, "FORWARD": 0}
    for line in payload.splitlines():
        if not line.startswith("-A "):
            continue
        try:
            parts = shlex.split(line, posix=True)
        except ValueError as err:
            raise BrokerError(
                "iptables-save returned malformed syntax",
                code="status_malformed") from err
        if len(parts) < 3 or parts[1] not in counts:
            continue
        if "-j" not in parts:
            continue
        try:
            target = parts[parts.index("-j") + 1]
        except IndexError:
            raise BrokerError("firewall rule is malformed", code="status_malformed")
        if target != "DROP":
            continue
        matched = False
        if source is not None and "-s" in parts:
            try:
                found = _network(parts[parts.index("-s") + 1])
                matched = parts == [
                    "-A", parts[1], "-s", found, "-j", "DROP"] and found == source
            except (IndexError, BrokerError):
                matched = False
        if set_name is not None and "--match-set" in parts:
            try:
                index = parts.index("--match-set")
                matched = (
                    parts[index + 1:index + 3] == [set_name, "src"] and
                    parts == ["-A", parts[1], "-m", "set", "--match-set",
                              set_name, "src", "-j", "DROP"])
            except IndexError:
                matched = False
        if matched:
            counts[parts[1]] += 1
    return counts


def _read_rules():
    child, stdout, unused = _run_child(
        [IPTABLES_SAVE], stdout_limit=MAX_RULES_OUTPUT_BYTES)
    if not child["ok"]:
        raise BrokerError("cannot read firewall rules", code="status_unavailable")
    return child, stdout


def _ip_status(source):
    child, rules = _read_rules()
    counts = _rule_counts(rules, source=source)
    forwarding = _forwarding_required()
    desired_forward = 1 if forwarding else 0
    return child, {
        "ip": source.split("/", 1)[0],
        "present": counts["INPUT"] == 1 and counts["FORWARD"] == desired_forward,
        "input_count": counts["INPUT"],
        "forward_count": counts["FORWARD"],
        "forwarding_required": forwarding,
    }


def _parse_set_save(payload, set_name):
    family = None
    set_type = None
    members = []
    for line in payload.splitlines():
        try:
            parts = shlex.split(line, posix=True)
        except ValueError as err:
            raise BrokerError("ipset returned malformed syntax", code="status_malformed") from err
        if not parts:
            continue
        if parts[0] == "create" and len(parts) >= 3 and parts[1] == set_name:
            set_type = parts[2]
            family = "inet"
            if "family" in parts:
                try:
                    family = parts[parts.index("family") + 1]
                except IndexError as err:
                    raise BrokerError(
                        "ipset family is malformed", code="status_malformed") from err
        elif parts[0] == "add" and len(parts) == 3 and parts[1] == set_name:
            if len(members) >= MAX_CIDRS:
                raise BrokerError("ipset member bound exceeded", code="status_overflow")
            if len(parts[2].encode("utf-8")) > 128:
                raise BrokerError("ipset member is too long", code="status_malformed")
            members.append(parts[2])
    if set_type is None:
        raise BrokerError("ipset definition is missing", code="status_malformed")
    if set_type == "hash:net" and family == "inet":
        try:
            canonical = sorted(set(canonical_ipv4(member) for member in members))
        except FirewallTruthError as err:
            raise BrokerError(str(err), code="set_member_invalid") from err
    else:
        # Incompatible types/families still need an observable status so the
        # normalizer can remove references, destroy them, and recreate the
        # reviewed IPv4 hash:net shape. Their member text is never executed.
        canonical = sorted(set(members))
    if len(canonical) != len(members):
        raise BrokerError("ipset contains duplicate members", code="status_malformed")
    digest = hashlib.sha256("\n".join(canonical).encode("utf-8")).hexdigest()
    return set_type, family, canonical, digest


def _set_status(set_name):
    child, stdout, stderr = _run_child(
        [IPSET, "save", set_name], stdout_limit=MAX_RULES_OUTPUT_BYTES)
    if child["returncode"] == 1 and _expected_absence(stderr, "ipset"):
        child["ok"] = True
        exists = False
        set_type, family, members = None, None, []
        digest = network_digest([])
    elif not child["ok"]:
        raise BrokerError("cannot read ipset", code="status_unavailable")
    else:
        exists = True
        set_type, family, members, digest = _parse_set_save(stdout, set_name)
    rule_child, rules = _read_rules()
    counts = _rule_counts(rules, set_name=set_name)
    forwarding = _forwarding_required()
    desired_forward = 1 if forwarding else 0
    present = bool(
        exists and set_type == "hash:net" and family == "inet" and
        counts["INPUT"] == 1 and counts["FORWARD"] == desired_forward)
    return [child, rule_child], {
        "name": set_name, "present": present, "exists": exists,
        "type": set_type, "family": family, "count": len(members),
        "digest": digest,
        "input_count": counts["INPUT"],
        "forward_count": counts["FORWARD"],
        "forwarding_required": forwarding,
    }


def _normalize_rules(source=None, set_name=None, present=True):
    read_child, rules = _read_rules()
    children = [read_child]
    counts = _rule_counts(rules, source=source, set_name=set_name)
    matcher = (["-s", source] if source is not None else
               ["-m", "set", "--match-set", set_name, "src"])
    for chain in ("INPUT", "FORWARD"):
        for unused_count in range(counts[chain]):
            child, stdout, stderr = _run_child(
                [IPTABLES, "-D", chain] + matcher + ["-j", "DROP"])
            children.append(child)
            if not child["ok"]:
                raise BrokerError("cannot remove duplicate rule", code="normalize_failed")
    forwarding = _forwarding_required()
    if present:
        chains = ["INPUT"] + (["FORWARD"] if forwarding else [])
        for chain in chains:
            child, stdout, stderr = _run_child(
                [IPTABLES, "-I", chain] + matcher + ["-j", "DROP"])
            children.append(child)
            if not child["ok"]:
                raise BrokerError("cannot establish firewall rule", code="normalize_failed")
    return children


def _replace_set(set_name, cidrs):
    temporary = set_name + "_tmp"
    lines = [f"create {set_name} hash:net family inet -exist",
             f"create {temporary} hash:net family inet -exist",
             f"flush {temporary}"]
    lines.extend(f"add {temporary} {item}" for item in cidrs)
    lines.extend((f"swap {set_name} {temporary}", f"destroy {temporary}"))
    stdin = "\n".join(lines) + "\n"
    if len(stdin.encode()) > MAX_RESTORE_BYTES:
        raise BrokerError("canonical restore program exceeds bound", code="network_limit")
    child, stdout, stderr = _run_child(
        [IPSET, "restore"], stdin, BULK_TIMEOUT_SECONDS)
    if not child["ok"]:
        raise BrokerError("cannot replace ipset", code="normalize_failed")
    return child


def _normalize_ip(source, present):
    children = _normalize_rules(source=source, present=present)
    child, observed = _ip_status(source)
    children.append(child)
    expected_forward = 1 if observed["forwarding_required"] and present else 0
    ok = (observed["input_count"] == (1 if present else 0) and
          observed["forward_count"] == expected_forward)
    return children, observed, ok


def _normalize_set(set_name, cidrs, present):
    children, before = _set_status(set_name)
    if present:
        if (before["exists"] and
                (before["type"] != "hash:net" or before["family"] != "inet")):
            children.extend(_normalize_rules(set_name=set_name, present=False))
            child, stdout, stderr = _run_child([IPSET, "destroy", set_name])
            children.append(child)
            if not child["ok"]:
                raise BrokerError(
                    "cannot replace incompatible ipset", code="normalize_failed")
        children.append(_replace_set(set_name, cidrs))
        children.extend(_normalize_rules(set_name=set_name, present=True))
    else:
        children.extend(_normalize_rules(set_name=set_name, present=False))
        child, stdout, stderr = _run_child([IPSET, "destroy", set_name])
        children.append(child)
        if child["returncode"] == 1 and _expected_absence(stderr, "ipset"):
            child["ok"] = True
        elif not child["ok"]:
            raise BrokerError("cannot remove ipset", code="normalize_failed")
    status_children, observed = _set_status(set_name)
    children.extend(status_children)
    expected_forward = 1 if observed["forwarding_required"] and present else 0
    ok = (observed["exists"] is present and
          observed["input_count"] == (1 if present else 0) and
          observed["forward_count"] == expected_forward)
    if present:
        ok = (ok and observed["type"] == "hash:net" and
              observed["family"] == "inet" and
              observed["count"] == len(cidrs) and
              observed["digest"] == network_digest(cidrs))
    return children, observed, ok


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
        if built["operation"] == "ip.status":
            child, observed = _ip_status(built["source"])
            children.append(child)
            result = {"ok": True, "observed": observed}
        elif built["operation"] == "ip.normalize":
            children, observed, ok = _normalize_ip(
                built["source"], built["present"])
            result = {"ok": ok, "observed": observed}
        elif built["operation"] == "set.status":
            children, observed = _set_status(built["set_name"])
            result = {"ok": True, "observed": observed}
        elif built["operation"] in ("set.normalize", "permanent.normalize"):
            children, observed, ok = _normalize_set(
                built["set_name"], built["cidrs"], built["present"])
            result = {"ok": ok, "observed": observed}
        elif built["operation"] == "geolocated.normalize":
            ip_children, ip_observed, ip_ok = _normalize_ip(
                built["source"], built["temporary_present"])
            set_children, set_observed, set_ok = _normalize_set(
                built["set_name"], built["cidrs"], True)
            children = ip_children + set_children
            observed = {"ip": ip_observed, "permanent": set_observed}
            result = {"ok": bool(ip_ok and set_ok), "observed": observed}
        elif built["operation"] == "permanent.add":
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
        if built["operation"] not in (
                "ip.status", "ip.normalize", "set.status", "set.normalize",
                "permanent.normalize", "geolocated.normalize"):
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
            elif built["operation"] in (
                    "set.rule_ensure", "permanent.rule_ensure") and \
                    child["returncode"] == 1:
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
        else:
            ok = result["ok"]
            child = children[-1]
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
        raise BrokerError(
            "target timed out" if err.timeout else "target execution failed",
            code="broker_target_timeout" if err.timeout
            else "broker_target_failure") from err
    except subprocess.TimeoutExpired as err:
        _receipt("result", operation_id, context, built, children=children,
                 target_pid=0, target_start_ticks=0, returncode=-1, ok=False, error="timeout")
        raise BrokerError("target timed out", code="broker_target_timeout") from err
    except BrokerError as err:
        _receipt("result", operation_id, context, built, children=children,
                 target_pid=0, target_start_ticks=0, returncode=-1, ok=False,
                 error=err.code)
        raise
    except (OSError, subprocess.SubprocessError) as err:
        _receipt("result", operation_id, context, built, children=children,
                 target_pid=0, target_start_ticks=0, returncode=-1, ok=False,
                 error="target_failure")
        raise BrokerError(
            "target execution failed", code="broker_target_failure") from err


def execute_status(request):
    """Return broker readiness with a root-authored, non-mutating proof pair."""
    operation_id = uuid.uuid4().hex
    context = {
        "execution_id": operation_id, "job_id": operation_id,
        "function": READINESS_FUNCTION,
    }
    built = build_operation(request, function=READINESS_FUNCTION)
    begin = _receipt("begin", operation_id, context, built, children=[])
    started = time.monotonic()
    try:
        result = broker_status()
        _receipt(
            "result", operation_id, context, built, children=[],
            target_pid=os.getpid(),
            target_start_ticks=begin["broker_start_ticks"], returncode=0,
            duration_ms=int((time.monotonic() - started) * 1000), ok=True)
        return result
    except BrokerError as err:
        _receipt(
            "result", operation_id, context, built, children=[],
            target_pid=os.getpid(),
            target_start_ticks=begin["broker_start_ticks"], returncode=1,
            duration_ms=int((time.monotonic() - started) * 1000), ok=False,
            error=err.code)
        raise


def _verify_caller():
    if os.geteuid() != 0:
        raise BrokerError("broker must run as root", code="broker_caller_invalid")
    raw_uid = os.environ.get("SUDO_UID", "")
    if not raw_uid.isdigit() or int(raw_uid) <= 0:
        raise BrokerError(
            "SUDO_UID is missing or invalid", code="broker_caller_invalid")
    try:
        expected = pwd.getpwnam("ec2-user").pw_uid
    except KeyError as err:
        raise BrokerError(
            "application account is missing", code="broker_caller_invalid") from err
    if int(raw_uid) != expected:
        raise BrokerError(
            "caller is not the application account", code="broker_caller_invalid")
    info = os.stat(BROKER_PATH, follow_symlinks=False)
    if (info.st_uid != 0 or info.st_gid != 0 or info.st_mode & 0o022 or
            not stat.S_ISREG(info.st_mode)):
        raise BrokerError(
            "installed broker metadata is unsafe",
            code="broker_installation_unsafe")
    broker_status()


def broker_status(lifecycle=None):
    """Read protected enrollment/assets only; never touch locks or kernel state."""
    from mojo.deploy.firewall_deploy import Lifecycle
    try:
        state = (lifecycle or Lifecycle()).check()
    except (OSError, ValueError) as err:
        raise BrokerError("firewall installation is invalid",
                          code="broker_installation_unsafe") from err
    if state.get("status") != "ready":
        raise BrokerError("firewall broker is not enrolled and ready",
                          code="broker_not_ready")
    return {"ok": True, "schema": "mojo.firewall.broker", "version": 1,
            "permanent_set_name": state["permanent_set_name"]}


def _virtual_address_space_bytes(path="/proc/self/statm"):
    """Read this process's post-import virtual address-space footprint."""
    descriptor = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        payload = os.read(descriptor, MAX_STATM_BYTES + 1)
        if not payload or len(payload) > MAX_STATM_BYTES:
            raise ValueError("statm size is invalid")
        pages = int(payload.split(None, 1)[0])
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages <= 0 or page_size <= 0:
            raise ValueError("statm values are invalid")
        return pages * page_size
    except (OSError, ValueError, IndexError, OverflowError) as err:
        raise BrokerError(
            "cannot establish broker memory baseline",
            code="broker_resource_limit_unavailable") from err
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _install_address_space_limit(
        baseline_bytes=None, *, get_limit=resource.getrlimit,
        set_limit=resource.setrlimit):
    """Install reviewed post-import growth headroom under a hard ceiling."""
    if baseline_bytes is None:
        baseline_bytes = _virtual_address_space_bytes()
    if (isinstance(baseline_bytes, bool) or
            not isinstance(baseline_bytes, int) or baseline_bytes <= 0):
        raise BrokerError(
            "broker memory baseline is invalid",
            code="broker_resource_limit_unavailable")
    target = baseline_bytes + ADDRESS_SPACE_GROWTH_BYTES
    if target > MAX_ADDRESS_SPACE_BYTES:
        raise BrokerError(
            "broker memory headroom exceeds the absolute ceiling",
            code="broker_resource_limit_unavailable")
    try:
        unused_soft, hard = get_limit(resource.RLIMIT_AS)
        if hard != resource.RLIM_INFINITY and hard < target:
            raise BrokerError(
                "existing address-space limit cannot provide safe headroom",
                code="broker_resource_limit_unavailable")
        set_limit(resource.RLIMIT_AS, (target, target))
    except BrokerError:
        raise
    except (OSError, TypeError, ValueError, OverflowError) as err:
        raise BrokerError(
            "cannot install broker address-space limit",
            code="broker_resource_limit_unavailable") from err
    return target


def _write_prebuilt_response(response):
    """Use prebuilt response bytes when normal JSON may lack heap headroom."""
    try:
        os.write(1, response)
    except OSError:
        pass


def render_sudoers(user="ec2-user"):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", user):
        raise BrokerError("sudoers user is invalid")
    # An empty quoted argument is sudoers' exact no-arguments grammar.
    return f"{user} ALL=(root) NOPASSWD: {BROKER_PATH} \"\"\n"


def _acquire_host_lock():
    path = "/run/lock/mojo-firewall-broker.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    info = os.fstat(descriptor)
    if (info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or
            info.st_mode & 0o077):
        os.close(descriptor)
        raise BrokerError(
            "host lock metadata is unsafe", code="broker_host_lock_unsafe")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as err:
        os.close(descriptor)
        raise BrokerError("host firewall reconciliation is busy",
                          code="host_busy") from err
    return descriptor


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        print("firewall broker accepts no arguments", file=sys.stderr)
        return 2
    descriptor = None
    try:
        _verify_caller()
        _install_address_space_limit()
        payload = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        request = parse_request(payload)
        if request["operation"] == "broker.status":
            print(json.dumps(execute_status(request), sort_keys=True, separators=(",", ":")))
            return 0
        descriptor = _acquire_host_lock()
        print(json.dumps(execute(request), sort_keys=True, separators=(",", ":")))
        return 0
    except BrokerError as err:
        if err.code == "broker_resource_limit_unavailable":
            _write_prebuilt_response(_RESOURCE_LIMIT_UNAVAILABLE_RESPONSE)
            return 1
        print(json.dumps({
            "ok": False,
            "error": {"code": err.code, "message": str(err)[:256]},
        }, sort_keys=True, separators=(",", ":")))
        return 1
    except (OSError, ValueError):
        print(json.dumps({
            "ok": False,
            "error": {"code": "broker_failure", "message": "broker failed"},
        }, sort_keys=True, separators=(",", ":")))
        return 1
    except MemoryError:
        _write_prebuilt_response(_RESOURCE_EXHAUSTED_RESPONSE)
        return 1
    finally:
        if descriptor is not None:
            os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
