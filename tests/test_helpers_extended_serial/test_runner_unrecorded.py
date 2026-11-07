"""Discovered-versus-recorded accounting (item #2774).

Every counted test ends recorded, skipped/not run, unrecorded or aborted, and
for every module `tests == passed + failed + skipped + unrecorded`. Fixture
test files are real files in a temporary package, because the per-file count
is read from the file, and they run through the real thread runner (rich
tracker) and the real serial runner (no tracker).
"""
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
        stop_on_fail = helpers.STOP_ON_FAIL
        helpers.TEST_RUN = objict(
            total=0, passed=0, failed=0, skipped=0, results={}, records=[],
            unrecorded=[], started_at=1.0, finished_at=2.0, aborted=False,
        )
        helpers.STOP_ON_FAIL = False
        try:
            yield helpers.TEST_RUN
        finally:
            helpers.TEST_RUN = previous
            helpers._set_active_test(active)
            helpers._set_display_fn(display)
            helpers.VERBOSE = verbose
            helpers.STOP_ON_FAIL = stop_on_fail
    return isolated()


def _package(files):
    """A temporary importable test package holding `files` ({stem: source})."""
    import contextlib
    import os
    import shutil
    import sys
    import tempfile
    import textwrap
    import uuid
    from types import SimpleNamespace

    @contextlib.contextmanager
    def made():
        root = tempfile.mkdtemp(prefix="t2774_")
        name = f"t2774_{uuid.uuid4().hex[:10]}"
        path = os.path.join(root, name)
        os.makedirs(path)
        with open(os.path.join(path, "__init__.py"), "w") as fh:
            fh.write("")
        for stem, source in files.items():
            with open(os.path.join(path, f"{stem}.py"), "w") as fh:
                fh.write(textwrap.dedent(source))
        sys.path.insert(0, root)
        try:
            yield SimpleNamespace(name=name, path=path)
        finally:
            sys.path.remove(root)
            for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
                sys.modules.pop(key, None)
            shutil.rmtree(root, ignore_errors=True)
    return made()


def _opts(record):
    from types import SimpleNamespace
    return SimpleNamespace(
        verbose=False, errors=False, stop=False, host="http://127.0.0.1:9",
        logger=None, test_modules=[record.name], select_all=True,
        extra_list=[], extra=[], selected_tags=None)


def _run(record, tracked):
    """Run every file of `record`, then build the agent report.

    tracked=True goes through _run_module_in_thread with a _ModuleTracker
    whose total is the static count, as the rich runner sets it up.
    tracked=False goes through run_tests_for_record with no display, as the
    plain runner and file selections do.
    """
    from types import SimpleNamespace
    from testit import runner, helpers

    with _scratch_run() as state:
        escaped = None
        if tracked:
            tracker = runner._ModuleTracker(record.name, runner._count_record_tests(record))
            try:
                runner._run_module_in_thread(_opts(record), record, tracker)
            except Exception as err:
                escaped = err
            display = SimpleNamespace(trackers={record.name: tracker}, _order=[record.name])
        else:
            helpers._set_display_fn(None)
            try:
                runner.run_tests_for_record(_opts(record), record)
            except Exception as err:
                escaped = err
            display = SimpleNamespace(trackers={}, _order=[])
        report = runner._build_agent_report(SimpleNamespace(), display=display)
        return SimpleNamespace(
            report=report,
            entry=report["modules"].get(record.name, {}),
            codes=[r["code"] for r in report["reasons"]],
            unrecorded=list(getattr(state, "unrecorded", None) or []),
            records=list(state.records),
            escaped=escaped,
            mode="tracked" if tracked else "untracked",
        )


def _counts(result):
    entry = result.entry
    return (entry.get("tests"), entry.get("passed", 0), entry.get("failed", 0),
            entry.get("skipped", 0), entry.get("unrecorded", 0))


def _check(result, expected):
    """Assert the module's (tests, passed, failed, skipped, unrecorded) and the invariant."""
    assert result.escaped is None, (
        f"[{result.mode}] no exception may escape the runner, got {result.escaped!r}")
    tests, passed, failed, skipped, unrecorded = _counts(result)
    assert tests == passed + failed + skipped + unrecorded, (
        f"[{result.mode}] tests must equal passed + failed + skipped + unrecorded: "
        f"{result.entry}")
    assert _counts(result) == expected, (
        f"[{result.mode}] expected (tests, passed, failed, skipped, unrecorded) "
        f"{expected}, got {_counts(result)}: {result.entry}")


CONTROL = """
from testit import helpers as th

@th.unit_test("control")
def test_one(opts):
    pass
"""

TEARDOWN = """
from testit import helpers as th

CALLS = []

@th.unit_test("control")
def test_one(opts):
    CALLS.append("test_one")

@th.unit_setup()
def test_zz_teardown(opts):
    CALLS.append("teardown")
"""

TEARDOWN_ALIASED = """
from testit import helpers as th

CALLS = []
teardown = th.unit_setup

@th.unit_test("control")
def test_one(opts):
    CALLS.append("test_one")

@teardown()
def test_zz_teardown(opts):
    CALLS.append("teardown")
"""

