import json
import inspect
import os
import threading
import time
import functools
import traceback
import contextlib
from objict import objict
from mojo.helpers import logit


_lock = threading.Lock()

TEST_RUN = objict(
    total=0,
    passed=0,
    failed=0,
    skipped=0,
    tests=objict(active_test=None),
    results={},
    records=[],
    started_at=None,
    finished_at=None,
)
STOP_ON_FAIL = True
VERBOSE = False
INDENT = "    "

# Display callback — per-thread so parallel modules don't overwrite each other.
# Signature: display_fn(event, **kwargs)
# Events: "test_result", "setup_progress", "setup_done"
_thread_local = threading.local()

# Agent mode — when True, collect structured failure context
AGENT_MODE = False


def _get_display_fn():
    return getattr(_thread_local, "display_fn", None)


def _set_display_fn(fn):
    _thread_local.display_fn = fn


class TestitAbort(Exception):
    pass


class TestitSkip(Exception):
    pass


def is_app_installed(app_label):
    """Check if a Django app is in INSTALLED_APPS."""
    from django.apps import apps
    return apps.is_installed(app_label)


def requires_app(app_label):
    """
    Decorator that skips a test or setup function if the given app is not installed.
    Use for optional apps (e.g. chat) that may not be in INSTALLED_APPS.

    Usage:
        @th.requires_app("mojo.apps.chat")
        @th.django_unit_setup()
        def setup_chat(opts):
            ...
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if not is_app_installed(app_label):
                raise TestitSkip(f"app '{app_label}' not installed")
            return func(*args, **kwargs)
        # Preserve testit attributes
        for attr in ("_test_name", "_requires_extra"):
            if hasattr(func, attr):
                setattr(wrapper, attr, getattr(func, attr))
        return wrapper
    return decorator


def _run_setup(func, *args, **kwargs):
    name = kwargs.get("name", func.__name__)
    dfn = _get_display_fn()
    if dfn:
        dfn("setup_progress", name=name)
    else:
        logit.color_print(f"{INDENT}{name.ljust(80, '.')}", logit.ConsoleLogger.PINK, end="")
    res = func(*args, **kwargs)
    dfn = _get_display_fn()
    if dfn:
        dfn("setup_done", name=name)
    else:
        logit.color_print("DONE", logit.ConsoleLogger.PINK, end="\n")
    return res


def unit_setup():
    """
    Decorator to mark a function as a test setup function.
    Will be run before each test in the test class.

    Usage:
    @unit_setup()
    def setup():
        # Setup code here
        pass
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return _run_setup(func, *args, **kwargs)
        wrapper._is_setup = True
        return wrapper
    return decorator


