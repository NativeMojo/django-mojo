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

## Why the registry alone is not enough

The registry is a single JSON file in a home directory, and every way it can be
lost hands a live checkout's Redis index to somebody else: it gets deleted, it
gets truncated by a killed writer, it fails to parse and is rebuilt empty. A
port survives that — `port_is_free` BINDS before handing one out, so the
allocator asks the machine rather than its own bookkeeping. A Redis index had
no such check, so a lost registry meant the next `flushdb()` silently emptied
another suite mid-run.

So an index carries an ownership stamp — `testenv:owner`, holding the checkout
path — written by the test runner and read here. It is checked twice: when an
index is allocated (skip anything a *live* foreign checkout has stamped) and
immediately before a `FLUSHDB` (refuse outright). The key name is deliberately
shared across every repo on the machine; two repos with different names would
be blind to each other, which is the entire failure being prevented.

The two checks fail in opposite directions on purpose. **Allocation fails
closed**: it writes a record a checkout then uses for months, so one flaky
probe must never latch a wrong answer in. **The flush fails open**: if Redis
cannot be reached, `flushdb()` cannot destroy anything either.
`MOJO_TESTENV_NO_REDIS=1` is the documented opt-out for images with no Redis.

The stamp is a backstop for when the registry is gone — not a replacement for
its lock.
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

# The ownership stamp. This exact string is shared with every other repo on the
# machine (maestro writes it from its own runner) — two repos using different
# key names would be blind to each other's stamps, which is the collision this
# whole mechanism exists to prevent. Do not rename it, do not namespace it.
REDIS_OWNER_KEY = "testenv:owner"

# Probe defaults. Overridable by MOJO_TESTENV_REDIS_HOST / MOJO_TESTENV_REDIS_PORT,
# read at call time so a test or a shell can change them without a reimport.
REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
REDIS_PROBE_TIMEOUT = 0.25


class AllocationError(Exception):
    """No slot could be allocated. Always actionable — never silently reused."""


class RedisUnreachable(Exception):
    """The ownership probe could not get an answer.

    Emphatically NOT the same as "the index is free" — collapsing the two is
    how a 200ms network hiccup hands out an index somebody is mid-run on.

    `refused` distinguishes "there is no server here" (stop asking; every other
    index will say the same) from a timeout, which means Redis IS up and merely
    busy — exactly when guessing "free" does the destructive thing.
    """

    def __init__(self, message, refused=False):
        super().__init__(message)
        self.refused = refused


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


def _preserve_corrupt_registry(raw):
    """Keep the bytes of a registry we could not parse.

    Rebuilding empty is right — the values are derived — but the rebuild is
    written straight back, so ONE bad parse silently destroys every other
    checkout's record on the machine with nothing left to look at afterwards.

    A COPY, not a rename: the caller is holding this inode open under the flock
    and is about to write the rebuilt registry through that handle. Moving the
    directory entry aside would send the rebuild into the evidence file and
    leave no registry at all.

    Best effort throughout — losing the evidence must never break allocation.
    """
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        with open(f"{REGISTRY_PATH}.corrupt-{stamp}", "w") as out:
            out.write(raw)
    except OSError:
        pass


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
        _preserve_corrupt_registry(raw)
        return _empty()
    if not isinstance(data, dict) or "allocations" not in data:
        _preserve_corrupt_registry(raw)
        return _empty()
    return data


def _write(handle, data):
    """Replace the registry's contents in place, without a zero-byte window.

    Serialised FIRST so a serialisation failure cannot leave a partial write
    behind, then written over the top and truncated to the NEW length. The
    obvious order — truncate, then dump — leaves the file genuinely zero bytes
    on disk for the whole of the dump, and a reader landing there gets
    `_empty()` and hands out indexes that are already in use.

    Deliberately in place: same inode, same flock, no tempfile and no
    `os.replace`. Adopting repos run an OLDER installed copy of this file
    against the same registry, and any scheme that swaps the inode or moves the
    lock elsewhere gives those two copies zero mutual exclusion during a
    version skew — manufacturing the exact registry loss this guards against.
    """
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    written = len(payload.encode(handle.encoding or "utf-8"))
    handle.seek(0)
    handle.write(payload)
    handle.flush()
    os.fsync(handle.fileno())
    handle.truncate(written)
    handle.flush()
    os.fsync(handle.fileno())


