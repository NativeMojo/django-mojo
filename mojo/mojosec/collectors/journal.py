"""Bounded journald collector with a durable cursor owned by the store."""

import json
import subprocess

from ..detectors import detect_journal


class JournalCollector:
    name = "journal"

    def __init__(self, config):
        self.config = config

    def poll(self, cursor=None):
        command = ["/usr/bin/journalctl", "--output=json", "--no-pager",
                   f"--lines={self.config['max_lines']}"]
        if cursor:
            command.append(f"--after-cursor={cursor}")
        else:
            command.append(f"--since=-{self.config['lookback_seconds']} seconds")
        done = subprocess.run(
            command, capture_output=True, text=True,
            timeout=self.config["timeout_seconds"], check=False,
        )
        if done.returncode:
            error = done.stderr.strip()[:512] or f"journalctl exited {done.returncode}"
            raise RuntimeError(error)
        observations = []
        next_cursor = cursor
        malformed = 0
        for line in done.stdout.splitlines()[:self.config["max_lines"]]:
            if len(line) > 256 * 1024:
                malformed += 1
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not isinstance(record, dict):
                malformed += 1
                continue
            if isinstance(record.get("__CURSOR"), str):
                next_cursor = record["__CURSOR"][:4096]
            detected = detect_journal(record)
            if detected:
                observations.append(detected)
        return {"observations": observations, "cursor": next_cursor, "malformed": malformed}
