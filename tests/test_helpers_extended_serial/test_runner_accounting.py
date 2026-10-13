"""Result accounting at the runner/decorator and rich-report boundaries."""
from testit import helpers as th


def _scratch_run():
    import contextlib
    from objict import objict
    from testit import helpers

    @contextlib.contextmanager
    def isolated():
        previous = helpers.TEST_RUN
        active = helpers._active_context().active
        display = helpers._get_display_fn()
        verbose = helpers.VERBOSE
        helpers.TEST_RUN = objict(
            total=0, passed=0, failed=0, skipped=0, results={}, records=[],
            started_at=1.0, finished_at=2.0, aborted=False,
        )
        events = []
        helpers._set_display_fn(lambda event, **kw: events.append((event, kw)))
        try:
            yield helpers.TEST_RUN, events
        finally:
            helpers.TEST_RUN = previous
            helpers._set_active_test(active)
            helpers._set_display_fn(display)
            helpers.VERBOSE = verbose
    return isolated()


@th.django_unit_test("extras skips count once with either decorator order")
def test_extra_skip_accounting(opts):
    from types import SimpleNamespace
    from testit import runner

    def body(opts):
        raise AssertionError("An unselected extra must never execute its body")

    outer = th.requires_extra("extended")(th.unit_test("outer extra")(body))
    inner = th.unit_test("inner extra")(th.requires_extra("extended")(body))
    local_opts = SimpleNamespace(verbose=False, errors=False, stop=False,
                                 extra_list=[], extra=[])
    module = SimpleNamespace(test_outer=outer, test_inner=inner)
    with _scratch_run() as (state, events):
        for name in ("test_outer", "test_inner"):
            runner.run_test(local_opts, module, name, "test_sample", "extras")
        assert (state.total, state.skipped, state.passed, state.failed) == (2, 2, 0, 0), \
            "Each decorator order must produce exactly one counted skip"
        assert len(state.records) == 2, "Both skipped tests must appear in records"
        assert {r['function'] for r in state.records} == {'test_outer', 'test_inner'}, \
            "Skipped records must retain the test function identity"
        assert all(r['detail'] == "requires extra flag 'extended'" for r in state.records), \
            "The skip reason must survive into the report"
        assert len([e for e, kw in events if e == 'test_result']) == 2, \
            "Rich trackers must receive exactly one result per skipped test"


@th.django_unit_test("file selections remain counted alongside rich module trackers")
def test_file_selection_report_accounting(opts):
    from types import SimpleNamespace
    from testit import runner, helpers

    with _scratch_run() as (state, events):
        helpers._set_active_test("test_file:selected:test_example")
        helpers._record_result("selected example", status="passed")
        state.total = state.passed = 1
        empty = SimpleNamespace(trackers={}, _order=[])
        report = runner._build_agent_report(SimpleNamespace(), display=empty)
        assert report['total'] == report['passed'] == 1, \
            "A file-only rich run must report its executed test"
        assert report['modules']['test_file']['tests'] == 1, \
            "File selection must retain its module in the report"

        tracker = runner._ModuleTracker("test_optin", 3)
        tracker.skipped = 3
        tracker.skip_reason = "not selected"
        mixed = SimpleNamespace(trackers={'test_optin': tracker}, _order=['test_optin'])
        report = runner._build_agent_report(SimpleNamespace(), display=mixed)
        assert (report['total'], report['passed'], report['skipped']) == (4, 1, 3), \
            "File results and whole-skipped modules must both survive rollup"

        completed = runner._ModuleTracker("test_file", 1)
        completed.passed = 1
        tracked = SimpleNamespace(trackers={'test_file': completed}, _order=['test_file'])
        report = runner._build_agent_report(SimpleNamespace(), display=tracked)
        assert report['total'] == report['passed'] == 1, \
            "Records already covered by a tracker must not be double-counted"

        helpers._set_active_test("test_file:second:test_example")
        helpers._record_result("another selection", status="passed")
        state.total = state.passed = 2
        report = runner._build_agent_report(SimpleNamespace(), display=tracked)
        assert report['total'] == report['passed'] == 2, \
            "An extra file selection in a tracked package must also be counted"
        assert report['modules']['test_file']['tests'] == 2, \
            "The shared module must include both tracked and untracked executions"

        helpers._set_active_test("test_optin:selected:test_extra")
        helpers._record_result("selected extra", status="skipped")
        state.total = 3
        state.skipped = 1
        report = runner._build_agent_report(SimpleNamespace(), display=mixed)
        assert report['modules']['test_optin']['tests'] == 4, \
            "A selected file adds an execution to its whole-skipped package"
        assert report['modules']['test_optin']['skipped'] == 4, \
            "A file skip must not disappear into the package's unrecorded skips"


