"""Tail newline-delimited structured nginx security logs safely."""

import hashlib
import json
import os
import stat

from ..detectors import detect_nginx


_CURSOR_ANCHOR_BYTES = 256
# Four nginx-default 8 KiB client buffers (request target, host, referrer, UA),
# worst-case six-byte JSON escaping, and a bounded envelope fit below 256 KiB.
MAX_STRUCTURED_LINE_BYTES = 256 * 1024


def _anchor(descriptor, offset):
    start = max(0, offset - _CURSOR_ANCHOR_BYTES)
    payload = os.pread(descriptor, offset - start, start)
    return hashlib.sha256(payload).hexdigest()


def _reject_constant(value):
    raise ValueError(f"non-finite JSON number: {value}")


def _strict_record(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate nginx field: {key}")
        result[key] = value
    return result


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
                saved_anchor = cursor.get("anchor_sha256")
                if offset and saved_anchor:
                    if (not isinstance(saved_anchor, str) or len(saved_anchor) != 64 or
                            _anchor(descriptor, offset) != saved_anchor):
                        offset = 0
            elif current.st_size > self.config["max_bytes_per_poll"]:
                offset = current.st_size - self.config["max_bytes_per_poll"]
            os.lseek(descriptor, offset, os.SEEK_SET)
            data = os.read(descriptor, self.config["max_bytes_per_poll"])
            # copytruncate preserves the inode. Recheck the committed anchor
            # after the read so a truncate racing this poll cannot make us
            # continue at an offset in unrelated, newly-written content.
            if (offset and cursor and cursor.get("anchor_sha256") and
                    _anchor(descriptor, offset) != cursor["anchor_sha256"]):
                offset = 0
                os.lseek(descriptor, 0, os.SEEK_SET)
                data = os.read(descriptor, self.config["max_bytes_per_poll"])
            end = offset + len(data)
            anchor_end = end
            if (data and not data.endswith(b"\n") and
                    len(data) < self.config["max_bytes_per_poll"]):
                newline = data.rfind(b"\n")
                anchor_end = offset + (newline + 1 if newline >= 0 else 0)
            next_anchor = _anchor(descriptor, anchor_end)
        finally:
            os.close(descriptor)

        malformed = 0
        observations = []
        lines = data.splitlines()
        if offset and data and not data.startswith(b"{"):
            lines = lines[1:]
            malformed += 1
        if data and not data.endswith(b"\n"):
            last = lines.pop() if lines else b""
            if len(data) >= self.config["max_bytes_per_poll"]:
                malformed += 1
            else:
                end -= len(last)
        for line in lines:
            if len(line) > min(self.config["max_line_bytes"], MAX_STRUCTURED_LINE_BYTES):
                malformed += 1
                continue
            try:
                record = json.loads(
                    line.decode("utf-8"), object_pairs_hook=_strict_record,
                    parse_constant=_reject_constant,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                malformed += 1
                continue
            if not isinstance(record, dict):
                malformed += 1
                continue
            try:
                detected = detect_nginx(record)
            except Exception:
                malformed += 1
                continue
            if detected:
                observations.append(detected)
        next_cursor = {
            "device": current.st_dev, "inode": current.st_ino, "offset": end,
            "anchor_sha256": next_anchor,
        }
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
