"""Bounded RPM and system-interpreter integrity collection for AL2023."""

import json
import os
import re
import select
import subprocess
import tempfile
import time

from .fim import FimCollector


_NEVRA_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.:~-]{0,511}$")
_VERIFY_POSITIONS = (
    set(".S?"), set(".M?"), set(".5?"), set(".D?"), set(".L?"),
    set(".U?"), set(".G?"), set(".T?"), set(".P?"),
)
_FILE_MARKERS = set("cdglra")
_MAX_OWNER_PATH_BYTES = 4096
_MAX_HELPER_RESPONSE_BYTES = 65536
_HELPER_READ_SECONDS = 5
_HELPER_TEARDOWN_SECONDS = 2


_OWNER_HELPER_SCRIPT = r'''import json,os,re,sys
import rpm

NEVRA=re.compile(r'^[A-Za-z0-9][A-Za-z0-9+_.:~-]{0,511}$')

def emit(value):
    sys.stdout.write(json.dumps(value,separators=(',',':'))+'\n')
    sys.stdout.flush()

def cookie(transaction):
    value=transaction.dbCookie()
    if (not isinstance(value,str) or not value or
            len(value.encode('utf-8'))>4096):
        raise RuntimeError('RPM database cookie is unavailable or unbounded')
    return value

def installed_owners(headers,path,normal):
    owners=[]
    for header in headers:
        filenames=header['filenames']
        states=header['filestates']
        if (not isinstance(filenames,(list,tuple)) or
                not isinstance(states,(list,tuple)) or
                len(filenames)!=len(states)):
            raise RuntimeError('RPM ownership header metadata is malformed')
        if not any(name==path and state==normal
                   for name,state in zip(filenames,states)):
            continue
        owner=header.sprintf('%{NEVRA}')
        if not isinstance(owner,str) or not NEVRA.fullmatch(owner):
            raise RuntimeError('RPM ownership header NEVRA is invalid')
        if owner not in owners:
            owners.append(owner)
        if len(owners)==2:
            break
    return owners

def installed_probe(transaction,index,normal):
    path='/usr/bin/rpm'
    owners=installed_owners(transaction.dbMatch(index,path),path,normal)
    if len(owners)!=1:
        raise RuntimeError('RPM installed-file index preflight failed')

try:
    if not hasattr(rpm,'TransactionSet') or not hasattr(rpm,'RPMDBI_INSTFILENAMES'):
        raise RuntimeError('RPM installed-file binding is unavailable')
    normal=getattr(rpm,'RPMFILE_STATE_NORMAL',0)
    transaction=rpm.TransactionSet('/')
    if transaction.openDB()!=0:
        raise RuntimeError('RPM database open failed')
    index=rpm.RPMDBI_INSTFILENAMES
    before=cookie(transaction)
    installed_probe(transaction,index,normal)
    emit({'op':'ready','ready':True})
    for raw in sys.stdin.buffer:
        if len(raw)>8192:
            raise RuntimeError('RPM helper request is unbounded')
        request=json.loads(raw.decode('utf-8'))
        if request=={'op':'finish'}:
            stable=cookie(transaction)==before
            emit({'op':'finish','stable':stable})
            raise SystemExit(0 if stable else 4)
        if not isinstance(request,dict) or set(request)!=set(('op','path')):
            raise RuntimeError('RPM helper request is malformed')
        path=request.get('path')
        if (request.get('op')!='owner' or not isinstance(path,str) or
                not path.startswith('/') or os.path.normpath(path)!=path or
                len(path.encode('utf-8'))>4096):
            raise RuntimeError('RPM helper path is invalid')
        matches=transaction.dbMatch(index,path)
        emit({'op':'owner','owners':installed_owners(matches,path,normal)})
    raise RuntimeError('RPM helper input closed before finish')
except SystemExit:
    raise
except Exception as error:
    emit({'op':'error','error':type(error).__name__})
    raise SystemExit(3)
'''


class RpmError(RuntimeError):
    pass


def _json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise RpmError("RPM helper response repeats a field")
        value[key] = item
    return value


