"""Bounded RPM and system-interpreter integrity collection for AL2023."""

import json
import os
import re
import subprocess
import tempfile

from .fim import FimCollector


_NEVRA_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.:~-]{0,511}$")
_VERIFY_POSITIONS = (
    set(".S?"), set(".M?"), set(".5?"), set(".D?"), set(".L?"),
    set(".U?"), set(".G?"), set(".T?"), set(".P?"),
)
_FILE_MARKERS = set("cdglra")


class RpmError(RuntimeError):
    pass


def _contained(path, root):
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def parse_verify_output(payload, approved_roots):
    """Parse RPM's documented nine verification columns and file marker."""
    if not isinstance(payload, str):
        raise RpmError("RPM verify output must be text")
    result = {}
    for raw in payload.splitlines():
        line = raw.rstrip("\r")
        if not line:
            continue
        if line.startswith("missing"):
            status = "missing"
            remainder = line[7:].strip()
        else:
            if len(line) < 11:
                raise RpmError("RPM verify output is truncated")
            status = line[:9]
            if any(character not in _VERIFY_POSITIONS[index]
                   for index, character in enumerate(status)):
                raise RpmError("RPM verify output contains an unknown status marker")
            remainder = line[9:].strip()
        marker = ""
        pieces = remainder.split(None, 1)
        if len(pieces) == 2 and len(pieces[0]) == 1 and pieces[0] in _FILE_MARKERS:
            marker, path = pieces
        else:
            path = remainder
        if not path.startswith("/") or os.path.normpath(path) != path:
            raise RpmError("RPM verify output contains an invalid path")
        if not any(_contained(path, root) for root in approved_roots):
            continue
        if path in result:
            raise RpmError("RPM verify output repeats one logical path")
        result[path] = {"status": status, "marker": marker}
    return result


