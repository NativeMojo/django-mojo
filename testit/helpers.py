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
        return wrapper
    return decorator
