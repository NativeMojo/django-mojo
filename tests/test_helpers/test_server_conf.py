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
    }
    paths.VAR_ROOT = tmpdir
    th._poll_server_up = lambda host, port, timeout=10: True
    th._SLEEP = lambda seconds: None
    th._POLL = lambda host, port, timeout=10: True
    return saved


def _restore_server_settings_seams(saved):
    from mojo.helpers import paths

    paths.VAR_ROOT = saved["VAR_ROOT"]
    th._poll_server_up = saved["_poll_server_up"]
    th._read_dev_server_conf = saved["_read_dev_server_conf"]
    for name in ("_SLEEP", "_POLL"):
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
@th.unit_test("conf helpers: an interleaved apply/restore pair leaves no residue")
def test_interleaved_apply_restore_composition(opts):
    baseline = "SECRET_KEY = 'abc'\nDEBUG = True\n"

    # A applies, B captures while A is live, A restores, B restores. This is
    # exactly the sequence that used to strand ALPHA.
    a_snap = th._capture_conf_lines(baseline, {"ALPHA": 1}.keys())
    with_a = th._apply_conf_overrides(baseline, {"ALPHA": 1})

    b_snap = th._capture_conf_lines(with_a, {"BETA": 2}.keys())
    with_ab = th._apply_conf_overrides(with_a, {"BETA": 2})

    without_a = th._restore_conf_overrides(with_ab, a_snap)
    assert "ALPHA" not in without_a, f"A's key should be gone after A restores, got {without_a!r}"
    assert "BETA = 2" in without_a, f"B's live override must survive A's restore, got {without_a!r}"

    final = th._restore_conf_overrides(without_a, b_snap)
    assert final == baseline, f"the pair should compose back to the baseline, got {final!r}"


@th.unit_test("conf helpers: a pre-existing value is restored verbatim, exactly once")
def test_preexisting_value_restored_verbatim(opts):
    baseline = "SECRET_KEY = 'abc'\nDEBUG =   True   # keep my spacing\n"

    snapshot = th._capture_conf_lines(baseline, {"DEBUG": False}.keys())
    applied = th._apply_conf_overrides(baseline, {"DEBUG": False})
    assert "DEBUG = False" in applied, f"the override should be written, got {applied!r}"

    restored = th._restore_conf_overrides(applied, snapshot)
    assert restored == baseline, f"the original line must come back byte-for-byte, got {restored!r}"
    assert restored.count("DEBUG") == 1, f"the key must appear exactly once, got {restored!r}"


@th.unit_test("conf helpers: a key absent at capture is removed, not left behind")
def test_absent_key_is_removed(opts):
    baseline = "SECRET_KEY = 'abc'\n"

    snapshot = th._capture_conf_lines(baseline, {"NEW_KEY": "x"}.keys())
    assert snapshot["NEW_KEY"] is None, f"NEW_KEY was not in the file; got {snapshot['NEW_KEY']!r}"

    applied = th._apply_conf_overrides(baseline, {"NEW_KEY": "x"})
    restored = th._restore_conf_overrides(applied, snapshot)
    assert restored == baseline, f"an added key must be subtracted back out, got {restored!r}"


@th.unit_test("conf helpers: a concurrent context's key is left alone")
def test_concurrent_key_untouched(opts):
    baseline = "SECRET_KEY = 'abc'\n"

    snapshot = th._capture_conf_lines(baseline, {"MINE": 1}.keys())
    applied = th._apply_conf_overrides(baseline, {"MINE": 1})
    # Someone else writes their own key while ours is live.
    with_theirs = th._apply_conf_overrides(applied, {"THEIRS": 2})

    restored = th._restore_conf_overrides(with_theirs, snapshot)
    assert "MINE" not in restored, f"our key should be gone, got {restored!r}"
    assert "THEIRS = 2" in restored, f"another context's key must survive, got {restored!r}"
    assert "SECRET_KEY = 'abc'" in restored, f"untouched lines must survive, got {restored!r}"


@th.unit_test("conf helpers: a key re-added by someone else is still subtracted")
def test_vanished_key_is_reappended(opts):
    baseline = "SECRET_KEY = 'abc'\nDEBUG = True\n"

    snapshot = th._capture_conf_lines(baseline, {"DEBUG": False}.keys())
    # Simulate the file losing the line entirely while our override is live.
    mangled = "SECRET_KEY = 'abc'\n"

    restored = th._restore_conf_overrides(mangled, snapshot)
    assert "DEBUG = True" in restored, \
        f"a captured line that vanished must be put back, got {restored!r}"
    assert restored.count("DEBUG") == 1, f"and only once, got {restored!r}"


@th.unit_test("conf helpers: duplicate assignments capture the last one (loader is last-wins)")
def test_duplicate_key_captures_last(opts):
    baseline = "DEBUG = True\nSECRET_KEY = 'abc'\nDEBUG = False\n"

    snapshot = th._capture_conf_lines(baseline, {"DEBUG": 0}.keys())
    assert snapshot["DEBUG"] == "DEBUG = False\n", \
        f"the effective (last) assignment should be captured, got {snapshot['DEBUG']!r}"

    applied = th._apply_conf_overrides(baseline, {"DEBUG": 0})
    restored = th._restore_conf_overrides(applied, snapshot)
    assert restored.count("DEBUG") == 1, \
        f"duplicates should collapse to the effective one, got {restored!r}"
    assert "DEBUG = False" in restored, f"and it must be the last-wins value, got {restored!r}"


