"""A refused launch never leaves an older report looking current (item #8052).

Every test here works in its own temp var root; none touches the live run's
lock or report. The launcher tests run a copy of bin/testit.py in a temp
project whose `mojo` and `testit` link back to this checkout.
"""
from testit import helpers as th


def _repo_root():
    import os
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _runlock_path():
    import os
    return os.path.join(_repo_root(), "testit", "runlock.py")


def _scratch_var_root():
    """A temp directory standing in for paths.VAR_ROOT for one test."""
    import contextlib
    import tempfile
    from unittest import mock
    from mojo.helpers import paths

    @contextlib.contextmanager
    def scratch():
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(paths, "VAR_ROOT", tmp):
            yield tmp
    return scratch()


def _lock_is_free(var_root):
    """True when a fresh open file description can take the lock."""
    import fcntl
    import os
    fd = os.open(os.path.join(var_root, "testit.lock"), os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


_HOLDER = r'''
import importlib.util, sys, time
spec = importlib.util.spec_from_file_location("runlock_probe", sys.argv[1])
runlock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runlock)
claim, holder = runlock.acquire(sys.argv[2])
print("HELD" if claim else "BUSY", flush=True)
sys.stdin.read()
'''


@th.django_unit_test("run lock: a launch refused by a live run leaves the report byte-identical")
def test_live_lock_leaves_report_byte_identical(opts):
    import os
    import subprocess
    import sys
    from testit import runner

    with _scratch_var_root() as tmp:
        report = os.path.join(tmp, "test_failures.json")
        with open(report, "w", encoding="utf-8") as fh:
            fh.write('{"status": "passed", "total": 7}')
        os.utime(report, (1_000_000_000, 1_000_000_000))
        before = os.stat(report)
        with open(report, "rb") as fh:
            before_bytes = fh.read()

        holder = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, _runlock_path(), tmp],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            line = holder.stdout.readline().strip()
            assert line == "HELD", f"the holder subprocess must take the lock, got {line!r}"
            code = None
            try:
                runner.claim_run([], var_root=tmp)
            except SystemExit as exit_:
                code = exit_.code
            assert code == 1, f"a launch refused by a live run must exit 1, got {code!r}"
        finally:
            holder.stdin.close()
            holder.wait(timeout=10)

        after = os.stat(report)
        with open(report, "rb") as fh:
            after_bytes = fh.read()
        assert after_bytes == before_bytes, "the live run's report bytes must be unchanged"
        assert after.st_ino == before.st_ino, "the report must not have been replaced (inode)"
        assert after.st_mtime == before.st_mtime, "the report must not have been rewritten (mtime)"
        leftovers = [name for name in os.listdir(tmp) if name.endswith(".tmp")]
        assert not leftovers, f"a refused launch must leave no temp file, found {leftovers}"


@th.django_unit_test("run lock: the pending marker keeps longest-first duration history")
def test_marker_keeps_duration_history(opts):
    import json
    import os
    from testit import runner, runlock

    with _scratch_var_root() as tmp:
        with open(os.path.join(tmp, "test_failures.json"), "w", encoding="utf-8") as fh:
            json.dump({"status": "passed", "modules": {
                "test_big": {"duration": 42.5}, "test_small": {"duration": 1.25}}}, fh)
        identity = runner.claim_run([], var_root=tmp)
        try:
            assert identity is not None, "a plain run must return its run identity"
            with open(os.path.join(tmp, "test_failures.json"), encoding="utf-8") as fh:
                marker = json.load(fh)
            assert marker["modules"] == {}, \
                f"the marker replaces the old report, got modules={marker['modules']}"
            durations = runner._previous_module_durations()
            assert durations == {"test_big": 42.5, "test_small": 1.25}, \
                f"the claimed run must still see the previous durations, got {durations}"
        finally:
            runlock.release(runlock.current(tmp))


_RACER = r'''
import importlib.util, os, sys, time
spec = importlib.util.spec_from_file_location("runlock_probe", sys.argv[1])
runlock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runlock)
go = sys.argv[3]
while not os.path.exists(go):
    time.sleep(0.005)
claim, holder = runlock.acquire(sys.argv[2])
print("WIN" if claim else "LOSE", flush=True)
time.sleep(1.5)
'''


