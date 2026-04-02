import ast
import copy
import json
import os
import sys
import time
import traceback
import inspect
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from importlib import import_module

from mojo.helpers import logit
from testit import helpers
import testit.client

from mojo.helpers import paths
from objict import objict

TEST_ROOT = paths.APPS_ROOT / "tests"
_LOCK_FILE = os.path.join(paths.VAR_ROOT, "testit.lock")

_resume = objict(active=False, module=None, test_name=None, reached=False)

# ---------------------------------------------------------------------------
# Interactive abort — set by keyboard listener or signal handler
# ---------------------------------------------------------------------------
_abort_event = threading.Event()

# ---------------------------------------------------------------------------
# Keyboard listener (Rich UI mode only, Unix terminals)
# ---------------------------------------------------------------------------
try:
    import termios
    import tty
    _HAS_TERMIOS = True
except ImportError:
    _HAS_TERMIOS = False


class _KeyboardListener:
    """Background daemon thread that reads single keypresses during Rich UI mode."""

    def __init__(self, display):
        self._display = display
        self._thread = None
        self._stop = threading.Event()
        self._old_settings = None

    def start(self):
        if not _HAS_TERMIOS or not sys.stdin.isatty():
            return
        self._old_settings = termios.tcgetattr(sys.stdin)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._old_settings:
            try:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._old_settings)
            except Exception:
                pass
            self._old_settings = None

    def _run(self):
        try:
            tty.setcbreak(sys.stdin.fileno())
            while not self._stop.is_set():
                import select
                ready, _, _ = select.select([sys.stdin], [], [], 0.2)
                if not ready:
                    continue
                ch = sys.stdin.read(1)
                if ch == "q":
                    _abort_event.set()
                    self._display.set_status_message("Quitting after current tests finish...")
                    self._display.refresh()
                elif ch == "f":
                    helpers.STOP_ON_FAIL = True
                    self._display.fail_fast_active = True
                    self._display.refresh()
                elif ch == "r":
                    self._display.show_running = not self._display.show_running
                    self._display.refresh()
                elif ch == "v":
                    self._display.show_verbose = not self._display.show_verbose
                    self._display.refresh()
        except Exception:
            pass
        finally:
            if self._old_settings:
                try:
                    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._old_settings)
                except Exception:
                    pass

# ---------------------------------------------------------------------------
# Rich UI (optional — falls back to plain text)
# ---------------------------------------------------------------------------
try:
    from rich.live import Live
    from rich.table import Table
    from rich.text import Text
    from rich.progress import Progress, BarColumn, TextColumn, SpinnerColumn
    from rich.console import Console
    from rich.panel import Panel
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

# ---------------------------------------------------------------------------
# Module config
# ---------------------------------------------------------------------------
_DEFAULT_CONFIG = objict(
    server_settings={},
    serial=False,
    requires_apps=[],
    requires_extra=[],
)


# ---------------------------------------------------------------------------
# Run lock — prevents concurrent test runs from colliding
# ---------------------------------------------------------------------------
def _acquire_lock():
    """Acquire the test run lock. Returns True if acquired, False if another run is active."""
    os.makedirs(os.path.dirname(_LOCK_FILE), exist_ok=True)
    if os.path.exists(_LOCK_FILE):
        try:
            with open(_LOCK_FILE, "r") as fh:
                info = json.load(fh)
            pid = info.get("pid")
            # Check if the locking process is still alive
            if pid and _pid_alive(pid):
                return False, info
            # Stale lock — process is gone
        except (json.JSONDecodeError, OSError):
            pass

    lock_info = {
        "pid": os.getpid(),
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "user": os.environ.get("USER", "unknown"),
    }
    with open(_LOCK_FILE, "w") as fh:
        json.dump(lock_info, fh)
    return True, lock_info


def _release_lock():
    """Release the test run lock."""
    try:
        if os.path.exists(_LOCK_FILE):
            with open(_LOCK_FILE, "r") as fh:
                info = json.load(fh)
            # Only remove if we own the lock
            if info.get("pid") == os.getpid():
                os.remove(_LOCK_FILE)
    except (json.JSONDecodeError, OSError):
        try:
            os.remove(_LOCK_FILE)
        except OSError:
            pass


def _pid_alive(pid):
    """Check if a process is still running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _load_module_config(module_path):
    """Load TESTIT config from a module's __init__.py via AST (no import side effects)."""
    init_path = os.path.join(module_path, "__init__.py")
    if not os.path.exists(init_path):
        return objict(_DEFAULT_CONFIG)

    try:
        with open(init_path, "r", encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source, filename=init_path)
    except (OSError, SyntaxError):
        return objict(_DEFAULT_CONFIG)

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "TESTIT":
                    try:
                        value = ast.literal_eval(node.value)
                        if isinstance(value, dict):
                            merged = dict(_DEFAULT_CONFIG)
                            merged.update(value)
                            return objict(merged)
                    except (ValueError, TypeError):
                        pass
    return objict(_DEFAULT_CONFIG)


