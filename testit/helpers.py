import json
import os
import time
import functools
import traceback
from objict import objict
from mojo.helpers import logit


TEST_RUN = objict(
    total=0,
    passed=0,
    failed=0,
    skipped=0,
    tests=objict(active_test=None),
    results=objict(),
    records=[],
    started_at=None,
    finished_at=None,
)
STOP_ON_FAIL = True
VERBOSE = False
INDENT = "    "


class TestitAbort(Exception):
    pass


class TestitSkip(Exception):
    pass


def _run_setup(func, *args, **kwargs):
    name = kwargs.get("name", func.__name__)
    logit.color_print(f"{INDENT}{name.ljust(60, '.')}", logit.ConsoleLogger.PINK, end="")
    res = func(*args, **kwargs)
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


def _run_unit(func, name, *args, **kwargs):
    if TEST_RUN.started_at is None:
        TEST_RUN.started_at = time.time()
    TEST_RUN.total += 1
    if name:
        test_name = name
    else:
        test_name = kwargs.get("test_name", func.__name__)
        if test_name.startswith("test_"):
            test_name = test_name[5:]

    # Print test start message
    name_line = f"{INDENT}{test_name.ljust(60, '.')}"

    try:
        result = func(*args, **kwargs)
        _record_result(test_name, status="passed")
        TEST_RUN.passed += 1
        logit.color_print(f"{name_line}PASSED", logit.ConsoleLogger.GREEN, end="\n")
        return result

    except TestitSkip as skip:
        _record_result(test_name, status="skipped", detail=str(skip))
        TEST_RUN.skipped += 1
        logit.color_print(f"{name_line}SKIPPED", logit.ConsoleLogger.BLUE, end="\n")
        if str(skip):
            logit.color_print(f"{INDENT}{INDENT}{skip}", logit.ConsoleLogger.BLUE)
        return None

    except AssertionError as error:
        TEST_RUN.failed += 1
        _record_result(test_name, status="failed", detail=str(error))

        # Print failure message
        logit.color_print(f"{name_line}FAILED", logit.ConsoleLogger.RED, end="\n")
        logit.color_print(f"{INDENT}{INDENT}{error}", logit.ConsoleLogger.PINK)

        if STOP_ON_FAIL:
            raise TestitAbort()

    except Exception as error:
        TEST_RUN.failed += 1
        detail = traceback.format_exc() if VERBOSE else str(error)
        _record_result(test_name, status="error", detail=detail)

        # Print error message
        logit.color_print(f"{name_line}FAILED", logit.ConsoleLogger.RED, end="\n")
        if VERBOSE:
            logit.color_print(traceback.format_exc(), logit.ConsoleLogger.PINK)
        if STOP_ON_FAIL:
            raise TestitAbort()
    return False


def _active_context():
    active = TEST_RUN.tests.active_test or ""
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


def _record_result(test_name, *, status, detail=None):
    context = _active_context()
    key = _result_key(test_name)
    if status == "passed":
        TEST_RUN.results[key] = True
    elif status in {"failed", "error"}:
        TEST_RUN.results[key] = False
    else:
        TEST_RUN.results[key] = None

    record = {
        "module": context.module,
        "test_module": context.test_module,
        "function": context.function,
        "name": test_name,
        "status": status,
    }
    if detail:
        record["detail"] = detail
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