@th.django_unit_test("run lock: four simultaneous launches, exactly one wins")
def test_simultaneous_claims_one_winner(opts):
    import os
    import subprocess
    import sys
    import tempfile
    import time

    with tempfile.TemporaryDirectory() as tmp:
        go = os.path.join(tmp, "go")
        var_root = os.path.join(tmp, "var")
        procs = [subprocess.Popen(
            [sys.executable, "-c", _RACER, _runlock_path(), var_root, go],
            stdout=subprocess.PIPE, text=True) for _ in range(4)]
        time.sleep(0.5)
        open(go, "w").close()
        results = []
        for proc in procs:
            out, _ = proc.communicate(timeout=30)
            results.append(out.strip())
        assert sorted(results) == ["LOSE", "LOSE", "LOSE", "WIN"], \
            f"exactly one of four simultaneous launches must win, got {results}"
        assert os.path.exists(os.path.join(var_root, "testit.lock")), \
            "the lock file stays in place after release"


@th.django_unit_test("run lock: an isolation-policy refusal writes its record and frees the lock")
def test_policy_refusal_writes_record_and_releases(opts):
    import json
    import os
    import tempfile
    from testit import runner, runlock

    with _scratch_var_root() as tmp, tempfile.TemporaryDirectory() as tests_root:
        package = os.path.join(tests_root, "test_undeclared")
        os.makedirs(package)
        with open(os.path.join(package, "__init__.py"), "w") as fh:
            fh.write("# no TESTIT declaration\n")
        with open(os.path.join(package, "sample.py"), "w") as fh:
            fh.write("def test_x(opts):\n    pass\n")

        claim, _ = runlock.acquire(tmp)
        runlock.mark_pending(claim)
        message = None
        try:
            runner._enforce_policies(claim, tests_root, None)
        except SystemExit as exit_:
            message = exit_.code
        finally:
            if runlock.current(tmp) is not None:
                runlock.release(runlock.current(tmp))

        assert isinstance(message, str) and "no tests were run" in message, \
            f"the policy must still exit with today's message, got {message!r}"
        with open(os.path.join(tmp, "test_failures.json"), encoding="utf-8") as fh:
            record = json.load(fh)
        assert record["status"] == "refused", f"expected status refused, got {record['status']!r}"
        codes = [reason["code"] for reason in record["reasons"]]
        assert codes == ["isolation_policy"], f"expected the isolation_policy reason, got {codes}"
        assert record["reasons"][0]["message"] == message, \
            "the record carries the same message the run exits with"
        assert record["total"] == 0 and record["modules"] == {}, \
            "a refusal record carries no test results"
        assert record["run"]["run_id"] == claim.identity["run_id"], \
            "the record names the run that refused"
        assert claim.released, "the refusing run must release its claim"
        assert _lock_is_free(tmp), "the lock must be free after a policy refusal"


@th.django_unit_test("run lock: a completed run replaces its own marker and cannot write after release")
def test_completed_run_overwrites_its_own_marker(opts):
    import json
    import os
    from testit import runlock

    with _scratch_var_root() as tmp:
        claim, _ = runlock.acquire(tmp)
        try:
            runlock.mark_pending(claim)
            with open(os.path.join(tmp, "test_failures.json"), encoding="utf-8") as fh:
                marker = json.load(fh)
            assert marker["status"] == "refused", "the marker reads refused until replaced"
            assert marker["reasons"][0]["code"] == "not_completed", \
                f"the marker's reason is not_completed, got {marker['reasons']}"
            runlock.write_report(claim, {"status": "passed", "total": 3})
            with open(os.path.join(tmp, "test_failures.json"), encoding="utf-8") as fh:
                final = json.load(fh)
            assert final["status"] == "passed", "the completed report replaces the marker"
            assert final["run"]["run_id"] == marker["run"]["run_id"], \
                "marker and final report must share one run_id"
        finally:
            runlock.release(claim)
        raised = False
        try:
            runlock.write_report(claim, {"status": "passed"})
        except runlock.ClaimReleased:
            raised = True
        assert raised, "write_report must refuse once the claim is released"
        assert _lock_is_free(tmp), "release must free the lock"