# ---------------------------------------------------------------------------
# Checkpoint (resume from failure)
# ---------------------------------------------------------------------------
def _checkpoint_path():
    return os.path.join(paths.VAR_ROOT, "testit_checkpoint.json")


def save_checkpoint(module_name, test_name):
    path = _checkpoint_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"module": module_name, "test_name": test_name}, handle)


def load_checkpoint():
    try:
        with open(_checkpoint_path(), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def clear_checkpoint():
    try:
        os.remove(_checkpoint_path())
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Host / config
# ---------------------------------------------------------------------------
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
                    if host == "0.0.0.0":
                        host = "127.0.0.1"
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
        "extra": ("extra", str),
        "host": ("host", str),
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
    parser.add_argument("-t", "--test", action="append", dest="test_modules",
                        help="Run specific module or test file: -t module or -t module.testfile (repeatable)")
    parser.add_argument("-q", "--quick", action="store_true",
                        help="Run only tests flagged as critical/quick")
    parser.add_argument("-x", "--extra", type=str, default=None,
                        help="Specify extra data to pass to test")
    parser.add_argument("-s", "--stop", action="store_true",
                        help="Stop on errors")
    parser.add_argument("--continue", dest="resume", action="store_true",
                        help="Continue from last checkpoint (saved by -s on failure)")
    parser.add_argument("-e", "--errors", action="store_true",
                        help="Show errors")
    parser.add_argument("--host", type=str, default=get_host(),
                        help="Specify host for API tests")
    parser.add_argument("--nomojo", action="store_true",
                        help="Do not run Mojo app tests")
    parser.add_argument("--onlymojo", action="store_true",
                        help="Only run Mojo app tests")
    parser.add_argument("--ignore", action="append", dest="ignore_modules",
                        help="Ignore specific test modules (can be used multiple times)")
    parser.add_argument("-j", "--jobs", type=int, default=None,
                        help="Parallel module threads (default 4, forced to 1 with -s or -v)")
    parser.add_argument("--agent", action="store_true",
                        help="Write structured failure report to var/test_failures.json for LLM agents")
    parser.add_argument("--plain", action="store_true",
                        help="Force plain text output (no rich progress UI)")
    parser.add_argument("--full", action="store_true",
                        help="Include opt-in modules (same as --extra slow)")

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
    if opts.full and "slow" not in extra_values:
        extra_values.append("slow")
    opts.extra_list = extra_values
    opts.extra = ",".join(extra_values) if extra_values else ""

    # Resolve parallelism
    if opts.jobs is None:
        if opts.stop or opts.verbose:
            opts.jobs = 1
        else:
            opts.jobs = 4
    if opts.stop or opts.verbose:
        opts.jobs = 1
    if getattr(opts, "resume", False) and opts.jobs > 1:
        opts.jobs = 1

    return opts


# ---------------------------------------------------------------------------
# Test execution (single module)
# ---------------------------------------------------------------------------
def run_test(opts, module, func_name, module_name, test_name):
    """Run a specific test function inside a module."""
    test_key = f"{module_name}.{test_name}.{func_name}"
    helpers.VERBOSE = opts.verbose or opts.errors
    helpers._set_active_test(test_key.replace(".", ":"))
    try:
        getattr(module, func_name)(opts)
    except helpers.TestitSkip:
        return
    except helpers.TestitAbort:
        raise
    except Exception as err:
        if opts.verbose:
            print(f"  Test Error: {err}")
            traceback.print_exc()
        if opts.stop:
            save_checkpoint(module_name, test_name)
            raise helpers.TestitAbort()


def run_setup(opts, module, func_name, module_name, test_name):
    """Run a specific setup function. Returns True if skipped."""
    test_key = f"{module_name}.{test_name}.{func_name}"
    helpers.VERBOSE = opts.verbose or opts.errors
    try:
        getattr(module, func_name)(opts)
        return False
    except helpers.TestitSkip as skip:
        msg = str(skip) if str(skip) else "skipped"
        if not helpers._get_display_fn():
            logit.color_print(f"{helpers.INDENT}{msg}", logit.ConsoleLogger.BLUE)
        return True
    except Exception as err:
        if opts.verbose:
            print(f"  Setup Error: {err}")
            traceback.print_exc()
        if opts.stop:
            raise helpers.TestitAbort()
        return False


def import_module_for_testing(module_name, test_name):
    """Dynamically import a test module."""
    try:
        name = f"{module_name}.{test_name}"
        module = import_module(name)
        return module
    except (ImportError, RuntimeError):
        print(f"  Failed to import test module: {name}")
        traceback.print_exc()
        return None


def _sort_key(name):
    prefix = name.split("_", 1)[0]
    return (int(prefix), name) if prefix.isdigit() else (float("inf"), name)


def _count_tests_in_file(file_path, quick=False):
    """Count test functions in a file via AST scan (no import)."""
    prefix = "quick_" if quick else "test_"
    try:
        with open(file_path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=file_path)
    except (OSError, SyntaxError):
        return 0
    count = 0
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith(prefix):
            count += 1
    return count


def _discover_test_files(module_name, test_root, parent_test_root=None):
    """Find the module directory and return sorted list of (test_name, file_path)."""
    module_path = os.path.join(test_root, module_name)
    if not os.path.exists(module_path):
        if parent_test_root:
            module_path = os.path.join(parent_test_root, module_name)
        if not os.path.exists(module_path):
            return [], module_path

    test_files = [f for f in os.listdir(module_path)
                  if f.endswith(".py") and f not in ["__init__.py", "setup.py"]
                  and not f.startswith("_")]

    result = []
    for test_file in sorted(test_files, key=_sort_key):
        test_name = test_file.rsplit('.', 1)[0]
        file_path = os.path.join(module_path, test_file)
        result.append((test_name, file_path))
    return result, module_path


def run_module_tests_by_name(opts, module_name, test_name):
    """Run all test functions in a specific test module in the order they appear."""
    module = import_module_for_testing(module_name, test_name)
    if not module:
        return
    skipped = run_module_setup(opts, module, test_name, module_name)
    if skipped:
        # Count all tests in this file as skipped so totals stay consistent
        prefix = "quick_" if opts.quick else "test_"
        functions = inspect.getmembers(module, inspect.isfunction)
        for func_name, func in functions:
            if func_name.startswith(prefix):
                display_name = func_name[len(prefix):]
                helpers._increment("total")
                helpers._increment("skipped")
                helpers._record_result(display_name, status="skipped", detail="setup skipped")
                dfn = helpers._get_display_fn()
                if dfn:
                    dfn("test_result", name=display_name, status="skipped", detail="setup skipped")
        return
    run_module_tests(opts, module, test_name, module_name)


def run_module_setup(opts, module, test_name, module_name):
    """Run all setup functions for a module. Returns True if module was skipped."""
    opts.client = testit.client.RestClient(opts.host, logger=opts.logger)
    test_key = f"{module_name}.{test_name}"
    started = time.time()
    prefix = "setup_"

    functions = inspect.getmembers(module, inspect.isfunction)
    functions = sorted(
        functions,
        key=lambda func: inspect.getsourcelines(func[1])[1]
    )
    setup_funcs = []
    for func_name, func in functions:
        if func_name.startswith(prefix):
            setup_funcs.append((module, func_name))

    if len(setup_funcs):
        if not helpers._get_display_fn():
            logit.color_print(f"\nRUNNING SETUP: {test_key}", logit.ConsoleLogger.BLUE)
        for module, func_name in setup_funcs:
            skipped = run_setup(opts, module, func_name, module_name, test_name)
            if skipped:
                return True
        if not helpers._get_display_fn():
            duration = time.time() - started
            print(f"{helpers.INDENT}---------\n{helpers.INDENT}run time: {duration:.2f}s")
    return False


def run_module_tests(opts, module, test_name, module_name):
    if not getattr(opts, 'client', None):
        opts.client = testit.client.RestClient(opts.host, logger=opts.logger)
    test_key = f"{module_name}.{test_name}"
    if not helpers._get_display_fn():
        logit.color_print(f"\nRUNNING TEST: {test_key}", logit.ConsoleLogger.BLUE)
    started = time.time()
    prefix = "test_" if not opts.quick else "quick_"

    functions = inspect.getmembers(module, inspect.isfunction)
    functions = sorted(
        functions,
        key=lambda func: inspect.getsourcelines(func[1])[1]
    )

    for func_name, func in functions:
        if func_name.startswith(prefix):
            if _abort_event.is_set():
                raise helpers.TestitAbort()
            # Track current test for the running display
            dfn = helpers._get_display_fn()
            if dfn:
                dfn("test_running", name=func_name)
            run_test(opts, module, func_name, module_name, test_name)

    if not helpers._get_display_fn():
        duration = time.time() - started
        print(f"{helpers.INDENT}---------\n{helpers.INDENT}run time: {duration:.2f}s")


def run_tests_for_module(opts, module_name, test_root, parent_test_root=None):
    """Discover and run tests for a given module."""
    test_files, module_path = _discover_test_files(module_name, test_root, parent_test_root)
    if not test_files:
        return

    for test_name, file_path in test_files:
        if _resume.active and not _resume.reached:
            if module_name != _resume.module or test_name != _resume.test_name:
                continue
            _resume.reached = True
        run_module_tests_by_name(opts, module_name, test_name)


# ---------------------------------------------------------------------------
# Extras scanning (unchanged logic, refactored for shared helpers)
# ---------------------------------------------------------------------------
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
    for test_file in sorted(test_files, key=_sort_key):
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


# ---------------------------------------------------------------------------
# Rich progress display
# ---------------------------------------------------------------------------
class _ModuleTracker:
    """Per-module state for the rich progress display."""

    def __init__(self, module_name, total_tests):
        self.module_name = module_name
        self.total = total_tests
        self.completed = 0
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.started_at = None
        self.finished_at = None
        self.finished = False
        self.failures = []
        self.current_test = None
        self.skip_reason = None
        self._lock = threading.Lock()

    def start(self):
        self.started_at = time.time()

    def set_current(self, name):
        with self._lock:
            self.current_test = name

    def record(self, status, name=None, detail=None):
        with self._lock:
            self.completed += 1
            if status == "passed":
                self.passed += 1
            elif status in ("failed", "error"):
                self.failed += 1
                self.failures.append({"name": name, "detail": detail})
            elif status == "skipped":
                self.skipped += 1

    @property
    def elapsed(self):
        start = self.started_at or time.time()
        end = self.finished_at or time.time()
        return end - start


class _RichDisplay:
    """Manages the rich Live panel showing per-module progress."""

    def __init__(self):
        self.console = Console()
        self.trackers = {}
        self._order = []
        self._lock = threading.Lock()
        self._live = None
        self._started_at = None
        self.show_running = False
        self.show_verbose = False
        self.fail_fast_active = False
        self._status_message = None

    def set_status_message(self, msg):
        self._status_message = msg

    def add_module(self, module_name, total_tests):
        tracker = _ModuleTracker(module_name, total_tests)
        with self._lock:
            self.trackers[module_name] = tracker
            self._order.append(module_name)
        return tracker

    def _build_table(self):
        from rich.text import Text as RichText

        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("module", min_width=20)
        table.add_column("bar", min_width=30)
        table.add_column("counts", min_width=28)
        table.add_column("time", min_width=8, justify="right")
        table.add_column("status", min_width=3)

        with self._lock:
            for name in self._order:
                t = self.trackers[name]

                # Progress bar
                if t.total > 0:
                    pct = t.completed / t.total
                    filled = int(pct * 25)
                    bar = "[green]" + "━" * filled + "[/green]"
                    if filled < 25:
                        bar += "[dim]" + "━" * (25 - filled) + "[/dim]"
                else:
                    bar = "[dim]" + "━" * 25 + "[/dim]"

                counts = f"{t.completed}/{t.total}  "
                counts += f"[green]✓ {t.passed}[/green]  "
                if t.failed:
                    counts += f"[red]✗ {t.failed}[/red]  "
                else:
                    counts += f"[dim]✗ {t.failed}[/dim]  "
                if t.skipped:
                    counts += f"[blue]⊘ {t.skipped}[/blue]"
                else:
                    counts += f"[dim]⊘ {t.skipped}[/dim]"

                elapsed = f"{t.elapsed:.1f}s"

                if t.finished:
                    status = "[green]✔[/green]" if t.failed == 0 else "[red]✘[/red]"
                else:
                    status = "[yellow]…[/yellow]"

                table.add_row(name, bar, counts, elapsed, status)

                # Show currently running test name when toggled
                if self.show_running and not t.finished and t.current_test:
                    table.add_row(
                        f"  [dim]{t.current_test}[/dim]", "", "", "", ""
                    )

        # Running clock at bottom
        if self._started_at:
            wall = time.time() - self._started_at
            mins, secs = divmod(int(wall), 60)
            clock = f"[dim]elapsed {mins}:{secs:02d}[/dim]"
            table.add_row("", "", "", clock, "")

        # Status message (e.g., "Quitting after current tests finish...")
        if self._status_message:
            table.add_row(f"[yellow]{self._status_message}[/yellow]", "", "", "", "")

        # Keyboard hints — escape brackets with backslash for Rich
        # Split across columns so they don't wrap
        if self.fail_fast_active:
            h1 = "[dim]\\[q]uit[/dim]"
            h2 = "[bold dim]\\[f]ail-fast ✓[/bold dim]"
        else:
            h1 = "[dim]\\[q]uit[/dim]"
            h2 = "[dim]\\[f]ail-fast[/dim]"
        h3 = "[dim]\\[r]unning  \\[v]erbose[/dim]"
        table.add_row(h1, h2, h3, "", "")

        return table

    def start(self):
        self._started_at = time.time()
        self._live = Live(self._build_table(), console=self.console, refresh_per_second=4)
        self._live.start()

    def refresh(self):
        if self._live:
            self._live.update(self._build_table())

    def stop(self):
        if self._live:
            self._live.update(self._build_table())
            self._live.stop()
            self._live = None


def _print_summary_rich(display, duration):
    """Print a final summary table with failures expanded."""
    console = display.console

    # Summary table
    table = Table(title="Test Results", show_lines=False)
    table.add_column("Module", style="bold")
    table.add_column("Tests", justify="right")
    table.add_column("Passed", justify="right", style="green")
    table.add_column("Failed", justify="right", style="red")
    table.add_column("Skipped", justify="right", style="blue")
    table.add_column("Time", justify="right")

    for name in display._order:
        t = display.trackers[name]
        table.add_row(
            name,
            str(t.total),
            str(t.passed),
            str(t.failed) if t.failed else "-",
            str(t.skipped) if t.skipped else "-",
            f"{t.elapsed:.1f}s",
        )

    # Totals — sum from trackers for consistency with per-module rows
    total_tests = sum(t.total for t in display.trackers.values())
    total_passed = sum(t.passed for t in display.trackers.values())
    total_failed = sum(t.failed for t in display.trackers.values())
    total_skipped = sum(t.skipped for t in display.trackers.values())
    table.add_section()
    table.add_row(
        "TOTAL",
        str(total_tests),
        str(total_passed),
        str(total_failed) if total_failed else "-",
        str(total_skipped) if total_skipped else "-",
        f"{duration:.1f}s",
        style="bold",
    )
    console.print()
    console.print(table)

    # Failures detail
    all_failures = []
    for name in display._order:
        t = display.trackers[name]
        for f in t.failures:
            all_failures.append((name, f))

    if all_failures:
        console.print()
        console.print("[bold red]Failures:[/bold red]")
        for module_name, fail in all_failures:
            console.print(f"  [red]✗[/red] [bold]{module_name}[/bold] > {fail['name']}")
            if fail.get("detail"):
                console.print(f"    [dim]{fail['detail']}[/dim]")


def _print_summary_plain(duration, skipped_modules=None):
    """Print the original plain-text summary."""
    print("\n" + "=" * 80)
    if skipped_modules:
        for name, reason, total in skipped_modules:
            logit.color_print(f"SKIPPED: {name} — {total} tests ({reason})", logit.ConsoleLogger.BLUE)
    logit.color_print(f"TOTAL RUN: {helpers.TEST_RUN.total}\t", logit.ConsoleLogger.YELLOW)
    logit.color_print(f"TOTAL PASSED: {helpers.TEST_RUN.passed}", logit.ConsoleLogger.GREEN)
    if helpers.TEST_RUN.skipped:
        logit.color_print(f"TOTAL SKIPPED: {helpers.TEST_RUN.skipped}", logit.ConsoleLogger.BLUE)
    if helpers.TEST_RUN.failed > 0:
        logit.color_print(f"TOTAL FAILED: {helpers.TEST_RUN.failed}", logit.ConsoleLogger.RED)
    print("=" * 80)


# ---------------------------------------------------------------------------
# Agent output
# ---------------------------------------------------------------------------
def _write_agent_report(opts, display=None):
    """Write structured test report to var/test_failures.json for LLM agents.

    This is the primary output channel for --agent mode. Agents should read
    this file instead of parsing terminal output.
    """
    failures = []
    for record in helpers.TEST_RUN.records:
        if record["status"] not in ("failed", "error"):
            continue
        entry = {
            "test_name": record.get("name"),
            "module": record.get("module"),
            "test_file": record.get("test_module"),
            "function": record.get("function"),
            "status": record["status"],
            "assertion": record.get("detail"),
        }
        # Merge agent context if available
        agent_ctx = record.get("agent_context")
        if agent_ctx:
            entry["file_path"] = agent_ctx.get("file_path")
            entry["line"] = agent_ctx.get("line")
            entry["test_source"] = agent_ctx.get("test_source")
            if agent_ctx.get("traceback"):
                entry["traceback"] = agent_ctx["traceback"]

        # Server error log tail
        try:
            log_path = os.path.join(paths.VAR_ROOT, "error.log")
            if os.path.exists(log_path):
                with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.readlines()
                entry["server_log_tail"] = "".join(lines[-20:])
        except Exception:
            pass

        failures.append(entry)

    # Per-module stats from rich display trackers (when available)
    modules = {}
    if display and hasattr(display, "trackers"):
        for name in display._order:
            t = display.trackers[name]
            entry = {
                "tests": t.total,
                "passed": t.passed,
                "failed": t.failed,
                "skipped": t.skipped,
                "duration": round(t.elapsed, 2),
            }
            if t.skip_reason:
                entry["skipped_reason"] = t.skip_reason
            modules[name] = entry
    else:
        # Build per-module stats from records
        for record in helpers.TEST_RUN.records:
            mod = record.get("module") or "unknown"
            if mod not in modules:
                modules[mod] = {"tests": 0, "passed": 0, "failed": 0, "skipped": 0}
            modules[mod]["tests"] += 1
            status = record.get("status", "")
            if status == "passed":
                modules[mod]["passed"] += 1
            elif status in ("failed", "error"):
                modules[mod]["failed"] += 1
            elif status == "skipped":
                modules[mod]["skipped"] += 1

    duration = (helpers.TEST_RUN.finished_at or time.time()) - (helpers.TEST_RUN.started_at or time.time())

    report = {
        "status": "passed" if helpers.TEST_RUN.failed == 0 else "failed",
        "total": helpers.TEST_RUN.total,
        "passed": helpers.TEST_RUN.passed,
        "failed": helpers.TEST_RUN.failed,
        "skipped": helpers.TEST_RUN.skipped,
        "duration": round(duration, 2),
        "modules": modules,
        "failures": failures,
    }

    report_path = os.path.join(paths.VAR_ROOT, "test_failures.json")
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)