class _locked_registry:
    """Open the registry under an exclusive flock.

    One developer machine, so `flock` is sufficient and `O_CREAT` on the
    registry itself avoids a separate lock file going stale. The lock is held
    for the whole read-modify-write, which is what makes two suites starting
    simultaneously safe.

    Opened O_RDWR|O_CREAT rather than `"a+"`: append mode sets O_APPEND, which
    makes every write land at EOF no matter where the handle is seeked. That
    was invisible while `_write` truncated to zero first, and would silently
    append a second copy of the registry now that it does not.
    """

    def __enter__(self):
        os.makedirs(REGISTRY_DIR, exist_ok=True)
        fd = os.open(REGISTRY_PATH, os.O_RDWR | os.O_CREAT, 0o644)
        self.handle = os.fdopen(fd, "r+")
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


def probing_disabled():
    """Whether the ownership probe is switched off for this process.

    The documented escape for an image with no Redis at all. It is opt-IN to
    danger, so it must be explicit — nothing infers it.
    """
    return bool(os.environ.get("MOJO_TESTENV_NO_REDIS"))


def _redis_client(index):
    """A bare redis-py client for one database index, or None if unavailable.

    Bare on purpose. `mojo.helpers.redis` resolves its URL from a Django
    setting and caches a process-global client before any network I/O, so
    reaching it from here would pin an adopting repo's whole process to index 0
    — see the module docstring. `import redis` alone drags in nothing from
    `mojo` and needs no configured project.

    A CLIENT-OWNED pool, never `redis.Redis(connection_pool=...)`: the explicit
    form sets `auto_close_connection_pool=False`, so `close()` leaves the
    socket open and a cold allocation leaks one per candidate index.
    """
    if probing_disabled():
        return None
    try:
        import redis
    except ImportError:
        # A last-resort interpreter without redis-py. The caller decides what
        # to do about it; it must not read as "the index is free".
        return None

    host = os.environ.get("MOJO_TESTENV_REDIS_HOST") or REDIS_HOST
    try:
        port = int(os.environ.get("MOJO_TESTENV_REDIS_PORT", REDIS_PORT))
    except (TypeError, ValueError):
        port = REDIS_PORT

    return redis.Redis(
        host=host, port=port, db=index,
        socket_connect_timeout=REDIS_PROBE_TIMEOUT,
        socket_timeout=REDIS_PROBE_TIMEOUT,
        decode_responses=True)


def _is_refusal(err):
    """Whether an error means "no server here" rather than "server is busy".

    A refused connect is machine-wide, so there is no point asking about the
    other fourteen indexes. A timeout is not: Redis is up, and an index that
    times out is far more likely to be in use than free.
    """
    names = {cls.__name__ for cls in type(err).__mro__}
    if "TimeoutError" in names:
        return False
    if isinstance(err, OSError) and err.errno == errno.ECONNREFUSED:
        return True
    return "ConnectionError" in names


def redis_owner(index):
    """The checkout path stamped on a Redis index, or None when unstamped.

    Raises `RedisUnreachable` when the question could not be asked at all.
    """
    client = _redis_client(index)
    if client is None:
        raise RedisUnreachable(
            "no Redis client is available to check index "
            f"{index} for an owner", refused=True)
    try:
        return client.get(REDIS_OWNER_KEY) or None
    except Exception as err:
        raise RedisUnreachable(
            f"could not read the owner of Redis index {index}: {err}",
            refused=_is_refusal(err))
    finally:
        try:
            client.close()
        except Exception:
            pass


def _same_checkout(owner, root):
    return os.path.realpath(str(owner)) == os.path.realpath(str(root))


def redis_index_is_free(index, root):
    """Whether `root` may use a Redis index, as far as the stamp knows.

    False ONLY when the stamp names a different checkout that still exists on
    disk. Unstamped is free (nobody has claimed it, including every adopter
    that never stamps). Our own stamp is free. A stamp naming a directory that
    is gone is STALE — the checkout was deleted, so its leftover data is
    nobody's, and holding the slot forever would leak the scarcest resource
    here.

    Propagates `RedisUnreachable`; the caller decides which way to fail.
    """
    owner = redis_owner(index)
    if not owner:
        return True
    if _same_checkout(owner, root):
        return True
    return not os.path.isdir(owner)


