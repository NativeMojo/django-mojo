"""Moved from the default-tier sibling (maestro item #1839): these tests mutate shared testit/production module state process-wide (seam rebinding, module-attribute save/restore), which races every parallel module.
"""
"""
Maestro item 543 — `th.server_settings()` must never strand an override in
var/django.conf.

THE BUG
-------
The old implementation snapshotted the whole file (`original =
conf_path.read_text()`) OUTSIDE the server lock and restored it wholesale on
exit. With modules running as threads, thread B could snapshot while thread A's
override was live; B's exit then wrote that stale snapshot back and A's key was
re-introduced — permanently. A stranded key survives the run (and, for a value
like an API key, sits in a gitignored file nobody looks at).

THE FIX
-------
Capture happens INSIDE the lock and records only the lines for the keys being
overridden; restore is SUBTRACTIVE against the file as it stands. Whole
contexts are also serialized against each other.

SEAM NOTE
---------
These tests point `mojo.helpers.paths.VAR_ROOT` at a temp dir rather than at a
testit-only indirection, deliberately: that is the one seam the OLD code and
the NEW code share, so `test_interleaved_contexts_do_not_strand` can be run
against either and is a real regression test rather than a test of the fix's
own scaffolding. `_SLEEP`/`_POLL` (new) and `_poll_server_up` (old) are all
stubbed for the same reason. VAR_ROOT is process-global, so the patch window is
kept to a few milliseconds and always restored in a finally.
"""
import threading
import time

from testit import helpers as th


def _patch_server_settings_seams(tmpdir):
    """Point server_settings at `tmpdir` and stub out its sleeps/polls.

    Returns the saved state for _restore_server_settings_seams(). Patches both
    the new seams (_SLEEP/_POLL) and the old direct helper (_poll_server_up) so
    this runs against either implementation.
    """
    from mojo.helpers import paths

    saved = {
        "VAR_ROOT": paths.VAR_ROOT,
        "_poll_server_up": th._poll_server_up,
        "_SLEEP": getattr(th, "_SLEEP", None),
        "_POLL": getattr(th, "_POLL", None),
        "_read_dev_server_conf": th._read_dev_server_conf,
        "_READY_TIMEOUT": getattr(th, "_READY_TIMEOUT", None),
    }
    paths.VAR_ROOT = tmpdir
    th._poll_server_up = lambda host, port, timeout=10: True
    th._SLEEP = lambda seconds: None
    th._POLL = lambda host, port, timeout=10: True
    # The real deadline is 30s. Tests that deliberately never satisfy the wait
    # would otherwise sit through all of it -- slow tests are what this whole
    # item exists to remove.
    if saved["_READY_TIMEOUT"] is not None:
        th._READY_TIMEOUT = 0.3
    return saved


def _restore_server_settings_seams(saved):
    from mojo.helpers import paths

    paths.VAR_ROOT = saved["VAR_ROOT"]
    th._poll_server_up = saved["_poll_server_up"]
    th._read_dev_server_conf = saved["_read_dev_server_conf"]
    for name in ("_SLEEP", "_POLL", "_READY_TIMEOUT"):
        if saved[name] is None:
            if hasattr(th, name):
                delattr(th, name)
        else:
            setattr(th, name, saved[name])