# ---------------------------------------------------------------------------
# Setup errors, not-run tests and the run verdict (item #4761)
# ---------------------------------------------------------------------------
def _local_opts(**kwargs):
    from types import SimpleNamespace
    base = dict(verbose=False, errors=False, stop=False, host="http://127.0.0.1:9",
                logger=None, select_all=False, test_modules=["probe"],
                selected_tags=set(), extra_list=[], extra=[])
    base.update(kwargs)
    return SimpleNamespace(**base)


def _probe_module(dotted, calls, *, setup_raises=True, file_tier=None):
    """An in-memory test file: two setups, three tests, in source order.
    Registered in sys.modules so import_module returns it; the caller removes it."""
    import sys
    import types

    module = types.ModuleType(dotted)
    module.__file__ = __file__

    @th.unit_setup()
    def setup_first(opts):
        calls.append("setup_first")
        if setup_raises:
            raise RuntimeError("probe setup exploded")

    @th.unit_setup()
    def setup_second(opts):
        calls.append("setup_second")

    @th.unit_test("probe one")
    def test_one(opts):
        calls.append("test_one")

    @th.unit_test("probe two")
    def test_two(opts):
        calls.append("test_two")

    @th.unit_test("probe three")
    def test_three(opts):
        calls.append("test_three")

    for func in (setup_first, setup_second, test_one, test_two, test_three):
        setattr(module, func.__name__, func)
    if file_tier:
        module.TESTIT_TIER = file_tier
    sys.modules[dotted] = module
    return module


@th.unit_test("setup error: becomes one structured error record, without --verbose")
def test_setup_error_becomes_error_record(opts):
    import sys
    from types import SimpleNamespace
    from testit import runner

    calls = []
    _probe_module("t4761_probe.setup_record", calls)
    try:
        with _scratch_run() as (state, events):
            runner.run_module_tests_by_name(_local_opts(), "t4761_probe", "setup_record")
            errors = [r for r in state.records if r.get("kind") == "setup_error"]
            assert len(errors) == 1, f"one setup_error record expected, got {state.records}"
            record = errors[0]
            assert record["status"] == "error", f"the setup record is an error, got {record}"
            assert record["function"] == "setup_first", f"the record names the raising setup, got {record}"
            assert "RuntimeError: probe setup exploded" in record["detail"], f"the detail carries the exception type and message, got {record}"
            ctx = record["agent_context"]
            assert ctx["function"] == "setup_first", f"the context names the setup function, got {ctx}"
            assert "RuntimeError: probe setup exploded" in ctx["traceback"], f"the context carries the traceback, got {ctx}"
            assert ctx["file_path"] == __file__, (
                f"file_path must be the test file, not the decorator's: {ctx.get('file_path')}")
            assert "def setup_first" in ctx["test_source"], f"the context carries the setup source, got {ctx}"
            report = runner._build_agent_report(
                SimpleNamespace(), display=SimpleNamespace(trackers={}, _order=[]))
            assert report["status"] == "failed", f"a setup error fails the report, got {report['status']}"
            failure = [f for f in report["failures"] if f["kind"] == "setup_error"]
            assert len(failure) == 1 and failure[0]["file_path"] == __file__, f"failures carry one setup_error with the test file, got {report['failures']}"
            assert "setup_error" in {r["code"] for r in report["reasons"]}, f"the report names setup_error, got {report['reasons']}"
    finally:
        sys.modules.pop("t4761_probe.setup_record", None)