# ---------------------------------------------------------------------------
# Parallel module runner
# ---------------------------------------------------------------------------
def _run_module_in_thread(opts_template, module_name, test_root, parent_test_root, tracker):
    """Run all test files for a module. Called from a thread."""
    # Each thread gets its own opts copy with its own client
    opts = copy.copy(opts_template)
    opts.client = testit.client.RestClient(opts.host, logger=opts.logger)

    # Wire up a per-thread display callback
    def _on_event(event, **kwargs):
        if event == "test_running":
            tracker.set_current(kwargs.get("name"))
            if display_ref:
                display_ref.refresh()
        elif event == "test_result":
            tracker.record(kwargs.get("status"), kwargs.get("name"), kwargs.get("detail"))
            if display_ref:
                display_ref.refresh()

    helpers._set_display_fn(_on_event)

    tracker.start()
    test_files, module_path = _discover_test_files(module_name, test_root, parent_test_root)
    try:
        for test_name, file_path in test_files:
            if _abort_event.is_set():
                raise helpers.TestitAbort()
            run_module_tests_by_name(opts, module_name, test_name)
    except helpers.TestitAbort:
        pass
    finally:
        tracker.set_current(None)
        tracker.finished_at = time.time()
        tracker.finished = True
        helpers._set_display_fn(None)
        if display_ref:
            display_ref.refresh()

    return module_name


