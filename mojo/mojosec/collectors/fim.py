"""Polling filesystem-integrity collector for an explicit target profile."""

import fnmatch
import hashlib
import os
import stat

from ..events import observation
from ..protocol import canonical_json


class FimCollector:
    name = "fim"

    def __init__(self, config):
        self.config = config
        self.profile = hashlib.sha256(
            canonical_json(config["targets"]).encode("utf-8")
        ).hexdigest()

    def _excluded(self, target, path):
        relative = os.path.relpath(path, target["path"])
        return any(fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(path, pattern)
                   for pattern in target.get("exclude", []))

    def _entry(self, path):
        info = os.lstat(path)
        entry = {
            "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid,
            "size": info.st_size, "mtime_ns": info.st_mtime_ns,
        }
        if stat.S_ISREG(info.st_mode):
            entry["kind"] = "file"
            if info.st_size <= self.config["max_file_bytes"]:
                flags = os.O_RDONLY
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(path, flags)
                try:
                    opened = os.fstat(descriptor)
                    if opened.st_dev != info.st_dev or opened.st_ino != info.st_ino:
                        raise OSError(f"file changed while hashing: {path}")
                    digest = hashlib.sha256()
                    while True:
                        block = os.read(descriptor, 1024 * 1024)
                        if not block:
                            break
                        digest.update(block)
                    finished = os.fstat(descriptor)
                    if (finished.st_dev != info.st_dev or finished.st_ino != info.st_ino or
                            finished.st_size != info.st_size or finished.st_mtime_ns != info.st_mtime_ns):
                        raise OSError(f"file changed while hashing: {path}")
                    entry["sha256"] = digest.hexdigest()
                finally:
                    os.close(descriptor)
            else:
                entry["hash_skipped"] = True
        elif stat.S_ISDIR(info.st_mode):
            entry["kind"] = "directory"
        elif stat.S_ISLNK(info.st_mode):
            entry["kind"] = "symlink"
            entry["target_sha256"] = hashlib.sha256(os.readlink(path).encode("utf-8")).hexdigest()
        else:
            entry["kind"] = "other"
        return entry

    def _scan_target(self, target, snapshot, state):
        root = target["path"]
        pending = [root]
        while pending:
            path = pending.pop()
            if self._excluded(target, path):
                continue
            if len(snapshot) >= self.config["max_entries"]:
                state["complete"] = False
                return
            try:
                entry = self._entry(path)
            except FileNotFoundError:
                continue
            snapshot[path] = entry
            if entry["kind"] != "directory" or (path != root and not target.get("recursive", False)):
                continue
            try:
                with os.scandir(path) as iterator:
                    children = []
                    remaining = self.config["max_entries"] - len(snapshot)
                    for child in iterator:
                        if len(children) >= remaining:
                            state["complete"] = False
                            break
                        children.append(child.path)
            except (FileNotFoundError, NotADirectoryError, PermissionError):
                state["complete"] = False
                continue
            pending.extend(children)

    def scan(self):
        snapshot = {}
        state = {"complete": True}
        for target in self.config["targets"]:
            self._scan_target(target, snapshot, state)
            if not state["complete"] and len(snapshot) >= self.config["max_entries"]:
                break
        return {"profile": self.profile, "snapshot": snapshot, "complete": state["complete"]}

    def diff(self, baseline, scan):
        observations = []
        current = scan["snapshot"]
        paths = set(current) | (set(baseline) if scan["complete"] else set())
        for path in sorted(paths):
            before = baseline.get(path)
            after = current.get(path)
            if before == after:
                continue
            if before is None:
                change = "created"
            elif after is None:
                change = "deleted"
            else:
                change = "modified"
            entry = after or before or {}
            observations.append(observation(
                "fim.change", "high", "Targeted filesystem integrity change",
                attributes={"path": path[:2048], "change": change, "kind": entry.get("kind", "unknown")},
                fingerprint_values=(path, change, canonical_json(after or {})),
                aggregate=False, recommendation="review",
            ))
        if not scan["complete"]:
            observations.append(observation(
                "fim.overflow", "critical", "Filesystem integrity scan was incomplete",
                attributes={"profile": self.profile, "entries": len(current)},
                fingerprint_values=(self.profile,), aggregate=True, recommendation="review",
            ))
        return observations
