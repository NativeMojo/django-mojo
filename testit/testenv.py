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

## Which server gets asked, and by which check

The two checks reach Redis differently, and the difference is the whole point.

- **The flush guard uses the runner's own live client**, handed in as
  `client=`. That client IS the one about to be flushed, so the guard reads the
  ownership stamp off the exact server and database that `flushdb()` will
  empty. Resolving host and port independently — from
  `MOJO_TESTENV_REDIS_HOST`/`_PORT`, defaulting to localhost — would protect
  index N on one machine while the flush empties index N on another, and a
  guard that checks the wrong server is worse than no guard at all.
- **Allocation builds a probe** against `MOJO_TESTENV_REDIS_HOST`/`_PORT`
  (localhost by default), because there is no live client yet: it runs before
  the project exists, deriving the values the settings will later hold, and
  `create_testproject` writes localhost. The honest limitation: an adopter
  whose test Redis is remote gets no allocation-time protection. They still get
  the flush-time guard, which is the one that destroys data.

`MOJO_TESTENV_NO_REDIS=1` is the documented opt-out for images with no Redis.
It switches off the allocation-time **probe** and nothing else — it cannot
disable the flush guard, which is not building a probe at all but reading a
client the runner is demonstrably holding.

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
# The ceiling MOJO_TESTENV_REDIS_LIMIT is clamped to. Redis's own `databases`
# is configurable without an upper bound, but every index above this one costs
# a fresh connection during a cold allocation — so a mistyped "99999999" would
# sit there opening sockets for hours instead of failing. 64 is well past any
# real fleet and still finishes in a blink.
REDIS_MAX_LIMIT = 64
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


