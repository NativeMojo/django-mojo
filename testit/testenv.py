"""
Per-checkout test isolation: a database name, a Redis index and a port that
belong to exactly one working tree.

## The rule this module exists to enforce

**Derive, never inherit.**

Isolation across the mojo repos has always existed, but it was hand-set: a
human picked `mojo_maestro_test` / index 15 / port 9109 once, and it worked
because a human made the values different. A **worktree defeats that
completely** — it is a copy, so it silently inherits its parent's database
name, Redis index and port. Nobody chose anything, so nothing is distinct, and
two suites truncate and `flushdb()` each other mid-run.

So nothing here may live in a config file a worktree would clone. Values are
derived from the checkout's own absolute path and recorded in a registry
OUTSIDE any checkout.

## Why a registry rather than a pure hash

Redis ships `databases 16`. Hashing a path into `% 16` across five repos times
N worktrees collides by birthday almost immediately — and silently, because two
suites on one index look fine until one flushes. The registry hands out the
lowest free slot, is a readable JSON file, and makes stale entries reclaimable.

## Two deliberate offsets

- **Redis indexes start at 1.** Index 0 is what every not-yet-adopted project
  gets from `REDIS_DB_INDEX`'s default, so leaving it unallocated means an
  adopted checkout can never collide with one that has not adopted yet.
- **Ports start at 5600.** Clear of every hand-set port in the fleet today
  (5555, 7575, 9009, 9109, 9999), so adoption is not a flag day.

Both matter only during the migration, and both cost nothing to keep.

## Why this module imports nothing from `mojo`

`allocate()` runs in two places where `mojo` is not safely importable:

- **Before the project exists.** `bin/create_testproject` invokes this file by
  path precisely because there is no configured project yet — any `mojo` import
  dies partway down its own chain, and a bare `except` around it just turns the
  breakage into a wrong-but-quiet answer.
- **From half-built settings modules.** An adopting repo calls `allocate()`
  while its settings module is still executing. `mojo.helpers.redis` resolves
  its URL from `REDIS_DB_INDEX` and caches a process-global client BEFORE any
  network I/O, so importing it there pins the whole process to index 0 for its
  lifetime — the exact cross-checkout collision this module exists to prevent.

So the invariant is: **`allocate()` imports nothing from `mojo.*` and reads no
Django setting.** Anything it cannot derive from the path or the registry is
either passed in by the caller or read from the environment. Keep it that way.
"""

import errno
import fcntl
import hashlib
import json
import os
import socket
from datetime import datetime, timezone


REGISTRY_DIR = os.path.expanduser("~/.mojo")
REGISTRY_PATH = os.path.join(REGISTRY_DIR, "testenv.json")
REGISTRY_VERSION = 1

# See the module docstring for why these are not 0 and 5555.
REDIS_FIRST_INDEX = 1
REDIS_FALLBACK_LIMIT = 16
PORT_BASE = 5600
PORT_RANGE = 100


class AllocationError(Exception):
    """No slot could be allocated. Always actionable — never silently reused."""


# ----------------------------------------------------------------------
# identity
# ----------------------------------------------------------------------

def slug(path):
    """A stable short id for a checkout.

    The absolute realpath is the input, so two worktrees of one repo differ and
    the same tree is stable across reboots, re-clones of the same location, and
    branch switches. `realpath` matters: /var and /private/var on macOS are the
    same directory, and hashing both would allocate two slots to one tree.
    """
    real = os.path.realpath(str(path))
    return hashlib.sha1(real.encode("utf-8")).hexdigest()[:8]


def project_root(start=None):
    """Best-effort checkout root: the nearest ancestor holding a `.git`.

    A convenience for callers that have no better idea. Anything that KNOWS its
    root — `bin/create_testproject` knows `$REPO_ROOT` — should pass it
    explicitly rather than rely on where the process happens to be.

    Note `.git` is a FILE in a worktree and a directory in a normal checkout,
    so this deliberately tests existence rather than isdir.
    """
    current = os.path.realpath(start or os.getcwd())
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return os.path.realpath(start or os.getcwd())
        current = parent


# ----------------------------------------------------------------------
# registry
# ----------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc).isoformat()


def _empty():
    return {"version": REGISTRY_VERSION, "allocations": {}}