def django_unit_setup():
    """
    Decorator to mark a function as a test setup function.
    Will be run before each test in the test class.

    Usage:
    @django_setup()
    def setup():
        # Setup code here
        pass
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            import os
            import django
            os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'settings')
            django.setup()
            return _run_setup(func, *args, **kwargs)
        wrapper._is_setup = True
        return wrapper
    return decorator


def _increment(field, value=1):
    """Thread-safe increment of TEST_RUN counters."""
    with _lock:
        current = getattr(TEST_RUN, field, 0)
        setattr(TEST_RUN, field, current + value)


def _collect_failure_context(func, test_name, error, status):
    """Collect structured context for agent mode failure reports."""
    context = {
        "test_name": test_name,
        "function": func.__name__,
        "status": status,
        "assertion": str(error),
    }
    # Source code of the test function
    try:
        context["test_source"] = inspect.getsource(func)
        source_file = inspect.getfile(func)
        _, line_no = inspect.getsourcelines(func)
        context["file_path"] = source_file
        context["line"] = line_no
    except (OSError, TypeError):
        pass

    # Traceback
    if status == "error":
        context["traceback"] = traceback.format_exc()

    return context


def _run_unit(func, name, *args, **kwargs):
    if TEST_RUN.started_at is None:
        TEST_RUN.started_at = time.time()
    _increment("total")
    if name:
        test_name = name
    else:
        test_name = kwargs.get("test_name", func.__name__)
        if test_name.startswith("test_"):
            test_name = test_name[5:]

    # Print test start message
    name_line = f"{INDENT}{test_name.ljust(80, '.')}"

    try:
        result = func(*args, **kwargs)
        _record_result(test_name, status="passed")
        _increment("passed")
        dfn = _get_display_fn()
        if dfn:
            dfn("test_result", name=test_name, status="passed")
        else:
            logit.color_print(f"{name_line}PASSED", logit.ConsoleLogger.GREEN, end="\n")
        return result

    except TestitSkip as skip:
        _record_result(test_name, status="skipped", detail=str(skip))
        _increment("skipped")
        dfn = _get_display_fn()
        if dfn:
            dfn("test_result", name=test_name, status="skipped", detail=str(skip))
        else:
            logit.color_print(f"{name_line}SKIPPED", logit.ConsoleLogger.BLUE, end="\n")
            if str(skip):
                logit.color_print(f"{INDENT}{INDENT}{skip}", logit.ConsoleLogger.BLUE)
        return None

    except AssertionError as error:
        _increment("failed")
        fail_context = _collect_failure_context(func, test_name, error, "failed") if AGENT_MODE else None
        _record_result(test_name, status="failed", detail=str(error), agent_context=fail_context)

        dfn = _get_display_fn()
        if dfn:
            dfn("test_result", name=test_name, status="failed", detail=str(error))
        else:
            logit.color_print(f"{name_line}FAILED", logit.ConsoleLogger.RED, end="\n")
            logit.color_print(f"{INDENT}{INDENT}{error}", logit.ConsoleLogger.PINK)

        if STOP_ON_FAIL:
            raise TestitAbort()

    except Exception as error:
        _increment("failed")
        detail = traceback.format_exc() if VERBOSE else str(error)
        fail_context = _collect_failure_context(func, test_name, error, "error") if AGENT_MODE else None
        _record_result(test_name, status="error", detail=detail, agent_context=fail_context)

        dfn = _get_display_fn()
        if dfn:
            dfn("test_result", name=test_name, status="error", detail=detail)
        else:
            logit.color_print(f"{name_line}FAILED", logit.ConsoleLogger.RED, end="\n")
            if VERBOSE:
                logit.color_print(traceback.format_exc(), logit.ConsoleLogger.PINK)
        if STOP_ON_FAIL:
            raise TestitAbort()
    return False


def _set_active_test(key):
    _thread_local.active_test = key


def _active_context():
    active = getattr(_thread_local, "active_test", None) or ""
    parts = active.split(":") if active else []
    context = objict(
        active=active or None,
        module=parts[0] if len(parts) > 0 else None,
        test_module=parts[1] if len(parts) > 1 else None,
        function=parts[2] if len(parts) > 2 else None,
    )
    return context


def _result_key(test_name):
    context = _active_context()
    if context.active:
        return f"{context.active}:{test_name}"
    return test_name


def _record_result(test_name, *, status, detail=None, agent_context=None):
    context = _active_context()
    key = _result_key(test_name)
    record = {
        "module": context.module,
        "test_module": context.test_module,
        "function": context.function,
        "name": test_name,
        "status": status,
    }
    if detail:
        record["detail"] = detail
    if agent_context:
        record["agent_context"] = agent_context

    with _lock:
        if status == "passed":
            dict.__setitem__(TEST_RUN.results, key, True)
        elif status in {"failed", "error"}:
            dict.__setitem__(TEST_RUN.results, key, False)
        else:
            dict.__setitem__(TEST_RUN.results, key, None)
        TEST_RUN.records.append(record)


def _normalize_extra(extra):
    if extra is None:
        return set()
    if isinstance(extra, str):
        items = [part.strip() for part in extra.split(",")]
        return {item for item in items if item}
    if isinstance(extra, (list, tuple, set)):
        return {str(item).strip() for item in extra if str(item).strip()}
    if isinstance(extra, dict):
        return {str(key).strip() for key, value in extra.items() if value}
    return {str(extra).strip()} if str(extra).strip() else set()


def _extra_satisfied(extra, requirement):
    values = _normalize_extra(extra)
    if requirement is None:
        return bool(values)
    return requirement in values

# Test Decorator
def unit_test(name=None):
    """
    Decorator to track unit test execution.

    Usage:
    @unit_test("Custom Test Name")
    def my_test():
        assert 1 == 1
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            _run_unit(func, name, *args, **kwargs)
        if hasattr(func, "_requires_extra"):
            wrapper._requires_extra = getattr(func, "_requires_extra")
        return wrapper
    return decorator