# Module-level ref so threads can access the display
display_ref = None


def _collect_modules(opts, test_root, parent_test_root):
    """Collect all module names to run, respecting filters and ignore lists."""
    modules = []
    ignored = opts.ignore_modules or []

    if opts.test_modules:
        # Specific modules requested
        for test_spec in opts.test_modules:
            if '.' in test_spec:
                # Specific file — run directly (not parallelizable at module level)
                modules.append(("file", test_spec))
            else:
                if test_spec not in ignored:
                    modules.append(("module", test_spec))
        return modules

    parent_test_modules = None
    if parent_test_root and os.path.exists(parent_test_root):
        parent_test_modules = sorted([
            d for d in os.listdir(parent_test_root)
            if os.path.isdir(os.path.join(parent_test_root, d))
            and not d.startswith("__")
        ])

    if parent_test_modules and not opts.nomojo:
        for name in parent_test_modules:
            if name not in ignored:
                modules.append(("module", name))

    if not opts.onlymojo:
        app_test_root = os.path.join(paths.APPS_ROOT, "tests")
        if os.path.exists(app_test_root):
            app_modules = sorted([
                d for d in os.listdir(app_test_root)
                if os.path.isdir(os.path.join(app_test_root, d))
                and not d.startswith("__")
            ])
            for name in app_modules:
                if name not in ignored:
                    modules.append(("module", name))

    return modules


