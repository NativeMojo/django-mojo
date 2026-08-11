"""Strict SSH-session attribution for journal evidence."""

import datetime
import os
import re
import selectors
import subprocess
import time

from .events import valid_ip


WHO_FRESH_SECONDS = 5 * 60
WHO_MAX_BYTES = 128 * 1024
WHO_MAX_LINES = 4096
WHO_TIMEOUT_SECONDS = 2
_BOOT_ID = re.compile(r"^[a-f0-9]{32}$")
_USER = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")
_TTY = re.compile(r"^[A-Za-z0-9_.\-/]{1,96}$")
_MESSAGE_TTY = re.compile(r"(?:^|;)\s*TTY=(?P<tty>[^ ;]+)")
_WHO_LINE = re.compile(
    r"^(?P<user>\S+)\s+(?P<tty>\S+)\s+(?P<date>\d{4}-\d{2}-\d{2})\s+"
    r"(?P<time>\d{2}:\d{2})(?:\s+(?:\([^)]*\)|\S+))?\s*$")
_AUDIT_TYPE_MAP = {"USER_START": "1105", "USER_LOGIN": "1112"}
_SSH_AUDIT_EXES = {
    "/usr/sbin/sshd", "/usr/bin/sshd",
    "/usr/libexec/openssh/sshd-session",
}
_SUDO_EXES = {"/usr/bin/sudo", "/usr/sbin/sudo"}
_SUDO_UNIT = re.compile(r"^session-[0-9]+\.scope$")


def canonical_user(value):
    value = str(value or "")
    return value if _USER.fullmatch(value) else ""


def canonical_tty(value):
    value = str(value or "").strip()
    if value.startswith("/dev/"):
        value = value[5:]
    if not _TTY.fullmatch(value) or ".." in value:
        return ""
    return value


def canonical_uid(value):
    value = str(value or "")
    if not re.fullmatch(r"0|[1-9][0-9]{0,9}", value):
        return None
    uid = int(value)
    return uid if 0 <= uid < 4294967295 else None


def audit_context(record):
    boot_id = str(record.get("_BOOT_ID") or record.get("BOOT_ID") or "").replace("-", "").lower()
    if not _BOOT_ID.fullmatch(boot_id):
        boot_id = ""
    raw_session = record.get("_AUDIT_SESSION", record.get("AUDIT_SESSION"))
    try:
        if isinstance(raw_session, bool):
            raise ValueError
        audit_session = int(raw_session)
    except (TypeError, ValueError):
        audit_session = None
    if audit_session is not None and not 0 <= audit_session < 4294967295:
        audit_session = None
    tty = canonical_tty(record.get("_TTY") or record.get("TTY"))
    if not tty:
        match = _MESSAGE_TTY.search(str(record.get("MESSAGE") or ""))
        tty = canonical_tty(match.group("tty")) if match else ""
    return {"boot_id": boot_id, "audit_session": audit_session, "tty": tty}


def record_time(record):
    try:
        return int(record.get("__REALTIME_TIMESTAMP")) / 1000000
    except (TypeError, ValueError, OSError):
        return None


def _audit_value(message, name):
    match = re.search(
        rf"(?:^|\s){re.escape(name)}=(?:\"([^\"]*)\"|'([^']*)'|([^\s']+))",
        message)
    if not match:
        return ""
    return next((value for value in match.groups() if value is not None), "")


def trusted_journal_source(record, service):
    """Require kernel/journald-owned identity fields for auth text parsing."""
    exe = str(record.get("_EXE") or "")
    comm = str(record.get("_COMM") or "")
    unit = str(record.get("_SYSTEMD_UNIT") or "")
    uid = canonical_uid(record.get("_UID"))
    if service == "sshd":
        if uid != 0:
            return False
        if comm == "sshd-session":
            return bool(
                exe == "/usr/libexec/openssh/sshd-session" and
                unit == "sshd.service")
        if comm != "sshd" or unit not in ("", "sshd.service"):
            return False
        # Some legacy journal exports omit _EXE. Compatibility is restricted
        # to the otherwise exact root-owned legacy tuple.
        return exe in ("", "/usr/sbin/sshd", "/usr/bin/sshd")
    if service == "sudo":
        return bool(
            uid is not None and comm == "sudo" and exe in _SUDO_EXES and
            (not unit or _SUDO_UNIT.fullmatch(unit)))
    return False


def ssh_session(record):
    if str(record.get("_TRANSPORT") or "") != "audit":
        return None
    type_names = {
        str(record.get(key)).upper() for key in ("_AUDIT_TYPE_NAME", "AUDIT_TYPE_NAME")
        if record.get(key) not in (None, "")
    }
    type_numbers = {
        str(record.get(key)) for key in ("_AUDIT_TYPE", "AUDIT_TYPE")
        if record.get(key) not in (None, "")
    }
    if len(type_names) > 1 or len(type_numbers) > 1:
        return None
    audit_type = next(iter(type_names), "")
    audit_number = next(iter(type_numbers), "")
    if not audit_type and not audit_number:
        return None
    if audit_type and audit_type not in _AUDIT_TYPE_MAP:
        return None
    if audit_number and audit_number not in _AUDIT_TYPE_MAP.values():
        return None
    if audit_type and audit_number and _AUDIT_TYPE_MAP[audit_type] != audit_number:
        return None
    if str(record.get("_UID", "")) != "0":
        return None
    message = str(record.get("MESSAGE") or "")
    if (_audit_value(message, "res").lower() != "success" or
            _audit_value(message, "terminal").lower() != "ssh" or
            _audit_value(message, "exe") not in _SSH_AUDIT_EXES):
        return None
    context = audit_context(record)
    actor = canonical_user(_audit_value(message, "acct"))
    source_ip = valid_ip(_audit_value(message, "addr"))
    if (not context["boot_id"] or context["audit_session"] is None or
            not source_ip or not actor):
        return None
    return {
        "boot_id": context["boot_id"],
        "audit_session": context["audit_session"],
        "actor": actor,
        "tty": context["tty"],
        "source_ip": source_ip,
        "observed_at": record_time(record) or time.time(),
        "ambiguous": False,
    }


