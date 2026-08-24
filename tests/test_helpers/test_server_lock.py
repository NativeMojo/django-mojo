"""
Maestro item 275 — the server-restart lock that keeps th.server_settings()
from tearing down live HTTP requests and websockets.

The race itself is not reproducible on demand (it needs a settings override to
land inside another module's client-request window during a parallel full-suite
run), so these tests pin the LOCK'S INVARIANTS instead — which is what makes
the race impossible by construction:

  - a shared holder (HTTP request or websocket connection) excludes an
    exclusive holder (server_settings), and vice versa
  - shared holders do not exclude each other
  - a waiting exclusive holder blocks NEW shared holders (writer preference),
    so a stream of websocket tests cannot starve a settings override
  - every acquisition degrades on timeout instead of deadlocking

Fail-when-broken: revert testit/server_lock.py's acquire_read to ignore
_writer/_writers_waiting and test_reader_excludes_writer /
test_waiting_writer_blocks_new_readers fail.
"""
import threading
import time
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TESTIT_TIER = "bug"  # #2792 tier curation


@th.django_unit_test("server_lock: an open shared hold excludes an exclusive one")
def test_reader_excludes_writer(opts):
    from testit.server_lock import ServerRestartLock

    lock = ServerRestartLock()
    assert_true(lock.acquire_read(timeout=1.0), "the first shared hold should be granted immediately")

    # A writer must NOT get in while the reader holds it.
    assert_true(
        not lock.acquire_write(timeout=0.3),
        "an exclusive hold must be refused while a websocket connection is open",
    )

    lock.release_read()
    assert_true(
        lock.acquire_write(timeout=1.0),
        "the exclusive hold should be granted once the last shared holder releases",
    )
    lock.release_write()


@th.django_unit_test("server_lock: an exclusive hold excludes shared holds")
def test_writer_excludes_readers(opts):
    from testit.server_lock import ServerRestartLock

    lock = ServerRestartLock()
    assert_true(lock.acquire_write(timeout=1.0), "the exclusive hold should be granted on an idle lock")

    # From ANOTHER thread — the exclusive holder itself is allowed straight
    # through (it is the restarter; see test_server_conf.py), so the exclusion
    # this test pins has to be checked from a thread that is not it.
    granted = []

    def _reader():
        granted.append(lock.acquire_read(timeout=0.3))

    t = threading.Thread(target=_reader)
    t.start()
    t.join(timeout=10)
    assert_eq(
        granted, [False],
        f"a client request must not start while the server is restarting, got {granted}",
    )

    lock.release_write()
    assert_true(lock.acquire_read(timeout=1.0), "a shared hold should be granted once the restart finishes")
    lock.release_read()


@th.django_unit_test("server_lock: shared holds do not exclude each other")
def test_readers_are_concurrent(opts):
    from testit.server_lock import ServerRestartLock

    lock = ServerRestartLock()
    assert_true(lock.acquire_read(timeout=1.0), "first shared hold should be granted")
    assert_true(lock.acquire_read(timeout=1.0), "a second concurrent websocket should also be granted")
    readers, writer_held, _waiting = lock.state()
    assert_eq(readers, 2, f"both shared holders should be counted, got {readers}")
    assert_true(not writer_held, "no exclusive hold should be recorded while readers hold the lock")
    lock.release_read()
    lock.release_read()
    readers, _writer, _waiting = lock.state()
    assert_eq(readers, 0, f"reader count should return to zero after both release, got {readers}")


@th.django_unit_test("server_lock: a waiting exclusive holder blocks new shared holds (no writer starvation)")
def test_waiting_writer_blocks_new_readers(opts):
    from testit.server_lock import ServerRestartLock

    lock = ServerRestartLock()
    assert_true(lock.acquire_read(timeout=1.0), "the in-flight websocket should hold the lock")

    writer_got_it = []

    def _writer():
        writer_got_it.append(lock.acquire_write(timeout=5.0))

    t = threading.Thread(target=_writer)
    t.start()

    # Let the writer register itself as waiting.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if lock.state()[2] > 0:
            break
        time.sleep(0.01)
    assert_true(lock.state()[2] > 0, "the writer should be recorded as waiting")

    # A NEW reader must queue behind the waiting writer rather than jumping in.
    assert_true(
        not lock.acquire_read(timeout=0.3),
        "a new client request must not start while a settings override is waiting",
    )

    lock.release_read()
    t.join(timeout=5)
    assert_true(not t.is_alive(), "the writer thread should have finished once the reader released")
    assert_eq(writer_got_it, [True], f"the waiting writer should have been granted the lock, got {writer_got_it}")
    lock.release_write()