@th.django_unit_test("server_settings: interleaved contexts do not strand an override")
def test_interleaved_contexts_do_not_strand(opts):
    import shutil
    import tempfile
    from pathlib import Path

    baseline = "SECRET_KEY = 'interleave-baseline'\n"
    tmpdir = Path(tempfile.mkdtemp(prefix="testit-conf-interleave-"))
    conf = tmpdir / "django.conf"
    conf.write_text(baseline)

    b_thread_name = "server-settings-B"
    b_at_gate = threading.Event()
    a_inside = threading.Event()
    errors = []

    saved = _patch_server_settings_seams(tmpdir)

    def _gate_read_dev_server_conf():
        # Called by server_settings BEFORE it takes any lock, in both the old
        # and the new implementation. In the OLD code the whole-file snapshot
        # has already happened by this point (that is the bug); in the NEW code
        # nothing has been captured yet and B is about to block on the
        # serialization lock. Firing the gate here therefore releases A at the
        # exact moment that distinguishes the two.
        if threading.current_thread().name == b_thread_name:
            b_at_gate.set()
        return ("127.0.0.1", 5555)

    try:
        th._read_dev_server_conf = _gate_read_dev_server_conf

        def _thread_a():
            try:
                with th.server_settings(ALPHA_SETTING=1):
                    a_inside.set()
                    # Bounded on purpose: with the fix, B is serialized behind
                    # A and never reaches the gate while A is inside, so this
                    # wait simply expires instead of deadlocking the test.
                    b_at_gate.wait(5.0)
            except Exception as err:
                errors.append(("A", repr(err)))

        def _thread_b():
            try:
                with th.server_settings(BETA_SETTING=2):
                    pass
            except Exception as err:
                errors.append(("B", repr(err)))

        thread_a = threading.Thread(target=_thread_a, name="server-settings-A")
        thread_a.start()
        # Generous: another module's real server_settings may be holding the
        # process-wide server lock when this starts.
        assert a_inside.wait(60.0), "thread A never entered its server_settings context"

        thread_b = threading.Thread(target=_thread_b, name=b_thread_name)
        thread_b.start()
        assert b_at_gate.wait(60.0), "thread B never reached the server_settings gate"

        thread_a.join(timeout=60)
        thread_b.join(timeout=60)
        assert not thread_a.is_alive(), "thread A never left its server_settings context"
        assert not thread_b.is_alive(), "thread B never left its server_settings context"
        assert errors == [], f"server_settings raised in a worker thread: {errors}"

        final = conf.read_text()
        assert final == baseline, (
            "interleaved server_settings contexts stranded an override in "
            f"django.conf — expected {baseline!r}, got {final!r}"
        )
    finally:
        _restore_server_settings_seams(saved)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# The pure capture/restore helpers — strings in, strings out, no I/O
# ---------------------------------------------------------------------------














@th.unit_test("conf helpers: concurrent _conf_apply calls do not lose keys")
def test_conf_apply_is_mutex_protected(opts):
    import shutil
    import tempfile
    import time as _time
    from pathlib import Path

    tmpdir = Path(tempfile.mkdtemp(prefix="testit-conf-mutex-"))
    conf = tmpdir / "django.conf"
    conf.write_text("SECRET_KEY = 'abc'\n")

    saved_yield = th._CONF_TEST_YIELD
    try:
        # Widen the read-modify-write window so an unsynchronized apply would
        # lose every key but the last writer's, deterministically.
        th._CONF_TEST_YIELD = lambda: _time.sleep(0.05)

        threads = []
        for index in range(6):
            key = f"CONCURRENT_{index}"
            threads.append(threading.Thread(
                target=lambda k=key, v=index: th._conf_apply(conf, {k: v})))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        text = conf.read_text()
        missing = [f"CONCURRENT_{i}" for i in range(6) if f"CONCURRENT_{i} = {i}" not in text]
        assert missing == [], \
            f"every concurrent apply must survive the read-modify-write, lost {missing}: {text!r}"
        assert "SECRET_KEY = 'abc'" in text, f"the original line must survive, got {text!r}"
    finally:
        th._CONF_TEST_YIELD = saved_yield
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# End-of-run drift detection
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# server_lock behaviors server_settings depends on
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Readiness signal (item 1094) — waiting for the NEW worker instead of sleeping
# ---------------------------------------------------------------------------
def _write_ready(tmpdir, pid, conf_sha):
    import json
    (tmpdir / "asgi_ready.json").write_text(json.dumps({"pid": pid, "conf": conf_sha}))


def _sha(text):
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


@th.django_unit_test("readiness: a stale worker never satisfies the wait (regression, cf. 543)")
def test_wait_rejects_the_stale_worker(opts):
    """The bug the 3s sleep was hiding.

    _poll_server_up returns on ANY response, and the old worker keeps answering
    until it exits — so a poll-only implementation returns while the overrides
    are still live, and the next test reads them. The wait must refuse a signal
    whose pid is the one that was serving before the write, no matter how
    healthy the socket looks.
    """
    import shutil
    import tempfile
    from pathlib import Path

    tmpdir = Path(tempfile.mkdtemp(prefix="testit-ready-stale-"))
    saved = _patch_server_settings_seams(tmpdir)
    try:
        target = _sha("SECRET_KEY = 'x'\nNEW = 1\n")
        # The OLD worker is up, answering, and already reports the new config —
        # the most favourable possible case for a naive implementation.
        _write_ready(tmpdir, pid=4242, conf_sha=target)

        raised = None
        try:
            th._wait_for_worker("127.0.0.1", 5555, target, exclude_pid=4242,
                                timeout=0.4, interval=0.02)
        except RuntimeError as err:
            raised = err

        assert raised is not None, \
            "waiting must not be satisfied by the worker that was serving before the write"
        assert "4242" in str(raised), \
            f"the timeout should name the previous pid for diagnosis, got: {raised}"
    finally:
        _restore_server_settings_seams(saved)
        shutil.rmtree(tmpdir, ignore_errors=True)


