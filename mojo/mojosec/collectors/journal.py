"""Bounded journald collector with a durable cursor owned by the store."""

import json
import os
import re
import selectors
import subprocess
import time

from ..detectors import detect_journal
from ..attribution import AttributionResolver
from ..lineage import CompoundAssembler, firewall_receipt, walk_parents


MAX_STDERR_BYTES = 4096
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

    def poll(self, cursor=None, ssh_sessions=None, who_sessions=None,
             audit_fragments=None):
        command = ["/usr/bin/journalctl", "--output=json", "--no-pager"]
        if cursor:
            command.append(f"--after-cursor={cursor}")
        else:
            command.append(f"--since=-{self.config['lookback_seconds']} seconds")
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
        lineage = CompoundAssembler(audit_fragments).ingest(parsed_records)
        process_nodes = []
        for node in lineage["complete"]:
            if node.get("pid"):
                parents = walk_parents(node["pid"])
                node["live_ancestors"] = parents["nodes"]
                node["ambiguous"] = bool(node.get("ambiguous") or parents["ambiguous"])
                live = parents["nodes"][0] if parents["nodes"] else None
                if live is not None:
                    node["start_ticks"] = live["start_ticks"]
                    node["unit"] = live.get("unit", "")
                    node["cgroup"] = live.get("cgroup", "")
                    node["namespaces"] = live.get("namespaces", {})
                    node["selinux"] = node.get("selinux") or live.get("selinux", "")
                    node["pinned"] = bool(
                        os.path.basename(node.get("exe", "")).startswith("python3") and
                        any(str(part).endswith("/bin/jobs.py") or part == "bin/jobs.py"
                            for part in live.get("cmdline", [])) and
                        "engine" in live.get("cmdline", []) and
                        "foreground" in live.get("cmdline", []))
            process_nodes.append(node)
        receipts = [found for found in
                    (firewall_receipt(record) for record in parsed_records) if found]
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
            "audit_fragments": lineage["fragments"],
            "process_nodes": process_nodes, "firewall_receipts": receipts,
        }