def _decode_helper_response(payload, expected_op, maximum):
    if (not isinstance(payload, bytes) or len(payload) > maximum or
            not payload.endswith(b"\n")):
        raise RpmError("RPM helper response exceeded its bound")
    try:
        value = json.loads(payload.decode("utf-8", "strict"), object_pairs_hook=_json_object)
    except (UnicodeError, json.JSONDecodeError) as err:
        raise RpmError("RPM helper response is malformed") from err
    if not isinstance(value, dict):
        raise RpmError("RPM helper response is not an object")
    if value.get("op") == "error" and set(value) == {"op", "error"}:
        raise RpmError(f"RPM helper failed: {str(value['error'])[:128]}")
    expected = {
        "ready": {"op", "ready"},
        "owner": {"op", "owners"},
        "finish": {"op", "stable"},
    }
    if value.get("op") != expected_op or set(value) != expected[expected_op]:
        raise RpmError("RPM helper response has an unexpected shape")
    if expected_op == "ready" and value["ready"] is not True:
        raise RpmError("RPM helper did not complete its database preflight")
    if expected_op == "finish" and not isinstance(value["stable"], bool):
        raise RpmError("RPM helper returned an invalid database cookie result")
    if expected_op == "owner":
        owners = value["owners"]
        if (not isinstance(owners, list) or len(owners) > 2 or
                any(not isinstance(owner, str) or len(owner) > 512 for owner in owners)):
            raise RpmError("RPM helper returned an invalid owner set")
    return value


def _installed_owners(headers, path, normal_state):
    """Return at most two exact installed-state NEVRAs from binding headers."""
    owners = []
    for header in headers:
        try:
            filenames = header["filenames"]
            states = header["filestates"]
        except (KeyError, TypeError) as err:
            raise RpmError("RPM ownership header is missing file metadata") from err
        if (not isinstance(filenames, (list, tuple)) or
                not isinstance(states, (list, tuple)) or
                len(filenames) != len(states)):
            raise RpmError("RPM ownership header file metadata is malformed")
        installed = any(
            filename == path and state == normal_state
            for filename, state in zip(filenames, states))
        if not installed:
            continue
        try:
            owner = header.sprintf("%{NEVRA}")
        except Exception as err:
            raise RpmError("RPM ownership header has no NEVRA") from err
        if not isinstance(owner, str) or not _NEVRA_RE.fullmatch(owner):
            raise RpmError("RPM ownership header returned an invalid NEVRA")
        if owner not in owners:
            owners.append(owner)
        if len(owners) == 2:
            break
    return owners