@th.django_unit_test("readiness: the wait clears once a new pid reports the expected config")
def test_wait_accepts_the_new_worker(opts):
    import shutil
    import tempfile
    from pathlib import Path

    tmpdir = Path(tempfile.mkdtemp(prefix="testit-ready-new-"))
    saved = _patch_server_settings_seams(tmpdir)
    try:
        target = _sha("SECRET_KEY = 'x'\nNEW = 1\n")
        _write_ready(tmpdir, pid=99, conf_sha=target)
        ok = th._wait_for_worker("127.0.0.1", 5555, target, exclude_pid=4242,
                                 timeout=2.0, interval=0.02)
        assert ok is True, "a new pid serving the expected config must satisfy the wait"
    finally:
        _restore_server_settings_seams(saved)
        shutil.rmtree(tmpdir, ignore_errors=True)


@th.django_unit_test("readiness: a new pid running the WRONG config does not satisfy the wait")
def test_wait_rejects_wrong_config(opts):
    """A restart alone proves nothing about which config was loaded."""
    import shutil
    import tempfile
    from pathlib import Path

    tmpdir = Path(tempfile.mkdtemp(prefix="testit-ready-wrongconf-"))
    saved = _patch_server_settings_seams(tmpdir)
    try:
        _write_ready(tmpdir, pid=99, conf_sha=_sha("something else entirely"))
        raised = None
        try:
            th._wait_for_worker("127.0.0.1", 5555, _sha("SECRET_KEY = 'x'\nNEW = 1\n"),
                                exclude_pid=4242, timeout=0.4, interval=0.02)
        except RuntimeError as err:
            raised = err
        assert raised is not None, \
            "a restarted worker running different config must not satisfy the wait"
    finally:
        _restore_server_settings_seams(saved)
        shutil.rmtree(tmpdir, ignore_errors=True)


@th.django_unit_test("readiness: absent signal falls back, unreadable signal keeps waiting")
def test_absent_signal_is_not_a_stale_read(opts):
    """These two must never be conflated.

    No file at all means the project does not write the signal, so testit has to
    fall back to sleeping. A file that cannot be parsed right now means a restart
    is in flight and we should keep waiting. Treating the second as the first
    would silently turn every wait back into a sleep and nobody would notice,
    because the fallback still works.
    """
    import shutil
    import tempfile
    from pathlib import Path

    tmpdir = Path(tempfile.mkdtemp(prefix="testit-ready-absent-"))
    saved = _patch_server_settings_seams(tmpdir)
    try:
        assert th._read_ready() is None, \
            "no asgi_ready.json must report the signal as UNAVAILABLE (None)"

        (tmpdir / "asgi_ready.json").write_text('{"pid": 1, "co')  # torn write
        assert th._read_ready() == (None, None), \
            "an unparseable signal must read as stale (None, None), not as unavailable"
    finally:
        _restore_server_settings_seams(saved)
        shutil.rmtree(tmpdir, ignore_errors=True)


@th.django_unit_test("readiness: an unchanged conf write performs no wait at all")
def test_noop_write_skips_the_wait(opts):
    """uvicorn reloads on the write event, not on a content diff.

    So rewriting identical bytes would queue a restart whose fingerprint already
    matches what we are waiting for — we would return instantly and the restart
    would land inside the next test. The write itself has to be skipped.
    """
    import shutil
    import tempfile
    from pathlib import Path

    tmpdir = Path(tempfile.mkdtemp(prefix="testit-ready-noop-"))
    conf = tmpdir / "django.conf"
    conf.write_text("SECRET_KEY = 'x'\nALREADY = 1\n")
    saved = _patch_server_settings_seams(tmpdir)
    try:
        before_mtime = conf.stat().st_mtime_ns

        # Applying the value the file already carries must not touch the file.
        _snapshot, _sha_out, changed = th._conf_apply(conf, {"ALREADY": 1})
        assert changed is False, \
            "applying a value the conf already holds must not rewrite the file"
        assert conf.stat().st_mtime_ns == before_mtime, \
            "an unchanged apply must leave the file untouched so no reload is queued"

        # A real change must still be reported as one.
        _snapshot2, _sha2, changed2 = th._conf_apply(conf, {"ALREADY": 2})
        assert changed2 is True, "a genuine value change must be reported as changed"
    finally:
        _restore_server_settings_seams(saved)
        shutil.rmtree(tmpdir, ignore_errors=True)


