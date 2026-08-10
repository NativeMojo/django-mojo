"""
Keeps server-restarting operations away from live test-client requests.

WHY THIS EXISTS
---------------
`th.server_settings()` deliberately RESTARTS the asgi_local server: it writes
overrides into var/django.conf and relies on uvicorn's
``--reload-include '*.conf'`` to pick them up. A reload kills the worker
process — and every in-flight HTTP or websocket request along with it. An
ordinary request then fails with RemoteDisconnected, while a test holding a
socket sees "Connection is already closed."

The test runner executes modules as THREADS inside one process
(``testit/runner.py`` uses ThreadPoolExecutor), so a plain in-process
reader/writer lock is enough to keep the two apart:

  - an HTTP request or websocket connection holds the lock SHARED while active
  - server_settings() holds it EXCLUSIVE for its whole duration

Many client requests still run concurrently with each other; a settings
override simply waits for active requests to finish and holds off new ones
until the server is back up.

One exception, and it is not a loophole: the thread that already holds the
exclusive hold gets a shared hold immediately. Making an HTTP request or
opening a websocket INSIDE a ``th.server_settings()`` body is normal, and that
thread is the restarter — it cannot tear down its own request, so waiting would
only stall it for the full reader timeout.

FAILSAFE
--------
Every acquisition takes a timeout and DEGRADES instead of deadlocking: on
timeout the caller proceeds anyway, which is exactly the behavior before this
lock existed. A leaked websocket (a test that never calls ``close()``) can
therefore delay a settings override, but can never hang the suite.

Writer-preference: readers queue behind a waiting writer, so a steady stream
of websocket tests cannot starve a pending server_settings() indefinitely.
"""
import threading
import time
from contextlib import contextmanager

# A settings override restarts the server twice (apply + restore), each with a
# sleep plus a 10s poll, so a writer may legitimately hold the lock a while.
WRITER_TIMEOUT = 60.0
# Client requests are short; if one has held the lock this long it has leaked.
READER_TIMEOUT = 30.0


class ServerRestartLock:
    """Writer-preferring reader/writer lock with degrade-on-timeout semantics."""

    def __init__(self):
        self._cond = threading.Condition()
        self._readers = 0
        self._writer = False
        self._writers_waiting = 0
        self._writer_thread = None

    def acquire_read(self, timeout=READER_TIMEOUT):
        """Returns True if the shared hold was acquired, False on timeout."""
        deadline = time.monotonic() + timeout
        with self._cond:
            # The thread that already holds the exclusive hold is the one doing
            # the restarting; a websocket IT opens is not the hazard this lock
            # guards against, and making it queue behind itself is a guaranteed
            # stall for the full reader timeout. Common shape:
            #     with th.server_settings(...):
            #         ws = WsClient(...); ws.connect()
            if self._writer and self._writer_thread == threading.get_ident():
                self._readers += 1
                return True
            while self._writer or self._writers_waiting > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            self._readers += 1
            return True

    def release_read(self):
        with self._cond:
            if self._readers > 0:
                self._readers -= 1
            if self._readers == 0:
                self._cond.notify_all()

    def acquire_write(self, timeout=WRITER_TIMEOUT):
        """Returns True if the exclusive hold was acquired, False on timeout."""
        deadline = time.monotonic() + timeout
        with self._cond:
            self._writers_waiting += 1
            try:
                while self._writer or self._readers > 0:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._cond.wait(remaining)
                self._writer = True
                self._writer_thread = threading.get_ident()
                return True
            finally:
                self._writers_waiting -= 1
                self._cond.notify_all()

    def release_write(self):
        with self._cond:
            self._writer = False
            self._writer_thread = None
            self._cond.notify_all()

    def state(self):
        """(readers, writer_held, writers_waiting) — for diagnostics/tests."""
        with self._cond:
            return (self._readers, self._writer, self._writers_waiting)


# One lock per process; the runner is single-process by design.
LOCK = ServerRestartLock()


def acquire_shared(timeout=READER_TIMEOUT):
    return LOCK.acquire_read(timeout=timeout)


def release_shared():
    LOCK.release_read()


@contextmanager
def exclusive(timeout=WRITER_TIMEOUT, logger=None, lock=None):
    """Hold the server exclusively — no websocket may be open across this."""
    target = lock or LOCK
    acquired = target.acquire_write(timeout=timeout)
    if not acquired and logger:
        readers, _writer, _waiting = target.state()
        logger.warning(
            f"[server_lock] proceeding without the exclusive hold after {timeout}s "
            f"({readers} client request(s) still active) — the caller is about "
            f"to mutate var/django.conf and restart the server, so a websocket test "
            f"may see its connection torn down by the reload"
        )
    try:
        yield acquired
    finally:
        if acquired:
            target.release_write()
