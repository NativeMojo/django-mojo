"""Bounded RPM CLI and system-interpreter integrity collection for AL2023."""

import hashlib
import json
import os
import re
import selectors
import subprocess
import time
import xml.etree.ElementTree as ET

from mojo.helpers.safe_text import bound_utf8, sanitize_scalar

from .fim import FimCollector


_NEVRA_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.:~-]{0,511}$")
_HEADER_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_INTEGER_RE = re.compile(r"^(0|[1-9][0-9]{0,19})$")
_VERIFY_POSITIONS = (
    set(".S?"), set(".M?"), set(".5?"), set(".D?"), set(".L?"),
    set(".U?"), set(".G?"), set(".T?"), set(".P?"),
)
_FILE_MARKERS = set("cdglra")
_MAX_OWNER_PATH_BYTES = 4096
_PRIVATE_STDERR_BYTES = 4096
_DIAGNOSTIC_LINES = 3
_DIAGNOSTIC_LINE_BYTES = 200
_DIAGNOSTIC_TAIL_BYTES = 512
_PROCESS_TEARDOWN_SECONDS = 1

# RPM 4.16's :xml formatter emits one rpmTag element whose typed child contains
# escaped data. FILENAMES and FILESTATES are parallel arrays; the iterator emits
# one literal file row for each paired element.
_INVENTORY_QUERYFORMAT = (
    "<mojosec-rpm-package>"
    "%{NEVRA:xml}"
    "%{SHA256HEADER:xml}"
    "%{INSTALLTID:xml}"
    "%{INSTALLTIME:xml}"
    "<mojosec-rpm-files>"
    "[<mojosec-rpm-file>%{FILENAMES:xml}%{FILESTATES:xml}</mojosec-rpm-file>]"
    "</mojosec-rpm-files>"
    "</mojosec-rpm-package>\n"
)
_INVENTORY_COMMAND = (
    "/usr/bin/rpm", "-qa", "--queryformat", _INVENTORY_QUERYFORMAT,
)


class RpmError(RuntimeError):
    """A fixed outward classification with optional private command detail."""

    def __init__(self, message, private_stderr=b""):
        super().__init__(message)
        if not isinstance(private_stderr, bytes):
            private_stderr = b""
        self._private_stderr_truncated = len(private_stderr) > _PRIVATE_STDERR_BYTES
        self.private_stderr = private_stderr[-_PRIVATE_STDERR_BYTES:]

    def diagnostic_tail(self):
        """Return the only subprocess detail allowed in privileged local logs."""
        if str(self) == "RPM command output exceeded its bound":
            return ""
        text = self.private_stderr.decode("utf-8", errors="replace")
        if self._private_stderr_truncated:
            # The rolling byte tail can begin inside a UTF-8 sequence or a
            # producer line. Only complete lines are eligible for diagnosis.
            pieces = text.splitlines()
            if len(pieces) <= 1:
                return ""
            text = "\n".join(pieces[1:])
        lines = [line for line in text.splitlines() if line.strip()][-_DIAGNOSTIC_LINES:]
        lines = [sanitize_scalar(
            line, max_input_characters=_PRIVATE_STDERR_BYTES,
            max_bytes=_DIAGNOSTIC_LINE_BYTES,
        ) for line in lines]
        if not lines:
            return ""
        while len("\n".join(lines).encode("utf-8")) > _DIAGNOSTIC_TAIL_BYTES:
            # Preserve the newest lines in full. The oldest selected line gets
            # the remaining aggregate budget, including its marker.
            newer_size = len("\n".join(lines[1:]).encode("utf-8"))
            separators = len(lines) - 1
            remaining = _DIAGNOSTIC_TAIL_BYTES - newer_size - separators
            if remaining <= 0:
                lines.pop(0)
                continue
            lines[0] = bound_utf8(lines[0], remaining)
            break
        return "\n".join(lines)


def _contained(path, root):
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def _only_whitespace(value):
    return value is None or not value.strip()


def _typed_tag(element, expected_name, expected_type):
    if (element.tag != "rpmTag" or element.attrib != {"name": expected_name} or
            not _only_whitespace(element.text) or len(element) != 1):
        raise RpmError("RPM inventory has malformed fields")
    child = element[0]
    if (child.tag != expected_type or child.attrib or len(child) or
            not _only_whitespace(child.tail)):
        raise RpmError("RPM inventory has malformed fields")
    value = child.text or ""
    if expected_type == "integer" and not _INTEGER_RE.fullmatch(value):
        raise RpmError("RPM inventory has malformed integer fields")
    return value


def _parse_file_row(element):
    if (element.tag != "mojosec-rpm-file" or element.attrib or
            not _only_whitespace(element.text) or len(element) != 2 or
            not _only_whitespace(element.tail)):
        raise RpmError("RPM inventory has malformed file rows")
    path = _typed_tag(element[0], "Filenames", "string")
    state_text = _typed_tag(element[1], "Filestates", "integer")
    if (not path.startswith("/") or os.path.normpath(path) != path or
            len(path.encode("utf-8", errors="strict")) > _MAX_OWNER_PATH_BYTES):
        raise RpmError("RPM inventory contains an invalid file path")
    state = int(state_text)
    if state > 255:
        raise RpmError("RPM inventory contains an invalid file state")
    return path, state


