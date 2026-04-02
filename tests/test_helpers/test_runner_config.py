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
    module_path = os.path.join(test_root, "test_accounts")

    config = _load_module_config(module_path)
    th.assert_true(config.serial is True, "test_accounts should be serial=True")
    th.assert_true("mojo.apps.account" in config.requires_apps,
                    "test_accounts should require mojo.apps.account")


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

    # Reset to known state
    with helpers._lock:
        helpers.TEST_RUN.total = 0

    threads = []
    for _ in range(100):
        t = threading.Thread(target=helpers._increment, args=("total",))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    # The total might include counts from other tests in this run,
    # so just verify it increased by at least 100
    th.assert_true(helpers.TEST_RUN.total >= 100,
                   f"Expected total >= 100 after 100 concurrent increments, got {helpers.TEST_RUN.total}")


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
