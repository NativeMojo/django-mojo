"""Strict SSH-session attribution for journal evidence."""

import datetime
import re
import subprocess
import time

from .events import valid_ip


WHO_FRESH_SECONDS = 5 * 60
_BOOT_ID = re.compile(r"^[a-f0-9]{32}$")
_USER = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")
_TTY = re.compile(r"^[A-Za-z0-9_.\-/]{1,96}$")
_SSH_ACCEPTED = re.compile(
    r"Accepted (?P<method>\S+) for (?P<user>\S+) from (?P<ip>\S+) port \d+", re.I)
_MESSAGE_TTY = re.compile(r"(?:^|;)\s*TTY=(?P<tty>[^ ;]+)")
_WHO_LINE = re.compile(
    r"^(?P<user>\S+)\s+(?P<tty>\S+)\s+(?P<date>\d{4}-\d{2}-\d{2})\s+"
    r"(?P<time>\d{2}:\d{2})(?:\s+(?:\([^)]*\)|\S+))?\s*$")


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


def ssh_session(record):
    match = _SSH_ACCEPTED.search(str(record.get("MESSAGE") or ""))
    context = audit_context(record)
    if (not match or not context["boot_id"] or context["audit_session"] is None or
            not valid_ip(match.group("ip")) or not canonical_user(match.group("user"))):
        return None
    return {
        "boot_id": context["boot_id"],
        "audit_session": context["audit_session"],
        "actor": canonical_user(match.group("user")),
        "tty": context["tty"],
        "source_ip": valid_ip(match.group("ip")),
        "observed_at": record_time(record) or time.time(),
    }


def load_who_sessions(now=None):
    """Return strict `who` candidates; failures merely disable fallback."""
    try:
        done = subprocess.run(
            ["/usr/bin/who", "--ips", "--time-format=iso"], capture_output=True,
            text=True, timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode:
        return []
    local_tz = datetime.datetime.now().astimezone().tzinfo
    rows = []
    for line in done.stdout.splitlines()[:4096]:
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


class AttributionResolver:
    def __init__(self, persisted=None, who_sessions=None):
        self.sessions = {}
        for row in persisted or ():
            key = (row.get("boot_id"), row.get("audit_session"))
            if key[0] and key[1] is not None:
                self.sessions[key] = dict(row)
        self.who_sessions = who_sessions
        self.new_sessions = []

    def overlay(self, records):
        for record in records:
            found = ssh_session(record)
            if found is None:
                continue
            self.sessions[(found["boot_id"], found["audit_session"])] = found
            self.new_sessions.append(found)
        return self.new_sessions

    def resolve(self, record, actor, tty):
        actor = canonical_user(actor)
        tty = canonical_tty(tty)
        context = audit_context(record)
        key = (context["boot_id"], context["audit_session"])
        session = self.sessions.get(key) if key[0] and key[1] is not None else None
        if (session and session.get("actor") == actor and
                (not tty or not session.get("tty") or session.get("tty") == tty)):
            return session["source_ip"], "audit_session", context

        if not actor or not tty or record_time(record) is None:
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