@th.unit_test("setup error: stops the file and marks its tests not run")
def test_setup_error_stops_file_and_marks_tests_not_run(opts):
    import sys
    from testit import runner

    calls = []
    _probe_module("t4761_probe.not_run", calls)
    try:
        with _scratch_run() as (state, events):
            runner.run_module_tests_by_name(_local_opts(), "t4761_probe", "not_run")
            assert calls == ["setup_first"], (
                f"the later setup and every test body must not run, got {calls}")
            not_run = [r for r in state.records if r.get("kind") == "not_run"]
            assert [r["name"] for r in not_run] == ["one", "two", "three"], f"every test is recorded not run, in order, got {not_run}"
            assert all(r["status"] == "skipped" for r in not_run), f"not-run tests are skips naming the cause, got {not_run}"
            assert all(r["detail"] == "not run: setup failed in setup_first: "
                       "RuntimeError: probe setup exploded" for r in not_run), f"not-run tests are skips naming the cause, got {not_run}"
            assert (state.total, state.failed, state.skipped, state.passed) == (4, 1, 3, 0), \
                "the setup record is the file's only counted failure"
            results = [kw for e, kw in events if e == "test_result"]
            assert len(results) == 3 and all(kw["status"] == "skipped" for kw in results), \
                "only the not-run records reach the tracker"
            codes = [r["code"] for r in runner._run_verdict(state)]
            assert codes == ["setup_error"], f"a setup error alone is setup_error, got {codes}"
    finally:
        sys.modules.pop("t4761_probe.not_run", None)


@th.unit_test("setup error: under --stop the run aborts red with a checkpoint")
def test_setup_error_under_stop_aborts_red(opts):
    import sys
    from unittest import mock
    from testit import runner, helpers

    calls, saved = [], []
    _probe_module("t4761_probe.stop", calls)
    try:
        with _scratch_run() as (state, events):
            with mock.patch.object(runner, "save_checkpoint",
                                   lambda m, t: saved.append((m, t))):
                raised = False
                try:
                    runner.run_module_tests_by_name(
                        _local_opts(stop=True), "t4761_probe", "stop")
                except helpers.TestitAbort:
                    raised = True
            assert raised, "a setup error under --stop must raise TestitAbort"
            assert state.aborted is True, "the raise site must mark the run aborted"
            assert saved == [("t4761_probe", "stop")], f"the checkpoint names the stopped file, got {saved}"
            codes = [r["code"] for r in runner._run_verdict(state)]
            assert codes == ["setup_error", "aborted"], f"a stopped setup error is setup_error and aborted, got {codes}"
            assert runner._keeps_checkpoint(runner._run_verdict(state)), \
                "a run stopped by a setup error keeps its --continue checkpoint"
    finally:
        sys.modules.pop("t4761_probe.stop", None)


@th.unit_test("tier gate: a file with no selected test runs no setup")
def test_unselected_file_runs_no_setup(opts):
    import sys
    from testit import runner

    calls = []
    _probe_module("t4761_probe.unselected", calls, file_tier="admin")
    try:
        with _scratch_run() as (state, events):
            local = _local_opts(test_modules=None, selected_tags={"core", "framework"})
            runner.run_module_tests_by_name(local, "t4761_probe", "unselected")
            assert calls == [], f"no setup or test may run, got {calls}"
            assert all(r["detail"] == "tier 'admin' not selected" for r in state.records), \
                state.records
            assert (state.total, state.skipped, state.failed) == (3, 3, 0), \
                f"three counted tier skips expected, got {(state.total, state.skipped, state.failed)}"
            assert not any(r.get("kind") for r in state.records), f"no record carries a kind, got {state.records}"
            # A tier skip is a skip: alone it is "nothing executed", never a setup error.
            assert [r["code"] for r in runner._run_verdict(state)] == ["nothing_executed"], \
                "tier skips alone are nothing executed, never a setup error"
            state.passed = state.total = 4
            assert runner._run_verdict(state) == [], \
                "tier skips beside a passing test leave no reason"
    finally:
        sys.modules.pop("t4761_probe.unselected", None)


