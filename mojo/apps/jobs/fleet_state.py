"""Non-secret, incarnation-bound proof from real job daemon processes.

Files are evidence, never signal authority. Readers must independently prove
the expected local command and require every expected component to agree.
"""
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time


MAX_BYTES = 4096
MAX_AGE = 15
FIELDS = {"schema", "component", "pid", "start_ticks", "loaded_revision",
          "started_at", "updated_at", "ready", "draining"}
COMPONENTS = {"engine", "scheduler"}
REVISION = re.compile(r"[a-f0-9]{32,64}")


def _start_ticks(pid):
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return None
        # comm is parenthesized and can itself contain spaces or ')'.
        fields = raw.rsplit(b")", 1)[1].split()
        if fields[0] == b"Z":
            return None
        value = int(fields[19])  # /proc field 22, following comm (field 2)
        return value if value > 0 else None
    except (OSError, ValueError, IndexError):
        return None


def _started_at(start_ticks):
    """Convert kernel birth to epoch time, independent of Django startup."""
    try:
        ticks_per_second = os.sysconf("SC_CLK_TCK")
        now = time.time()
        uptime = time.clock_gettime(time.CLOCK_BOOTTIME)
        if (type(start_ticks) is not int or start_ticks <= 0
                or type(ticks_per_second) is not int or ticks_per_second <= 0
                or not math.isfinite(now) or not math.isfinite(uptime)
                or uptime <= 0):
            return None
        age = uptime - start_ticks / ticks_per_second
        birth = now - age
        if age < 0 or not math.isfinite(birth) or birth <= 0:
            return None
        return birth
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        return None


def begin(component, *, root=None):
    """Capture loaded settings once, only when a real daemon starts."""
    from mojo.helpers.settings import settings
    try:
        revision = settings.get_static("MOJO_FLEET_CONFIG_REVISION", None)
        if (component not in COMPONENTS or not isinstance(revision, str)
                or not REVISION.fullmatch(revision)):
            return None
        if root is None:
            from mojo.helpers import paths
            root = settings.get_static("VAR_ROOT", getattr(paths, "VAR_ROOT", None))
        if root is None:
            return None
        pid = os.getpid()
        ticks = _start_ticks(pid)
        if ticks is None:
            return None
        birth = _started_at(ticks)
        if birth is None:
            return None
        return {"schema": 1, "component": component, "pid": pid,
                "start_ticks": ticks, "loaded_revision": revision,
                "started_at": birth, "_root": str(root)}
    except Exception:
        # Missing proof withholds fleet health, never job processing.
        return None


def _path(component, pid, root):
    return Path(root) / "job_processes" / f"{component}-{pid}.json"


def publish(state, draining=False):
    """Refresh atomically; no settings/file reload can change its revision."""
    if state is None:
        return False
    temporary = None
    try:
        path = _path(state["component"], state["pid"], state["_root"])
        path.parent.mkdir(parents=True, exist_ok=True)
        document = {key: state[key] for key in FIELDS if key in state}
        document.update(updated_at=time.time(), ready=True, draining=bool(draining))
        raw = json.dumps(document, sort_keys=True, allow_nan=False).encode()
        if len(raw) > MAX_BYTES:
            return False
        fd, temporary = tempfile.mkstemp(prefix=".proof-", dir=path.parent)
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o644)
            handle.write(raw)
        os.replace(temporary, path)
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def remove(state):
    if state is None:
        return
    try:
        _path(state["component"], state["pid"], state["_root"]).unlink()
    except (OSError, ValueError, TypeError, KeyError):
        pass


def read(component, pid, root="/opt/api/var"):
    """Return fresh bounded proof for this exact live kernel incarnation."""
    if component not in COMPONENTS:
        return None
    try:
        if isinstance(pid, str) and pid.isascii() and pid.isdigit():
            pid = int(pid)
        if type(pid) is not int or pid <= 0:
            return None
        path = _path(component, pid, root)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory)
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                    return None
                raw = handle.read(MAX_BYTES + 1)
        finally:
            os.close(directory)
        if len(raw) > MAX_BYTES:
            return None
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != FIELDS
                or type(value["schema"]) is not int or value["schema"] != 1
                or value["component"] != component
                or type(value["pid"]) is not int or value["pid"] != pid
                or type(value["start_ticks"]) is not int
                or value["start_ticks"] <= 0
                or value["start_ticks"] != _start_ticks(pid)
                or not isinstance(value["loaded_revision"], str)
                or not REVISION.fullmatch(value["loaded_revision"])
                or value["ready"] is not True
                or type(value["draining"]) is not bool):
            return None
        now = time.time()
        for key in ("started_at", "updated_at"):
            stamp = value[key]
            if (type(stamp) not in (int, float) or not math.isfinite(stamp)
                    or stamp <= 0 or stamp > now + 5):
                return None
        if (value["updated_at"] < value["started_at"]
                or now - value["updated_at"] > MAX_AGE):
            return None
        birth = _started_at(value["start_ticks"])
        if birth is None or value["updated_at"] < birth:
            return None
        # The proof may have been written long after process startup. A
        # claimed later epoch cannot prove it was born after installation.
        value["started_at"] = birth
        return value
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return None