def expected_pubsub_prefix(root):
    """The Pub/Sub isolation prefix this checkout's testproject must carry.

    Single source for derivation (bin/create_testproject writes it into the
    generated settings) and verification (bin/testit.py refuses to run when
    the live setting disagrees — a stale pre-prefix testproject would
    otherwise silently talk on the legacy shared channels). Same identity as
    the cache KEY_PREFIX: mojot_ + the checkout slug.
    """
    return f"mojot_{slug(root)}"


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

    ONCE per distinct corrupt file, which is why the suffix is a digest of the
    content rather than a timestamp. Read-only callers — `allocations()`, the
    CLI's `list`, the extra `allocations()` inside `prune` — never rewrite the
    registry, so an unfixed corrupt file is re-read on every single invocation.
    A timestamped name grew a new copy each time, unbounded, forever; a
    content-derived one collides with the copy already on disk and `O_EXCL`
    turns that collision into "already preserved, nothing to do".

    `O_EXCL | O_NOFOLLOW` rather than `open(..., "w")`: the exclusive create is
    what makes "once" enforceable at the filesystem rather than by a racy
    existence check, and refusing to follow a symlink comes along for free.

    Best effort throughout — losing the evidence must never break allocation.
    """
    try:
        digest = hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:12]
        fd = os.open(f"{REGISTRY_PATH}.corrupt-{digest}",
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600)
    except FileExistsError:
        # These exact bytes are already preserved. Caught before OSError below,
        # which it subclasses.
        return
    except OSError:
        return

    try:
        with os.fdopen(fd, "w") as out:
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

    Clamped to `REDIS_MAX_LIMIT` at the top for the same reason it is floored at
    1 at the bottom: `_pick_redis_index` opens a fresh client per candidate
    index, so an accidental extra digit does not error, it HANGS — hours of
    connecting to indexes no server has. Clamped rather than rejected, so an
    over-large value still allocates instead of blocking every suite.

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
        value = int(os.environ.get("MOJO_TESTENV_REDIS_LIMIT", REDIS_FALLBACK_LIMIT))
    except (TypeError, ValueError):
        return REDIS_FALLBACK_LIMIT
    return max(1, min(REDIS_MAX_LIMIT, value))


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
    """Whether the ownership PROBE is switched off for this process.

    The documented escape for an image with no Redis at all. It is opt-IN to
    danger, so it must be explicit — nothing infers it.

    Scope matters: this governs the probe, which is a client this module builds
    from the environment because no live one exists. It does NOT reach a caller
    that hands in `client=` — see `_client_for`.
    """
    return bool(os.environ.get("MOJO_TESTENV_NO_REDIS"))


def _client_db(client):
    """Which database index a live client is pointed at, or None if unknowable.

    Read off the client's own connection pool, the same way `bin/testit.py`
    does immediately before its `FLUSHDB`. A cluster client has no single
    database and answers None.
    """
    pool = getattr(client, "connection_pool", None)
    if pool is None:
        return None
    return getattr(pool, "connection_kwargs", {}).get("db", 0)


def _client_for(index, client):
    """The client to use for one ownership read/write, and whether we own it.

    Two paths, and the difference is the point:

    - **A caller supplied one** (the runner, holding the very client it is
      about to flush). Use it exactly as-is: same server, same database, no
      environment lookup, no second connection. The caller closes it, not us —
      hence the returned flag.

      `probing_disabled()` deliberately does NOT apply here. There is no probe
      to disable; `MOJO_TESTENV_NO_REDIS` exists for images with no Redis at
      all, and a caller holding a live client has demonstrated otherwise. Left
      applying here it silently disarmed the pre-FLUSHDB guard, which is the
      one check standing between a misallocated index and another suite's data.

    - **Nobody supplied one.** Build a probe from the environment, which is
      where `probing_disabled()` and the localhost defaults belong.

    A supplied client MUST agree with `index`. Disagreement is a programming
    error, and the specific one this whole mechanism exists to prevent — a
    guard reading index N while the flush empties index M — so it raises rather
    than picking a side.
    """
    if client is None:
        return _redis_client(index), True

    live = _client_db(client)
    if live is None:
        raise AllocationError(
            f"cannot tell which Redis database the supplied client is pointed "
            f"at, so the ownership check for index {index} would be guessing "
            f"about the wrong database. Do not call this with a client whose "
            f"database is indeterminate (a cluster client, for one).")
    try:
        agrees = int(live) == int(index)
    except (TypeError, ValueError):
        agrees = False
    if not agrees:
        raise AllocationError(
            f"the supplied Redis client is on database {live!r} but the "
            f"ownership check was asked about index {index!r}. Checking one "
            f"database while flushing another is worse than no check at all; "
            f"derive the index from the client you are about to flush.")
    return client, False


def _redis_client(index):
    """A bare redis-py PROBE client for one database index, or None.

    Bare on purpose. `mojo.helpers.redis` resolves its URL from a Django
    setting and caches a process-global client before any network I/O, so
    reaching it from here would pin an adopting repo's whole process to index 0
    — see the module docstring. `import redis` alone drags in nothing from
    `mojo` and needs no configured project.

    A CLIENT-OWNED pool, never `redis.Redis(connection_pool=...)`: the explicit
    form sets `auto_close_connection_pool=False`, so `close()` leaves the
    socket open and a cold allocation leaks one per candidate index.

    Host and port come from `MOJO_TESTENV_REDIS_HOST`/`_PORT`, defaulting to
    localhost, and are unrelated to any Django `REDIS_URL`. That is fine for
    allocation, which is deriving values the settings do not hold yet; it is
    NOT fine for the flush guard, which must read the server it is about to
    empty. Callers that hold a live client pass it in instead — see
    `_client_for`, which routes past this function entirely.
    """
    # Only the PROBE path reaches this, so MOJO_TESTENV_NO_REDIS switches off
    # only the probe. A caller holding a live client never gets here, which is
    # what keeps the pre-FLUSHDB guard armed regardless of the variable.
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


def redis_owner(index, client=None):
    """The checkout path stamped on a Redis index, or None when unstamped.

    Pass `client` to read the stamp off a client you already hold — that reads
    the actual server and database that client is on, rather than whatever
    `MOJO_TESTENV_REDIS_HOST`/`_PORT` happen to name. It must be on `index`,
    and closing it stays the caller's job. Without one, a probe is built.

    Raises `RedisUnreachable` when the question could not be asked at all, and
    `AllocationError` when a supplied client disagrees with `index`.
    """
    conn, owned = _client_for(index, client)
    if conn is None:
        raise RedisUnreachable(
            "no Redis client is available to check index "
            f"{index} for an owner", refused=True)
    try:
        return conn.get(REDIS_OWNER_KEY) or None
    except Exception as err:
        raise RedisUnreachable(
            f"could not read the owner of Redis index {index}: {err}",
            refused=_is_refusal(err))
    finally:
        if owned:
            try:
                conn.close()
            except Exception:
                pass


def _owner_realpath(owner):
    """A stamp resolved to a path, or None when it will not resolve at all.

    The stamp is arbitrary bytes written by another process, so it is untrusted
    input. Most hostile values resolve perfectly well and then simply read as
    "not me" — `../../..`, `~/`, empty, whitespace — and fall through to the
    `isdir` check, which is the right answer for all of them. A NUL byte does
    not: `realpath` raises `ValueError`, which escaped as an unhandled
    traceback past every caller (the runner only catches `AllocationError`).
    """
    try:
        return os.path.realpath(str(owner))
    except (TypeError, ValueError):
        return None


def _same_checkout(owner, root):
    """Whether a stamp names `root`. Raises on a stamp that will not resolve.

    Fails CLOSED: a stamp we cannot resolve is a stamp whose owner is unknown,
    and "unknown owner" must never read as "not you, go ahead and flush it".
    """
    resolved = _owner_realpath(owner)
    if resolved is None:
        raise AllocationError(
            f"the Redis ownership stamp {owner!r} cannot be resolved to a "
            f"path, so there is no way to tell whose index this is. Refusing "
            f"rather than guessing. Clear it once you know what wrote it: "
            f"`redis-cli -n <index> DEL {REDIS_OWNER_KEY}`.")
    return resolved == os.path.realpath(str(root))


def redis_index_is_free(index, root, client=None):
    """Whether `root` may use a Redis index, as far as the stamp knows.

    False ONLY when the stamp names a different checkout that still exists on
    disk. Unstamped is free (nobody has claimed it, including every adopter
    that never stamps). Our own stamp is free. A stamp naming a directory that
    is gone is STALE — the checkout was deleted, so its leftover data is
    nobody's, and holding the slot forever would leak the scarcest resource
    here.

    `client` is passed straight through to `redis_owner`.

    Propagates `RedisUnreachable`; the caller decides which way to fail. Raises
    `AllocationError` on a stamp that will not resolve to a path.
    """
    owner = redis_owner(index, client=client)
    if not owner:
        return True
    if _same_checkout(owner, root):
        return True
    return not os.path.isdir(owner)


def claim_redis_index(index, root, client=None):
    """Refuse the index unless `root` may destroy what is in it.

    Called immediately before a FLUSHDB. Fails OPEN when Redis cannot be
    reached — a flush that cannot connect cannot destroy anything either — and
    hard-refuses anything below `REDIS_FIRST_INDEX`, because index 0 is the
    developer's own cache and is never ours to empty.

    **Pass the client you are about to flush.** The whole value of this check
    is that it interrogates the same server and database `flushdb()` will
    empty; without `client` it falls back to a probe against
    `MOJO_TESTENV_REDIS_HOST`/`_PORT` (localhost by default), which answers
    about a different machine entirely whenever your Redis is not there. It
    must be on `index` — a disagreement raises rather than picking one.

    Note that a supplied client also makes `MOJO_TESTENV_NO_REDIS` irrelevant
    here: that variable turns off the probe, and there is no probe to turn off.
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
        owner = redis_owner(index, client=client)
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