def django_unit_test(arg=None):
    """
    Decorator to track unit test execution.

    Usage:
    @unit_test("Custom Test Name")
    def my_test():
        assert 1 == 1
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            import os
            import django
            os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'settings')
            django.setup()

            test_name = getattr(wrapper, '_test_name', None)
            if test_name is None:
                # Strip 'test_' if it exists
                test_name = func.__name__
                if test_name.startswith('test_'):
                    test_name = test_name[5:]

            _run_unit(func, test_name, *args, **kwargs)

        # Store the custom test name if provided
        if isinstance(arg, str):
            wrapper._test_name = arg
        if hasattr(func, "_requires_extra"):
            wrapper._requires_extra = getattr(func, "_requires_extra")
        return wrapper

    if callable(arg):
        # Used as @django_unit_test with no arguments
        return decorator(arg)
    else:
        # Used as @django_unit_test("name") or @django_unit_test()
        return decorator


def requires_extra(flag=None):
    """
    Decorator to short-circuit tests unless a matching --extra flag is provided.
    """
    def decorator(func):
        requirement = flag

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if not args:
                return func(*args, **kwargs)

            opts = args[0]
            extra = getattr(opts, "extra_list", None)
            if not extra:
                extra = getattr(opts, "extra", None)
            if _extra_satisfied(extra, requirement):
                return func(*args, **kwargs)

            display_name = getattr(wrapper, "_test_name", None)
            if display_name is None:
                display_name = wrapper.__name__
                if display_name.startswith("test_"):
                    display_name = display_name[5:]

            requirement_msg = (
                f"requires extra flag '{requirement}'" if requirement else "requires --extra data"
            )
            raise TestitSkip(requirement_msg)

        wrapper._test_name = getattr(func, "_test_name", None)
        wrapper._requires_extra = requirement
        return wrapper

    return decorator


def get_database_kind():
    """
    Returns the database engine kind as a short string: 'sqlite', 'postgresql', 'mysql', 'oracle', or the full engine string if unknown.
    Assumes Django is already configured (call only inside django_unit_test or django_unit_setup).
    """
    from django.conf import settings as django_settings
    engine = django_settings.DATABASES.get("default", {}).get("ENGINE", "")
    for kind in ("sqlite", "postgresql", "mysql", "oracle"):
        if kind in engine:
            return kind
    return engine


def is_sqlite():
    """
    Returns True if the default Django database is SQLite.
    Assumes Django is already configured (call only inside django_unit_test or django_unit_setup).
    """
    return get_database_kind() == "sqlite"


def reset_test_run():
    TEST_RUN.total = 0
    TEST_RUN.passed = 0
    TEST_RUN.failed = 0
    TEST_RUN.skipped = 0
    TEST_RUN.tests.active_test = None
    TEST_RUN.results = objict()
    TEST_RUN.records = []
    TEST_RUN.started_at = None
    TEST_RUN.finished_at = None


def save_results(path):
    payload = {
        "total": TEST_RUN.total,
        "passed": TEST_RUN.passed,
        "failed": TEST_RUN.failed,
        "skipped": TEST_RUN.skipped,
        "started_at": TEST_RUN.started_at,
        "finished_at": TEST_RUN.finished_at,
        "duration": None,
        "records": TEST_RUN.records,
        "results": dict(TEST_RUN.results),
    }
    if TEST_RUN.started_at and TEST_RUN.finished_at:
        payload["duration"] = TEST_RUN.finished_at - TEST_RUN.started_at

    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)


def get_mock_request(user=None, ip="127.0.0.1", path='/', method='GET', META=None):
    """
    Creates a mock Django request object with a user and request.ip information.

    Args:
        user (User, optional): A mock user object. Defaults to None.
        ip (str, optional): The IP address for the request. Defaults to "127.0.0.1".
        path (str, optional): The path for the request. Defaults to '/'.
        method (str, optional): The HTTP method for the request. Defaults to 'GET'.
        META (dict, optional): Additional metadata for the request.
                               Merges with default if provided. Defaults to None.

    Returns:
        objict: A mock request object with request.ip, request.user, and additional attributes.
    """
    request = objict()
    request.ip = ip
    request.user = user if user else get_mock_user()
    default_META = {
        'SERVER_PROTOCOL': 'HTTP/1.1',
        'QUERY_STRING': '',
        'HTTP_USER_AGENT': 'Mozilla/5.0',
        'HTTP_HOST': 'localhost',
    }
    request.META = {**default_META, **(META or {})}
    request.method = method
    request.path = path
    return request

def get_mock_user():
    """
    Creates a mock user object.

    Returns:
        objict: A mock user object with basic attributes.
    """
    from mojo.helpers import crypto
    user = objict()
    user.id = 1
    user.username = "mockuser"
    user.email = "mockuser@example.com"
    user.is_authenticated = True
    user.password = crypto.random_string(16)
    user.has_permission = lambda perm: True
    return user

def get_admin_user():
    """
    Creates a mock admin user object.

    Returns:
        objict: A mock admin user object with basic attributes.
    """
    user = get_mock_user()
    user.is_superuser = True
    user.is_staff = True
    return user


def assert_true(value, msg):
    assert bool(value), msg


def assert_eq(actual, expected, msg):
    assert actual == expected, f"{msg} | expected={expected} got={actual}"


def assert_in(item, container, msg):
    assert item in container, f"{msg} | missing={item} in {container}"


def expect(value, got, name="field"):
    assert value == got, f"{name} expected {value} got {got}"


def run_pending_jobs(channel=None, status="pending"):
    """
    Execute pending jobs from the DB the same way the job engine does.
    No Redis or running engine needed.

    Queries Job.objects.filter(status=status), optionally filtered by channel.
    For each job: imports the function via load_job_function(job.func),
    calls func(job) — exactly like job_engine.py:642.
    Marks job completed on success, failed on exception.

    Returns count of jobs executed.
    """
    from mojo.apps.jobs.models import Job
    from mojo.apps.jobs.job_engine import load_job_function

    qs = Job.objects.filter(status=status)
    if channel:
        qs = qs.filter(channel=channel)
    qs = qs.order_by("created")

    count = 0
    for job in qs:
        func = load_job_function(job.func)
        try:
            func(job)
            job.status = "completed"
            job.save(update_fields=["status", "modified"])
        except Exception:
            job.status = "failed"
            job.save(update_fields=["status", "modified"])
        count += 1
    return count


def _format_conf_value(value):
    if isinstance(value, bool):
        return 'True' if value else 'False'
    if isinstance(value, str):
        return f"'{value}'"
    return str(value)


def _conf_line_key(line):
    """The setting name a django.conf line assigns, or None if it assigns none.

    Same rules the settings loader uses (mojo/helpers/settings/parser.py):
    a line assigns when it contains '=' and is not a comment; the key is
    everything before the FIRST '='.
    """
    stripped = line.strip()
    if '=' in stripped and not stripped.startswith('#'):
        return stripped.split('=', 1)[0].strip()
    return None


def _join_conf_lines(parts):
    """Join conf lines, making sure only the last one may lack a newline."""
    out = []
    last = len(parts) - 1
    for index, part in enumerate(parts):
        if index < last and part and not part.endswith('\n'):
            part += '\n'
        out.append(part)
    return ''.join(out)


def _apply_conf_overrides(original_text, overrides):
    lines = original_text.splitlines(keepends=True)
    applied = set()
    result = []
    for line in lines:
        key = _conf_line_key(line)
        if key is not None and key in overrides:
            result.append(f"{key} = {_format_conf_value(overrides[key])}\n")
            applied.add(key)
            continue
        result.append(line)
    for key, value in overrides.items():
        if key not in applied:
            result.append(f"{key} = {_format_conf_value(value)}\n")
    return _join_conf_lines(result)


def _capture_conf_lines(text, keys):
    """Snapshot the exact source line that sets each key — None when unset.

    Only the overridden keys are recorded, never the whole file: that is what
    makes the restore subtractive and therefore safe against a concurrent
    context that is touching OTHER keys. Duplicate assignments capture the LAST
    one, matching the loader's last-wins parse.
    """
    snapshot = {key: None for key in keys}
    for line in text.splitlines(keepends=True):
        key = _conf_line_key(line)
        if key is not None and key in snapshot:
            snapshot[key] = line
    return snapshot


def _restore_conf_overrides(current_text, snapshot):
    """Undo an override against the file AS IT STANDS, key by key.

    For each captured key: put its original line back verbatim exactly once
    (collapsing duplicates, last position wins), drop the key entirely when it
    was absent at capture time, and re-append it if it has since vanished.
    Every other line in the file is left untouched — including keys another
    thread's context is holding.
    """
    if not snapshot:
        return current_text

    lines = current_text.splitlines(keepends=True)
    last_index = {}
    for index, line in enumerate(lines):
        key = _conf_line_key(line)
        if key is not None and key in snapshot:
            last_index[key] = index

    parts = []
    restored = set()
    for index, line in enumerate(lines):
        key = _conf_line_key(line)
        if key is not None and key in snapshot:
            if snapshot[key] is None:
                continue                        # absent at capture — remove it
            if index != last_index[key]:
                continue                        # duplicate — collapse to one
            parts.append(snapshot[key])
            restored.add(key)
            continue
        parts.append(line)

    for key, line in snapshot.items():
        if line is not None and key not in restored:
            parts.append(line)                  # vanished under us — put it back
    return _join_conf_lines(parts)


# Serializes the read-modify-write of django.conf itself. Held only for the
# file operation — never across a sleep, a poll or a server reload.
_CONF_MUTEX = threading.Lock()

# Test hook: called between the read and the write inside the mutex, to widen
# the window a missing mutex would lose data in. None in normal operation.
_CONF_TEST_YIELD = None


def _conf_apply(conf_path, overrides):
    """Apply overrides to django.conf and return the pre-override snapshot."""
    with _CONF_MUTEX:
        text = conf_path.read_text()
        snapshot = _capture_conf_lines(text, overrides.keys())
        if _CONF_TEST_YIELD is not None:
            _CONF_TEST_YIELD()
        conf_path.write_text(_apply_conf_overrides(text, overrides))
        return snapshot


def _conf_restore(conf_path, snapshot):
    """Subtract an override from django.conf as it currently stands."""
    with _CONF_MUTEX:
        text = conf_path.read_text()
        if _CONF_TEST_YIELD is not None:
            _CONF_TEST_YIELD()
        conf_path.write_text(_restore_conf_overrides(text, snapshot))


def _read_dev_server_conf():
    from mojo.helpers import paths
    host = '127.0.0.1'
    port = 5555
    conf = paths.resolve_conf('dev_server.conf')
    if conf.exists():
        for line in conf.read_text().splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                k, v = k.strip(), v.strip()
                if k == 'host':
                    host = v
                elif k == 'port':
                    port = int(v)
    return host, port


def _poll_server_up(host, port, timeout=10):
    import requests as _requests
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _requests.get(f'http://{host}:{port}/', timeout=1)
            return True
        except Exception:
            time.sleep(0.2)
    return False


# Seams so tests can drive server_settings() without real sleeps or a real
# server. Production code paths never rebind these.
_SLEEP = time.sleep
_POLL = _poll_server_up

# Serializes whole server_settings contexts against each other. Two contexts
# overlapping is what used to strand overrides; even with the subtractive
# restore it means two live reloads fighting over one uvicorn worker, which is
# never what a test wants. Acquisition has a timeout and degrades loudly rather
# than hanging the suite (a leaked context would otherwise be terminal).
_SETTINGS_SERIALIZE = threading.Lock()
_SETTINGS_SERIALIZE_TIMEOUT = 120.0


@contextlib.contextmanager
def server_settings(**overrides):
    """
    Context manager that temporarily applies Django settings overrides on the
    running asgi_local test server, then restores the original settings on exit.

    WHY THIS EXISTS — and why override_settings() does NOT work here:
    ----------------------------------------------------------------
    opts.client makes real HTTP calls to a separate asgi_local uvicorn process.
    Django's override_settings() only patches the *test process* — the server
    process has its own Django settings loaded at startup and never sees the
    patch. Code like this silently does nothing to the server:

        with override_settings(BOUNCER_REQUIRE_TOKEN=True):   # WRONG
            resp = opts.client.post('/api/login', ...)

    The correct way is to write the override to var/django.conf and let uvicorn
    reload. asgi_local is started with --reload-include '*.conf' specifically to
    support this pattern.

    Usage:
        with th.server_settings(BOUNCER_REQUIRE_TOKEN=True):
            resp = opts.client.post('/api/login', {'username': 'x', 'password': 'y'})
            assert_eq(resp.status_code, 403, ...)

    The context manager:
      1. Writes the overrides into var/django.conf (merging with existing values)
      2. Waits for uvicorn to reload and the server to come back up
      3. Yields — your test runs here against the live server with new settings
      4. Removes ONLY the keys it set, restoring their previous lines
      5. Waits for the server to reload and come back up again

    Raises RuntimeError if the server does not come back up within the timeout.

    RESTORE IS SUBTRACTIVE (maestro item 543)
    -----------------------------------------
    Step 4 used to write back a whole-file snapshot taken on the way in. Test
    modules run as THREADS, so a second context could snapshot the file while
    the first one's override was live and then write that stale snapshot back on
    exit — re-introducing the first context's key and stranding it in
    var/django.conf for good. Now the snapshot is taken INSIDE the lock and
    holds only the lines for the keys being overridden; the restore removes
    exactly those keys from the file as it stands and leaves every other line
    alone. Whole contexts are serialized against each other on top of that.

    CAUTION: overrides are written to var/django.conf in CLEARTEXT. Do not pass
    a real credential you would mind sitting in a gitignored file — and if a
    run does strand one, the runner's end-of-run drift check names the key (it
    never prints the value).
    """
    from mojo.helpers import paths
    host, port = _read_dev_server_conf()
    logger = logit.get_logger("testit", "testit.log")

    # Both reloads below kill the uvicorn worker, and with it every in-flight
    # websocket. Hold the server exclusively so no WsClient has a socket open
    # across either restart (see testit/server_lock.py). Degrades to the old
    # unsynchronized behavior on timeout rather than hanging the suite.
    from testit import server_lock

    # One settings context at a time, process-wide. The timeout is the liveness
    # valve: a context that somehow never released must not deadlock the run.
    serialized = _SETTINGS_SERIALIZE.acquire(timeout=_SETTINGS_SERIALIZE_TIMEOUT)
    if not serialized:
        logger.error(
            f"[server_settings] proceeding WITHOUT the serialization lock after "
            f"{_SETTINGS_SERIALIZE_TIMEOUT}s — another settings context is still "
            f"open. Overrides {sorted(overrides.keys())} may interleave with it."
        )
    try:
        # Resolved under the lock so a test that redirects VAR_ROOT cannot be
        # seen half-applied by a context waiting to start.
        conf_path = paths.VAR_ROOT / 'django.conf'
        with server_lock.exclusive(logger=logger):
            snapshot = _conf_apply(conf_path, overrides)
            # Give uvicorn's watchdog time to detect the change and begin
            # reloading, then wait for the server to come back up.
            _SLEEP(1.5)
            if not _POLL(host, port, timeout=10):
                _conf_restore(conf_path, snapshot)
                raise RuntimeError(
                    f"server_settings: server at {host}:{port} did not come back up "
                    f"after writing overrides {sorted(overrides.keys())}"
                )

            try:
                yield
            finally:
                _conf_restore(conf_path, snapshot)
                # Wait long enough for uvicorn to detect the conf change, kill the old
                # worker, start and warm up the new worker.  The initial sleep must be
                # long enough that the OLD worker (still running with the overrides) has
                # exited before the poll gets a response — otherwise the poll
                # returns True against the stale worker and subsequent tests see the
                # overrides still in effect.
                _SLEEP(3)
                if not _POLL(host, port, timeout=10):
                    raise RuntimeError(
                        f"server_settings: server at {host}:{port} did not come back up "
                        f"after restoring django.conf"
                    )
    finally:
        if serialized:
            _SETTINGS_SERIALIZE.release()


class assert_raises:
    def __init__(self, expected_exception):
        self.expected_exception = expected_exception

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            raise AssertionError(f"Expected {self.expected_exception.__name__} to be raised, but nothing was raised")

        if not issubclass(exc_type, self.expected_exception):
            raise AssertionError(f"Expected {self.expected_exception.__name__}, but got {exc_type.__name__}")

        self.exception = exc_val
        return True  # suppresses the exception


# ---------------------------------------------------------------------------
# Job execution
# ---------------------------------------------------------------------------
#
# WHY A SYNCHRONOUS DRAIN — and why tests must NOT rely on a job daemon:
# ---------------------------------------------------------------------
# Job handlers are exactly the code that talks to AWS, ACME and DNS providers,
# so a test has to be able to `mock.patch` them. A real job engine daemon runs
# in a SEPARATE process and would never see those patches — the same trap that
# makes `override_settings()` useless against `opts.client` (see
# `server_settings` above). It would also execute a snapshot of the code it
# imported at startup, and force every test into poll-and-sleep.
#
# So `run_jobs()` executes pending jobs INLINE, in the test process:
#   - `mock.patch` works, because it is the same interpreter
#   - it runs the code you are editing right now, not a daemon's stale copy
#   - it is an explicit barrier: publish, drain, assert. No sleeps, no races.
#
# The real `jobs.publish()` path is still exercised end to end — a Job row is
# written, the payload round-trips through JSON, and the id is popped off the
# real Redis queue — so serialization and channel-routing bugs still surface.
#
# NOTE: do not run a job-engine daemon during the suite. It would race this
# drain for the same queue, and any job it won would execute in the daemon's
# process where the test's patches do not apply.


def _drain_engine(channels):
    """A JobEngine used only for its synchronous `execute_job`, never started."""
    from mojo.apps.jobs.job_engine import JobEngine

    engine = getattr(_thread_local, "drain_engine", None)
    if engine is None:
        engine = JobEngine(channels=list(channels), runner_id="testit-drain")
        _thread_local.drain_engine = engine
    return engine


def _job_channels(channel=None):
    from mojo.helpers.settings import settings
    from mojo.apps.jobs import DEFAULT_CHANNELS

    if channel:
        return [channel] if isinstance(channel, str) else list(channel)
    # publish() no longer reroutes unlisted channels onto "default", so the
    # drain has to cover every channel the framework actually publishes to.
    channels = settings.get("JOBS_CHANNELS", DEFAULT_CHANNELS)
    if isinstance(channels, str):
        channels = [channels]
    return list(channels)


def promote_scheduled_jobs(channel=None, now=None):
    """
    Move due jobs out of the scheduled ZSET into their queue.

    This is the one thing the Scheduler daemon does that matters for a test:
    a job published with `delay=`/`run_at=` lands in a ZSET and is invisible to
    the queue until promoted. Returns how many were promoted.
    """
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys
    from mojo.helpers import dates

    redis = get_adapter()
    keys = JobKeys()
    now = now or dates.utcnow()
    cutoff = now.timestamp() * 1000
    promoted = 0

    for ch in _job_channels(channel):
        for zset in (keys.sched(ch), keys.sched_broadcast(ch)):
            due = redis.zrangebyscore(zset, 0, cutoff) or []
            for job_id in due:
                redis.rpush(keys.queue(ch), job_id)
                redis.zrem(zset, job_id)
                promoted += 1
    return promoted


def run_jobs(channel=None, max_jobs=100, include_scheduled=True):
    """
    Execute every pending job synchronously, in THIS process, then return.

    The explicit barrier between publishing and asserting:

        jobs.publish(func="app.asyncjobs.do_thing", payload={...})
        th.run_jobs()
        assert row.status == "done"

    Args:
        channel: one channel, a list of them, or None for JOBS_CHANNELS.
        max_jobs: safety stop, so a handler that re-publishes itself cannot
            spin forever.
        include_scheduled: also promote due `delay=`/`run_at=` jobs first.

    Returns:
        objict(count, job_ids, hit_limit)
    """
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    channels = _job_channels(channel)
    redis = get_adapter()
    keys = JobKeys()

    if include_scheduled:
        promote_scheduled_jobs(channel=channels)

    engine = _drain_engine(channels)
    executed = []
    hit_limit = False

    while True:
        if len(executed) >= max_jobs:
            hit_limit = True
            break

        # llen first so an empty queue costs nothing — brpop would block for
        # its full timeout on every drain that finds nothing left.
        target = None
        for ch in channels:
            if redis.llen(keys.queue(ch)) > 0:
                target = ch
                break
        if target is None:
            break

        popped = redis.brpop([keys.queue(target)], timeout=1)
        if not popped:
            break
        _queue_key, job_id = popped

        engine.execute_job(target, job_id)
        executed.append(job_id)

    return objict(count=len(executed), job_ids=executed, hit_limit=hit_limit)


def clear_jobs(channel=None, delete_rows=True):
    """
    Drop pending jobs so a module starts from a known state.

    Tests run against a long-lived database and a shared Redis, so a job left
    queued by an earlier module would otherwise be picked up by the first
    `run_jobs()` that happens to run after it.
    """
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    redis = get_adapter()
    keys = JobKeys()

    for ch in _job_channels(channel):
        redis.delete(keys.queue(ch))
        redis.delete(keys.sched(ch))
        redis.delete(keys.sched_broadcast(ch))
        redis.delete(keys.processing(ch))

    if delete_rows:
        from mojo.apps.jobs.models import Job
        Job.objects.filter(status__in=["pending", "running"]).delete()


def pending_job_count(channel=None):
    """How many jobs are sitting in the immediate queues right now."""
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    redis = get_adapter()
    keys = JobKeys()
    return sum(redis.llen(keys.queue(ch)) for ch in _job_channels(channel))
