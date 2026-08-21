"""
Per-checkout test isolation (testit/testenv.py).

Moved out of tests/test_helpers/testenv.py: every test here runs through
`_isolated_registry()`, which patches testit.testenv module attributes
(REGISTRY_DIR/REGISTRY_PATH/_redis_client and friends), and several also write
os.environ — process-global mutations visible to every parallel module — so
they run only in the opt-in serial tier (maestro item #1839). The allocator's
env-reading functions (`redis_limit`, `probing_disabled`) take no parameters,
so there is no seam to convert through. The tests that touch no shared state
stayed behind.

These pin the properties that make a worktree safe. Most of them are about
things NOT happening — two checkouts not sharing an index, a released slot not
lingering, index 0 never being handed out — which is exactly the class that
rots silently, since the symptom of getting it wrong is another suite's data
disappearing rather than an error here.

Every test runs against a temporary registry, so nothing touches the real
~/.mojo/testenv.json.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from unittest import mock

from testit import helpers as th


# The ownership stamps every test in this file reads and writes, keyed
# (redis index, key). A dict, so nothing here can touch a real Redis database
# or flake when one is not running.
_FAKE_REDIS = {}

# The literal key, spelled out rather than imported: these tests have to run
# against code that does not define it yet, and the exact string is a
# cross-repo contract (maestro's runner writes the same one).
_OWNER_KEY = "testenv:owner"


class _FakePool:
    """Just enough of a redis-py connection pool to read the database off."""

    def __init__(self, index):
        self.connection_kwargs = {"db": index}


class _FakeRedis:
    """A dict-backed stand-in for one Redis database index.

    `store` defaults to the shared `_FAKE_REDIS`, which stands in for "the
    local server every probe talks to". Handing over a separate dict models a
    DIFFERENT SERVER on the same index — the case where the runner's Redis is
    somewhere other than the localhost a probe resolves to.

    `connection_pool` mirrors redis-py closely enough that anything deriving
    the live database index off a client (bin/testit.py's `_live_redis_index`,
    testenv's `_client_db`) works against this.
    """

    def __init__(self, index, store=None):
        self.index = index
        self.store = _FAKE_REDIS if store is None else store
        self.connection_pool = _FakePool(index)

    def get(self, key):
        return self.store.get((self.index, key))

    def set(self, key, value):
        self.store[(self.index, key)] = value
        return True

    def close(self):
        pass


def _stamp(index, path):
    """Mark a Redis index as owned by a checkout, as the runner would."""
    _FAKE_REDIS[(index, _OWNER_KEY)] = os.path.realpath(str(path))


def _isolated_registry():
    """Point the allocator at a throwaway registry and a fake Redis."""
    from testit import testenv

    tmpdir = tempfile.mkdtemp(prefix="testenv-")
    _FAKE_REDIS.clear()
    patches = [
        mock.patch.object(testenv, "REGISTRY_DIR", tmpdir),
        mock.patch.object(testenv, "REGISTRY_PATH",
                          os.path.join(tmpdir, "testenv.json")),
        # create=True so these run against code that has no probe yet — a
        # regression has to FAIL while broken, not error at patch time.
        mock.patch.object(testenv, "_redis_client", create=True,
                          side_effect=lambda index: _FakeRedis(index)),
    ]
    for patch in patches:
        patch.start()
    return patches, tmpdir


def _restore(patches):
    for patch in reversed(patches):
        patch.stop()
    _FAKE_REDIS.clear()


@th.django_unit_test("allocation is idempotent for one checkout")
def test_allocate_is_idempotent(opts):
    """A re-run of create_testproject must not move a tree to a new database
    and orphan the old one."""
    from testit import testenv

    patches, tmpdir = _isolated_registry()
    try:
        first = testenv.allocate("/tmp/repo-a", "mojo_test")
        second = testenv.allocate("/tmp/repo-a", "mojo_test")

        assert first["db_name"] == second["db_name"], "the database name moved"
        assert first["redis_index"] == second["redis_index"], "the redis index moved"
        assert first["port"] == second["port"], "the port moved"
    finally:
        _restore(patches)


@th.django_unit_test("two checkouts never share a database, index or port")
def test_allocations_are_distinct(opts):
    from testit import testenv

    patches, tmpdir = _isolated_registry()
    try:
        a = testenv.allocate("/tmp/repo-a", "mojo_test")
        b = testenv.allocate("/tmp/repo-b", "mojo_test")

        assert a["db_name"] != b["db_name"], \
            "two checkouts share a database — one would DROP the other's"
        assert a["redis_index"] != b["redis_index"], \
            "two checkouts share a Redis index — flushdb() would wipe the other"
        assert a["port"] != b["port"], "two checkouts share a port"
    finally:
        _restore(patches)


@th.django_unit_test("the same base name in two checkouts still yields distinct DBs")
def test_same_base_name_is_disambiguated(opts):
    """This is the worktree case: identical project, identical base name, and
    the ONLY thing that differs is the path."""
    from testit import testenv

    patches, tmpdir = _isolated_registry()
    try:
        main = testenv.allocate("/tmp/proj", "mojo_test")
        tree = testenv.allocate("/tmp/proj-worktree", "mojo_test")

        assert main["db_name"] != tree["db_name"], \
            "a worktree inherited its parent's database name"
        assert main["db_name"].startswith("mojo_test_"), \
            f"the base name was lost: {main['db_name']}"
    finally:
        _restore(patches)


@th.django_unit_test("Redis index 0 is never allocated")
def test_redis_index_zero_is_reserved(opts):
    """Index 0 is what a project that has NOT adopted this gets from
    REDIS_DB_INDEX's default. Leaving it unallocated means an adopted checkout
    can never collide with an unadopted one during the migration."""
    from testit import testenv

    patches, tmpdir = _isolated_registry()
    try:
        for index in range(5):
            record = testenv.allocate(f"/tmp/repo-{index}", "mojo_test")
            assert record["redis_index"] != 0, \
                "index 0 was allocated — it belongs to not-yet-adopted projects"
    finally:
        _restore(patches)


@th.django_unit_test("a released slot is reclaimed and handed out again")
def test_release_reclaims(opts):
    from testit import testenv

    patches, tmpdir = _isolated_registry()
    try:
        first = testenv.allocate("/tmp/repo-a", "mojo_test")
        assert testenv.release("/tmp/repo-a"), "release reported nothing removed"
        assert not testenv.release("/tmp/repo-a"), \
            "releasing twice reported a second removal"

        reused = testenv.allocate("/tmp/repo-b", "mojo_test")
        assert reused["redis_index"] == first["redis_index"], \
            "a freed Redis index was not reused — the 16 available would leak away"
    finally:
        _restore(patches)


@th.django_unit_test("prune drops allocations whose checkout is gone")
def test_prune(opts):
    """Deleting a worktree leaves its slot held, and Redis indexes are the
    scarce resource."""
    from testit import testenv

    patches, tmpdir = _isolated_registry()
    try:
        alive = tempfile.mkdtemp(prefix="testenv-alive-")
        dead = tempfile.mkdtemp(prefix="testenv-dead-")
        testenv.allocate(alive, "mojo_test")
        testenv.allocate(dead, "mojo_test")
        os.rmdir(dead)

        gone = testenv.prune()
        paths = [record["path"] for record in gone]
        assert os.path.realpath(dead) in paths, \
            f"a deleted checkout was not pruned: {paths}"
        assert os.path.realpath(alive) in testenv.allocations(), \
            "prune removed a live checkout"

        # The slot comes back but the Postgres database does not, so prune has
        # to hand back enough to clean it up. Without the name, orphaned
        # databases accumulate with nothing ever pointing at them.
        assert all(record.get("db_name") for record in gone), \
            f"prune did not report the orphaned database name: {gone}"
    finally:
        _restore(patches)


@th.django_unit_test("the port allocator skips a port that is actually bound")
def test_port_skips_a_bound_socket(opts):
    """Registry bookkeeping is not enough — a port can be held by something
    that never asked us.

    The held port is an EPHEMERAL one the OS hands out, not `PORT_BASE`.
    Binding `PORT_BASE` directly is what the first version did, and it failed
    for a reason worth recording: this checkout is itself allocated 5600, so
    the live test server is holding it while these tests run. The test then
    died on its own `bind()` before reaching an assertion.
    """
    from testit import testenv

    patches, tmpdir = _isolated_registry()
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Port 0 -> the OS picks something genuinely free right now.
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        held = holder.getsockname()[1]

        assert not testenv.port_is_free(held), \
            "a bound port reported as free — the allocator would hand it out"

        # Start the search AT the held port, so skipping it is the only way to
        # return anything else.
        with mock.patch.object(testenv, "PORT_BASE", held):
            record = testenv.allocate("/tmp/repo-a", "mojo_test")

        assert record["port"] != held, \
            "the allocator handed out a port that is already bound"
    finally:
        holder.close()
        _restore(patches)


@th.django_unit_test("running out of Redis indexes FAILS rather than reusing one")
def test_exhausted_redis_fails_closed(opts):
    """Silently reusing an index is the whole bug this module exists to
    prevent, so exhaustion must be an error with a fix in the message."""
    from testit import testenv

    patches, tmpdir = _isolated_registry()
    try:
        # A tiny server: indexes 1..3 usable, so the fourth checkout has none.
        with mock.patch.object(testenv, "redis_limit", return_value=4):
            for index in range(3):
                testenv.allocate(f"/tmp/repo-{index}", "mojo_test")

            err = None
            try:
                testenv.allocate("/tmp/repo-overflow", "mojo_test")
            except testenv.AllocationError as caught:
                err = caught

        assert err is not None, \
            "a fourth checkout was allocated an index that was already taken"
        assert "redis.conf" in str(err), \
            f"the exhaustion error does not say how to fix it: {err}"
    finally:
        _restore(patches)


@th.django_unit_test("allocate() accepts an explicit Redis ceiling")
def test_allocate_accepts_an_explicit_limit(opts):
    """A caller that already knows the ceiling can hand it over, so adopting
    repos need neither the environment variable nor a probe."""
    from testit import testenv

    patches, tmpdir = _isolated_registry()
    try:
        # limit=4 -> indexes 1..3 usable, so exactly three checkouts fit.
        for index in range(3):
            record = testenv.allocate(f"/tmp/repo-{index}", "mojo_test", limit=4)
            assert record["redis_index"] in (1, 2, 3), \
                f"an explicit limit=4 handed out index {record['redis_index']}"

        err = None
        try:
            testenv.allocate("/tmp/repo-overflow", "mojo_test", limit=4)
        except testenv.AllocationError as caught:
            err = caught

        assert err is not None, \
            "an explicit limit was ignored — a fourth checkout got an index anyway"
    finally:
        _restore(patches)


@th.django_unit_test("the Redis ceiling comes from MOJO_TESTENV_REDIS_LIMIT")
def test_redis_limit_honors_the_env_var(opts):
    """Raising `databases` in redis.conf does nothing on its own now — the
    allocator has to be told, because asking the server is what dragged Django
    and a process-global Redis client into a settings module."""
    from testit import testenv

    with mock.patch.dict(os.environ, {"MOJO_TESTENV_REDIS_LIMIT": "24"}):
        assert testenv.redis_limit() == 24, \
            "MOJO_TESTENV_REDIS_LIMIT was ignored — a raised redis.conf is unreachable"

    with mock.patch.dict(os.environ, {"MOJO_TESTENV_REDIS_LIMIT": "sixty-four"}):
        assert testenv.redis_limit() == testenv.REDIS_FALLBACK_LIMIT, \
            "a junk MOJO_TESTENV_REDIS_LIMIT did not fall back to the default"

    with mock.patch.dict(os.environ):
        os.environ.pop("MOJO_TESTENV_REDIS_LIMIT", None)
        assert testenv.redis_limit() == testenv.REDIS_FALLBACK_LIMIT, \
            "an unset MOJO_TESTENV_REDIS_LIMIT changed the shipped default"


@th.django_unit_test("an absurd MOJO_TESTENV_REDIS_LIMIT is clamped, not obeyed")
def test_redis_limit_is_clamped(opts):
    """`_pick_redis_index` opens a fresh client per candidate index, so an
    accidental extra digit does not fail — it HANGS, spending hours connecting
    to indexes no server has. Clamped rather than rejected, so the typo still
    allocates instead of blocking every suite on the machine."""
    from testit import testenv

    assert testenv.REDIS_MAX_LIMIT >= testenv.REDIS_FALLBACK_LIMIT, \
        (f"the clamp ({testenv.REDIS_MAX_LIMIT}) is below Redis's own shipped "
         f"default ({testenv.REDIS_FALLBACK_LIMIT}) — it would cost real slots")

    with mock.patch.dict(os.environ, {"MOJO_TESTENV_REDIS_LIMIT": "99999999"}):
        assert testenv.redis_limit() == testenv.REDIS_MAX_LIMIT, \
            (f"an absurd Redis ceiling was taken verbatim "
             f"({testenv.redis_limit()}) — a cold allocation would hang for "
             f"hours rather than error")


@th.django_unit_test("a corrupt registry is rebuilt rather than wedging every suite")
def test_corrupt_registry_recovers(opts):
    """The values are derived, so losing them costs a different slot next run —
    not a broken machine."""
    from testit import testenv

    patches, tmpdir = _isolated_registry()
    try:
        testenv.allocate("/tmp/repo-a", "mojo_test")
        with open(testenv.REGISTRY_PATH, "w") as handle:
            handle.write("{not json at all")

        record = testenv.allocate("/tmp/repo-b", "mojo_test")
        assert record["db_name"], "a corrupt registry blocked allocation"
    finally:
        _restore(patches)


@th.django_unit_test("a reset registry never hands out a live checkout's index")
def test_registry_reset_does_not_hand_out_a_live_index(opts):
    """The headline case. The registry is one JSON file in a home directory and
    every way it can be lost — deleted, truncated by a killed writer, rebuilt
    after a bad parse — reads as "nothing is allocated". A port survives that
    because the allocator BINDS before handing one out; a Redis index has to
    ask the index itself who owns it."""
    from testit import testenv

    patches, tmpdir = _isolated_registry()
    alive = tempfile.mkdtemp(prefix="testenv-alive-")
    other = tempfile.mkdtemp(prefix="testenv-other-")
    try:
        first = testenv.allocate(alive, "mojo_test")
        _stamp(first["redis_index"], alive)

        # The registry is gone. The checkout using that index is not.
        with open(testenv.REGISTRY_PATH, "w") as handle:
            handle.write("")

        second = testenv.allocate(other, "mojo_test")
        assert second["redis_index"] != first["redis_index"], \
            ("a reset registry handed a live checkout's Redis index to another "
             "tree — the next flushdb() would wipe that suite mid-run")
    finally:
        _restore(patches)
        shutil.rmtree(alive, ignore_errors=True)
        shutil.rmtree(other, ignore_errors=True)


@th.django_unit_test("a stale owner stamp is reclaimed, not honoured forever")
def test_a_stale_owner_stamp_is_reclaimed(opts):
    """A deleted worktree leaves its stamp behind. Honouring it would poison
    the slot permanently, and Redis indexes are the scarce resource."""
    from testit import testenv

    patches, tmpdir = _isolated_registry()
    dead = tempfile.mkdtemp(prefix="testenv-dead-")
    live = tempfile.mkdtemp(prefix="testenv-live-")
    try:
        os.rmdir(dead)
        _stamp(testenv.REDIS_FIRST_INDEX, dead)

        record = testenv.allocate(live, "mojo_test")
        assert record["redis_index"] == testenv.REDIS_FIRST_INDEX, \
            ("a stamp naming a checkout that no longer exists blocked the "
             f"index anyway: got {record['redis_index']}")
    finally:
        _restore(patches)
        shutil.rmtree(live, ignore_errors=True)


@th.django_unit_test("the flush guard refuses an index a live checkout owns")
def test_claim_refuses_a_live_foreign_owner(opts):
    from testit import testenv

    patches, tmpdir = _isolated_registry()
    owner = tempfile.mkdtemp(prefix="testenv-owner-")
    mine = tempfile.mkdtemp(prefix="testenv-mine-")
    try:
        _stamp(3, owner)

        err = None
        try:
            testenv.claim_redis_index(3, mine)
        except testenv.AllocationError as caught:
            err = caught

        assert err is not None, \
            "the guard allowed a FLUSHDB on another live checkout's index"
        assert os.path.realpath(owner) in str(err), \
            f"the refusal does not name the checkout that owns it: {err}"
        assert "prune" in str(err), \
            f"the refusal does not say how to fix it: {err}"
    finally:
        _restore(patches)
        shutil.rmtree(owner, ignore_errors=True)
        shutil.rmtree(mine, ignore_errors=True)


@th.django_unit_test("the flush guard allows own, unowned and stale indexes")
def test_claim_allows_own_unowned_and_stale(opts):
    """The negative case above is only meaningful if the guard is not simply
    refusing everything — that would block every run on the machine."""
    from testit import testenv

    patches, tmpdir = _isolated_registry()
    mine = tempfile.mkdtemp(prefix="testenv-mine-")
    dead = tempfile.mkdtemp(prefix="testenv-dead-")
    try:
        os.rmdir(dead)

        assert testenv.claim_redis_index(1, mine), \
            "an unstamped index was refused — no adopter that never stamps could run"

        _stamp(2, mine)
        assert testenv.claim_redis_index(2, mine), \
            "this checkout was refused its own index"

        _stamp(3, dead)
        assert testenv.claim_redis_index(3, mine), \
            "an index stamped by a deleted checkout stayed blocked forever"
    finally:
        _restore(patches)
        shutil.rmtree(mine, ignore_errors=True)


@th.django_unit_test("the flush guard never allows Redis index 0")
def test_claim_refuses_index_zero(opts):
    """Index 0 is the developer's own cache. testenv never allocates it, so
    being pointed at it means something is misconfigured — and the action being
    guarded is FLUSHDB."""
    from testit import testenv

    patches, tmpdir = _isolated_registry()
    mine = tempfile.mkdtemp(prefix="testenv-mine-")
    try:
        err = None
        try:
            testenv.claim_redis_index(0, mine)
        except testenv.AllocationError as caught:
            err = caught
        assert err is not None, \
            "index 0 was claimable — a run would flush the developer's own cache"
    finally:
        _restore(patches)
        shutil.rmtree(mine, ignore_errors=True)


@th.django_unit_test("the flush guard reads the client it was handed, not a probe")
def test_claim_uses_the_supplied_client(opts):
    """The guard is only worth anything if it interrogates the server that
    `flushdb()` is about to empty.

    The probe resolves host and port from MOJO_TESTENV_REDIS_HOST/_PORT,
    defaulting to 127.0.0.1:6379. The live client comes from REDIS_URL and can
    point anywhere — maestro sets it. So a guard that builds its own probe
    reads index N on the LOCAL server, finds it unstamped, and lets the run
    flush index N on the REMOTE one regardless of who owns it.
    """
    from testit import testenv

    patches, tmpdir = _isolated_registry()
    owner = tempfile.mkdtemp(prefix="testenv-owner-")
    mine = tempfile.mkdtemp(prefix="testenv-mine-")
    probes = []
    try:
        # A separate store is a separate server: index 4 is owned there, and
        # unstamped on the local one any probe would reach.
        remote = {}
        live = _FakeRedis(4, store=remote)
        live.set(_OWNER_KEY, os.path.realpath(owner))

        def _probe(index):
            probes.append(index)
            return _FakeRedis(index)

        with mock.patch.object(testenv, "_redis_client", create=True,
                               side_effect=_probe):
            err = None
            try:
                testenv.claim_redis_index(4, mine, client=live)
            except testenv.AllocationError as caught:
                err = caught

        assert not probes, \
            (f"the guard built its own probe client for index(es) {probes} "
             f"instead of using the live client it was handed — it would read "
             f"the ownership stamp on one server while flushdb() empties "
             f"another")
        assert err is not None, \
            ("the guard allowed a FLUSHDB on an index the supplied client "
             "shows belongs to another live checkout")
        assert os.path.realpath(owner) in str(err), \
            f"the refusal does not name the checkout that owns it: {err}"
    finally:
        _restore(patches)
        shutil.rmtree(owner, ignore_errors=True)
        shutil.rmtree(mine, ignore_errors=True)


@th.django_unit_test("MOJO_TESTENV_NO_REDIS cannot disarm the flush guard")
def test_claim_with_a_supplied_client_ignores_no_redis_env(opts):
    """The variable is documented for an image with no Redis at all.

    That rationale cannot reach the flush path, which runs only when the runner
    IS holding a client it is about to flush. Letting it through made
    `export MOJO_TESTENV_NO_REDIS=1` a way to FLUSHDB another checkout's index
    with no guard at all — and, second-order, to leave the index unstamped
    afterwards so the next allocation hands it to a third tree.
    """
    from testit import testenv

    patches, tmpdir = _isolated_registry()
    owner = tempfile.mkdtemp(prefix="testenv-owner-")
    mine = tempfile.mkdtemp(prefix="testenv-mine-")
    try:
        live = _FakeRedis(5)
        live.set(_OWNER_KEY, os.path.realpath(owner))

        err = None
        with mock.patch.dict(os.environ, {"MOJO_TESTENV_NO_REDIS": "1"}):
            try:
                testenv.claim_redis_index(5, mine, client=live)
            except testenv.AllocationError as caught:
                err = caught

        assert err is not None, \
            ("MOJO_TESTENV_NO_REDIS switched the pre-FLUSHDB guard off — the "
             "run would have wiped a live checkout's Redis index unguarded")
    finally:
        _restore(patches)
        shutil.rmtree(owner, ignore_errors=True)
        shutil.rmtree(mine, ignore_errors=True)


@th.django_unit_test("a client whose database disagrees with the index is refused")
def test_claim_rejects_a_client_whose_db_disagrees(opts):
    """Checking one database while flushing another is the exact failure the
    guard exists to prevent, so a caller that supplies both and has them
    disagree is told, not quietly resolved in one direction."""
    from testit import testenv

    patches, tmpdir = _isolated_registry()
    mine = tempfile.mkdtemp(prefix="testenv-mine-")
    try:
        err = None
        try:
            testenv.claim_redis_index(5, mine, client=_FakeRedis(3))
        except testenv.AllocationError as caught:
            err = caught

        assert err is not None, \
            ("a client on database 3 was accepted as the ownership check for "
             "index 5 — the guard would clear index 5 by reading index 3")
    finally:
        _restore(patches)
        shutil.rmtree(mine, ignore_errors=True)


@th.django_unit_test("an owner stamp that will not resolve fails closed")
def test_an_unusable_owner_stamp_fails_closed(opts):
    """The stamp is arbitrary bytes written by another process.

    Most hostile values resolve fine and then read as "not me" — `../../..`,
    `~/`, empty, whitespace. A NUL byte does not: `realpath` raises ValueError,
    which sailed past every caller (the runner catches only AllocationError)
    and killed the run on a traceback instead of the message written for
    exactly this situation.
    """
    from testit import testenv

    patches, tmpdir = _isolated_registry()
    mine = tempfile.mkdtemp(prefix="testenv-mine-")
    try:
        _FAKE_REDIS[(6, _OWNER_KEY)] = "/tmp/whatever\x00wrote-this"

        err = None
        try:
            testenv.claim_redis_index(6, mine)
        except testenv.AllocationError as caught:
            err = caught

        assert err is not None, \
            ("a stamp that will not resolve to a path did not raise "
             "AllocationError — the runner cannot report it, and an unknown "
             "owner must never read as no owner")
    finally:
        _restore(patches)
        shutil.rmtree(mine, ignore_errors=True)


@th.django_unit_test("allocation fails closed when Redis cannot be reached")
def test_allocation_fails_closed_when_redis_cannot_be_reached(opts):
    """Allocation writes a record a checkout uses for months. One unanswerable
    probe must not read as "all fifteen indexes are free"."""
    from testit import testenv

    patches, tmpdir = _isolated_registry()
    try:
        with mock.patch.object(testenv, "_redis_client", create=True,
                               return_value=None):
            err = None
            try:
                testenv.allocate("/tmp/repo-a", "mojo_test")
            except testenv.AllocationError as caught:
                err = caught

        assert err is not None, \
            "an unreachable Redis read as 'every index is free' and allocated one"
        assert "MOJO_TESTENV_NO_REDIS" in str(err), \
            f"the error does not document the one supported opt-out: {err}"
    finally:
        _restore(patches)


@th.django_unit_test("MOJO_TESTENV_NO_REDIS allocates without the probe")
def test_no_redis_env_var_allows_allocation(opts):
    """The documented escape for a CI image with no Redis at all."""
    from testit import testenv

    patches, tmpdir = _isolated_registry()
    try:
        with mock.patch.dict(os.environ, {"MOJO_TESTENV_NO_REDIS": "1"}):
            with mock.patch.object(testenv, "_redis_client", create=True,
                                   return_value=None):
                record = testenv.allocate("/tmp/repo-a", "mojo_test")

        assert record["redis_index"], \
            "MOJO_TESTENV_NO_REDIS did not turn the ownership probe off"
    finally:
        _restore(patches)


@th.django_unit_test("a corrupt registry is kept, not silently overwritten")
def test_a_corrupt_registry_is_preserved(opts):
    """Rebuilding empty is right, but the rebuild is written straight back — so
    one bad parse used to destroy every repo's record on the machine with
    nothing left to look at."""
    from testit import testenv

    patches, tmpdir = _isolated_registry()
    try:
        testenv.allocate("/tmp/repo-a", "mojo_test")
        junk = "{not json at all"
        with open(testenv.REGISTRY_PATH, "w") as handle:
            handle.write(junk)

        testenv.allocate("/tmp/repo-b", "mojo_test")

        saved = [name for name in os.listdir(tmpdir)
                 if name.startswith("testenv.json.corrupt-")]
        assert saved, \
            f"a corrupt registry was discarded with no copy kept: {os.listdir(tmpdir)}"
        with open(os.path.join(tmpdir, saved[0])) as handle:
            kept = handle.read()
        assert kept == junk, \
            f"the preserved copy is not what was on disk: {kept!r}"
    finally:
        _restore(patches)


@th.django_unit_test("a corrupt registry is preserved once, not once per read")
def test_a_corrupt_registry_is_preserved_once(opts):
    """Read-only callers never rewrite the registry — `allocations()`, the
    CLI's `list`, and the extra `allocations()` inside the `prune` CLI — so an
    unfixed corrupt file is re-read on every single invocation. A timestamped
    copy per read grows without bound in the user's home directory until
    somebody happens to look."""
    from testit import testenv

    patches, tmpdir = _isolated_registry()
    try:
        testenv.allocate("/tmp/repo-a", "mojo_test")
        junk = "{not json at all"
        with open(testenv.REGISTRY_PATH, "w") as handle:
            handle.write(junk)

        for _ in range(3):
            testenv.allocations()

        saved = [name for name in os.listdir(tmpdir)
                 if name.startswith("testenv.json.corrupt-")]
        assert len(saved) == 1, \
            (f"three read-only reads of ONE corrupt registry left {len(saved)} "
             f"copies behind: {sorted(saved)}")
        with open(os.path.join(tmpdir, saved[0])) as handle:
            kept = handle.read()
        assert kept == junk, \
            f"the preserved copy is not what was on disk: {kept!r}"
    finally:
        _restore(patches)


@th.django_unit_test("prune reports which project each reclaimed slot was for")
def test_prune_reports_the_project_each_slot_belonged_to(opts):
    """prune is machine-global. Reclaiming another repo's slot is fine and
    intended; doing it without saying so is not."""
    from testit import testenv

    patches, tmpdir = _isolated_registry()
    try:
        first = tempfile.mkdtemp(prefix="testenv-one-")
        second = tempfile.mkdtemp(prefix="testenv-two-")
        testenv.allocate(first, "mojo_test")
        testenv.allocate(second, "mojo_maestro_test")
        os.rmdir(first)
        os.rmdir(second)

        gone = testenv.prune()
        names = sorted(record.get("base_name") for record in gone)
        assert names == ["mojo_maestro_test", "mojo_test"], \
            f"prune did not report the project each slot belonged to: {gone}"
    finally:
        _restore(patches)


@th.django_unit_test("allocating with no base name is refused")
def test_base_name_required(opts):
    from testit import testenv

    patches, tmpdir = _isolated_registry()
    try:
        err = None
        try:
            testenv.allocate("/tmp/repo-a", "")
        except testenv.AllocationError as caught:
            err = caught
        assert err is not None, \
            "an empty base name was accepted — the database would be named '_<slug>'"
    finally:
        _restore(patches)