class RpmOwnershipSession:
    """One isolated installed-file transaction with a stable RPM DB cookie."""

    def __init__(self, config, deadline=None, popen=None):
        self.config = config
        self.deadline = (time.monotonic() + config["timeout_seconds"]
                         if deadline is None else deadline)
        self.maximum = min(config["max_output_bytes"], _MAX_HELPER_RESPONSE_BYTES)
        self.stderr_size = 0
        self.stderr_eof = False
        launcher = popen or subprocess.Popen
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "LANG": "C"}
        try:
            self.process = launcher(
                [config["interpreter"], "-I", "-u", "-c", _OWNER_HELPER_SCRIPT],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, close_fds=True)
            self._read("ready")
        except Exception:
            self._teardown(False)
            raise

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RpmError("RPM ownership helper exceeded its total deadline")
        return min(remaining, _HELPER_READ_SECONDS)

    def _read(self, expected_op):
        payload = b""
        stdout_descriptor = self.process.stdout.fileno()
        stderr_descriptor = self.process.stderr.fileno()
        while b"\n" not in payload:
            descriptors = [stdout_descriptor]
            if not self.stderr_eof:
                descriptors.append(stderr_descriptor)
            ready, unused_write, unused_error = select.select(
                descriptors, [], [], self._remaining())
            if not ready:
                raise RpmError("RPM ownership helper response timed out")
            if stderr_descriptor in ready:
                error = os.read(stderr_descriptor, min(4096, self.maximum + 1))
                if error:
                    self.stderr_size += len(error)
                    if self.stderr_size > self.maximum:
                        raise RpmError("RPM ownership helper stderr exceeded its bound")
                    raise RpmError("RPM ownership helper wrote unexpected stderr")
                self.stderr_eof = True
            if stdout_descriptor not in ready:
                continue
            block = os.read(
                stdout_descriptor, min(4096, self.maximum + 1 - len(payload)))
            if not block:
                raise RpmError("RPM ownership helper exited before responding")
            payload += block
            if len(payload) > self.maximum:
                raise RpmError("RPM ownership helper response exceeded its bound")
        line, separator, remainder = payload.partition(b"\n")
        if remainder:
            raise RpmError("RPM ownership helper emitted unsolicited output")
        return _decode_helper_response(line + separator, expected_op, self.maximum)

    def _write(self, value):
        payload = (json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8")
        if len(payload) > _MAX_OWNER_PATH_BYTES + 128:
            raise RpmError("RPM ownership helper request exceeded its bound")
        try:
            self.process.stdin.write(payload)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as err:
            raise RpmError("RPM ownership helper died during a request") from err

    def owner(self, path):
        if (not isinstance(path, str) or not path.startswith("/") or
                os.path.normpath(path) != path or
                len(path.encode("utf-8", errors="strict")) > _MAX_OWNER_PATH_BYTES):
            raise RpmError("RPM ownership path is invalid or unbounded")
        self._write({"op": "owner", "path": path})
        return self._read("owner")["owners"]

    def _teardown(self, require_clean):
        process = getattr(self, "process", None)
        if process is None:
            return
        for handle in (process.stdin, process.stdout):
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
        if process.poll() is None and require_clean:
            try:
                process.wait(timeout=_HELPER_TEARDOWN_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=_HELPER_TEARDOWN_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=_HELPER_TEARDOWN_SECONDS)
        error = b""
        if process.stderr is not None:
            error = process.stderr.read(self.maximum + 1)
            process.stderr.close()
        if len(error) > self.maximum:
            raise RpmError("RPM ownership helper stderr exceeded its bound")
        if require_clean and (process.returncode != 0 or error):
            raise RpmError("RPM ownership helper exited unsuccessfully or wrote stderr")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        clean = exc_type is None
        try:
            if clean:
                self._write({"op": "finish"})
                result = self._read("finish")
                if result["stable"] is not True:
                    raise RpmError("RPM database cookie changed during scan")
        finally:
            self._teardown(clean)
        return False


def probe_rpm_capability(config, owner_session_factory=None):
    """Boundedly prove the exact binding, transaction, DB and index capability."""
    factory = owner_session_factory or RpmOwnershipSession
    deadline = time.monotonic() + min(config["timeout_seconds"], 10)
    with factory(config, deadline):
        return True


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

    def __init__(self, config, identity, expected_changes_path=None, runner=None,
                 owner_session_factory=None):
        self.config = config
        self.identity = dict(identity)
        self.expected_changes_path = expected_changes_path
        self.runner = runner or self._run
        self.owner_session_factory = owner_session_factory or RpmOwnershipSession
        self.owners = {}
        self.owner_queries = 0
        self.owner_session = None
        self.scan_deadline = None
        self.site_roots = []
        self.baseline_key = ":".join((identity["name"], identity["digest"], "rpm"))

    def _run(self, argv, accepted=(0,)):
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "LANG": "C"}
        timeout = self.config["timeout_seconds"]
        if self.scan_deadline is not None:
            timeout = min(timeout, self.scan_deadline - time.monotonic())
            if timeout <= 0:
                raise RpmError("RPM integrity scan exceeded its total deadline")
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                done = subprocess.run(
                    argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                    env=env, timeout=timeout, check=False)
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
        if self.owner_session is None:
            raise RpmError("RPM ownership query has no active database session")
        owners = self.owner_session.owner(path)
        if (not isinstance(owners, list) or len(owners) > 2 or
                any(not isinstance(owner, str) or not _NEVRA_RE.fullmatch(owner)
                    for owner in owners)):
            raise RpmError("RPM ownership query returned an invalid owner set")
        if len(owners) > 1:
            raise RpmError("RPM ownership query returned multiple installed owners")
        owner = owners[0] if owners else ""
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
        # Package ownership is mutable across slow scans. Never carry a prior
        # scan's `rpm -qf` result or query budget into the next generation.
        self.owners = {}
        self.owner_queries = 0
        self.scan_deadline = time.monotonic() + self.config["timeout_seconds"]
        try:
            roots = self.discover_site_roots()
            local = [root for root in roots
                     if root.startswith(("/usr/local/lib/", "/usr/local/lib64/"))]
            system = [root for root in roots if root not in local]
            with self.owner_session_factory(
                    self.config, self.scan_deadline) as self.owner_session:
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
        finally:
            self.owner_session = None
            self.scan_deadline = None

    def diff(self, baseline, scan):
        helper = FimCollector(
            {"targets": [], "max_entries": 1, "max_file_bytes": 0, "max_depth": 1},
            self.expected_changes_path, self.identity, "rpm")
        return helper.diff(baseline, scan)