def _read(handle):
    handle.seek(0)
    raw = handle.read()
    if not raw.strip():
        return _empty()
    try:
        data = json.loads(raw)
    except ValueError:
        # A corrupt registry must not wedge every suite on the machine. Start
        # over: the values are derived, so the cost of losing them is that a
        # tree gets a different slot next run, not that anything breaks.
        return _empty()
    if not isinstance(data, dict) or "allocations" not in data:
        return _empty()
    return data


def _write(handle, data):
    handle.seek(0)
    handle.truncate()
    json.dump(data, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())


class _locked_registry:
    """Open the registry under an exclusive flock.

    One developer machine, so `flock` is sufficient and `O_CREAT` on the
    registry itself avoids a separate lock file going stale. The lock is held
    for the whole read-modify-write, which is what makes two suites starting
    simultaneously safe.
    """

    def __enter__(self):
        os.makedirs(REGISTRY_DIR, exist_ok=True)
        self.handle = open(REGISTRY_PATH, "a+")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self.handle

    def __exit__(self, *exc):
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        return False


# ----------------------------------------------------------------------
# slot selection
# ----------------------------------------------------------------------

def redis_limit():
    """How many Redis databases this server has, assumed rather than asked.

    Returns `REDIS_FALLBACK_LIMIT` (Redis's own shipped default) unless
    `MOJO_TESTENV_REDIS_LIMIT` says otherwise. A junk or missing value means the
    default — allocation must never be the thing that fails because an
    environment variable was mistyped.

    It does NOT ask the server, and that is deliberate. Reaching Redis from here
    meant importing `mojo.helpers.redis`, which resolves its URL from a Django
    setting and caches a process-global client BEFORE any network I/O. This
    module is loaded before the project exists (`bin/create_testproject`, by
    file path) and from half-built settings modules in adopting repos, so that
    import either explodes or silently pins the whole process's Redis client to
    index 0 for its lifetime. Neither is worth a number we can be told.

    If you raised `databases` in redis.conf, set `MOJO_TESTENV_REDIS_LIMIT` to
    match — raising redis.conf alone does nothing here.
    """
    try:
        return max(1, int(os.environ.get("MOJO_TESTENV_REDIS_LIMIT", REDIS_FALLBACK_LIMIT)))
    except (TypeError, ValueError):
        return REDIS_FALLBACK_LIMIT


def port_is_free(port, host="127.0.0.1"):
    """Whether a port can actually be bound right now.

    Registry bookkeeping alone is not enough: a port can be taken by something
    that never asked us — another service, a stale server from a killed run.
    SO_REUSEADDR is deliberately NOT set, so a socket in TIME_WAIT reads as
    busy rather than as free.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
            return True
        except OSError as err:
            if err.errno in (errno.EADDRINUSE, errno.EACCES):
                return False
            raise


def _pick_redis_index(taken, limit):
    for index in range(REDIS_FIRST_INDEX, limit):
        if index not in taken:
            return index
    raise AllocationError(
        f"No free Redis database index (limit={limit}, indexes "
        f"{REDIS_FIRST_INDEX}..{limit - 1} are all allocated). "
        f"Raise `databases` in redis.conf AND set MOJO_TESTENV_REDIS_LIMIT to "
        f"match, or reclaim slots with "
        f"`python -m testit.testenv release <path>`.")


def _pick_port(taken):
    for port in range(PORT_BASE, PORT_BASE + PORT_RANGE):
        if port in taken:
            continue
        if port_is_free(port):
            return port
    raise AllocationError(
        f"No free port in {PORT_BASE}..{PORT_BASE + PORT_RANGE - 1}. "
        f"Reclaim slots with `python -m testit.testenv release <path>`.")


# ----------------------------------------------------------------------
# the public API
# ----------------------------------------------------------------------

def allocate(root, base_name, limit=None):
    """Return this checkout's `{db_name, port, redis_index, slug}`.

    Idempotent: the same root gets the same values forever, so a re-run of
    `create_testproject` does not move a tree to a new database and orphan the
    old one.

    `limit` is the Redis database ceiling. Left as None it comes from
    `redis_limit()`, resolved at call time.
    """
    root = os.path.realpath(str(root))
    if not base_name:
        raise AllocationError("a base name is required to allocate a database")

    with _locked_registry() as handle:
        data = _read(handle)
        allocations = data["allocations"]

        existing = allocations.get(root)
        if existing:
            existing["last_used"] = _now()
            _write(handle, data)
            return dict(existing)

        taken_indexes = {a.get("redis_index") for a in allocations.values()}
        taken_ports = {a.get("port") for a in allocations.values()}

        # Resolved HERE, not as a `limit=redis_limit()` default argument — a
        # default is evaluated once at def time, which would permanently defeat
        # every `redis_limit` patch (the suite's, and maestro's shim). Leave it.
        if limit is None:
            limit = redis_limit()

        record = {
            "slug": slug(root),
            "base_name": base_name,
            "db_name": f"{base_name}_{slug(root)}",
            "redis_index": _pick_redis_index(taken_indexes, limit),
            "port": _pick_port(taken_ports),
            "created": _now(),
            "last_used": _now(),
        }
        allocations[root] = record
        _write(handle, data)
        return dict(record)


def release(root):
    """Give a checkout's slot back. Returns True when something was removed."""
    root = os.path.realpath(str(root))
    with _locked_registry() as handle:
        data = _read(handle)
        removed = data["allocations"].pop(root, None) is not None
        if removed:
            _write(handle, data)
        return removed


