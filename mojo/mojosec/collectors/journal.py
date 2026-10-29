"""Bounded journald collector with a durable cursor owned by the store."""

import json
import os
import re
import selectors
import subprocess
import time

from ..detectors import detect_journal
from ..attribution import AttributionResolver


MAX_STDERR_BYTES = 4096
# Filter at the source: journalctl ORs the groups separated by "+" and ANDs the
# terms inside a group, so only the producers the detectors read are decoded —
# sshd and sshd-session (logins), sudo (commands and failures), the (systemd)
# PAM helper (user session opens), PID 1 (unit failures), the kernel (OOM kills)
# and the two Audit session records the SSH attribution map is built from.
JOURNAL_MATCHES = (
    "_COMM=sshd", "+", "_COMM=sshd-session", "+", "_COMM=sudo", "+",
    "_COMM=(systemd)", "+", "_PID=1", "+", "_TRANSPORT=kernel", "+",
    "_TRANSPORT=audit", "_AUDIT_TYPE_NAME=USER_START", "+",
    "_TRANSPORT=audit", "_AUDIT_TYPE_NAME=USER_LOGIN",
)
# Every field detect_journal, trusted_journal_source, ssh_session and
# audit_context read. __CURSOR, __REALTIME_TIMESTAMP, __MONOTONIC_TIMESTAMP and
# _BOOT_ID are always emitted by journalctl regardless of this list.
JOURNAL_FIELDS = (
    "MESSAGE,MESSAGE_ID,PRIORITY,SYSLOG_IDENTIFIER,UNIT,_TRANSPORT,_AUDIT_TYPE,"
    "_AUDIT_TYPE_NAME,_AUDIT_SESSION,_AUDIT_LOGINUID,_UID,_PID,_COMM,_EXE,"
    "_SYSTEMD_UNIT,_SYSTEMD_CGROUP,_SELINUX_CONTEXT,_TTY"
)
_CURSOR_BYTES = re.compile(rb'"__CURSOR"\s*:\s*"([A-Za-z0-9=;:_+.-]{1,4096})"')


def _reject_constant(value):
    raise ValueError(f"non-finite JSON number: {value}")


def _strict_record(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate journal field: {key}")
        result[key] = value
    return result


class JournalCollector:
    name = "journal"

    def __init__(self, config):
        self.config = config

    def poll(self, cursor=None, ssh_sessions=None, who_sessions=None):
        command = ["/usr/bin/journalctl", "--output=json", "--no-pager",
                   f"--output-fields={JOURNAL_FIELDS}"]
        if cursor:
            command.append(f"--after-cursor={cursor}")
        else:
            command.append(f"--since=-{self.config['lookback_seconds']} seconds")
        command.extend(JOURNAL_MATCHES)
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, bufsize=0,
        )
        observations = []
        next_cursor = cursor
        malformed = 0
        records = 0
        bytes_read = 0
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        limited = False
        byte_limited = False
        parsed_records = []
        deadline = time.monotonic() + self.config["timeout_seconds"]

        def process_line(line):
            nonlocal next_cursor, malformed, records
            records += 1
            raw_cursor = _CURSOR_BYTES.search(line)
            if len(line) > self.config["max_record_bytes"]:
                malformed += 1
                if raw_cursor:
                    next_cursor = raw_cursor.group(1).decode("ascii", errors="strict")
                return
            try:
                record = json.loads(
                    line.decode("utf-8"), object_pairs_hook=_strict_record,
                    parse_constant=_reject_constant,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                malformed += 1
                if raw_cursor:
                    next_cursor = raw_cursor.group(1).decode("ascii", errors="strict")
                return
            if not isinstance(record, dict):
                malformed += 1
                return
            record_cursor = record.get("__CURSOR")
            if not isinstance(record_cursor, str) or not record_cursor or len(record_cursor) > 4096:
                malformed += 1
                return
            next_cursor = record_cursor
            parsed_records.append(record)

        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        try:
            while selector.get_map() and not limited:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    process.wait()
                    raise RuntimeError("journalctl timed out")
                ready = selector.select(min(remaining, 0.25))
                if not ready:
                    continue
                for key, mask in ready:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == "stderr":
                        available = MAX_STDERR_BYTES - len(stderr_buffer)
                        if available > 0:
                            stderr_buffer.extend(chunk[:available])
                        continue
                    available = self.config["max_bytes_per_poll"] - bytes_read
                    if available <= 0:
                        limited = True
                        byte_limited = True
                        break
                    accepted = chunk[:available]
                    stdout_buffer.extend(accepted)
                    bytes_read += len(accepted)
                    if len(accepted) < len(chunk):
                        limited = True
                        byte_limited = True
                    while b"\n" in stdout_buffer and records < self.config["max_records"]:
                        line, _, remainder = stdout_buffer.partition(b"\n")
                        stdout_buffer = bytearray(remainder)
                        process_line(line)
                    if records >= self.config["max_records"]:
                        limited = True
                    if limited:
                        break
            if limited:
                if byte_limited and len(stdout_buffer) > self.config["max_record_bytes"]:
                    malformed += 1
                    match = _CURSOR_BYTES.search(stdout_buffer)
                    if match:
                        next_cursor = match.group(1).decode("ascii", errors="strict")
                process.terminate()
            else:
                if stdout_buffer:
                    process_line(bytes(stdout_buffer))
            try:
                returncode = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as err:
                process.kill()
                process.wait()
                raise RuntimeError("journalctl timed out") from err
        finally:
            selector.close()
            if process.poll() is None:
                process.kill()
                process.wait()
        if not limited and returncode:
            error = stderr_buffer.decode("utf-8", errors="replace").strip()[:512]
            raise RuntimeError(error or f"journalctl exited {returncode}")
        resolver = AttributionResolver(ssh_sessions, who_sessions=who_sessions)
        sessions = resolver.overlay(parsed_records)
        for record in parsed_records:
            try:
                detected = detect_journal(record, resolver)
            except Exception:
                malformed += 1
                continue
            if detected:
                observations.append(detected)
        return {
            "observations": observations, "cursor": next_cursor,
            "malformed": malformed, "ssh_sessions": sessions,
        }