@th.django_unit_test("server_lock: acquisitions degrade on timeout instead of deadlocking")
def test_timeout_degrades(opts):
    from testit.server_lock import ServerRestartLock

    lock = ServerRestartLock()
    # Simulate a leaked websocket: a shared hold nobody releases.
    assert_true(lock.acquire_read(timeout=1.0), "the leaked shared hold should be granted")

    started = time.monotonic()
    granted = lock.acquire_write(timeout=0.5)
    elapsed = time.monotonic() - started

    assert_true(not granted, "the exclusive hold must be refused, not granted, when a reader leaked")
    assert_true(
        elapsed < 3.0,
        f"acquire_write must return promptly on timeout rather than hang, took {elapsed:.2f}s",
    )
    # And the lock is still usable afterwards — the failed writer left no residue.
    readers, writer_held, waiting = lock.state()
    assert_eq(readers, 1, f"the leaked reader should still be counted, got {readers}")
    assert_true(not writer_held, "a timed-out writer must not leave the lock marked as held")
    assert_eq(waiting, 0, f"a timed-out writer must deregister itself, got waiting={waiting}")
    lock.release_read()


@th.django_unit_test("server_lock: exclusive() context manager releases on the way out")
def test_exclusive_context_manager(opts):
    from testit import server_lock

    lock = server_lock.ServerRestartLock()
    with server_lock.exclusive(timeout=2.0, lock=lock) as acquired:
        assert_true(acquired, "the test-owned lock should be idle")
        _readers, writer_held, _waiting = lock.state()
        assert_true(writer_held, "the exclusive hold should be recorded inside the context")

    _readers, writer_held, _waiting = lock.state()
    assert_true(not writer_held, "the exclusive hold must be released when the context exits")


class _FakeElapsed:
    def total_seconds(self):
        return 0.001


class _FakeResponse:
    content = b"{}"
    status_code = 200
    ok = True
    reason = "OK"
    headers = {}
    elapsed = _FakeElapsed()

    def json(self):
        return {}


class _RecordingServerLock:
    def __init__(self, events):
        self.events = events
        self.held = False

    def acquire_shared(self):
        self.events.append("acquire")
        self.held = True
        return True

    def release_shared(self):
        self.events.append("release")
        self.held = False


class _RecordingSession:
    def __init__(self, lock, events, error=None):
        self.lock = lock
        self.events = events
        self.error = error

    def request(self, *args, **kwargs):
        assert_true(self.lock.held, "the restart lock must be held while an HTTP request is in flight")
        self.events.append("request")
        if self.error:
            raise self.error
        return _FakeResponse()


@th.django_unit_test("server_lock: RestClient holds a shared lock around every HTTP request")
def test_rest_client_holds_shared_lock(opts):
    from testit.client import RestClient

    events = []
    lock = _RecordingServerLock(events)
    client = RestClient(opts.host)
    client._server_lock = lock
    client.session = _RecordingSession(lock, events)

    response = client.get("/api/health")

    assert_eq(response.status_code, 200, "the fake HTTP request should complete")
    assert_eq(events, ["acquire", "request", "release"],
              f"the shared lock must surround the request, got {events}")


@th.django_unit_test("server_lock: RestClient releases its shared lock after a request error")
def test_rest_client_releases_shared_lock_on_error(opts):
    from testit.client import RestClient

    events = []
    lock = _RecordingServerLock(events)
    client = RestClient(opts.host)
    client._server_lock = lock
    client.session = _RecordingSession(lock, events, error=RuntimeError("simulated disconnect"))

    try:
        client.get("/api/health")
    except RuntimeError:
        pass
    else:
        assert_true(False, "the simulated request error should propagate")

    assert_eq(events, ["acquire", "request", "release"],
              f"the shared lock must be released after an error, got {events}")
