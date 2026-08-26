"""Runner-state tests moved from tests/test_helpers/test_runner_config.py
(maestro item #1839): they mutate shared testit run state (TEST_RUN counters
and records, AGENT_MODE, the live var/test_failures.json report) or rebind
mojo.helpers.paths globals — process-wide effects that race parallel modules
(one of these overwrote the live agent report mid-run during this item's own
baseline).
"""
from testit import helpers as th


@th.django_unit_setup()
def setup_connection_boundary(opts):
    from django.db import connection, connections

    connections.close_all()
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")


@th.unit_test("django_unit_setup closes connections at its boundary")
def test_django_setup_closes_connections(opts):
    from django.db import connection

    th.assert_true(
        connection.connection is None,
        "django_unit_setup must close its thread-local connection at the boundary",
    )


@th.django_unit_test("connection lifecycle probe")
def test_connection_lifecycle_probe(opts):
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")


@th.unit_test("django_unit_test closes connections at its boundary")
def test_django_test_closes_connections(opts):
    from django.db import connection

    th.assert_true(
        connection.connection is None,
        "django_unit_test must close its thread-local connection at the boundary",
    )


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


@th.unit_test("an unrunnable test file is a recorded failure, never a silent skip")
def test_unrunnable_file_is_recorded(opts):
    """Regression for the #1839 security review: a refused shadow import (or
    any import failure) used to return silently — the module vanished from the
    report, which still said passed."""
    import io
    from contextlib import redirect_stdout
    from objict import objict
    from testit import helpers, runner

    saved = (helpers.TEST_RUN.total, helpers.TEST_RUN.failed,
             list(helpers.TEST_RUN.records))
    saved_display = helpers._get_display_fn()
    try:
        # The simulated failure must not reach the live progress tracker —
        # this thread's display callback would count it against THIS module.
        helpers._set_display_fn(None)
        before_failed = helpers.TEST_RUN.failed
        fake_opts = objict(verbose=False, errors=False, stop=False)
        with redirect_stdout(io.StringIO()):
            runner.run_module_tests_by_name(
                fake_opts, "no_such_pkg_1839", "no_such_file",
                expected_root="/nonexistent/root")
        assert helpers.TEST_RUN.failed == before_failed + 1, (
            "an unimportable test file must land exactly one recorded failure, "
            f"got failed={helpers.TEST_RUN.failed} (was {before_failed})"
        )
        last = helpers.TEST_RUN.records[-1]
        assert last["status"] == "error" and "did not run" in last.get("detail", ""), (
            f"the failure record must say the file did not run, got {last}"
        )
    finally:
        helpers._set_display_fn(saved_display)
        helpers.TEST_RUN.total, helpers.TEST_RUN.failed = saved[0], saved[1]
        helpers.TEST_RUN.records = saved[2]