@th.unit_test("conf helpers: applying to a file with no trailing newline stays parseable")
def test_apply_without_trailing_newline(opts):
    baseline = "SECRET_KEY = 'abc'"  # no trailing newline

    applied = th._apply_conf_overrides(baseline, {"NEW_KEY": 7})
    keys = [th._conf_line_key(line) for line in applied.splitlines()]
    assert "SECRET_KEY" in keys, f"the existing key must survive as its own line, got {applied!r}"
    assert "NEW_KEY" in keys, f"the new key must be its own line, got {applied!r}"
    assert "SECRET_KEY = 'abc'NEW_KEY" not in applied, \
        f"the new key must not be glued onto the last line, got {applied!r}"


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
@th.django_unit_test("runner: conf drift reports the key names of stranded overrides")
def test_conf_drift_detects_strands(opts):
    import shutil
    import tempfile
    from pathlib import Path
    from testit import runner

    tmpdir = Path(tempfile.mkdtemp(prefix="testit-conf-drift-"))
    try:
        before_path = tmpdir / "before.conf"
        after_path = tmpdir / "after.conf"
        before_path.write_text("SECRET_KEY = 'abc'\nDEBUG = True\n")
        after_path.write_text("SECRET_KEY = 'abc'\nDEBUG = False\nSTRANDED_KEY = 'leftover'\n")

        before = runner._snapshot_conf(before_path)
        after = runner._snapshot_conf(after_path)
        assert before is not None and after is not None, "both snapshots should parse"

        drift = runner._conf_drift(before, after)
        assert drift == ["DEBUG", "STRANDED_KEY"], \
            f"both the changed and the added key should be named, got {drift}"
        assert runner._conf_drift(before, before) == [], "an unchanged conf must report no drift"

        # A missing conf disables detection rather than failing the run.
        assert runner._snapshot_conf(tmpdir / "absent.conf") is None, \
            "an unreadable conf should snapshot as None"
        assert runner._conf_drift(None, after) == [], \
            "drift must be skipped when either snapshot is unavailable"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@th.django_unit_test("runner: conf drift never reports a value, only the key name")
def test_conf_drift_never_reports_values(opts):
    import shutil
    import tempfile
    from pathlib import Path
    from testit import runner

    secret = "sk-do-not-print-me"
    tmpdir = Path(tempfile.mkdtemp(prefix="testit-conf-drift-secret-"))
    try:
        before_path = tmpdir / "before.conf"
        after_path = tmpdir / "after.conf"
        before_path.write_text("SECRET_KEY = 'abc'\n")
        after_path.write_text(f"SECRET_KEY = 'abc'\nLLM_HANDLER_API_KEY = '{secret}'\n")

        drift = runner._conf_drift(
            runner._snapshot_conf(before_path), runner._snapshot_conf(after_path))
        assert drift == ["LLM_HANDLER_API_KEY"], f"the key name should be reported, got {drift}"
        assert not any(secret in entry for entry in drift), \
            "a stranded value must never appear in the drift report — it may be a credential"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# server_lock behaviors server_settings depends on
# ---------------------------------------------------------------------------
@th.django_unit_test("server_lock: a timed-out exclusive acquisition warns through the logger")
def test_exclusive_degrade_warns(opts):
    from testit import server_lock

    warnings = []

    class _FakeLogger:
        def warning(self, message):
            warnings.append(message)

    # A leaked shared hold — the thing that makes an override degrade. The wait
    # is generous because a real server_settings elsewhere may hold the lock.
    assert server_lock.acquire_shared(timeout=30.0), \
        "the shared hold was never granted — the process-wide lock stayed held for 30s"
    try:
        with server_lock.exclusive(timeout=0.2, logger=_FakeLogger()) as acquired:
            assert not acquired, "the exclusive hold must be refused while a shared hold is open"
    finally:
        server_lock.release_shared()

    assert len(warnings) == 1, f"degrading must be logged exactly once, got {warnings}"
    assert "django.conf" in warnings[0], \
        f"the warning should say the caller is about to mutate django.conf, got {warnings[0]!r}"


@th.django_unit_test("server_lock: the exclusive holder can open its own websocket without waiting")
def test_writer_thread_gets_read_immediately(opts):
    from testit.server_lock import ServerRestartLock

    lock = ServerRestartLock()
    assert lock.acquire_write(timeout=2.0), "the exclusive hold should be granted on an idle lock"

    started = time.monotonic()
    granted = lock.acquire_read(timeout=5.0)
    elapsed = time.monotonic() - started
    assert granted, "the thread holding the exclusive hold must get a shared hold — it is the restarter"
    assert elapsed < 0.5, \
        f"the owning thread must not wait for its own hold, waited {elapsed:.2f}s"

    # Everyone else still queues, as before.
    other = []

    def _other_thread():
        other.append(lock.acquire_read(timeout=0.3))

    thread = threading.Thread(target=_other_thread)
    thread.start()
    thread.join(timeout=10)
    assert other == [False], \
        f"a different thread must still be excluded by the exclusive hold, got {other}"

    lock.release_read()
    lock.release_write()


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