@th.django_unit_test("run lock: the refusal record has every key a run writes, and is never pushed")
def test_refusal_record_shape_and_readers(opts):
    import ast
    import inspect
    import textwrap
    from types import SimpleNamespace
    from testit import runner, runlock

    empty = SimpleNamespace(trackers={}, _order=[])
    built = runner._build_agent_report(SimpleNamespace(), display=empty)
    # Keys main adds after building the report (budget_violations, slow_stacks,
    # and anything a later item adds), read from main's own source so the
    # refusal record cannot quietly fall behind it.
    tree = ast.parse(textwrap.dedent(inspect.getsource(runner.main)))
    added = {node.slice.value for node in ast.walk(tree)
             if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store)
             and isinstance(node.value, ast.Name) and node.value.id == "report"
             and isinstance(node.slice, ast.Constant)}
    assert {"budget_violations", "slow_stacks"} <= added, \
        f"main's added keys should be found, got {sorted(added)}"
    record = runlock.refusal_record({"pid": 1, "run_id": "r", "started_at": 1.0},
                                    "bad_arguments", "  bad")
    missing = (set(built) | added | {"run"}) - set(record)
    assert not missing, f"the refusal record lacks report keys {sorted(missing)}"
    assert record["status"] == "refused", "a refusal record's status is refused"
    assert record["status"] not in ("passed", "failed"), "refused is its own status"
    push_opts = SimpleNamespace(test_modules=[], ignore_modules=[])
    assert runner._push_refused(push_opts, record) == "run was refused", \
        "a refused report must never be pushed to maestro"


@th.django_unit_test("run lock: every report names the run that wrote it")
def test_every_report_names_its_run(opts):
    import os
    from types import SimpleNamespace
    from mojo.helpers import paths
    from testit import runner, runlock

    empty = SimpleNamespace(trackers={}, _order=[])
    report = runner._build_agent_report(SimpleNamespace(), display=empty)
    run = report.get("run")
    assert run is not None, "the live run's report must carry a run identity"
    assert run["pid"] == os.getpid(), \
        f"run.pid must be the live runner's pid {os.getpid()}, got {run['pid']}"
    holder = runlock.read_holder(paths.VAR_ROOT)
    assert holder.get("run_id") == run["run_id"], \
        "the report's run_id must match the lock file's holder"
    for key in ("started", "started_at", "user", "token"):
        assert key in run, f"run identity must carry {key!r}, got {sorted(run)}"


# --- launcher copies --------------------------------------------------------

_BROKEN_SETTINGS = 'raise RuntimeError("settings deliberately broken for #8052")\n'

_MINIMAL_SETTINGS = '''
SECRET_KEY = "launcher-copy"
INSTALLED_APPS = []
DATABASES = {{"default": {{"ENGINE": "django.db.backends.sqlite3",
                           "NAME": {db!r}}}}}
# Port 1 refuses at once; testenv's Redis guard fails open on an unreachable server.
REDIS_URL = "redis://127.0.0.1:1/5"
REDIS_PUBSUB_PREFIX = {prefix!r}
'''


def _launcher_copy(root, settings_body=None):
    """A temp project with a copy of bin/testit.py. settings_body None means
    no testproject at all."""
    import os
    import shutil
    repo = _repo_root()
    os.makedirs(os.path.join(root, "bin"))
    shutil.copy(os.path.join(repo, "bin", "testit.py"), os.path.join(root, "bin", "testit.py"))
    os.symlink(os.path.join(repo, "mojo"), os.path.join(root, "mojo"))
    os.symlink(os.path.join(repo, "testit"), os.path.join(root, "testit"))
    if settings_body is not None:
        settings_dir = os.path.join(root, "testproject", "config", "settings")
        os.makedirs(settings_dir)
        with open(os.path.join(settings_dir, "__init__.py"), "w") as fh:
            fh.write(settings_body)
    return os.path.join(root, "bin", "testit.py")