def stamp_redis_owner(index, root, client=None):
    """Record `root` as the owner of a Redis index. Returns True when written.

    Best effort by design. Failing a whole test run because a stamp could not
    be written costs more than the protection is worth — the run is about to
    happen either way, and the next successful stamp restores the guard.

    Refuses anything below `REDIS_FIRST_INDEX` for the same reason
    `claim_redis_index` does: index 0 is not ours to label.

    **Pass the client you just flushed**, for the mirror image of the reason
    `claim_redis_index` wants one: a probe stamps whatever server
    `MOJO_TESTENV_REDIS_HOST`/`_PORT` names, so a runner whose Redis is
    elsewhere writes a claim onto a local index it never uses — other checkouts
    then skip that index at allocation on the strength of a stamp nobody owns.

    A client that disagrees with `index` raises `AllocationError` — the one
    thing here that is not best-effort, because it is a programming error and
    not a runtime condition.
    """
    try:
        index = int(index)
    except (TypeError, ValueError):
        return False
    if index < REDIS_FIRST_INDEX:
        return False

    conn, owned = _client_for(index, client)
    if conn is None:
        return False
    try:
        conn.set(REDIS_OWNER_KEY, os.path.realpath(str(root)))
        return True
    except Exception:
        return False
    finally:
        if owned:
            try:
                conn.close()
            except Exception:
                pass


def _pick_redis_index(taken, limit, root):
    """The lowest index free in the registry AND not stamped by a live checkout.

    Fails CLOSED when the stamp cannot be read. `allocate` writes a record a
    checkout then uses for months, so one flaky probe reading all fifteen
    candidates as free would latch a collision in permanently — the opposite
    trade-off from the flush guard, which fails open.

    **This path uses the probe, and that is a real limitation, stated plainly.**
    There is no live client to borrow here: `create_testproject` calls this
    before any project exists, deriving the values the settings will go on to
    hold, and it writes localhost — so asking `MOJO_TESTENV_REDIS_HOST`/`_PORT`
    (localhost by default) is the correct question for this repo. An adopter
    whose test Redis lives on another host and who does not set those variables
    gets a probe that asks the wrong server, and therefore NO allocation-time
    protection. What they still get is the flush-time guard, which reads the
    runner's own live client — and that is the check that stands between a
    wrong index and destroyed data.
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