@th.django_unit_test("readiness: a failed wait must not strand the override it just wrote")
def test_failed_wait_restores_the_override(opts):
    """Regression for the security review of item 1094.

    _conf_apply has already written the overrides by the time the wait runs, and
    that wait raises BEFORE the try/yield/finally that would normally restore
    them. So if nothing undoes the write on the way out, the override survives in
    var/django.conf -- and uvicorn (--reload-include '*.conf') then serves it to
    every later test. That is the item 543 leak reached through a different door,
    and it matters because these contexts set things like BOUNCER_REQUIRE_TOKEN:
    a stranded auth-relaxing value means later tests pass with the control off.
    """
    import shutil
    import tempfile
    from pathlib import Path

    baseline = "SECRET_KEY = 'leak-baseline'\n"
    tmpdir = Path(tempfile.mkdtemp(prefix="testit-ready-leak-"))
    conf = tmpdir / "django.conf"
    conf.write_text(baseline)
    saved = _patch_server_settings_seams(tmpdir)
    try:
        # A signal exists, so the event-driven path is taken -- but the pid never
        # changes, so the wait can only ever time out.
        _write_ready(tmpdir, pid=777, conf_sha="whatever-the-old-worker-had")

        raised = None
        try:
            with th.server_settings(BOUNCER_REQUIRE_TOKEN=True):
                raise AssertionError("the context must not yield when the wait fails")
        except RuntimeError as err:
            raised = err

        assert raised is not None, "a wait that never sees a new worker must raise"
        assert "BOUNCER_REQUIRE_TOKEN" not in conf.read_text(), (
            "the override must be removed from django.conf when the wait fails -- "
            f"otherwise every later test runs under it. conf is now: {conf.read_text()!r}")
        assert conf.read_text() == baseline, (
            f"django.conf should be byte-identical to before the context, got "
            f"{conf.read_text()!r}")
    finally:
        _restore_server_settings_seams(saved)
        shutil.rmtree(tmpdir, ignore_errors=True)


@th.django_unit_test("readiness: a no-op write against a worker on different config fails loudly")
def test_noop_write_verifies_the_running_worker(opts):
    """If no reload is needed we skip the wait -- but only if the running worker
    actually confirms it loaded this exact file. Otherwise the test would run
    against settings nobody chose, silently."""
    import shutil
    import tempfile
    from pathlib import Path

    baseline = "SECRET_KEY = 'noop-verify'\nALREADY = 1\n"
    tmpdir = Path(tempfile.mkdtemp(prefix="testit-ready-noopverify-"))
    conf = tmpdir / "django.conf"
    conf.write_text(baseline)
    saved = _patch_server_settings_seams(tmpdir)
    try:
        # Applying ALREADY=1 changes nothing, but the worker reports a config that
        # is not this file.
        _write_ready(tmpdir, pid=555, conf_sha=_sha("a completely different file"))

        raised = None
        try:
            with th.server_settings(ALREADY=1):
                raise AssertionError("must not yield against an unverified worker")
        except RuntimeError as err:
            raised = err

        assert raised is not None, \
            "a no-op write must still verify the running worker loaded this config"
        assert "555" in str(raised), \
            f"the error should name the worker it could not verify, got: {raised}"

        # And the happy case still skips the wait entirely.
        _write_ready(tmpdir, pid=555, conf_sha=_sha(baseline))
        with th.server_settings(ALREADY=1):
            pass
        assert conf.read_text() == baseline, \
            "a verified no-op context must leave the file untouched"
    finally:
        _restore_server_settings_seams(saved)
        shutil.rmtree(tmpdir, ignore_errors=True)
