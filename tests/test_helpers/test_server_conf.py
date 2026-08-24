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

TESTIT_TIER = "bug"  # #2792 tier curation








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














