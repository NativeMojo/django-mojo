"""
Tests for testit runner module config loading and parallel infrastructure.
"""
from testit import helpers as th


@th.django_unit_test("TESTIT config: loads from __init__.py")
def test_config_loads(opts):
    from testit.runner import _load_module_config
    import os
    from mojo.helpers import paths

    test_root = os.path.join(os.path.dirname(os.path.dirname(__file__)))
    # Use test_job_engine — it's serial for a legitimate reason
    # (JobEngine uses Python signal handlers that only work on the main thread).
    module_path = os.path.join(test_root, "test_job_engine")

    config = _load_module_config(module_path)
    th.assert_true(config.serial is True, "test_job_engine should be serial=True")
    th.assert_true("mojo.apps.jobs" in config.requires_apps,
                    "test_job_engine should require mojo.apps.jobs")


@th.django_unit_test("TESTIT config: defaults when no TESTIT defined")
def test_config_defaults(opts):
    from testit.runner import _load_module_config
    import tempfile
    import os

    # Create a temp dir with an empty __init__.py
    with tempfile.TemporaryDirectory() as tmpdir:
        init_path = os.path.join(tmpdir, "__init__.py")
        with open(init_path, "w") as fh:
            fh.write("# empty\n")

        config = _load_module_config(tmpdir)
        th.assert_eq(config.serial, False, "Default serial should be False")
        th.assert_eq(config.server_settings, {}, "Default server_settings should be empty")
        th.assert_eq(config.requires_apps, [], "Default requires_apps should be empty")
        th.assert_eq(config.requires_extra, [], "Default requires_extra should be empty")


@th.django_unit_test("TESTIT config: defaults when no __init__.py")
def test_config_no_init(opts):
    from testit.runner import _load_module_config
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        config = _load_module_config(tmpdir)
        th.assert_eq(config.serial, False, "Missing init should default serial=False")
        th.assert_eq(config.requires_apps, [], "Missing init should default requires_apps=[]")


@th.django_unit_test("TESTIT config: partial config merges with defaults")
def test_config_partial(opts):
    from testit.runner import _load_module_config
    import tempfile
    import os

    with tempfile.TemporaryDirectory() as tmpdir:
        init_path = os.path.join(tmpdir, "__init__.py")
        with open(init_path, "w") as fh:
            fh.write('TESTIT = {"serial": True}\n')

        config = _load_module_config(tmpdir)
        th.assert_eq(config.serial, True, "Partial config should set serial=True")
        th.assert_eq(config.server_settings, {}, "Unset fields should use defaults")
        th.assert_eq(config.requires_apps, [], "Unset fields should use defaults")


@th.django_unit_test("test count: _count_tests_in_file counts test_ functions")
def test_count_tests(opts):
    from testit.runner import _count_tests_in_file
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as fh:
        fh.write("""
def setup_something(opts):
    pass

def test_one(opts):
    pass

def test_two(opts):
    pass

def helper_func():
    pass
""")
        fh.flush()
        count = _count_tests_in_file(fh.name)
        os.unlink(fh.name)

    th.assert_eq(count, 2, "Should count exactly 2 test_ functions")


@th.django_unit_test("thread safety: _increment is atomic")
def test_increment_atomic(opts):
    import threading
    from testit import helpers

    # Use a scratch field so we don't clobber TEST_RUN.total mid-suite
    with helpers._lock:
        helpers.TEST_RUN._scratch_counter = 0

    threads = []
    for _ in range(100):
        t = threading.Thread(target=helpers._increment, args=("_scratch_counter",))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    th.assert_eq(helpers.TEST_RUN._scratch_counter, 100,
                 f"Expected exactly 100 after 100 concurrent increments, got {helpers.TEST_RUN._scratch_counter}")


