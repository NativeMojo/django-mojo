import ast
import json
import os
import sys
import time
import traceback
import inspect
import argparse
from importlib import import_module

from mojo.helpers import logit
from testit import helpers
import testit.client

from mojo.helpers import paths
TEST_ROOT = paths.APPS_ROOT / "tests"

def get_host():
    """Extract host and port from dev_server.conf."""
    host = "127.0.0.1"
    port = 8001
    try:
        config_path = paths.CONFIG_ROOT / "dev_server.conf"
        with open(config_path, 'r') as file:
            for line in file:
                if line.startswith("host"):
                    host = line.split('=')[1].strip()
                elif line.startswith("port"):
                    port = line.split('=')[1].strip()
    except FileNotFoundError:
        print("Configuration file not found.")
    except Exception as e:
        print(f"Error reading configuration: {e}")
    return f"http://{host}:{port}"


def load_config(config_path):
    """Load JSON configuration for the test runner."""
    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as error:
        raise SystemExit(f"Config file not found: {config_path}") from error
    except json.JSONDecodeError as error:
        raise SystemExit(f"Invalid JSON config {config_path}: {error}") from error

    if not isinstance(data, dict):
        raise SystemExit(f"Config file {config_path} must contain a JSON object.")
    return data


def _normalize_extra_value(value):
    """Normalize extra flags to a list of unique, ordered strings."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = [item.strip() for item in value.split(",")]
        return [item for item in parts if item]
    normalized = []
    if isinstance(value, (list, tuple, set)):
        for item in value:
            if item is None:
                continue
            if isinstance(item, str):
                normalized.extend(_normalize_extra_value(item))
            else:
                normalized.append(str(item))
        return [item for item in normalized if item]
    return [str(value)]


def apply_config_defaults(parser, config):
    """Apply config values as argparse defaults so CLI flags override them."""
    key_map = {
        "tests": ("test_modules", list),
        "ignore": ("ignore_modules", list),
        "stop_on_fail": ("stop", bool),
        "show_errors": ("errors", bool),
        "verbose": ("verbose", bool),
        "nomojo": ("nomojo", bool),
        "onlymojo": ("onlymojo", bool),
        "module": ("module", str),
        "extra": ("extra", str),
        "host": ("host", str),
        "setup": ("setup", bool),
        "quick": ("quick", bool),
        "force": ("force", bool),
        "user": ("user", str),
    }

    defaults = {}
    for key, value in config.items():
        target = key_map.get(key)
        if not target:
            continue
        dest, expected_type = target

        if dest == "extra":
            defaults[dest] = _normalize_extra_value(value)
        elif expected_type is list:
            if isinstance(value, (list, tuple, set)):
                defaults[dest] = [str(item) for item in value]
            else:
                defaults[dest] = [str(value)]
        elif expected_type is bool:
            defaults[dest] = bool(value)
        else:
            defaults[dest] = value

    if defaults:
        parser.set_defaults(**defaults)

def setup_parser(argv=None):
    """Setup command-line arguments for the test runner."""
    argv = sys.argv[1:] if argv is None else argv

    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument("--config", type=str,
                               help="Path to a JSON config file with default options")
    config_parser.add_argument("--list-extras", action="store_true",
                               help="Scan tests and list declared @requires_extra flags")

    parser = argparse.ArgumentParser(
        description="Django Test Runner",
        parents=[config_parser],
    )

    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable verbose logging")
    parser.add_argument("-f", "--force", action="store_true",
                        help="Force the test to run now")
    parser.add_argument("-u", "--user", type=str, default="nobody",
                        help="Specify the user the test should run as")
    parser.add_argument("-m", "--module", type=str, default=None,
                        help="Run only this app/module")
    parser.add_argument("--method", type=str, default=None,
                        help="Run only a specific test method")
    parser.add_argument("-t", "--test", action="append", dest="test_modules",
                        help="Specify test modules or specific tests (can be used multiple times)")
    parser.add_argument("-q", "--quick", action="store_true",
                        help="Run only tests flagged as critical/quick")
    parser.add_argument("-x", "--extra", type=str, default=None,
                        help="Specify extra data to pass to test")
    parser.add_argument("-l", "--list", action="store_true",
                        help="List available tests instead of running them")
    parser.add_argument("-s", "--stop", action="store_true",
                        help="Stop on errors")
    parser.add_argument("-e", "--errors", action="store_true",
                        help="Show errors")
    parser.add_argument("--host", type=str, default=get_host(),
                        help="Specify host for API tests")
    parser.add_argument("--setup", action="store_true",
                        help="Run setup before executing tests")
    parser.add_argument("--nomojo", action="store_true",
                        help="Do not run Mojo app tests")
    parser.add_argument("--onlymojo", action="store_true",
                        help="Only run Mojo app tests")
    parser.add_argument("--ignore", action="append", dest="ignore_modules",
                        help="Ignore specific test modules (can be used multiple times)")

    config_args, _ = config_parser.parse_known_args(argv)
    config_data = {}
    if config_args.config:
        config_data = load_config(config_args.config)
        apply_config_defaults(parser, config_data)

    opts = parser.parse_args(argv)
    opts.config = config_args.config
    opts.config_data = config_data
    opts.list_extras = config_args.list_extras or getattr(opts, "list_extras", False)
    opts.test_modules = list(opts.test_modules or [])
    opts.ignore_modules = list(opts.ignore_modules or [])

    # Normalize extras for both config defaults and CLI input.
    extra_values = _normalize_extra_value(opts.extra)
    opts.extra_list = extra_values
    opts.extra = ",".join(extra_values) if extra_values else ""

    return opts


def run_test(opts, module, func_name, module_name, test_name):
    """Run a specific test function inside a module."""
    test_key = f"{module_name}.{test_name}.{func_name}"
    helpers.VERBOSE = opts.verbose or opts.errors
    helpers.TEST_RUN.tests.active_test = test_key.replace(".", ":")
    try:
        getattr(module, func_name)(opts)
    except helpers.TestitSkip:
        return
    except Exception as err:
        if opts.verbose:
            print(f"⚠️ Test Error: {err}")
        if opts.stop:
            sys.exit(1)


def run_setup(opts, module, func_name, module_name, test_name):
    """Run a specific test function inside a module."""
    test_key = f"{module_name}.{test_name}.{func_name}"
    helpers.VERBOSE = opts.verbose or opts.errors
    try:
        getattr(module, func_name)(opts)
    except Exception as err:
        if opts.verbose:
            print(f"⚠️ Setup Error: {err}")
        if opts.stop:
            sys.exit(1)


def import_module_for_testing(module_name, test_name):
    """Dynamically import a test module."""
    try:
        name = f"{module_name}.{test_name}"
        module = import_module(name)
        return module
    except ImportError:
        print(f"⚠️ Failed to import test module: {name}")
        traceback.print_exc()
        return None


def run_module_tests_by_name(opts, module_name, test_name):
    """Run all test functions in a specific test module in the order they appear."""
    module = import_module_for_testing(module_name, test_name)
    if not module:
        return
    run_module_setup(opts, module, test_name, module_name)
    run_module_tests(opts, module, test_name, module_name)


def run_module_setup(opts, module, test_name, module_name):
    opts.client = testit.client.RestClient(opts.host, logger=opts.logger)
    test_key = f"{module_name}.{test_name}"
    started = time.time()
    prefix = "setup_"

    # Get all functions in the module
    functions = inspect.getmembers(module, inspect.isfunction)

    # Preserve definition order by using inspect.getsourcelines()
    functions = sorted(
        functions,
        key=lambda func: inspect.getsourcelines(func[1])[1]  # Sort by line number
    )
    setup_funcs = []
    for func_name, func in functions:
        if func_name.startswith(prefix):
            setup_funcs.append((module, func_name))

    if len(setup_funcs):
        logit.color_print(f"\nRUNNING SETUP: {test_key}", logit.ConsoleLogger.BLUE)
        for module, func_name in setup_funcs:
            run_setup(opts, module, func_name, module_name, test_name)
        duration = time.time() - started
        print(f"{helpers.INDENT}---------\n{helpers.INDENT}run time: {duration:.2f}s")


def run_module_tests(opts, module, test_name, module_name):
    if not getattr(opts, 'client', None):
        opts.client = testit.client.RestClient(opts.host, logger=opts.logger)
    test_key = f"{module_name}.{test_name}"
    logit.color_print(f"\nRUNNING TEST: {test_key}", logit.ConsoleLogger.BLUE)
    started = time.time()
    prefix = "test_" if not opts.quick else "quick_"

    # Get all functions in the module
    functions = inspect.getmembers(module, inspect.isfunction)

    # Preserve definition order by using inspect.getsourcelines()
    functions = sorted(
        functions,
        key=lambda func: inspect.getsourcelines(func[1])[1]  # Sort by line number
    )

    for func_name, func in functions:
        if func_name.startswith(prefix):
            run_test(opts, module, func_name, module_name, test_name)

    duration = time.time() - started
    print(f"{helpers.INDENT}---------\n{helpers.INDENT}run time: {duration:.2f}s")


def run_tests_for_module(opts, module_name, test_root, parent_test_root=None):
    """Discover and run tests for a given module."""
    module_path = os.path.join(test_root, module_name)
    if not os.path.exists(module_path):
        if parent_test_root is None:
            raise FileNotFoundError(f"Module '{module_name}' not found")
        module_path = os.path.join(parent_test_root, module_name)
        if not os.path.exists(module_path):
            raise FileNotFoundError(f"Module '{module_name}' not found")
    test_files = [f for f in os.listdir(module_path)
                  if f.endswith(".py") and f not in ["__init__.py", "setup.py"]]

    for test_file in sorted(test_files):
        if test_file.startswith("_"):
            continue
        test_name = test_file.rsplit('.', 1)[0]  # Remove .py extension
        run_module_tests_by_name(opts, module_name, test_name)


def _resolve_test_file(module_name, test_name, roots):
    filename = f"{test_name}.py"
    for root in roots:
        if not root:
            continue
        path = os.path.join(root, module_name, filename)
        if os.path.exists(path):
            return path
    return None


def _scan_requires_extra(file_path, module_name, test_name):
    """Parse a test file without importing to find @requires_extra usages."""
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            source = handle.read()
    except OSError:
        return []

    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError:
        return []

    extras = []

    def extract_flag(decorator):
        target = decorator
        args = []
        keywords = []
        if isinstance(decorator, ast.Call):
            target = decorator.func
            args = decorator.args
            keywords = decorator.keywords

        name = None
        if isinstance(target, ast.Name):
            name = target.id
        elif isinstance(target, ast.Attribute):
            name = target.attr

        if name != "requires_extra":
            return None

        requirement = None
        if args:
            arg = args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                requirement = arg.value.strip()
        if requirement is None and keywords:
            for kw in keywords:
                if kw.arg in (None, "flag"):
                    value = kw.value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        requirement = value.value.strip()
                        break
        return requirement

    def visit(node, prefix=""):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_name = f"{prefix}{node.name}"
            requirement = None
            for decorator in node.decorator_list:
                value = extract_flag(decorator)
                if value is not None:
                    requirement = value
                    break
            if requirement is not None:
                extras.append({
                    "flag": requirement if requirement else None,
                    "module": module_name,
                    "test_module": test_name,
                    "function": func_name,
                })
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                visit(child, prefix=f"{node.name}.")

    for stmt in tree.body:
        visit(stmt, prefix="")

    return extras


def collect_module_extras(module_name, test_name, *, file_path=None, safe=False):
    """Collect @requires_extra flags from a specific test module."""
    if safe:
        if file_path:
            return _scan_requires_extra(file_path, module_name, test_name)
        return []

    module = import_module_for_testing(module_name, test_name)
    if not module:
        return []
    functions = inspect.getmembers(module, inspect.isfunction)
    extras = []
    for func_name, func in functions:
        if hasattr(func, "_requires_extra"):
            requirement = getattr(func, "_requires_extra")
            extras.append({
                "flag": requirement,
                "module": module_name,
                "test_module": test_name,
                "function": func_name,
            })
    return extras


def collect_extras_for_module(module_name, test_root, parent_test_root=None, *, safe=False):
    """Collect extras across all test files in a module directory."""
    module_path = os.path.join(test_root, module_name)
    if not os.path.exists(module_path):
        if parent_test_root is None:
            return []
        module_path = os.path.join(parent_test_root, module_name)
        if not os.path.exists(module_path):
            return []

    test_files = [f for f in os.listdir(module_path)
                  if f.endswith(".py") and f not in ["__init__.py", "setup.py"]]
    extras = []
    for test_file in sorted(test_files):
        if test_file.startswith("_"):
            continue
        test_name = test_file.rsplit('.', 1)[0]
        file_path = os.path.join(module_path, test_file)
        extras.extend(collect_module_extras(
            module_name,
            test_name,
            file_path=file_path,
            safe=safe,
        ))
    return extras


def print_extra_flags(extras):
    if not extras:
        print("No @requires_extra flags were found.")
        return

    def _flag_label(item):
        return item["flag"] if item["flag"] else "[any]"

    extras = sorted(
        extras,
        key=lambda item: (
            _flag_label(item),
            item["module"],
            item["test_module"],
            item["function"],
        )
    )

    print("\nDeclared @requires_extra flags:\n")
    current_flag = None
    for item in extras:
        label = _flag_label(item)
        if label != current_flag:
            current_flag = label
            print(f"- {label}")
        print(f"    {item['module']}.{item['test_module']}.{item['function']}")
    print("")


def setup_modules():
    """Run setup scripts for all test modules."""
    logit.color_print("\n[TEST PREFLIGHT SETUP]\n", logit.ConsoleLogger.BLUE)

    test_modules = [d for d in os.listdir(TEST_ROOT) if os.path.isdir(os.path.join(TEST_ROOT, d))]

    for module_name in sorted(test_modules):
        setup_file = os.path.join(TEST_ROOT, module_name, "setup.py")
        if os.path.isfile(setup_file):
            module = import_module_for_testing(module_name, "setup")
            if module and hasattr(module, "run_setup"):
                logit.color_print(f"Setting up {module_name}...", logit.ConsoleLogger.YELLOW)
                module.run_setup(opts)
                logit.color_print("✔ DONE\n", logit.ConsoleLogger.GREEN)


def main(opts):
    """Main function to run tests."""
    helpers.reset_test_run()
    helpers.STOP_ON_FAIL = bool(opts.stop)
    helpers.VERBOSE = opts.verbose or opts.errors
    helpers.TEST_RUN.started_at = time.time()

    if opts.setup and not opts.list_extras:
        setup_modules()

    if opts.list:
        print("\n------------------------")
        print("Listing available test modules & tests")
        print("[module]")
        print("  [test1]")
        print("  [test2]")
        print("------------------------")
        return

    opts.logger = logit.get_logger("testit", "testit.log")

    if opts.module and '.' in opts.module:
        opts.module, opts.test = opts.module.split('.', 1)

    parent_test_root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tests")
    parent_test_modules = None
    if os.path.exists(parent_test_root):
        sys.path.insert(0, parent_test_root)
        parent_test_modules = sorted([d for d in os.listdir(parent_test_root) if os.path.isdir(os.path.join(parent_test_root, d))])

    if opts.list_extras:
        extras = []
        roots = [TEST_ROOT, parent_test_root]

        def add_from_module(module_name, test_name):
            file_path = _resolve_test_file(module_name, test_name, roots)
            extras.extend(collect_module_extras(
                module_name,
                test_name,
                file_path=file_path,
                safe=True,
            ))

        def add_from_directory(module_name, test_root):
            extras.extend(collect_extras_for_module(
                module_name,
                test_root,
                parent_test_root,
                safe=True,
            ))

        if opts.test_modules:
            for test_spec in opts.test_modules:
                if '.' in test_spec:
                    module_name, test_name = test_spec.split('.', 1)
                    add_from_module(module_name, test_name)
                else:
                    add_from_directory(test_spec, TEST_ROOT)
        elif opts.module and opts.test:
            add_from_module(opts.module, opts.test)
        elif opts.module:
            add_from_directory(opts.module, TEST_ROOT)
        else:
            test_root = os.path.join(paths.APPS_ROOT, "tests")
            test_modules = sorted([d for d in os.listdir(test_root) if os.path.isdir(os.path.join(test_root, d))])
            ignored_modules = opts.ignore_modules or []

            if parent_test_modules and not opts.nomojo:
                for module_name in parent_test_modules:
                    if module_name not in ignored_modules:
                        add_from_directory(module_name, parent_test_root)
            if not opts.onlymojo:
                for module_name in test_modules:
                    if module_name not in ignored_modules:
                        add_from_directory(module_name, test_root)

        print_extra_flags(extras)
        return

    # Handle multiple --test arguments
    if opts.test_modules:
        for test_spec in opts.test_modules:
            if '.' in test_spec:
                module_name, test_name = test_spec.split('.', 1)
                run_module_tests_by_name(opts, module_name, test_name)
            else:
                run_tests_for_module(opts, test_spec, TEST_ROOT, parent_test_root)
    elif opts.module and opts.test:
        run_module_tests_by_name(opts, opts.module, opts.test)
    elif opts.module:
        run_tests_for_module(opts, opts.module, TEST_ROOT, parent_test_root)
    else:
        test_root = os.path.join(paths.APPS_ROOT, "tests")
        test_modules = sorted([d for d in os.listdir(test_root) if os.path.isdir(os.path.join(test_root, d))])

        # Filter out ignored modules
        ignored_modules = opts.ignore_modules or []

        if parent_test_modules and not opts.nomojo:
            for module_name in parent_test_modules:
                if module_name not in ignored_modules:
                    run_tests_for_module(opts, module_name, parent_test_root)
        if not opts.onlymojo:
            for module_name in test_modules:
                if module_name not in ignored_modules:
                    run_tests_for_module(opts, module_name, test_root)

    # Summary Output
    print("\n" + "=" * 80)

    logit.color_print(f"TOTAL RUN: {helpers.TEST_RUN.total}\t", logit.ConsoleLogger.YELLOW)
    logit.color_print(f"TOTAL PASSED: {helpers.TEST_RUN.passed}", logit.ConsoleLogger.GREEN)
    if helpers.TEST_RUN.skipped:
        logit.color_print(f"TOTAL SKIPPED: {helpers.TEST_RUN.skipped}", logit.ConsoleLogger.BLUE)
    if helpers.TEST_RUN.failed > 0:
        logit.color_print(f"TOTAL FAILED: {helpers.TEST_RUN.failed}", logit.ConsoleLogger.RED)

    print("=" * 80)

    # Save Test Results
    helpers.TEST_RUN.finished_at = time.time()
    helpers.save_results(os.path.join(paths.VAR_ROOT, "test_results.json"))

    # Exit with failure status if any test failed
    if helpers.TEST_RUN.failed > 0:
        sys.exit("❌ Tests failed!")


if __name__ == "__main__":
    opts = setup_parser()
    main(opts)