@th.unit_test("setup skip: a TestitSkip in setup still skips the file, no error")
def test_setup_skip_still_skips_file(opts):
    import sys
    import types
    from testit import runner

    calls = []
    module = types.ModuleType("t4761_probe.setup_skip")
    module.__file__ = __file__

    def setup_skips(opts):
        calls.append("setup_skips")
        raise th.TestitSkip("not here")

    @th.unit_test("skipped one")
    def test_skipped(opts):
        calls.append("test_skipped")

    module.setup_skips = setup_skips
    module.test_skipped = test_skipped
    sys.modules["t4761_probe.setup_skip"] = module
    try:
        with _scratch_run() as (state, events):
            runner.run_module_tests_by_name(_local_opts(), "t4761_probe", "setup_skip")
            assert calls == ["setup_skips"], f"only the skipping setup runs, got {calls}"
            assert (state.total, state.skipped, state.failed) == (1, 1, 0), \
                f"one counted skip expected, got {(state.total, state.skipped, state.failed)}"
            assert [r["detail"] for r in state.records] == ["setup skipped"], f"the file's test is a setup skip, got {state.records}"
            assert not any(r.get("kind") for r in state.records), f"no record carries a kind, got {state.records}"
    finally:
        sys.modules.pop("t4761_probe.setup_skip", None)


@th.unit_test("run verdict: one reason list per run shape")
def test_run_verdict_cases(opts):
    from objict import objict
    from testit import runner

    def run(total=0, passed=0, failed=0, skipped=0, aborted=False, kinds=()):
        records = [{"module": "m", "test_module": "f", "function": f"fn{i}", "kind": k}
                   for i, k in enumerate(kinds)]
        return objict(total=total, passed=passed, failed=failed, skipped=skipped,
                      aborted=aborted, records=records)

    def codes(state):
        return [r["code"] for r in runner._run_verdict(state)]

    assert not runner._abort_event.is_set(), "the verdict cases assume no abort key"
    assert codes(run(total=2, passed=2)) == [], "a passing run has no reasons"
    failed = runner._run_verdict(run(total=2, passed=1, failed=1))
    assert [r["code"] for r in failed] == ["tests_failed"], f"a failed test is tests_failed, got {failed}"
    assert failed[0]["message"] == "  Tests failed!", "the historic message is unchanged"
    setup = runner._run_verdict(run(total=1, failed=1, kinds=["setup_error"]))
    assert [r["code"] for r in setup] == ["setup_error"], f"a setup error is setup_error only, got {setup}"
    assert "m.f.fn0" in setup[0]["message"], f"the message names module.file.function, got {setup}"
    assert codes(run(total=4, failed=1, skipped=3,
                     kinds=["setup_error", "not_run", "not_run", "not_run"])) == \
        ["setup_error"], "not-run tests never add nothing_executed beside a setup error"
    assert codes(run(total=1, passed=1, aborted=True)) == ["aborted"], "a stopped run is aborted"
    assert codes(run()) == ["nothing_selected"], "an empty selection is nothing_selected"
    assert codes(run(total=3, skipped=3)) == ["nothing_executed"], \
        "all-skipped is distinct from nothing selected"
    assert codes(run(total=3, passed=1, failed=2, kinds=["setup_error"])) == \
        ["tests_failed", "setup_error"], "a real failure beside a setup error keeps both"