def claim_redis_index(index, root):
    """Refuse the index unless `root` may destroy what is in it.

    Called immediately before a FLUSHDB. Fails OPEN when Redis cannot be
    reached — a flush that cannot connect cannot destroy anything either — and
    hard-refuses anything below `REDIS_FIRST_INDEX`, because index 0 is the
    developer's own cache and is never ours to empty.
    """
    try:
        index = int(index)
    except (TypeError, ValueError):
        raise AllocationError(
            f"refusing to claim a non-numeric Redis index: {index!r}")

    if index < REDIS_FIRST_INDEX:
        raise AllocationError(
            f"refusing to touch Redis index {index}: testenv only ever "
            f"allocates {REDIS_FIRST_INDEX} and up, and index 0 is the "
            f"developer's own cache — flushing it is never ours to do. "
            f"Run `bin/create_testproject` to get an allocated index.")

    try:
        owner = redis_owner(index)
    except RedisUnreachable:
        # Fail open: nothing we cannot reach can be destroyed.
        return True

    if not owner or _same_checkout(owner, root) or not os.path.isdir(owner):
        return True

    raise AllocationError(
        f"Redis index {index} belongs to another checkout:\n"
        f"  owner: {owner}\n"
        f"  this : {os.path.realpath(str(root))}\n"
        f"Flushing it would wipe that suite's data mid-run. Fix the "
        f"allocation, not the guard: reclaim dead slots with "
        f"`python testit/testenv.py prune`, then re-run "
        f"`bin/create_testproject` to get an index of your own.")


def stamp_redis_owner(index, root):
    """Record `root` as the owner of a Redis index. Returns True when written.

    Best effort by design. Failing a whole test run because a stamp could not
    be written costs more than the protection is worth — the run is about to
    happen either way, and the next successful stamp restores the guard.

    Refuses anything below `REDIS_FIRST_INDEX` for the same reason
    `claim_redis_index` does: index 0 is not ours to label.
    """
    try:
        index = int(index)
    except (TypeError, ValueError):
        return False
    if index < REDIS_FIRST_INDEX:
        return False

    client = _redis_client(index)
    if client is None:
        return False
    try:
        client.set(REDIS_OWNER_KEY, os.path.realpath(str(root)))
        return True
    except Exception:
        return False
    finally:
        try:
            client.close()
        except Exception:
            pass


def _pick_redis_index(taken, limit, root):
    """The lowest index free in the registry AND not stamped by a live checkout.

    Fails CLOSED when the stamp cannot be read. `allocate` writes a record a
    checkout then uses for months, so one flaky probe reading all fifteen
    candidates as free would latch a collision in permanently — the opposite
    trade-off from the flush guard, which fails open.
    """
    if probing_disabled():
        for index in range(REDIS_FIRST_INDEX, limit):
            if index not in taken:
                return index
        raise AllocationError(_exhausted_message(limit, []))

    owned = []
    unreachable = None
    for index in range(REDIS_FIRST_INDEX, limit):
        if index in taken:
            continue
        try:
            owner = redis_owner(index)
        except RedisUnreachable as err:
            unreachable = err
            if err.refused:
                # No server at all; the other candidates would say the same.
                break
            continue
        if not owner or _same_checkout(owner, root) or not os.path.isdir(owner):
            return index
        owned.append(f"{index} ({owner})")

    if unreachable is not None:
        raise AllocationError(
            f"Cannot allocate a Redis index: {unreachable}\n"
            f"Allocation fails closed here on purpose — the record it writes "
            f"is used for months, and guessing an index is free is how one "
            f"suite flushes another mid-run. Start Redis, or set "
            f"MOJO_TESTENV_NO_REDIS=1 to allocate without the ownership check "
            f"(the only supported way to opt out).")

    raise AllocationError(_exhausted_message(limit, owned))


def _exhausted_message(limit, owned):
    message = (
        f"No free Redis database index (limit={limit}, indexes "
        f"{REDIS_FIRST_INDEX}..{limit - 1} are all allocated). ")
    if owned:
        message += (
            "These are held by other live checkouts: "
            + "; ".join(owned) + ". ")
    return message + (
        f"Reclaim slots for deleted checkouts with "
        f"`python testit/testenv.py prune`, release one with "
        f"`python -m testit.testenv release <path>`, or raise `databases` in "
        f"redis.conf AND set MOJO_TESTENV_REDIS_LIMIT to match.")


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

    A candidate Redis index that another LIVE checkout has stamped is skipped,
    and a stamp that cannot be read is an error rather than a shrug — see
    `_pick_redis_index`. `MOJO_TESTENV_NO_REDIS=1` turns that check off.
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
            "redis_index": _pick_redis_index(taken_indexes, limit, root),
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
            # Name the project too. prune is machine-global — it reclaims slots
            # for every repo on the machine — and a bare path gives no clue
            # that the slot you just freed belonged to something else.
            print(f"  {record['path']}  ({record.get('base_name', 'unknown')})")
        if gone:
            reclaimed = sorted({r["base_name"] for r in gone if r.get("base_name")})
            mine = allocations().get(
                os.path.realpath(project_root()), {}).get("base_name")
            others = [name for name in reclaimed if name != mine]
            if others and (mine is not None or len(reclaimed) > 1):
                print(f"\nNote: slots for other projects were reclaimed "
                      f"({', '.join(others)}).")
                print("Their checkouts will get a different Redis index and "
                      "port next run.")
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