def _who_output(command):
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, env={
                "LC_ALL": "C", "LANG": "C", "PATH": "/usr/bin:/bin",
            }, bufsize=0)
    except OSError:
        return None
    selector = selectors.DefaultSelector()
    output = bytearray()
    deadline = time.monotonic() + WHO_TIMEOUT_SECONDS
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.wait()
                return None
            ready = selector.select(min(remaining, 0.1))
            if not ready:
                continue
            chunk = os.read(process.stdout.fileno(), 8192)
            if not chunk:
                selector.unregister(process.stdout)
                continue
            if (len(output) + len(chunk) > WHO_MAX_BYTES or
                    output.count(b"\n") + chunk.count(b"\n") > WHO_MAX_LINES):
                process.kill()
                process.wait()
                return None
            output.extend(chunk)
        try:
            returncode = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            return None
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait()
    return bytes(output) if returncode == 0 else None


def load_who_sessions(now=None, command=None):
    """Return strict `who` candidates; failures merely disable fallback."""
    payload = _who_output(command or ["/usr/bin/who"])
    if payload is None:
        return []
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return []
    local_tz = datetime.datetime.now().astimezone().tzinfo
    rows = []
    for line in text.splitlines():
        match = _WHO_LINE.fullmatch(line.strip())
        if not match:
            continue
        user = canonical_user(match.group("user"))
        tty = canonical_tty(match.group("tty"))
        source = line.rsplit(None, 1)[-1].strip("()")
        source_ip = valid_ip(source)
        try:
            started = datetime.datetime.fromisoformat(
                match.group("date") + "T" + match.group("time")).replace(
                    tzinfo=local_tz).timestamp()
        except ValueError:
            continue
        if user and tty and source_ip:
            rows.append({
                "actor": user, "tty": tty, "source_ip": source_ip,
                "observed_at": started,
            })
    return rows


def merge_session(existing, incoming):
    """Merge one audit identity; every conflict is a sticky tombstone."""
    incoming = dict(incoming)
    if existing is None:
        incoming["ambiguous"] = bool(incoming.get("ambiguous"))
        return incoming
    existing = dict(existing)
    observed_at = max(float(existing.get("observed_at", 0)),
                      float(incoming.get("observed_at", 0)))
    conflict = bool(existing.get("ambiguous") or incoming.get("ambiguous"))
    conflict = conflict or existing.get("actor") != incoming.get("actor")
    conflict = conflict or existing.get("source_ip") != incoming.get("source_ip")
    old_tty = existing.get("tty") or ""
    new_tty = incoming.get("tty") or ""
    conflict = conflict or bool(old_tty and new_tty and old_tty != new_tty)
    if conflict:
        return {
            "boot_id": incoming.get("boot_id") or existing.get("boot_id"),
            "audit_session": incoming.get("audit_session", existing.get("audit_session")),
            "actor": "", "tty": "", "source_ip": "",
            "observed_at": observed_at, "ambiguous": True,
        }
    existing["tty"] = old_tty or new_tty
    existing["observed_at"] = observed_at
    existing["ambiguous"] = False
    return existing


class AttributionResolver:
    def __init__(self, persisted=None, who_sessions=None):
        self.sessions = {}
        for row in persisted or ():
            key = (row.get("boot_id"), row.get("audit_session"))
            if key[0] and key[1] is not None:
                self.sessions[key] = merge_session(self.sessions.get(key), row)
        self.who_sessions = who_sessions
        self.new_sessions = []

    def overlay(self, records):
        for record in records:
            found = ssh_session(record)
            if found is None:
                continue
            key = (found["boot_id"], found["audit_session"])
            merged = merge_session(self.sessions.get(key), found)
            self.sessions[key] = merged
            self.new_sessions.append(merged)
        return self.new_sessions

    def resolve(self, record, actor, tty, allow_who=True):
        actor = canonical_user(actor)
        tty = canonical_tty(tty)
        context = audit_context(record)
        key = (context["boot_id"], context["audit_session"])
        if key[0] and key[1] is not None and key in self.sessions:
            session = self.sessions[key]
            if (not session.get("ambiguous") and session.get("actor") == actor and
                    (not tty or not session.get("tty") or session.get("tty") == tty)):
                return session["source_ip"], "audit_session", context
            # An exact audit identity is authoritative even when unusable.
            # Never let a looser TTY snapshot overwrite a conflict/mismatch.
            return "", "none", context

        if not allow_who or not actor or not tty or record_time(record) is None:
            return "", "none", context
        if self.who_sessions is None:
            self.who_sessions = load_who_sessions()
        observed = record_time(record)
        matches = [row for row in self.who_sessions
                   if row.get("actor") == actor and row.get("tty") == tty and
                   0 <= observed - row.get("observed_at", 0) <= WHO_FRESH_SECONDS]
        if len(matches) == 1:
            return matches[0]["source_ip"], "who", context
        return "", "none", context