@th.unit_test("exit and checkpoint: reasons exit non-zero; budget keeps its message")
def test_exit_for_and_checkpoint_rule(opts):
    from testit import runner

    def exit_message(report, budget_failed):
        try:
            runner._exit_for(report, budget_failed)
        except SystemExit as exc:
            return exc.code
        return None

    failed = {"reasons": [{"code": "tests_failed", "message": "  Tests failed!"}]}
    assert exit_message(failed, False) == "  Tests failed!", "a failed run exits with its message"
    assert exit_message(failed, True) == "  Tests failed!", "the verdict outranks the budget"
    empty = {"reasons": [{"code": "nothing_selected", "message": "  none"}]}
    assert exit_message(empty, False) == "  none", "an empty run exits non-zero"
    assert exit_message({"reasons": []}, True) == \
        "  Tier budget exceeded — see 'Tier budget' above.", "budget string unchanged"
    assert exit_message({"reasons": []}, False) is None, "a clean run returns"

    def keeps(*codes):
        return runner._keeps_checkpoint([{"code": c} for c in codes])

    assert keeps("aborted") and keeps("setup_error") and keeps("tests_failed", "aborted"), \
        "aborted and setup_error runs keep the checkpoint"
    assert not keeps(), "a clean run clears the checkpoint"
    assert not keeps("tests_failed"), "a completed failing run clears the checkpoint"
    assert not keeps("nothing_selected"), "a stale --continue must not stick red"
    assert not keeps("nothing_executed"), "an all-skipped run clears the checkpoint"


def _exit_module(dotted, calls, code):
    """An in-memory test file whose first setup raises SystemExit(code)."""
    import sys
    import types

    module = types.ModuleType(dotted)
    module.__file__ = __file__

    def setup_guard(opts):
        calls.append(f"{dotted}:setup_guard")
        raise SystemExit(code)

    def setup_after(opts):
        calls.append(f"{dotted}:setup_after")

    @th.unit_test("guarded one")
    def test_guarded(opts):
        calls.append(f"{dotted}:test_guarded")

    for func in (setup_guard, setup_after, test_guarded):
        setattr(module, func.__name__, func)
    sys.modules[dotted] = module
    return module


@th.unit_test("setup SystemExit: recorded as a setup error that stops the run")
def test_setup_system_exit_is_setup_error_and_aborts(opts):
    import threading
    from types import SimpleNamespace
    from unittest import mock
    from testit import runner

    for code, summary in ((0, "SystemExit: 0"), (None, "SystemExit: None"),
                          ("guard message", "SystemExit: guard message")):
        calls = []
        module = _exit_module("t4761_exit.seam", calls, code)
        try:
            with _scratch_run() as (state, events), \
                    mock.patch.object(runner, "_abort_event", threading.Event()):
                failed = None
                try:
                    runner.run_setup(_local_opts(), module, "setup_guard",
                                     "t4761_exit", "seam")
                except runner._SetupFailed as exc:
                    failed = exc
                except SystemExit as exc:
                    raise AssertionError(
                        f"SystemExit({exc.code!r}) escaped setup handling") from None
                assert failed is not None and failed.summary == summary, (
                    f"SystemExit({code!r}) must raise _SetupFailed with {summary!r}, got {failed!r}")
                errors = [r for r in state.records if r.get("kind") == "setup_error"]
                assert len(errors) == 1 and errors[0]["detail"] == \
                    f"setup failed in setup_guard: {summary}", \
                    f"SystemExit({code!r}) must give one setup_error record, got {state.records}"
                assert runner._abort_event.is_set(), \
                    f"SystemExit({code!r}) in setup must set the abort event"
                reasons = runner._run_verdict(state)
                assert [r["code"] for r in reasons] == ["setup_error", "aborted"], \
                    f"SystemExit({code!r}) must fail with setup_error and aborted, got {reasons}"
                report = runner._build_agent_report(
                    SimpleNamespace(), display=SimpleNamespace(trackers={}, _order=[]))
                assert report["status"] == "failed", \
                    f"SystemExit({code!r}) must fail the report, got {report['status']}"
                assert runner._keeps_checkpoint(report["reasons"]), \
                    "a run stopped by a setup SystemExit keeps its checkpoint"
                try:
                    runner._exit_for(report, False)
                    exit_code = None
                except SystemExit as exc:
                    exit_code = exc.code
                assert exit_code not in (None, 0), \
                    f"SystemExit({code!r}) in setup must exit non-zero, got {exit_code!r}"
        finally:
            import sys
            sys.modules.pop("t4761_exit.seam", None)


