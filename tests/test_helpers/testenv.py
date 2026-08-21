"""
Per-checkout test isolation (testit/testenv.py) — the parallel-safe remainder.

The bulk of this module moved to tests/test_helpers_extended_serial/testenv.py:
those tests patch testit.testenv module attributes (via `_isolated_registry`)
and write os.environ, process-global mutations that are unsafe under the
parallel default tier (maestro item #1839). What stays reads the allocator's
pure functions, binds throwaway sockets, or runs allocation in a subprocess —
nothing shared is touched.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile

from testit import helpers as th


# The literal key, spelled out rather than imported: these tests have to run
# against code that does not define it yet, and the exact string is a
# cross-repo contract (maestro's runner writes the same one).
_OWNER_KEY = "testenv:owner"


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


@th.django_unit_test("allocate() imports nothing from mojo")
def test_allocate_imports_nothing_from_mojo(opts):
    """The invariant that makes this module callable from a settings module.

    `allocate()` runs before the project exists (`create_testproject`, by file
    path) and from half-built settings modules in adopting repos. Anything it
    imports out of `mojo` either explodes there or — worse — resolves a Django
    setting and caches a process-global Redis client pinned to index 0.

    A SUBPROCESS, because that is the only way to see the import graph of a
    fresh interpreter, and because it is how production actually invokes this.
    """
    from testit import testenv

    testit_dir = os.path.dirname(os.path.abspath(testenv.__file__))
    tmpdir = tempfile.mkdtemp(prefix="testenv-subproc-")
    registry = os.path.join(tmpdir, "testenv.json")
    root = tempfile.mkdtemp(prefix="testenv-root-")

    script = (
        "import json, sys\n"
        f"sys.path.insert(0, {testit_dir!r})\n"
        "import testenv\n"
        f"testenv.REGISTRY_DIR = {tmpdir!r}\n"
        f"testenv.REGISTRY_PATH = {registry!r}\n"
        f"record = testenv.allocate({root!r}, 'probe')\n"
        "leaked = sorted(m for m in sys.modules if m.startswith('mojo'))\n"
        "assert not leaked, 'allocate() imported mojo modules: %s' % (leaked,)\n"
        "print(json.dumps(record))\n"
    )

    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=120)

    assert proc.returncode == 0, \
        f"the allocation subprocess failed (rc={proc.returncode}): {proc.stderr}"
    assert not proc.stderr.strip(), \
        f"the allocation subprocess wrote to stderr: {proc.stderr}"

    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload.get("redis_index"), \
        f"the subprocess did not allocate a redis index: {payload}"


@th.django_unit_test("the ownership key is the one every mojo repo uses")
def test_owner_key_is_the_shared_one(opts):
    """Two repos with different key names are blind to each other's stamps,
    which is the exact collision this mechanism exists to prevent. maestro's
    runner writes this literal string."""
    from testit import testenv

    assert testenv.REDIS_OWNER_KEY == _OWNER_KEY, \
        (f"the ownership key changed to {testenv.REDIS_OWNER_KEY!r} — every "
         f"other repo on the machine still writes {_OWNER_KEY!r}")