TEARDOWN_RAISES = """
from testit import helpers as th

@th.unit_test("control")
def test_one(opts):
    pass

@th.unit_setup()
def test_zz_teardown(opts):
    raise RuntimeError("teardown broke")
"""


def _calls(record, stem):
    import sys
    return list(sys.modules[f"{record.name}.{stem}"].CALLS)


@th.unit_test("control: a normal unit_test records one outcome and passes")
def test_control_unit_test(opts):
    for tracked in (True, False):
        with _package({"1_control": CONTROL}) as record:
            result = _run(record, tracked)
            _check(result, (1, 1, 0, 0, 0))
            assert result.codes == [], f"[{result.mode}] the control run passes, got {result.codes}"


@th.unit_test("setup-decorated test_ teardown: runs once as a step, not counted")
def test_setup_decorated_teardown_is_a_step(opts):
    for source in (TEARDOWN, TEARDOWN_ALIASED):
        for tracked in (True, False):
            with _package({"1_teardown": source}) as record:
                result = _run(record, tracked)
                _check(result, (1, 1, 0, 0, 0))
                assert result.codes == [], (
                    f"[{result.mode}] a returning teardown step is not a missing test, "
                    f"got {result.codes}")
                assert _calls(record, "1_teardown") == ["test_one", "teardown"], (
                    f"[{result.mode}] the teardown runs once, after the test, got "
                    f"{_calls(record, '1_teardown')}")


@th.unit_test("setup-decorated test_ teardown that raises: unrecorded, never failed")
def test_setup_decorated_teardown_raising_is_unrecorded(opts):
    for tracked in (True, False):
        with _package({"1_teardown": TEARDOWN_RAISES}) as record:
            result = _run(record, tracked)
            _check(result, (2, 1, 0, 0, 1))
            assert result.codes == ["unrecorded"], (
                f"[{result.mode}] a raising teardown turns the run red as unrecorded, "
                f"got {result.codes}")
            message = result.report["reasons"][0]["message"]
            assert f"{record.name}.1_teardown.test_zz_teardown" in message, (
                f"[{result.mode}] the reason names module.file.function, got {message!r}")
            assert "RuntimeError" in message, (
                f"[{result.mode}] the reason names the exception type, got {message!r}")


@th.unit_test("django_unit_setup on a test_: a step when it returns, unrecorded when it raises")
def test_django_setup_decorated_test(opts):
    returning = """
    from testit import helpers as th

    @th.django_unit_test("control")
    def test_one(opts):
        pass

    @th.django_unit_setup()
    def test_zz_teardown(opts):
        pass
    """
    raising = """
    from testit import helpers as th

    @th.django_unit_test("control two")
    def test_two(opts):
        pass

    @th.django_unit_setup()
    def test_zz_teardown(opts):
        raise KeyError("teardown broke")
    """
    for tracked in (True, False):
        with _package({"1_returns": returning, "2_raises": raising}) as record:
            result = _run(record, tracked)
            _check(result, (3, 2, 0, 0, 1))
            assert [(u["test_module"], u["function"], u["cause"]) for u in result.unrecorded] == [
                ("2_raises", "test_zz_teardown", "KeyError")], (
                f"[{result.mode}] only the raising teardown is unrecorded, got {result.unrecorded}")


@th.unit_test("undecorated test_: unrecorded whether it returns or raises; the run fails")
def test_undecorated_test_is_unrecorded(opts):
    source = """
    def test_returns(opts):
        pass

    def test_raises(opts):
        raise ValueError("no wrapper")
    """
    for tracked in (True, False):
        with _package({"1_plain": source}) as record:
            result = _run(record, tracked)
            _check(result, (2, 0, 0, 0, 2))
            causes = {u["function"]: u["cause"] for u in result.unrecorded}
            assert causes == {"test_returns": "returned", "test_raises": "ValueError"}, (
                f"[{result.mode}] each unrecorded test names its cause, got {result.unrecorded}")
            assert result.report["status"] == "failed", (
                f"[{result.mode}] a run whose tests record nothing is never green")
            assert result.codes == ["unrecorded"], (
                f"[{result.mode}] the verdict says unrecorded, not 'nothing selected', "
                f"got {result.codes}")


@th.unit_test("django_unit_test whose django.setup raises: unrecorded with the exception type")
def test_django_setup_failure_is_unrecorded(opts):
    from unittest import mock
    source = """
    from testit import helpers as th

    @th.django_unit_test("needs django")
    def test_db(opts):
        pass
    """
    for tracked in (True, False):
        with _package({"1_db": source}) as record:
            with mock.patch("django.setup", side_effect=RuntimeError("settings missing")):
                result = _run(record, tracked)
            _check(result, (1, 0, 0, 0, 1))
            assert [u["cause"] for u in result.unrecorded] == ["RuntimeError"], (
                f"[{result.mode}] the cause is the django.setup exception, got {result.unrecorded}")
            assert result.codes == ["unrecorded"], f"[{result.mode}] got {result.codes}"