class RpmCollector:
    name = "rpm"

    def __init__(self, config, identity, expected_changes_path=None, runner=None):
        self.config = config
        self.identity = dict(identity)
        self.expected_changes_path = expected_changes_path
        self.runner = runner or self._run
        self.owners = {}
        self.owner_queries = 0
        self.site_roots = []
        self.baseline_key = ":".join((identity["name"], identity["digest"], "rpm"))

    def _run(self, argv, accepted=(0,)):
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "LANG": "C"}
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                done = subprocess.run(
                    argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                    env=env, timeout=self.config["timeout_seconds"], check=False)
            except (OSError, subprocess.TimeoutExpired) as err:
                raise RpmError(f"cannot execute bounded integrity command: {argv[0]}") from err
            stdout.flush()
            stderr.flush()
            if (stdout.tell() > self.config["max_output_bytes"] or
                    stderr.tell() > self.config["max_output_bytes"]):
                raise RpmError(f"bounded integrity command exceeded output limit: {argv[0]}")
            stdout.seek(0)
            stderr.seek(0)
            out = stdout.read().decode("utf-8", "strict")
            err = stderr.read().decode("utf-8", "strict")
        if done.returncode not in accepted:
            raise RpmError(
                f"integrity command failed ({done.returncode}): {argv[0]}: {err[:256]}")
        return done.returncode, out, err

    def discover_site_roots(self):
        script = (
            "import json,site,sys,sysconfig;"
            "p=set();"
            "p.update(x for x in sysconfig.get_paths().values() if isinstance(x,str));"
            "p.update(x for x in site.getsitepackages() if isinstance(x,str));"
            "p.update(x for x in sys.path if isinstance(x,str));"
            "print(json.dumps(sorted(p),separators=(',',':')))"
        )
        _, output, _ = self.runner([self.config["interpreter"], "-I", "-c", script])
        try:
            candidates = json.loads(output)
        except (TypeError, json.JSONDecodeError) as err:
            raise RpmError("system Python returned invalid site roots") from err
        if not isinstance(candidates, list) or len(candidates) > 128:
            raise RpmError("system Python returned an unbounded site-root set")
        roots = []
        prefixes = ("/usr/lib/", "/usr/lib64/", "/usr/local/lib/", "/usr/local/lib64/")
        for candidate in candidates:
            if not isinstance(candidate, str) or not os.path.isabs(candidate):
                continue
            path = os.path.realpath(candidate)
            if (not path.startswith(prefixes) or
                    not path.endswith(("/site-packages", "/dist-packages"))):
                continue
            if path not in roots:
                roots.append(path)
        if not roots:
            raise RpmError("system Python has no approved site-packages root")
        self.site_roots = sorted(roots)
        return list(self.site_roots)

    def _owner(self, path):
        if path in self.owners:
            return self.owners[path]
        self.owner_queries += 1
        if self.owner_queries > self.config["max_owner_queries"]:
            raise RpmError("RPM ownership query bound was exceeded")
        rc, output, error = self.runner(
            ["/usr/bin/rpm", "-qf", "--qf", "%{NEVRA}\\n", "--", path],
            accepted=(0, 1))
        if rc == 1:
            if "not owned" not in error.lower() and "not owned" not in output.lower():
                raise RpmError("RPM ownership query failed without a not-owned result")
            owner = ""
        else:
            lines = [line.strip() for line in output.splitlines() if line.strip()]
            if len(lines) != 1 or not _NEVRA_RE.fullmatch(lines[0]):
                raise RpmError("RPM ownership query returned an invalid NEVRA")
            owner = lines[0]
        self.owners[path] = owner
        return owner

    def _targets(self, roots):
        return [{"path": root, "recursive": True, "optional": False} for root in roots]

    def _shared_local_snapshot(self, roots, shared_snapshot):
        result = {}
        for root in roots:
            if root.startswith(("/usr/local/lib/", "/usr/local/lib64/")):
                if shared_snapshot is None:
                    raise RpmError("/usr/local/lib requires the shared fast-tier traversal")
                for path, entry in shared_snapshot.items():
                    if _contained(path, root):
                        result[path] = dict(entry)
        return result

    def _verify_packages(self, packages, roots):
        if len(packages) > self.config["max_packages"]:
            raise RpmError("RPM package bound was exceeded")
        result = {}
        for package in sorted(packages):
            rc, output, error = self.runner(
                ["/usr/bin/rpm", "--verify", "--noscripts", "--nodeps", package],
                accepted=(0, 1))
            if rc == 1 and not output.strip():
                raise RpmError(f"RPM verification failed without classifiable output: {package}")
            parsed = parse_verify_output(output, roots)
            for path, status in parsed.items():
                status["package"] = package
                result[path] = status
            if error.strip():
                raise RpmError(f"RPM verification wrote unclassifiable stderr: {package}")
        return result

    def scan(self, previous=None, shared_snapshot=None):
        roots = self.discover_site_roots()
        local = [root for root in roots if root.startswith(("/usr/local/lib/", "/usr/local/lib64/"))]
        system = [root for root in roots if root not in local]
        snapshot = self._shared_local_snapshot(local, shared_snapshot)
        if system:
            walker_config = {
                "targets": self._targets(system),
                "max_entries": self.config["max_entries"],
                "max_file_bytes": self.config["max_file_bytes"],
                "max_depth": self.config["max_depth"],
            }
            walker = FimCollector(
                walker_config, self.expected_changes_path, self.identity, "rpm",
                hash_filter=lambda path: not bool(self._owner(path)))
            scan = walker.scan(previous)
            if not scan["complete"]:
                scan["baseline_key"] = self.baseline_key
                return scan
            snapshot.update(scan["snapshot"])
        if len(snapshot) > self.config["max_entries"]:
            raise RpmError("system Python entry bound was exceeded")
        packages = set()
        for path, entry in snapshot.items():
            if entry.get("kind") not in ("file", "symlink"):
                continue
            owner = self._owner(path)
            if owner:
                entry["rpm_owner"] = owner
                entry.pop("sha256", None)
                packages.add(owner)
        verified = self._verify_packages(packages, roots)
        for path, status in verified.items():
            if path not in snapshot:
                raise RpmError("RPM projected a path absent from the bounded site graph")
            snapshot[path]["rpm_verify"] = status
        return {
            "profile": self.identity["digest"], "baseline_key": self.baseline_key,
            "tier": "rpm", "snapshot": snapshot, "complete": True,
            "descriptor_safe": True, "entries": len(snapshot),
            "packages": len(packages), "anomalies": len(verified),
            "site_roots": roots,
        }

    def diff(self, baseline, scan):
        helper = FimCollector(
            {"targets": [], "max_entries": 1, "max_file_bytes": 0, "max_depth": 1},
            self.expected_changes_path, self.identity, "rpm")
        return helper.diff(baseline, scan)