def parse_inventory(payload):
    """Strictly parse one complete, XML-escaped installed-header inventory."""
    if not isinstance(payload, str) or not payload or "\x00" in payload:
        raise RpmError("RPM inventory output is malformed")
    if "<!" in payload or "<?" in payload:
        raise RpmError("RPM inventory output contains unsupported XML")
    try:
        root = ET.fromstring("<mojosec-rpm-inventory>" + payload +
                             "</mojosec-rpm-inventory>")
    except ET.ParseError as err:
        raise RpmError("RPM inventory output is malformed") from err
    if (root.attrib or not _only_whitespace(root.text) or
            not _only_whitespace(root.tail) or not len(root)):
        raise RpmError("RPM inventory output is malformed")

    records = []
    owners = {}
    identities = set()
    for package in root:
        if (package.tag != "mojosec-rpm-package" or package.attrib or
                not _only_whitespace(package.text) or len(package) != 5 or
                not _only_whitespace(package.tail)):
            raise RpmError("RPM inventory has malformed package records")
        nevra = _typed_tag(package[0], "Nevra", "string")
        header = _typed_tag(package[1], "Sha256header", "string")
        install_tid = _typed_tag(package[2], "Installtid", "integer")
        install_time = _typed_tag(package[3], "Installtime", "integer")
        if not _NEVRA_RE.fullmatch(nevra):
            raise RpmError("RPM inventory contains an invalid NEVRA")
        if not _HEADER_RE.fullmatch(header):
            raise RpmError("RPM inventory contains an invalid header identity")
        files_element = package[4]
        if (files_element.tag != "mojosec-rpm-files" or files_element.attrib or
                not _only_whitespace(files_element.text) or
                not _only_whitespace(files_element.tail)):
            raise RpmError("RPM inventory has malformed file rows")
        files = []
        seen_paths = set()
        for row in files_element:
            path, state = _parse_file_row(row)
            if path in seen_paths:
                raise RpmError("RPM inventory repeats a package file path")
            seen_paths.add(path)
            files.append([path, state])
            if state == 0:
                owners.setdefault(path, []).append(nevra)

        identity = (nevra, header.lower(), install_tid, install_time)
        if identity in identities:
            raise RpmError("RPM inventory repeats a package identity")
        identities.add(identity)
        records.append({
            "nevra": nevra,
            "sha256header": header.lower(),
            "installtid": install_tid,
            "installtime": install_time,
            "files": files,
        })

    canonical = json.dumps(
        sorted(records, key=lambda item: (
            item["nevra"], item["sha256header"], item["installtid"],
            item["installtime"], item["files"])),
        ensure_ascii=True, separators=(",", ":"), sort_keys=True,
    ).encode("ascii")
    return {
        "records": records,
        "owners": owners,
        "digest": hashlib.sha256(canonical).hexdigest(),
    }


