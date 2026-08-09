"""Tail newline-delimited structured nginx security logs safely."""

import json
import os
import stat

from ..detectors import detect_nginx


class NginxCollector:
    name = "nginx"

    def __init__(self, config):
        self.config = config

    def _read_path(self, path, cursor):
        try:
            before = os.lstat(path)
        except FileNotFoundError:
            return [], cursor, 0
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError(f"refusing non-regular nginx log: {path}")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            current = os.fstat(descriptor)
            if current.st_dev != before.st_dev or current.st_ino != before.st_ino:
                raise RuntimeError(f"nginx log changed while opening: {path}")
            offset = 0
            if (cursor and cursor.get("device") == current.st_dev and
                    cursor.get("inode") == current.st_ino):
                saved_offset = max(0, int(cursor.get("offset", 0)))
                offset = saved_offset if saved_offset <= current.st_size else 0
            elif current.st_size > self.config["max_bytes_per_poll"]:
                offset = current.st_size - self.config["max_bytes_per_poll"]
            os.lseek(descriptor, offset, os.SEEK_SET)
            data = os.read(descriptor, self.config["max_bytes_per_poll"])
            end = offset + len(data)
        finally:
            os.close(descriptor)

        malformed = 0
        observations = []
        lines = data.splitlines()
        if offset and data and not data.startswith(b"{"):
            lines = lines[1:]
        if data and not data.endswith(b"\n"):
            last = lines.pop() if lines else b""
            end -= len(last)
        for line in lines:
            if len(line) > self.config["max_line_bytes"]:
                malformed += 1
                continue
            try:
                record = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                malformed += 1
                continue
            detected = detect_nginx(record)
            if detected:
                observations.append(detected)
        next_cursor = {"device": current.st_dev, "inode": current.st_ino, "offset": end}
        return observations, next_cursor, malformed

    def poll(self, cursors=None):
        cursors = dict(cursors or {})
        next_cursors = {}
        observations = []
        malformed = 0
        for path in self.config["paths"]:
            found, cursor, count = self._read_path(path, cursors.get(path))
            observations.extend(found)
            next_cursors[path] = cursor
            malformed += count
        return {"observations": observations, "cursor": next_cursors, "malformed": malformed}