@th.unit_test("a test that records two outcomes counts both, once each")
def test_extra_outcomes_are_counted(opts):
    source = """
    from testit import helpers as th

    @th.unit_test("inner")
    def helper_inner(opts):
        pass

    @th.unit_test("outer")
    def test_outer(opts):
        helper_inner(opts)
    """
    for tracked in (True, False):
        with _package({"1_nested": source}) as record:
            result = _run(record, tracked)
            _check(result, (2, 2, 0, 0, 0))
            assert result.codes == [], f"[{result.mode}] two passing outcomes pass, got {result.codes}"


@th.unit_test("a counted def that is never collected is unrecorded as not_collected")
def test_shadowed_duplicate_is_not_collected(opts):
    source = """
    from testit import helpers as th

    @th.unit_test("a")
    def test_a(opts):
        pass

    @th.unit_test("dup first")
    def test_dup(opts):
        pass

    @th.unit_test("dup second")
    def test_dup(opts):
        pass
    """
    for tracked in (True, False):
        with _package({"1_dup": source}) as record:
            result = _run(record, tracked)
            _check(result, (3, 2, 0, 0, 1))
            assert [(u["function"], u["cause"], u["count"]) for u in result.unrecorded] == [
                ("*", "not_collected", 1)], (
                f"[{result.mode}] the shadowed def is not_collected, got {result.unrecorded}")
            assert result.codes == ["unrecorded"], f"[{result.mode}] got {result.codes}"


@th.unit_test("a file stopped by an unexpected exception: its tests unrecorded, the next file runs")
def test_file_stopped_by_exception(opts):
    from unittest import mock
    from testit import runner

    first = """
    from testit import helpers as th

    @th.unit_test("one")
    def test_one(opts):
        pass

    @th.unit_test("two")
    def test_two(opts):
        pass
    """
    second = """
    from testit import helpers as th

    @th.unit_test("three")
    def test_three(opts):
        pass
    """
    original = runner.run_module_tests

    def breaks_first(opts, module, test_name, module_name, setup_failure=None):
        if test_name == "1_boom":
            raise RuntimeError("runner broke")
        return original(opts, module, test_name, module_name, setup_failure=setup_failure)

    for tracked in (True, False):
        with _package({"1_boom": first, "2_after": second}) as record:
            with mock.patch.object(runner, "run_module_tests", side_effect=breaks_first):
                result = _run(record, tracked)
            _check(result, (3, 1, 0, 0, 2))
            assert [(u["test_module"], u["function"], u["cause"], u["count"])
                    for u in result.unrecorded] == [("1_boom", "*", "RuntimeError", 2)], (
                f"[{result.mode}] the stopped file's tests are unrecorded, got {result.unrecorded}")
            assert [r["name"] for r in result.records] == ["three"], (
                f"[{result.mode}] the next file still runs, got {result.records}")
            assert result.codes == ["unrecorded"], f"[{result.mode}] got {result.codes}"


@th.unit_test("setup failure and setup skip: a setup-decorated test_ is not a skipped test")
def test_setup_paths_do_not_count_setup_steps(opts):
    failing = """
    from testit import helpers as th

    @th.unit_setup()
    def setup_boom(opts):
        raise RuntimeError("setup broke")

    @th.unit_test("one")
    def test_one(opts):
        pass

    @th.unit_setup()
    def test_zz_teardown(opts):
        pass
    """
    skipping = """
    from testit import helpers as th

    @th.unit_setup()
    def setup_skip(opts):
        raise th.TestitSkip("not here")

    @th.unit_test("one")
    def test_one(opts):
        pass

    @th.unit_setup()
    def test_zz_teardown(opts):
        pass
    """
    for tracked in (True, False):
        with _package({"1_fail": failing}) as record:
            result = _run(record, tracked)
            # One not-run test plus the one setup_error record.
            _check(result, (2, 0, 1, 1, 0))
            assert result.codes == ["setup_error"], (
                f"[{result.mode}] #4761's setup_error stays the only reason, got {result.codes}")
            not_run = [r["name"] for r in result.records if r.get("kind") == "not_run"]
            assert not_run == ["one"], (
                f"[{result.mode}] only the real test is not run, got {not_run}")
        with _package({"1_skip": skipping}) as record:
            result = _run(record, tracked)
            _check(result, (1, 0, 0, 1, 0))
            skipped = [r["name"] for r in result.records if r["status"] == "skipped"]
            assert skipped == ["one"], (
                f"[{result.mode}] only the real test is a setup skip, got {skipped}")
            assert "unrecorded" not in result.codes, f"[{result.mode}] got {result.codes}"


@th.unit_test("verdict: no unrecorded field, or an empty list, adds no reason")
def test_verdict_without_unrecorded(opts):
    from objict import objict
    from testit import runner

    base = dict(total=1, passed=1, failed=0, skipped=0, records=[], aborted=False)
    assert runner._run_verdict(objict(**base)) == [], \
        "a TEST_RUN built without the unrecorded field passes"
    assert runner._run_verdict(objict(unrecorded=[], **base)) == [], \
        "an empty unrecorded list passes"