def probe_rpm_capability(config, runner=None):
    """Prove the exact RPM CLI inventory, parser, database and owner contract."""
    collector = RpmCollector(
        config, {"name": "probe", "version": 0, "digest": "0" * 64},
        runner=runner,
    )
    inventory = collector._inventory()
    if len(inventory["owners"].get("/usr/bin/rpm", [])) != 1:
        raise RpmError("RPM inventory ownership preflight failed")
    return True


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

    def __init__(self, config, identity, expected_changes_path=None, runner=None,
                 popen=None, fim_factory=None):
        self.config = config
        self.identity = dict(identity)
        self.expected_changes_path = expected_changes_path
        self.runner = runner or self._run
        self.popen = popen or subprocess.Popen
        self.fim_factory = fim_factory or FimCollector
        self.owners = {}
        self.owner_queries = 0
        self.owner_index = {}
        self.scan_deadline = None
        self.site_roots = []
        self.baseline_key = ":".join((identity["name"], identity["digest"], "rpm"))

    def _stop(self, process):
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=_PROCESS_TEARDOWN_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=_PROCESS_TEARDOWN_SECONDS)

    def _run(self, argv, accepted=(0,)):
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "LANG": "C"}
        timeout = self.config["timeout_seconds"]
        if self.scan_deadline is not None:
            timeout = min(timeout, self.scan_deadline - time.monotonic())
        if timeout <= 0:
            raise RpmError("RPM command timed out")
        try:
            process = self.popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, close_fds=True, bufsize=0,
            )
        except OSError as err:
            raise RpmError("RPM command is unavailable") from err

        maximum = self.config["max_output_bytes"]
        output = {"stdout": bytearray(), "stderr": bytearray()}
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        deadline = time.monotonic() + timeout
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._stop(process)
                    raise RpmError("RPM command timed out", bytes(output["stderr"]))
                ready = selector.select(min(remaining, 0.25))
                if not ready:
                    continue
                for key, unused_mask in ready:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    stream = output[key.data]
                    if len(stream) + len(chunk) > maximum:
                        self._stop(process)
                        raise RpmError("RPM command output exceeded its bound")
                    stream.extend(chunk)
            try:
                returncode = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as err:
                self._stop(process)
                raise RpmError("RPM command timed out", bytes(output["stderr"])) from err
        finally:
            selector.close()
            if process.poll() is None:
                self._stop(process)
            process.stdout.close()
            process.stderr.close()

        try:
            stdout = bytes(output["stdout"]).decode("utf-8", "strict")
            stderr = bytes(output["stderr"]).decode("utf-8", "strict")
        except UnicodeDecodeError as err:
            raise RpmError(
                "RPM command output is not valid UTF-8", bytes(output["stderr"]),
            ) from err
        if returncode not in accepted:
            raise RpmError("RPM command failed", bytes(output["stderr"]))
        return returncode, stdout, stderr

    def _inventory(self):
        unused_rc, output, error = self.runner(list(_INVENTORY_COMMAND))
        if error:
            private = error.encode("utf-8", errors="replace") if isinstance(error, str) else b""
            raise RpmError("RPM inventory command wrote unexpected stderr", private)
        return parse_inventory(output)

    def discover_site_roots(self):
        script = (
            "import json,site,sys,sysconfig;"
            "p=set();"
            "p.update(x for x in sysconfig.get_paths().values() if isinstance(x,str));"
            "p.update(x for x in site.getsitepackages() if isinstance(x,str));"
            "p.update(x for x in sys.path if isinstance(x,str));"
            "print(json.dumps(sorted(p),separators=(',',':')))"
        )
        unused_rc, output, error = self.runner(
            [self.config["interpreter"], "-I", "-c", script])
        if error:
            private = error.encode("utf-8", errors="replace") if isinstance(error, str) else b""
            raise RpmError("system Python wrote unexpected site-root stderr", private)
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
        if (not isinstance(path, str) or not path.startswith("/") or
                os.path.normpath(path) != path or
                len(path.encode("utf-8", errors="strict")) > _MAX_OWNER_PATH_BYTES):
            raise RpmError("RPM ownership path is invalid or unbounded")
        if path in self.owners:
            return self.owners[path]
        self.owner_queries += 1
        if self.owner_queries > self.config["max_owner_queries"]:
            raise RpmError("RPM ownership lookup bound was exceeded")
        matches = self.owner_index.get(path, [])
        if len(matches) > 1:
            raise RpmError("RPM inventory returned multiple installed owners")
        owner = matches[0] if matches else ""
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
                accepted=(0, 1),
            )
            if error:
                private = (error.encode("utf-8", errors="replace")
                           if isinstance(error, str) else b"")
                raise RpmError("RPM verification wrote unclassifiable stderr", private)
            if rc == 1 and not output.strip():
                raise RpmError("RPM verification failed without classifiable output")
            parsed = parse_verify_output(output, roots)
            for path, status in parsed.items():
                status["package"] = package
                result[path] = status
        return result

    def scan(self, previous=None, shared_snapshot=None):
        self.owners = {}
        self.owner_queries = 0
        self.scan_deadline = time.monotonic() + self.config["timeout_seconds"]
        try:
            roots = self.discover_site_roots()
            before = self._inventory()
            self.owner_index = before["owners"]
            local = [root for root in roots
                     if root.startswith(("/usr/local/lib/", "/usr/local/lib64/"))]
            system = [root for root in roots if root not in local]
            snapshot = self._shared_local_snapshot(local, shared_snapshot)
            if system:
                walker_config = {
                    "targets": self._targets(system),
                    "max_entries": self.config["max_entries"],
                    "max_file_bytes": self.config["max_file_bytes"],
                    "max_depth": self.config["max_depth"],
                }
                walker = self.fim_factory(
                    walker_config, self.expected_changes_path, self.identity, "rpm",
                    hash_filter=lambda path: not bool(self._owner(path)),
                )
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
                    raise RpmError("RPM projected a path absent from the site graph")
                snapshot[path]["rpm_verify"] = status
            after = self._inventory()
            if after["digest"] != before["digest"]:
                raise RpmError("RPM inventory changed during the integrity scan")
            return {
                "profile": self.identity["digest"], "baseline_key": self.baseline_key,
                "tier": "rpm", "snapshot": snapshot, "complete": True,
                "descriptor_safe": True, "entries": len(snapshot),
                "packages": len(packages), "anomalies": len(verified),
                "site_roots": roots, "rpm_generation": before["digest"],
            }
        finally:
            self.scan_deadline = None

    def diff(self, baseline, scan):
        helper = FimCollector(
            {"targets": [], "max_entries": 1, "max_file_bytes": 0, "max_depth": 1},
            self.expected_changes_path, self.identity, "rpm",
        )
        return helper.diff(baseline, scan)