@th.django_unit_test("thread-local display: per-thread isolation")
def test_display_thread_local(opts):
    import threading
    from testit import helpers

    results = {}

    def thread_fn(thread_id):
        def my_display(event, **kwargs):
            return thread_id
        helpers._set_display_fn(my_display)
        fn = helpers._get_display_fn()
        results[thread_id] = fn("test", name="x")

    t1 = threading.Thread(target=thread_fn, args=(1,))
    t2 = threading.Thread(target=thread_fn, args=(2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    th.assert_eq(results[1], 1, "Thread 1 should see its own display fn")
    th.assert_eq(results[2], 2, "Thread 2 should see its own display fn")


@th.django_unit_test("client: last_response captured on request")
def test_client_last_response(opts):
    import testit.client
    client = testit.client.RestClient(opts.host)
    resp = client.get("/api/health")
    th.assert_true(client.last_response is not None, "last_response should be set after request")
    th.assert_true(client.last_response.method == "GET", "last_response method should be GET")
    th.assert_true(client.last_response.status_code is not None, "last_response should have status_code")
    th.assert_true(client.last_response.elapsed_ms >= 0, "last_response should have elapsed_ms")


@th.django_unit_test("agent output: _write_agent_report creates file")
def test_agent_report_writes(opts):
    import os
    import json
    from testit import runner, helpers
    from mojo.helpers import paths

    # Enable agent mode temporarily
    old_agent = helpers.AGENT_MODE
    helpers.AGENT_MODE = True

    try:
        runner._write_agent_report(opts)
        report_path = os.path.join(paths.VAR_ROOT, "test_failures.json")
        th.assert_true(os.path.exists(report_path), "test_failures.json should be created")

        with open(report_path) as fh:
            data = json.load(fh)
        th.assert_true("total" in data, "Report should have total field")
        th.assert_true("failures" in data, "Report should have failures field")
        th.assert_true(isinstance(data["failures"], list), "failures should be a list")
    finally:
        helpers.AGENT_MODE = old_agent


class _FakeTracker:
    """Stands in for the rich display's per-module tracker."""

    def __init__(self, total, passed, failed, skipped, elapsed=0.0, skip_reason=None):
        self.total = total
        self.passed = passed
        self.failed = failed
        self.skipped = skipped
        self.elapsed = elapsed
        self.skip_reason = skip_reason


class _FakeDisplay:
    def __init__(self, trackers):
        self.trackers = trackers
        self._order = list(trackers.keys())


@th.django_unit_test("agent output: top-level rollup equals the sum of per-module values")
def test_agent_report_rollup_matches_modules(opts):
    """Regression for #1127.

    TEST_RUN's counters only see tests that actually executed, so a module skipped
    whole (requires_extra / requires_apps) used to contribute nothing to the
    top-level total/skipped while still being counted in its own module entry. A
    baseline recorded from the top-level numbers then drifted against one derived
    from the table by exactly the size of the opt-in tier.
    """
    import os
    import json
    from testit import runner, helpers
    from mojo.helpers import paths

    display = _FakeDisplay({
        # Ran normally.
        "test_alpha": _FakeTracker(total=10, passed=9, failed=0, skipped=1, elapsed=1.5),
        # Skipped whole — the case that used to vanish from the rollup.
        "test_optin": _FakeTracker(total=40, passed=0, failed=0, skipped=40,
                                   elapsed=0.0, skip_reason="requires --extra slow"),
    })

    old_agent = helpers.AGENT_MODE
    helpers.AGENT_MODE = True
    try:
        runner._write_agent_report(opts, display=display)
        with open(os.path.join(paths.VAR_ROOT, "test_failures.json")) as fh:
            data = json.load(fh)
    finally:
        helpers.AGENT_MODE = old_agent

    module_total = sum(m["tests"] for m in data["modules"].values())
    module_skipped = sum(m["skipped"] for m in data["modules"].values())

    th.assert_eq(data["total"], module_total,
                 f"top-level total ({data['total']}) must equal the sum of per-module "
                 f"tests ({module_total})")
    th.assert_eq(data["skipped"], module_skipped,
                 f"top-level skipped ({data['skipped']}) must equal the sum of per-module "
                 f"skipped ({module_skipped})")
    th.assert_eq(data["total"], 50,
                 f"the whole-skipped module's 40 tests must be counted, got {data['total']}")
    th.assert_true("ran" in data,
                   "report should carry a 'ran' block for what actually executed")


@th.django_unit_test("agent output: per-test durations feed a slowest list")
def test_agent_report_slowest(opts):
    """Module-level timing cannot say which test inside a slow module is the cost."""
    import os
    import json
    from testit import runner, helpers
    from mojo.helpers import paths

    old_records = helpers.TEST_RUN.records
    old_agent = helpers.AGENT_MODE
    helpers.AGENT_MODE = True
    try:
        helpers.TEST_RUN.records = [
            {"module": "test_alpha", "test_module": "a", "function": "f",
             "name": "quick_one", "status": "passed", "duration": 0.01},
            {"module": "test_alpha", "test_module": "a", "function": "f",
             "name": "slow_one", "status": "passed", "duration": 9.5},
            # No duration recorded — must not blow up the sort.
            {"module": "test_alpha", "test_module": "a", "function": "f",
             "name": "untimed", "status": "passed"},
        ]
        runner._write_agent_report(opts)
        with open(os.path.join(paths.VAR_ROOT, "test_failures.json")) as fh:
            data = json.load(fh)
    finally:
        helpers.TEST_RUN.records = old_records
        helpers.AGENT_MODE = old_agent

    th.assert_true("slowest" in data, "report should carry a 'slowest' list")
    th.assert_true(len(data["slowest"]) >= 2,
                   f"slowest should include the timed tests, got {len(data['slowest'])}")
    th.assert_eq(data["slowest"][0]["test_name"], "slow_one",
                 f"slowest entry should be the 9.5s test, got {data['slowest'][0]['test_name']}")
    th.assert_true(data["slowest"][0]["duration"] >= data["slowest"][1]["duration"],
                   "slowest list must be sorted descending by duration")


@th.django_unit_test("per-test timing: _record_result stores a duration")
def test_record_result_duration(opts):
    from testit import helpers

    old_records = helpers.TEST_RUN.records
    try:
        helpers.TEST_RUN.records = []
        helpers._record_result("timing_probe", status="passed", duration=0.25)
        record = helpers.TEST_RUN.records[-1]
        th.assert_true("duration" in record,
                       "a recorded result should carry its duration")
        th.assert_eq(record["duration"], 0.25,
                     f"duration should round-trip, got {record.get('duration')}")

        # A negative clock delta must never surface as a negative duration.
        helpers._record_result("timing_probe_neg", status="passed", duration=-1.0)
        th.assert_eq(helpers.TEST_RUN.records[-1]["duration"], 0.0,
                     "a negative duration must be clamped to zero")
    finally:
        helpers.TEST_RUN.records = old_records


@th.django_unit_test("dev_server.conf: var/ overrides config/ when both exist")
def test_resolve_conf_var_overrides(opts):
    from mojo.helpers import paths
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as var_dir, tempfile.TemporaryDirectory() as cfg_dir:
        var_root, cfg_root = Path(var_dir), Path(cfg_dir)
        (cfg_root / "dev_server.conf").write_text("host=10.0.0.1\nport=1111\n")
        (var_root / "dev_server.conf").write_text("host=10.0.0.2\nport=2222\n")

        resolved = paths.resolve_conf("dev_server.conf", var_root=var_root, config_root=cfg_root)
        th.assert_eq(str(resolved), str(var_root / "dev_server.conf"),
                     "var/dev_server.conf should win when it exists")


@th.django_unit_test("dev_server.conf: falls back to config/ when var/ absent")
def test_resolve_conf_fallback_to_config(opts):
    from mojo.helpers import paths
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as var_dir, tempfile.TemporaryDirectory() as cfg_dir:
        var_root, cfg_root = Path(var_dir), Path(cfg_dir)
        (cfg_root / "dev_server.conf").write_text("host=10.0.0.1\nport=1111\n")
        # deliberately no var/dev_server.conf

        resolved = paths.resolve_conf("dev_server.conf", var_root=var_root, config_root=cfg_root)
        th.assert_eq(str(resolved), str(cfg_root / "dev_server.conf"),
                     "config/dev_server.conf should be used when var/ is absent")


@th.django_unit_test("dev_server.conf: _read_dev_server_conf honors the var/ override")
def test_read_dev_server_conf_uses_var_override(opts):
    # Wire-through: proves the reader delegates to paths.resolve_conf (not a direct
    # CONFIG_ROOT read). Patches the paths globals to temp dirs and restores them in
    # finally — same pattern as test_agent_report_writes above. Never writes the live
    # var/dev_server.conf (that would trip uvicorn's --reload-include '*.conf').
    from mojo.helpers import paths
    from testit import helpers
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as var_dir, tempfile.TemporaryDirectory() as cfg_dir:
        var_root, cfg_root = Path(var_dir), Path(cfg_dir)
        (cfg_root / "dev_server.conf").write_text("host=127.0.0.1\nport=5555\n")
        (var_root / "dev_server.conf").write_text("host=192.168.1.50\nport=9999\n")

        old_var, old_cfg = paths.VAR_ROOT, paths.CONFIG_ROOT
        paths.VAR_ROOT, paths.CONFIG_ROOT = var_root, cfg_root
        try:
            host, port = helpers._read_dev_server_conf()
        finally:
            paths.VAR_ROOT, paths.CONFIG_ROOT = old_var, old_cfg

        th.assert_eq(host, "192.168.1.50",
                     "host should come from the var/dev_server.conf override")
        th.assert_eq(port, 9999,
                     "port should come from the var/dev_server.conf override, parsed as int")


@th.unit_test("fresh test runs clear framework logs but preserve unrelated files")
def test_reset_test_logs_clears_base_and_backups(opts):
    from pathlib import Path
    import tempfile
    from testit.runner import _reset_test_logs

    with tempfile.TemporaryDirectory() as tmpdir:
        log_root = Path(tmpdir)
        base_log = log_root / "testit.log"
        backup_log = log_root / "testit.log.1"
        unrelated = log_root / "keep.txt"
        base_log.write_text("old run\n", encoding="utf-8")
        backup_log.write_text("older run\n", encoding="utf-8")
        unrelated.write_text("keep me\n", encoding="utf-8")

        failures = _reset_test_logs(log_root)

        assert failures == [], f"Reset should succeed for writable temp logs, got {failures!r}"
        assert base_log.exists(), "Base log should be truncated in place, not removed"
        assert base_log.read_bytes() == b"", (
            f"Base log should be empty after reset, got {base_log.read_bytes()!r}"
        )
        assert not backup_log.exists(), "Numbered backup should be removed for a fresh run"
        assert unrelated.read_text(encoding="utf-8") == "keep me\n", (
            "Files outside the *.log / *.log.N test-log patterns must remain untouched"
        )


@th.unit_test("test log reset failures are isolated and reported")
def test_reset_test_logs_continues_after_one_failure(opts):
    import os
    from pathlib import Path
    import tempfile
    from unittest import mock
    from testit.runner import _reset_test_logs

    with tempfile.TemporaryDirectory() as tmpdir:
        log_root = Path(tmpdir)
        bad_log = log_root / "bad.log"
        good_log = log_root / "good.log"
        bad_log.write_text("cannot clear\n", encoding="utf-8")
        good_log.write_text("clear me\n", encoding="utf-8")
        original_open = os.open

        def flaky_open(path, *args, **kwargs):
            if Path(path).name == "bad.log":
                raise OSError("synthetic reset failure")
            return original_open(path, *args, **kwargs)

        with mock.patch.object(os, "open", flaky_open):
            failures = _reset_test_logs(log_root)

        assert len(failures) == 1, f"Exactly one reset failure should be reported, got {failures!r}"
        assert "bad.log" in failures[0], f"Failure should identify bad.log, got {failures!r}"
        assert good_log.read_bytes() == b"", (
            f"A failure clearing bad.log must not block good.log, got {good_log.read_bytes()!r}"
        )


@th.unit_test("test log reset refuses symlinked base logs")
def test_reset_test_logs_does_not_follow_symlinks(opts):
    from pathlib import Path
    import tempfile
    from testit.runner import _reset_test_logs

    with tempfile.TemporaryDirectory() as tmpdir:
        temp_root = Path(tmpdir)
        log_root = temp_root / "logs"
        log_root.mkdir()
        target = temp_root / "outside.txt"
        target.write_text("must survive\n", encoding="utf-8")
        linked_log = log_root / "linked.log"
        linked_log.symlink_to(target)

        failures = _reset_test_logs(log_root)

        assert target.read_text(encoding="utf-8") == "must survive\n", (
            "A symlinked *.log must never truncate its target"
        )
        assert linked_log.is_symlink(), "Rejected base-log symlink should remain inspectable"
        assert len(failures) == 1 and "linked.log" in failures[0], (
            f"Refused symlink should produce one actionable warning, got {failures!r}"
        )


@th.unit_test("test log reset refuses multiply linked base logs")
def test_reset_test_logs_does_not_truncate_hard_links(opts):
    import os
    from pathlib import Path
    import tempfile
    from testit.runner import _reset_test_logs

    with tempfile.TemporaryDirectory() as tmpdir:
        temp_root = Path(tmpdir)
        log_root = temp_root / "logs"
        log_root.mkdir()
        target = temp_root / "outside.txt"
        target.write_text("must survive\n", encoding="utf-8")
        linked_log = log_root / "linked.log"
        os.link(target, linked_log)

        failures = _reset_test_logs(log_root)

        assert target.read_text(encoding="utf-8") == "must survive\n", (
            "A multiply linked *.log must never truncate the shared inode"
        )
        assert linked_log.read_text(encoding="utf-8") == "must survive\n"
        assert len(failures) == 1 and "linked.log" in failures[0], (
            f"Refused hard link should produce one actionable warning, got {failures!r}"
        )


@th.unit_test("only fresh executing runs reset test logs")
def test_should_reset_test_logs_conditions(opts):
    from objict import objict
    from testit.runner import _should_reset_test_logs

    assert _should_reset_test_logs(objict(resume=False, list_extras=False)) is True, (
        "A fresh test execution should clear prior framework logs"
    )
    assert _should_reset_test_logs(objict(resume=True, list_extras=False)) is False, (
        "--continue is the same logical run and must preserve its existing logs"
    )
    assert _should_reset_test_logs(objict(resume=False, list_extras=True)) is False, (
        "--list-extras executes no tests and must not clear diagnostic logs"
    )