def _run_launcher(launcher, *args, token="t-8052"):
    import os
    import subprocess
    import sys
    env = dict(os.environ)
    env.pop("DJANGO_SETTINGS_MODULE", None)
    env["TESTIT_RUN_TOKEN"] = token
    return subprocess.run([sys.executable, launcher, *args], env=env, cwd=os.path.dirname(launcher),
                          capture_output=True, text=True, timeout=120)


@th.django_unit_test("launcher: a missing testproject exits 1 and creates nothing")
def test_launcher_missing_testproject_exits_cleanly(opts):
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as root:
        launcher = _launcher_copy(root)
        result = _run_launcher(launcher)
        assert result.returncode == 1, \
            f"expected exit 1, got {result.returncode}: {result.stdout}{result.stderr}"
        assert "testproject/ not found" in result.stdout, \
            f"the launcher must say why, got {result.stdout!r}"
        assert not os.path.exists(os.path.join(root, "testproject")), \
            "a missing-testproject exit must not create any folder"


@th.django_unit_test("launcher: a django.setup() failure replaces an old pass with a refusal")
def test_launcher_django_setup_refusal_writes_record(opts):
    import json
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as root:
        launcher = _launcher_copy(root, _BROKEN_SETTINGS)
        var_root = os.path.join(root, "testproject", "var")
        os.makedirs(var_root)
        report = os.path.join(var_root, "test_failures.json")
        with open(report, "w", encoding="utf-8") as fh:
            json.dump({"status": "passed", "total": 99, "passed": 99}, fh)

        result = _run_launcher(launcher, "--no-maestro")
        assert result.returncode != 0, \
            f"a broken settings module must exit non-zero, got 0: {result.stdout}"
        with open(report, encoding="utf-8") as fh:
            record = json.load(fh)
        assert record["status"] == "refused", \
            f"the old pass must not survive, got status {record['status']!r}"
        codes = [reason["code"] for reason in record["reasons"]]
        assert codes == ["django_setup"], f"expected the django_setup reason, got {codes}"
        assert "deliberately broken" in record["reasons"][0]["message"], \
            f"the reason must name the cause, got {record['reasons'][0]['message']!r}"
        assert record["run"]["pid"], "the record names the run that refused"
        assert record["run"]["token"] == "t-8052", \
            f"the run carries the launcher's TESTIT_RUN_TOKEN, got {record['run'].get('token')!r}"
        assert _lock_is_free(var_root), "the lock must be free after the refusal"


@th.django_unit_test("launcher: --list-extras takes no claim and never flushes")
def test_list_extras_does_not_flush(opts):
    import importlib.util
    import json
    import os
    import tempfile

    spec = importlib.util.spec_from_file_location(
        "testenv_probe", os.path.join(_repo_root(), "testit", "testenv.py"))
    testenv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(testenv)

    with tempfile.TemporaryDirectory() as root:
        settings = _MINIMAL_SETTINGS.format(
            db=os.path.join(root, "db.sqlite3"),
            prefix=testenv.expected_pubsub_prefix(root))
        launcher = _launcher_copy(root, settings)
        var_root = os.path.join(root, "testproject", "var")
        os.makedirs(var_root)
        report = os.path.join(var_root, "test_failures.json")
        with open(report, "w", encoding="utf-8") as fh:
            json.dump({"status": "passed", "total": 5}, fh)

        result = _run_launcher(launcher, "--list-extras", "--no-maestro")
        output = result.stdout + result.stderr
        assert "Flushing database and Redis" not in output, \
            f"--list-extras must never reset test state, got:\n{output}"
        with open(report, encoding="utf-8") as fh:
            kept = json.load(fh)
        assert kept["status"] == "passed" and "run" not in kept, \
            f"--list-extras is not a test run and must not touch the report, got {kept}"
        assert _lock_is_free(var_root), "--list-extras must not keep the lock"
        assert result.returncode == 0, \
            f"--list-extras should succeed, got {result.returncode}:\n{output}"
