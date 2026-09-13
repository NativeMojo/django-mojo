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
