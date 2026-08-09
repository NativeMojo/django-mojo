"""Polling filesystem-integrity collector for an explicit target profile."""

import fnmatch
import hashlib
import os
import stat

from ..events import observation
from ..expected_changes import load_manifest, annotation
from ..profiles import PRIVATE_TREES
from ..protocol import canonical_json


class FimCollector:
    name = "fim"

    def __init__(self, config, expected_changes_path=None, identity=None, tier="fim",
                 hash_filter=None):
        self.config = config
        self.expected_changes_path = expected_changes_path
        self.tier = tier
        self.hash_filter = hash_filter
        graph_digest = hashlib.sha256(
            canonical_json(config["targets"]).encode("utf-8")).hexdigest()
        self.identity = dict(identity or {})
        self.profile = self.identity.get("digest", graph_digest)
        self.baseline_key = ":".join(filter(None, (
            self.identity.get("name", "legacy"), self.profile, tier,
        )))

    def _excluded(self, target, path):
        for private in PRIVATE_TREES:
            try:
                if os.path.commonpath((path, private)) == private:
                    return True
            except ValueError:
                return True
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
            "ctime_ns": info.st_ctime_ns, "device": info.st_dev, "inode": info.st_ino,
        }
        return entry

    def _unchanged(self, previous, entry):
        fields = ("device", "inode", "size", "mtime_ns", "ctime_ns",
                  "mode", "uid", "gid")
        return previous and all(previous.get(field) == entry.get(field) for field in fields)

    def _pth_anomaly(self, path, material, root):
        if not path.endswith(".pth") or material is None:
            return ""
        try:
            lines = material.decode("utf-8").splitlines()
        except UnicodeError:
            return "pth_invalid_encoding"
        for line in lines[:1024]:
            value = line.strip()
            if not value or value.startswith("#"):
                continue
            if value.startswith(("import ", "import\t")):
                return "pth_executable"
            candidate = value if os.path.isabs(value) else os.path.join(os.path.dirname(path), value)
            candidate = os.path.normpath(candidate)
            try:
                if os.path.commonpath((candidate, root)) != root:
                    return "pth_external"
            except ValueError:
                return "pth_external"
        return ""

    def _entry_at(self, parent_fd, name, info, path, target, state, previous=None):
        entry = self._metadata(info)
        if stat.S_ISREG(info.st_mode):
            entry["kind"] = "file"
            should_hash = self.hash_filter(path) if self.hash_filter is not None else True
            if not should_hash:
                entry["hash_owned"] = True
            elif info.st_size <= self.config["max_file_bytes"]:
                if self._unchanged(previous, entry) and previous.get("sha256"):
                    entry["sha256"] = previous["sha256"]
                    if previous.get("anomaly"):
                        entry["anomaly"] = previous["anomaly"]
                    entry["digest_reused"] = True
                    return entry
                inode_key = (
                    info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                    info.st_ctime_ns, stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid,
                )
                cached = state["digests"].get(inode_key)
                if cached is not None:
                    entry.update(cached)
                    entry["digest_reused"] = True
                    return entry
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
                try:
                    opened = os.fstat(descriptor)
                    if opened.st_dev != info.st_dev or opened.st_ino != info.st_ino:
                        raise OSError(f"file changed while hashing: {path}")
                    digest = hashlib.sha256()
                    pth_material = bytearray() if path.endswith(".pth") else None
                    while True:
                        block = os.read(descriptor, 1024 * 1024)
                        if not block:
                            break
                        digest.update(block)
                        if pth_material is not None and len(pth_material) < 64 * 1024:
                            pth_material.extend(block[:64 * 1024 - len(pth_material)])
                    finished = os.fstat(descriptor)
                    if (finished.st_dev != info.st_dev or finished.st_ino != info.st_ino or
                            finished.st_size != info.st_size or finished.st_mtime_ns != info.st_mtime_ns):
                        raise OSError(f"file changed while hashing: {path}")
                    entry["sha256"] = digest.hexdigest()
                    anomaly = self._pth_anomaly(path, pth_material, target["path"])
                    if anomaly:
                        entry["anomaly"] = anomaly
                    state["digests"][inode_key] = {
                        key: entry[key] for key in ("sha256", "anomaly") if key in entry
                    }
                finally:
                    os.close(descriptor)
            else:
                entry["hash_skipped"] = True
        elif stat.S_ISDIR(info.st_mode):
            entry["kind"] = "directory"
        elif stat.S_ISLNK(info.st_mode):
            entry["kind"] = "symlink"
            link_target = os.readlink(name, dir_fd=parent_fd)
            entry["target_sha256"] = hashlib.sha256(link_target.encode("utf-8")).hexdigest()
            resolved = os.path.normpath(
                link_target if os.path.isabs(link_target)
                else os.path.join(os.path.dirname(path), link_target))
            try:
                contained = os.path.commonpath((resolved, target["path"])) == target["path"]
            except ValueError:
                contained = False
            if not contained:
                entry["anomaly"] = "symlink_escape"
            else:
                try:
                    os.stat(name, dir_fd=parent_fd, follow_symlinks=True)
                except OSError:
                    entry["anomaly"] = "symlink_dangling"
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

    def _scan_node(self, parent_fd, name, path, target, snapshot, state, depth,
                   previous, is_root=False):
        if len(snapshot) >= self.config["max_entries"]:
            state["complete"] = False
            return
        if not is_root and self._excluded(target, path):
            return
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        entry = self._entry_at(
            parent_fd, name, info, path, target, state, previous.get(path))
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
                                snapshot, state, depth + 1, previous,
                            )
                        except OSError:
                            state["complete"] = False
            except OSError:
                state["complete"] = False
        finally:
            os.close(descriptor)

    def _scan_target(self, target, snapshot, state, previous):
        path = os.path.normpath(target["path"])
        if target.get("optional") and not os.path.lexists(path):
            return
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
            self._scan_node(
                parent_fd, name, path, target, snapshot, state, 0, previous, is_root=True)
        except OSError:
            state["complete"] = False
        finally:
            os.close(parent_fd)

    def scan(self, previous=None):
        snapshot = {}
        state = {"complete": True, "digests": {}}
        previous = previous or {}
        if not self._descriptor_walk_supported():
            return {
                "profile": self.profile, "snapshot": snapshot,
                "complete": False, "descriptor_safe": False,
            }
        for target in self.config["targets"]:
            self._scan_target(target, snapshot, state, previous)
            if not state["complete"] and len(snapshot) >= self.config["max_entries"]:
                break
        return {
            "profile": self.profile, "baseline_key": self.baseline_key,
            "tier": self.tier, "snapshot": snapshot,
            "complete": state["complete"], "descriptor_safe": True,
            "entries": len(snapshot),
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
            for field in ("mode", "uid", "gid", "size", "sha256",
                          "target_sha256", "anomaly"):
                if field in entry:
                    attributes[field] = entry[field]
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
                attributes={"profile": self.profile, "tier": self.tier,
                            "entries": len(current)},
                fingerprint_values=(self.profile, self.tier), aggregate=False,
                recommendation="review",
            ))
        return observations
