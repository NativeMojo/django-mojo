"""Polling filesystem-integrity collector for an explicit target profile."""

import fnmatch
import hashlib
import os
import stat

from ..events import observation
from ..expected_changes import load_manifest, annotation
from ..protocol import canonical_json


class FimCollector:
    name = "fim"

    def __init__(self, config, expected_changes_path=None):
        self.config = config
        self.expected_changes_path = expected_changes_path
        self.profile = hashlib.sha256(
            canonical_json(config["targets"]).encode("utf-8")
        ).hexdigest()

    def _excluded(self, target, path):
        relative = os.path.relpath(path, target["path"])
        return any(fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(path, pattern)
                   for pattern in target.get("exclude", []))

    def _descriptor_walk_supported(self):
        return bool(
            os.name == "posix" and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY") and
            os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd and
            os.readlink in os.supports_dir_fd
        )

    def _metadata(self, info):
        entry = {
            "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid,
            "size": info.st_size, "mtime_ns": info.st_mtime_ns,
        }
        return entry

    def _entry_at(self, parent_fd, name, info, path):
        entry = self._metadata(info)
        if stat.S_ISREG(info.st_mode):
            entry["kind"] = "file"
            if info.st_size <= self.config["max_file_bytes"]:
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
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
            target = os.readlink(name, dir_fd=parent_fd)
            entry["target_sha256"] = hashlib.sha256(target.encode("utf-8")).hexdigest()
        else:
            entry["kind"] = "other"
        return entry

    def _open_directory(self, path):
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in [part for part in os.path.normpath(path).split(os.sep) if part]:
                child = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = child
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _scan_node(self, parent_fd, name, path, target, snapshot, state, depth, is_root=False):
        if len(snapshot) >= self.config["max_entries"]:
            state["complete"] = False
            return
        if not is_root and self._excluded(target, path):
            return
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        entry = self._entry_at(parent_fd, name, info, path)
        snapshot[path] = entry
        if entry["kind"] != "directory":
            return
        if not is_root and not target.get("recursive", False):
            return
        if depth >= self.config["max_depth"]:
            state["complete"] = False
            return
        descriptor = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd,
        )
        try:
            opened = os.fstat(descriptor)
            if opened.st_dev != info.st_dev or opened.st_ino != info.st_ino:
                raise OSError(f"directory changed while scanning: {path}")
            try:
                with os.scandir(descriptor) as iterator:
                    for child in iterator:
                        if len(snapshot) >= self.config["max_entries"]:
                            state["complete"] = False
                            break
                        child_path = os.path.join(path, child.name)
                        try:
                            self._scan_node(
                                descriptor, child.name, child_path, target,
                                snapshot, state, depth + 1,
                            )
                        except OSError:
                            state["complete"] = False
            except OSError:
                state["complete"] = False
        finally:
            os.close(descriptor)

    def _scan_target(self, target, snapshot, state):
        path = os.path.normpath(target["path"])
        parent_path = os.path.dirname(path) or "/"
        name = os.path.basename(path)
        if path == "/":
            parent_path = "/"
            name = "."
        try:
            parent_fd = self._open_directory(parent_path)
        except OSError:
            state["complete"] = False
            return
        try:
            self._scan_node(parent_fd, name, path, target, snapshot, state, 0, is_root=True)
        except OSError:
            state["complete"] = False
        finally:
            os.close(parent_fd)

    def scan(self):
        snapshot = {}
        state = {"complete": True}
        if not self._descriptor_walk_supported():
            return {
                "profile": self.profile, "snapshot": snapshot,
                "complete": False, "descriptor_safe": False,
            }
        for target in self.config["targets"]:
            self._scan_target(target, snapshot, state)
            if not state["complete"] and len(snapshot) >= self.config["max_entries"]:
                break
        return {
            "profile": self.profile, "snapshot": snapshot,
            "complete": state["complete"], "descriptor_safe": True,
        }

    def diff(self, baseline, scan):
        observations = []
        expected = load_manifest(self.expected_changes_path)
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
            attributes = {
                "path": path[:2048], "change": change,
                "kind": entry.get("kind", "unknown"),
            }
            expected_annotation = annotation(expected, path, change, before, after)
            if expected_annotation:
                attributes["expected_change"] = expected_annotation
            observations.append(observation(
                "fim.change", "high", "Targeted filesystem integrity change",
                attributes=attributes,
                fingerprint_values=(path, change, canonical_json(after or {})),
                aggregate=False, recommendation="review",
            ))
        if not scan["complete"]:
            observations.append(observation(
                "fim.overflow", "critical", "Filesystem integrity scan was incomplete",
                attributes={"profile": self.profile, "entries": len(current)},
                fingerprint_values=(self.profile,), aggregate=False, recommendation="review",
            ))
        return observations
