"""
Per-checkout test isolation (testit/testenv.py).

These pin the properties that make a worktree safe. Most of them are about
things NOT happening — two checkouts not sharing an index, a released slot not
lingering, index 0 never being handed out — which is exactly the class that
rots silently, since the symptom of getting it wrong is another suite's data
disappearing rather than an error here.

Every test runs against a temporary registry, so nothing touches the real
~/.mojo/testenv.json.
"""

import os
import socket
import tempfile
from unittest import mock

from testit import helpers as th


def _isolated_registry():
    """Point the allocator at a throwaway registry."""
    from testit import testenv

    tmpdir = tempfile.mkdtemp(prefix="testenv-")
    patches = [
        mock.patch.object(testenv, "REGISTRY_DIR", tmpdir),
        mock.patch.object(testenv, "REGISTRY_PATH",
                          os.path.join(tmpdir, "testenv.json")),
    ]
    for patch in patches:
        patch.start()
    return patches, tmpdir


def _restore(patches):
    for patch in reversed(patches):
        patch.stop()


@th.django_unit_test("a slug is stable for a path and differs between paths")
def test_slug_is_path_derived(opts):
    from testit import testenv

    first = testenv.slug("/tmp/alpha")
    assert first == testenv.slug("/tmp/alpha"), \
        "the same path produced two different slugs — allocations would not be stable"
    assert first != testenv.slug("/tmp/beta"), \
        "two different paths produced the same slug"
    assert len(first) == 8, f"unexpected slug length: {first}"


@th.django_unit_test("a slug resolves symlinks, so one tree gets one slot")
def test_slug_uses_realpath(opts):
    """/var and /private/var are the same directory on macOS. Hashing the
    unresolved path would allocate two slots to one checkout."""
    from testit import testenv

    real = tempfile.mkdtemp(prefix="testenv-real-")
    link = real + "-link"
    os.symlink(real, link)
    try:
        assert testenv.slug(link) == testenv.slug(real), \
            "a symlinked path got a different slug than its target"
    finally:
        os.unlink(link)


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
        assert os.path.realpath(dead) in gone, \
            f"a deleted checkout was not pruned: {gone}"
        assert os.path.realpath(alive) in testenv.allocations(), \
            "prune removed a live checkout"
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


@th.django_unit_test("a free port reports as free")
def test_port_is_free_positive(opts):
    """The negative case above is only meaningful if the check is not simply
    always returning False."""
    from testit import testenv

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    assert testenv.port_is_free(port), \
        "a port with nothing on it reported as busy"


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