def _count_module_tests(module_name, test_root, parent_test_root, quick=False):
    """Count total tests in a module by scanning all test files."""
    test_files, module_path = _discover_test_files(module_name, test_root, parent_test_root)
    total = 0
    for test_name, file_path in test_files:
        total += _count_tests_in_file(file_path, quick=quick)
    return total


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(opts):
    """Main function to run tests."""
    global display_ref

    # Acquire run lock to prevent concurrent test runs
    acquired, lock_info = _acquire_lock()
    if not acquired:
        pid = lock_info.get("pid", "?")
        started = lock_info.get("started", "?")
        user = lock_info.get("user", "?")
        print(f"\n  Another test run is active (pid={pid}, user={user}, started={started})")
        print(f"  Lock file: {_LOCK_FILE}")
        print(f"  If this is stale, remove it: rm {_LOCK_FILE}\n")
        sys.exit(1)

    _abort_event.clear()
    helpers.reset_test_run()
    helpers.STOP_ON_FAIL = bool(opts.stop)
    helpers.VERBOSE = opts.verbose or opts.errors
    helpers.AGENT_MODE = bool(opts.agent)
    helpers.TEST_RUN.started_at = time.time()

    # Set up resume state
    _resume.active = False
    _resume.reached = False
    if getattr(opts, "resume", False):
        checkpoint = load_checkpoint()
        if checkpoint:
            _resume.active = True
            _resume.reached = False
            _resume.module = checkpoint["module"]
            _resume.test_name = checkpoint["test_name"]
            print(f"==> Resuming from: {_resume.module}.{_resume.test_name}")
        else:
            print("==> No checkpoint found, running all tests")

    opts.logger = logit.get_logger("testit", "testit.log")

    parent_test_root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tests")
    if not os.path.exists(parent_test_root):
        parent_test_root = None
    else:
        if parent_test_root not in sys.path:
            sys.path.insert(0, parent_test_root)

    test_root = os.path.join(paths.APPS_ROOT, "tests")

    # Handle --list-extras
    if opts.list_extras:
        extras = []
        roots = [test_root, parent_test_root]

        def add_from_module(module_name, test_name):
            file_path = _resolve_test_file(module_name, test_name, roots)
            extras.extend(collect_module_extras(
                module_name, test_name, file_path=file_path, safe=True))

        def add_from_directory(module_name, _test_root):
            extras.extend(collect_extras_for_module(
                module_name, _test_root, parent_test_root, safe=True))

        if opts.test_modules:
            for test_spec in opts.test_modules:
                if '.' in test_spec:
                    module_name, test_name = test_spec.split('.', 1)
                    add_from_module(module_name, test_name)
                else:
                    add_from_directory(test_spec, test_root)
        else:
            all_modules = _collect_modules(opts, test_root, parent_test_root)
            for kind, name in all_modules:
                if kind == "module":
                    add_from_directory(name, test_root)

        print_extra_flags(extras)
        return

    # Collect modules
    all_modules = _collect_modules(opts, test_root, parent_test_root)

    # Determine if we use rich UI
    use_parallel = opts.jobs > 1 and not opts.verbose
    use_rich = HAS_RICH and not opts.plain and not opts.verbose and use_parallel

    # Load module configs and separate serial vs parallel
    parallel_modules = []
    serial_modules = []
    skipped_modules = []  # (name, reason, test_count)
    file_specs = []

    for kind, name in all_modules:
        if kind == "file":
            file_specs.append(name)
            continue

        # Find module path for config loading
        module_path = os.path.join(test_root, name)
        if not os.path.exists(module_path) and parent_test_root:
            module_path = os.path.join(parent_test_root, name)
        config = _load_module_config(module_path)

        # Check app requirements
        if config.requires_apps:
            try:
                from django.apps import apps
                skip = False
                missing_app = None
                for app_label in config.requires_apps:
                    if not apps.is_installed(app_label):
                        skip = True
                        missing_app = app_label
                        break
                if skip:
                    total = _count_module_tests(name, test_root, parent_test_root, quick=opts.quick)
                    skipped_modules.append((name, f"requires app: {missing_app}", total))
                    continue
            except Exception:
                pass

        # Check extra requirements (opt-in modules like "slow", "security")
        if config.requires_extra:
            required = set(_normalize_extra_value(config.requires_extra))
            provided = set(opts.extra_list or [])
            if not required.intersection(provided):
                total = _count_module_tests(name, test_root, parent_test_root, quick=opts.quick)
                flags = ", ".join(sorted(required))
                skipped_modules.append((name, f"requires --extra {flags}", total))
                continue

        if config.serial or opts.jobs <= 1:
            serial_modules.append(name)
        else:
            parallel_modules.append(name)

    # --- Execute ---
    display = None

    if use_rich:
        display = _RichDisplay()
        display_ref = display

        # Add trackers for parallel modules
        for name in parallel_modules:
            total = _count_module_tests(name, test_root, parent_test_root, quick=opts.quick)
            display.add_module(name, total)

        # Add trackers for serial modules
        for name in serial_modules:
            total = _count_module_tests(name, test_root, parent_test_root, quick=opts.quick)
            display.add_module(name, total)

        # Add trackers for skipped modules — count tests and mark all as skipped
        for name, reason, total in skipped_modules:
            tracker = display.add_module(name, total)
            tracker.completed = total
            tracker.skipped = total
            tracker.skip_reason = reason
            tracker.finished = True
            tracker.started_at = time.time()
            tracker.finished_at = tracker.started_at

        display.start()
        keyboard = _KeyboardListener(display)
        keyboard.start()

        try:
            # Run parallel modules
            if parallel_modules:
                with ThreadPoolExecutor(max_workers=opts.jobs) as executor:
                    futures = {}
                    for name in parallel_modules:
                        tracker = display.trackers[name]
                        future = executor.submit(
                            _run_module_in_thread,
                            opts, name, test_root, parent_test_root, tracker,
                        )
                        futures[future] = name

                    for future in as_completed(futures):
                        try:
                            future.result()
                        except Exception:
                            pass

            # Run serial modules sequentially
            for name in serial_modules:
                if _abort_event.is_set():
                    break
                tracker = display.trackers[name]

                def _make_event_handler(t, d):
                    def _on_event(event, **kwargs):
                        if event == "test_running":
                            t.set_current(kwargs.get("name"))
                            d.refresh()
                        elif event == "test_result":
                            t.record(kwargs.get("status"), kwargs.get("name"), kwargs.get("detail"))
                            d.refresh()
                    return _on_event

                helpers._set_display_fn(_make_event_handler(tracker, display))
                tracker.start()
                try:
                    run_tests_for_module(opts, name, test_root, parent_test_root)
                except helpers.TestitAbort:
                    break
                finally:
                    tracker.finished_at = time.time()
                    tracker.finished = True
                    display.refresh()

        finally:
            keyboard.stop()
            helpers._set_display_fn(None)
            display_ref = None
            display.stop()

        # Print summary
        duration = time.time() - helpers.TEST_RUN.started_at
        _print_summary_rich(display, duration)

    elif use_parallel:
        # Plain text parallel mode — no rich UI, but still run modules in threads
        helpers._set_display_fn(None)
        display_ref = None

        # Run file specs first (sequential)
        for spec in file_specs:
            module_name, test_name = spec.split('.', 1)
            try:
                run_module_tests_by_name(opts, module_name, test_name)
            except helpers.TestitAbort:
                pass

        # Run parallel modules in threads (no display callback — output interleaves)
        if parallel_modules:
            with ThreadPoolExecutor(max_workers=opts.jobs) as executor:
                futures = {}
                for name in parallel_modules:
                    tracker = _ModuleTracker(name, 0)
                    future = executor.submit(
                        _run_module_in_thread,
                        opts, name, test_root, parent_test_root, tracker,
                    )
                    futures[future] = name
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception:
                        pass

        # Run serial modules sequentially
        for name in serial_modules:
            if _resume.active and not _resume.reached:
                if name != _resume.module:
                    continue
            try:
                run_tests_for_module(opts, name, test_root, parent_test_root)
            except helpers.TestitAbort:
                break

        duration = time.time() - helpers.TEST_RUN.started_at
        _print_summary_plain(duration, skipped_modules)

    else:
        # Plain text sequential mode
        helpers._set_display_fn(None)
        display_ref = None

        # Run file specs first
        for spec in file_specs:
            module_name, test_name = spec.split('.', 1)
            try:
                run_module_tests_by_name(opts, module_name, test_name)
            except helpers.TestitAbort:
                break

        # Run all modules sequentially
        for name in parallel_modules + serial_modules:
            if _resume.active and not _resume.reached:
                if name != _resume.module:
                    continue
            try:
                run_tests_for_module(opts, name, test_root, parent_test_root)
            except helpers.TestitAbort:
                break

        duration = time.time() - helpers.TEST_RUN.started_at
        _print_summary_plain(duration, skipped_modules)

    # Handle file specs in rich mode too
    if use_rich and file_specs:
        helpers._set_display_fn(None)
        for spec in file_specs:
            module_name, test_name = spec.split('.', 1)
            try:
                run_module_tests_by_name(opts, module_name, test_name)
            except helpers.TestitAbort:
                break

    # Clear checkpoint on clean completion
    if helpers.TEST_RUN.failed == 0:
        clear_checkpoint()

    # Agent report — structured JSON for LLM consumption
    if opts.agent:
        _write_agent_report(opts, display=display)
        report_path = os.path.join(paths.VAR_ROOT, "test_failures.json")
        print(f"\n  Agent report: {report_path}")
        if helpers.TEST_RUN.failed > 0:
            print(f"  {helpers.TEST_RUN.failed} failure(s) — read the report for diagnostics")

    # Save results
    helpers.TEST_RUN.finished_at = time.time()
    helpers.save_results(os.path.join(paths.VAR_ROOT, "test_results.json"))

    # Release run lock
    _release_lock()

    # Exit with failure status if any test failed
    if helpers.TEST_RUN.failed > 0:
        sys.exit("  Tests failed!")


if __name__ == "__main__":
    opts = setup_parser()
    main(opts)
