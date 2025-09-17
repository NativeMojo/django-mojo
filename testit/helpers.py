import sys
from objict import objict
from mojo.helpers import logit
import functools
import traceback


TEST_RUN = objict(
    total=0, passed=0, failed=0,
    tests=objict(active_test=None),
    results=objict())
STOP_ON_FAIL = True
VERBOSE = False
INDENT = "    "


class TestitAbort(Exception):
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
    TEST_RUN.total += 1
    if name:
        test_name = name
    else:
        test_name = kwargs.get("test_name", func.__name__)
        if test_name.startswith("test_"):
            test_name = test_name[5:]

    # Print test start message
    logit.color_print(f"{INDENT}{test_name.ljust(60, '.')}", logit.ConsoleLogger.YELLOW, end="")

    try:
        result = func(*args, **kwargs)
        TEST_RUN.results[f"{TEST_RUN.active_test}:{test_name}"] = True
        TEST_RUN.passed += 1

        logit.color_print("PASSED", logit.ConsoleLogger.GREEN, end="\n")
        return result

    except AssertionError as error:
        TEST_RUN.failed += 1
        TEST_RUN.results[f"{TEST_RUN.active_test}:{test_name}"] = False

        # Print failure message
        logit.color_print("FAILED", logit.ConsoleLogger.RED, end="\n")
        logit.color_print(f"{INDENT}{INDENT}{error}", logit.ConsoleLogger.PINK)

        if STOP_ON_FAIL:
            raise TestitAbort()

    except Exception as error:
        TEST_RUN.failed += 1
        TEST_RUN.results[f"{TEST_RUN.active_test}:{test_name}"] = False

        # Print error message
        logit.color_print("FAILED", logit.ConsoleLogger.RED, end="\n")
        if VERBOSE:
            logit.color_print(traceback.format_exc(), logit.ConsoleLogger.PINK)
        if STOP_ON_FAIL:
            raise TestitAbort()
    return False

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
        return wrapper

    if callable(arg):
        # Used as @django_unit_test with no arguments
        return decorator(arg)
    else:
        # Used as @django_unit_test("name") or @django_unit_test()
        return decorator


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