def allocations():
    """Every recorded allocation, keyed by checkout path."""
    with _locked_registry() as handle:
        return dict(_read(handle)["allocations"])


def prune():
    """Drop allocations whose checkout no longer exists on disk.

    Deleting a worktree leaves its slot held, and Redis indexes are the scarce
    resource — 16 of them by default. Returns the reclaimed records, each
    carrying the `path` it was allocated to.

    **The Postgres database is deliberately NOT dropped.** Removing a database
    because a directory is currently missing would destroy data whenever a
    volume is unmounted or a checkout is moved rather than deleted. The caller
    is told the name instead — see the CLI, which prints a ready-to-run
    `dropdb`. Silence here is how orphaned databases accumulate forever.
    """
    with _locked_registry() as handle:
        data = _read(handle)
        gone = []
        for path in list(data["allocations"]):
            if os.path.isdir(path):
                continue
            record = dict(data["allocations"].pop(path))
            record["path"] = path
            gone.append(record)
        if gone:
            _write(handle, data)
        return gone


# ----------------------------------------------------------------------
# CLI — how shell callers (bin/create_testproject) reach this
# ----------------------------------------------------------------------

def _main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m testit.testenv",
        description="Per-checkout test isolation slots")
    sub = parser.add_subparsers(dest="command", required=True)

    p_alloc = sub.add_parser("allocate", help="allocate or read this checkout's slot")
    p_alloc.add_argument("--root", default=None,
                         help="checkout root (default: nearest .git ancestor)")
    p_alloc.add_argument("--base", required=True,
                         help="database base name, e.g. mojo_test")
    p_alloc.add_argument("--format", choices=["sh", "json"], default="sh",
                         help="sh emits eval-able KEY=VALUE lines")

    p_release = sub.add_parser("release", help="give a checkout's slot back")
    p_release.add_argument("root", nargs="?", default=None)

    sub.add_parser("list", help="show every allocation")
    sub.add_parser("prune", help="drop allocations whose checkout is gone")

    opts = parser.parse_args(argv)

    if opts.command == "allocate":
        record = allocate(opts.root or project_root(), opts.base)
        if opts.format == "json":
            print(json.dumps(record, indent=2, sort_keys=True))
        else:
            # Quoted so a path or name with a space cannot break `eval`.
            print(f"TESTENV_DB_NAME='{record['db_name']}'")
            print(f"TESTENV_PORT='{record['port']}'")
            print(f"TESTENV_REDIS_INDEX='{record['redis_index']}'")
            print(f"TESTENV_SLUG='{record['slug']}'")
        return 0

    if opts.command == "release":
        print("released" if release(opts.root or project_root()) else "nothing to release")
        return 0

    if opts.command == "prune":
        gone = prune()
        print(f"pruned {len(gone)}")
        for record in gone:
            print(f"  {record['path']}")
        if gone:
            # The slot is back; the database is not. Name it, because nothing
            # else ever will and they accumulate silently otherwise.
            print("\nThese databases are now orphaned. Drop them when you are "
                  "sure the checkout is really gone:")
            for record in gone:
                print(f"  dropdb {record['db_name']}")
        return 0

    for path, record in sorted(allocations().items()):
        print(f"{record['db_name']:<32} redis={record['redis_index']:<3} "
              f"port={record['port']:<6} {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