@th.unit_test("setup SystemExit: the file's tests are not run and the run stops")
def test_setup_system_exit_stops_file_without_stop_flag(opts):
    import sys
    import threading
    from unittest import mock
    from testit import runner, helpers

    calls, saved = [], []
    _exit_module("t4761_exit.file", calls, 0)
    try:
        with _scratch_run() as (state, events), \
                mock.patch.object(runner, "_abort_event", threading.Event()), \
                mock.patch.object(runner, "save_checkpoint",
                                  lambda m, t: saved.append((m, t))):
            raised = False
            try:
                runner.run_module_tests_by_name(_local_opts(), "t4761_exit", "file")
            except helpers.TestitAbort:
                raised = True
            except SystemExit as exc:
                raise AssertionError(
                    f"SystemExit({exc.code!r}) escaped setup handling") from None
            assert raised, "a setup SystemExit must stop the run even without --stop"
            assert calls == ["t4761_exit.file:setup_guard"], \
                f"the later setup and the test body must not run, got {calls}"
            kinds = [r.get("kind") for r in state.records]
            assert kinds == ["setup_error", "not_run"], \
                f"the file's test must still be recorded as not run, got {kinds}"
            assert state.aborted is True, "the raise site must mark the run aborted"
            assert saved == [("t4761_exit", "file")], \
                f"the checkpoint must name the stopped file, got {saved}"
    finally:
        sys.modules.pop("t4761_exit.file", None)


@th.unit_test("setup SystemExit: other workers start no new files")
def test_setup_system_exit_stops_other_workers(opts):
    import os
    import sys
    import threading
    from types import SimpleNamespace
    from unittest import mock
    from testit import runner

    calls = []
    _exit_module("t4761_par_a.first", calls, 0)
    _probe_module("t4761_par_b.second", calls, setup_raises=False)
    root = os.path.dirname(os.path.abspath(__file__))
    record_a = SimpleNamespace(name="t4761_par_a", path=root)
    record_b = SimpleNamespace(name="t4761_par_b", path=root)
    files = {"t4761_par_a": [("first", __file__)], "t4761_par_b": [("second", __file__)]}
    try:
        with _scratch_run() as (state, events), \
                mock.patch.object(runner, "_abort_event", threading.Event()), \
                mock.patch.object(runner, "save_checkpoint", lambda m, t: None), \
                mock.patch.object(runner, "_discover_record_files",
                                  lambda record: files[record.name]):
            template = _local_opts(test_modules=None)
            for record in (record_a, record_b):
                tracker = runner._ModuleTracker(record.name, 1)
                try:
                    runner._run_module_in_thread(template, record, tracker)
                except SystemExit as exc:
                    raise AssertionError(
                        f"SystemExit({exc.code!r}) escaped setup handling") from None
            assert calls == ["t4761_par_a.first:setup_guard"], (
                f"after one worker's setup exits, no other worker may start a file, got {calls}")
            assert state.aborted is True, "the stopped run must be marked aborted"
            codes = [r["code"] for r in runner._run_verdict(state)]
            assert codes == ["setup_error", "aborted"], \
                f"the parallel run must fail with setup_error and aborted, got {codes}"
    finally:
        sys.modules.pop("t4761_par_a.first", None)
        sys.modules.pop("t4761_par_b.second", None)
