import ast
import copy
import json
import os
import stat
import sys
import time
import traceback
import inspect
import argparse
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from importlib import import_module

from mojo.helpers import logit
from testit import helpers
import testit.client
import testit.maestro

from mojo.helpers import paths
from objict import objict

TEST_ROOT = paths.APPS_ROOT / "tests"
_LOCK_FILE = os.path.join(paths.VAR_ROOT, "testit.lock")

_resume = objict(active=False, module=None, test_name=None, reached=False)


def _should_reset_test_logs(opts):
    return not getattr(opts, "resume", False) and not getattr(opts, "list_extras", False)


def _truncate_regular_log(log_path):
    """Truncate one unchanged regular file without following a symlink."""
    before = log_path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise OSError("refusing to truncate a non-regular log file")
    if before.st_nlink != 1:
        raise OSError("refusing to truncate a multiply linked log file")

    flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(log_path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("refusing to truncate a non-regular log file")
        if opened.st_nlink != 1:
            raise OSError("refusing to truncate a multiply linked log file")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise OSError("log file changed while preparing to truncate it")
        os.ftruncate(descriptor, 0)
    finally:
        os.close(descriptor)


def _reset_test_logs(log_root=None):
    """Start a fresh run with empty framework logs.

    Base files are truncated in place so an already-running uvicorn process
    keeps writing to the same inode. Numbered rotation backups belong to the
    previous run and are removed. Failures are returned and never abort tests.
    """
    log_root = Path(paths.LOG_ROOT if log_root is None else log_root)
    if not log_root.exists():
        return []

    failures = []
    candidates = set(log_root.glob("*.log"))
    candidates.update(log_root.glob("*.log.*"))
    for log_path in sorted(candidates):
        if not log_path.is_file():
            continue
        try:
            suffix = log_path.name.rsplit(".log.", 1)
            is_numbered_backup = len(suffix) == 2 and suffix[1].isdigit()
            if is_numbered_backup:
                log_path.unlink()
            elif log_path.name.endswith(".log"):
                _truncate_regular_log(log_path)
        except (OSError, PermissionError) as exc:
            failures.append(f"{log_path.name}: {exc}")
    return failures

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
    default_core=False,
    tier=None,
    time_budget=None,
    file_parallel=False,
)

# Opt-in tiers that --all turns on. See setup_parser for what each one means.
ALL_EXTRAS = ("slow", "extended")

# ---------------------------------------------------------------------------
# Named tiers (maestro #2790)
# ---------------------------------------------------------------------------
# A package declares which BUCKET it belongs to; a runner selects a PRESET, a
# named set of buckets. A preset name not in this table is treated as a literal
# single bucket, so `--tier admin --tier edge` selects exactly those two.
#
#   core       — the ≤30s baseline every consumer runs (populated in Phase 3)
#   framework  — django-mojo's own critical contracts (today's default tier)
#   bug        — one isolated regression per fixed bug
#   extended   — correct-but-not-critical / exhaustive variants
#   admin/edge — admin-portal and edge-deployment coverage most consumers skip
#   slow       — expensive, pre-release only
TIER_PRESETS = {
    "core": {"core"},
    "framework": {"core", "framework", "bug"},
    # "all" is handled specially (select every package) — listed for --list-tiers.
    "all": None,
}

# The preset a bare `bin/run_tests` selects. Phase 3 flips this to "core" once
# core is populated; until then it is "framework" so the bare run is
# byte-identical to today's default tier.
DEFAULT_PRESET = "framework"

# Buckets whose packages run in the parallel ring and are held to the
# isolation contract. `serial` remains an orthogonal execution attribute.
PARALLEL_TIERS = frozenset({"core", "framework"})


def _resolve_tags(config):
    """The set of tier tags one package belongs to, unifying the new `tier`
    key with the legacy `default_core`/`requires_extra` vocabulary.

    Legacy mapping (so every existing package lands correctly with zero edits):
      - default_core=True                      -> {"framework"}
      - nonempty requires_extra, no tier       -> those opt-in tags (as today)
      - neither (permissive/consumer default)  -> {"framework"} (runs by default)
      - tier="X"                               -> {"X"} (plus any requires_extra)

    One-vocabulary errors (default_core with tier, or default_core with
    requires_extra) are reported by the isolation policy, not here — this helper
    only computes selection tags and must never raise mid-run.
    """
    tier = config.get("tier")
    default_core = bool(config.get("default_core", False))
    extras = set(_normalize_extra_value(config.get("requires_extra")))
    tags = set(extras)
    if tier:
        tags.add(tier)
    elif default_core:
        tags.add("framework")
    if not tags:
        # No tier, not default_core, no requires_extra: the historical
        # permissive default ran in the default tier, so it belongs to
        # framework. (Repository packages in this state fail the fail-closed
        # policy separately; consumer roots are exempt and keep running.)
        tags.add("framework")
    return tags


def _selected_tags(opts):
    """Resolve the run's selected tier tags. Returns (tags, select_all).

    select_all=True short-circuits filtering — every package runs (the `all`
    preset / legacy `--all`). Otherwise a package runs iff its tags intersect
    `tags`. `--extra X` adds ad-hoc tags on top of the preset, preserving the
    historical "default tier PLUS the X packages" meaning of `--extra`.
    """
    tier_names = list(getattr(opts, "tiers", None) or [])
    if getattr(opts, "all", False):
        tier_names.append("all")
    tags = set()
    select_all = False
    if not tier_names:
        tags |= TIER_PRESETS[DEFAULT_PRESET]
    for name in tier_names:
        if name == "all":
            select_all = True
        elif name in TIER_PRESETS and TIER_PRESETS[name] is not None:
            tags |= TIER_PRESETS[name]
        else:
            tags.add(name)
    tags |= set(getattr(opts, "extra_list", None) or [])
    return tags, select_all


def _selected_preset_label(opts):
    """A short label naming what this run selected, for the report and the
    maestro suite identity (so core / framework / all report separately).
    Safe to call before selection resolves select_all."""
    if getattr(opts, "select_all", False) or getattr(opts, "all", False):
        return "all"
    tiers = list(getattr(opts, "tiers", None) or [])
    if not tiers:
        return DEFAULT_PRESET
    if "all" in tiers:
        return "all"
    return "+".join(sorted(set(tiers)))


# Whole-suite wall-clock budgets per preset (seconds), overridable in
# tests/testit.json {"budgets": {...}} and scaled by TESTIT_BUDGET_SCALE for
# slow machines (maestro #2790).
_DEFAULT_TIER_BUDGETS = {"core": 30.0, "framework": 90.0}


def _load_tier_budgets(parent_test_root):
    budgets = dict(_DEFAULT_TIER_BUDGETS)
    if not parent_test_root:
        return budgets
    path = os.path.join(parent_test_root, "testit.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return budgets
    declared = data.get("budgets")
    if isinstance(declared, dict):
        for key, value in declared.items():
            try:
                budgets[key] = float(value)
            except (TypeError, ValueError):
                pass
    return budgets


def _budget_scale():
    try:
        return max(0.1, float(os.environ.get("TESTIT_BUDGET_SCALE", "1") or "1"))
    except (TypeError, ValueError):
        return 1.0


def _compute_budget_violations(opts, report, parent_test_root):
    """Wall-clock budget check (maestro #2790). Over budget x1.25 is a
    violation; below 0.6x budget is a stale-warning (lower it). Covers the
    selected preset's whole-suite budget and any per-package `time_budget`.
    Returns a list of dicts for the report and for printing."""
    scale = _budget_scale()
    violations = []
    # A partial run (-t / --ignore) is not the preset's wall clock, so the
    # whole-suite budget would be meaningless — but per-package time_budgets
    # still apply to whatever ran.
    partial = bool(getattr(opts, "test_modules", None)
                   or getattr(opts, "ignore_modules", None))
    preset = getattr(opts, "selected_preset", None)
    budgets = _load_tier_budgets(parent_test_root)
    if not partial and preset in budgets:
        budget = budgets[preset] * scale
        actual = report.get("duration") or 0
        if budget > 0 and actual > budget * 1.25:
            violations.append({
                "scope": "preset", "name": preset, "kind": "over",
                "budget": round(budget, 1), "actual": round(actual, 1)})
        elif budget > 0 and actual < budget * 0.6:
            violations.append({
                "scope": "preset", "name": preset, "kind": "stale",
                "budget": round(budget, 1), "actual": round(actual, 1)})
    time_budgets = getattr(opts, "_time_budgets", None) or {}
    modules = report.get("modules") or {}
    for name, budget in time_budgets.items():
        if not budget:
            continue
        try:
            budget = float(budget) * scale
        except (TypeError, ValueError):
            continue
        actual = (modules.get(name) or {}).get("duration") or 0
        if budget > 0 and actual > budget * 1.25:
            violations.append({
                "scope": "package", "name": name, "kind": "over",
                "budget": round(budget, 1), "actual": round(actual, 1)})
    return violations


# Slow-test stack dump (maestro #2790): a background watchdog that captures
# every thread's stack when a single test runs longer than the threshold. This
# is the tool for the unexplained exact-10s block/unblock stall that reproduces
# only under the parallel suite. Collected here for the report.
_SLOW_STACKS = []
_SLOW_STACKS_LOCK = threading.Lock()


def _slow_dump_threshold():
    try:
        return max(1.0, float(os.environ.get("TESTIT_SLOW_DUMP_SECS", "15") or "15"))
    except (TypeError, ValueError):
        return 15.0


def _capture_all_stacks(test_key, age):
    """Snapshot every thread's stack — the diagnostic for a test that hangs."""
    frames = sys._current_frames()
    names = {t.ident: t.name for t in threading.enumerate()}
    dump = []
    for tid, frame in frames.items():
        stack = "".join(traceback.format_stack(frame))
        dump.append(f"--- thread {names.get(tid, tid)} ({tid}) ---\n{stack}")
    text = "\n".join(dump)
    entry = {"test": test_key, "age": round(age, 1), "stacks": text}
    with _SLOW_STACKS_LOCK:
        _SLOW_STACKS.append(entry)
    try:
        logit.get_logger("testit", "testit.log").error(
            f"SLOW TEST >{round(age,1)}s: {test_key}\n{text}")
    except Exception:
        pass


class _SlowTestWatchdog(threading.Thread):
    """Polls the active-test registry; dumps stacks once per slow test."""

    def __init__(self, threshold, poll=2.0):
        super().__init__(daemon=True)
        self.threshold = threshold
        self.poll = poll
        self._stop = threading.Event()
        self._dumped = set()

    def run(self):
        while not self._stop.wait(self.poll):
            try:
                for key, age in helpers.active_test_ages():
                    if age >= self.threshold and key not in self._dumped:
                        self._dumped.add(key)
                        _capture_all_stacks(key, age)
            except Exception:
                pass

    def stop(self):
        self._stop.set()


def _print_budget_violations(opts, violations):
    """Print budget violations (maestro #2790). Informational for every preset;
    the exit-code consequence is decided by the caller."""
    if not violations:
        return
    print("\n  Tier budget:")
    for v in violations:
        if v["kind"] == "over":
            print(f"    ! {v['scope']} '{v['name']}' took {v['actual']}s, "
                  f"budget {v['budget']}s — tag slow tests into extended/bug, "
                  f"give them a seam, or raise the budget in tests/testit.json")
        elif v["kind"] == "stale":
            print(f"    · {v['scope']} '{v['name']}' took {v['actual']}s, well "
                  f"under budget {v['budget']}s — consider lowering the budget "
                  f"in tests/testit.json so it cannot silently regrow")


def _budget_fails_run(opts, violations):
    """Whether budget violations should fail the exit code (maestro #2790).

    The core preset is a hard gate — it is the ≤30s baseline. Other presets
    (framework included) only warn locally; framework is not under budget until
    Phases 2-3 land, and CI enforcement is wired in Phase 4."""
    preset = getattr(opts, "selected_preset", None)
    if preset == "core":
        return any(v["kind"] == "over" and v["scope"] in ("preset", "package")
                   for v in violations)
    return False


def _record_has_selected_tag(record, selected_tags):
    """True when a package that does not match at the package level still holds
    per-file (TESTIT_TIER) or per-function (@th.tier) tests tagged into a
    selected bucket. No such tags exist until Phase 3 curation, so this returns
    False everywhere today and leaves selection byte-identical."""
    if not selected_tags:
        return False
    from testit import isolation
    for _test_name, file_path in _discover_record_files(record):
        facts = isolation.cached_file_facts(file_path)
        if getattr(facts, "tiers", None) and facts.tiers & selected_tags:
            return True
    return False


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


def _load_module_config_ex(module_path):
    """Load TESTIT config from a module's __init__.py via AST (no import side
    effects). Returns (config, state) where state is one of:

    "ok"            — a literal TESTIT dict was read
    "missing_init"  — the directory has no __init__.py
    "missing_testit"— __init__.py exists but declares no TESTIT dict
    "invalid"       — __init__.py or its TESTIT value could not be parsed

    Every non-"ok" state yields the permissive defaults for backward
    compatibility; the repository policy treats them as fail-closed instead.
    """
    init_path = os.path.join(module_path, "__init__.py")
    if not os.path.exists(init_path):
        return objict(_DEFAULT_CONFIG), "missing_init"

    try:
        with open(init_path, "r", encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source, filename=init_path)
    except (OSError, SyntaxError):
        return objict(_DEFAULT_CONFIG), "invalid"

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "TESTIT":
                    try:
                        value = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        return objict(_DEFAULT_CONFIG), "invalid"
                    if isinstance(value, dict):
                        merged = dict(_DEFAULT_CONFIG)
                        merged.update(value)
                        return objict(merged), "ok"
                    return objict(_DEFAULT_CONFIG), "invalid"
    return objict(_DEFAULT_CONFIG), "missing_testit"


def _load_module_config(module_path):
    """Load TESTIT config from a module's __init__.py via AST (no import side effects)."""
    config, _state = _load_module_config_ex(module_path)
    return config


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
        config_path = paths.resolve_conf("dev_server.conf")
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
    config_parser.add_argument("--list-extras", "--list-tiers", action="store_true",
                               dest="list_extras",
                               help="Scan tests and list declared tier / @requires_extra flags")

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
    parser.add_argument("--tier", action="append", dest="tiers",
                        help="Select a tier preset (core, framework, all) or a "
                             "literal bucket (bug, extended, admin, edge, slow). "
                             "Repeatable. Default: the framework preset.")
    parser.add_argument("--all", action="store_true",
                        help="Run every tier (same as --tier all)")
    # Compatibility only: old commands keep running the ordinary default
    # suite, but the retired spelling no longer selects opt-in tiers or appears
    # in help.
    parser.add_argument("--full", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--maestro", action="store_true",
                        help="Warn if this run cannot be reported to maestro "
                             "(reporting is automatic when maestro is installed)")
    parser.add_argument("--no-maestro", dest="no_maestro", action="store_true",
                        help="Do not report this run to maestro")

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
    opts.tiers = list(opts.tiers or [])
    # Resolved again in main() once select_all is known; set here too so early
    # readers (the maestro suite identity) see the right label (maestro #2790).
    opts.selected_preset = _selected_preset_label(opts)

    # Normalize extras for both config defaults and CLI input.
    extra_values = _normalize_extra_value(opts.extra)
    if opts.all:
        # --all means "everything", so it must imply every opt-in tier. Two
        # tiers exist because they mean different things and are chosen on
        # different grounds:
        #   slow     — expensive or only meaningful before a release
        #   extended — correct and cheap enough, but not a critical contract
        # Overloading one word for both would make "why is this opt-in?"
        # unanswerable from the tag alone.
        for tier in ALL_EXTRAS:
            if tier not in extra_values:
                extra_values.append(tier)
    opts.extra_list = extra_values
    opts.extra = ",".join(extra_values) if extra_values else ""

    # Resolve parallelism
    if opts.jobs is None:
        if opts.stop or opts.verbose:
            opts.jobs = 1
        else:
            # Derived from CPU count, capped (maestro #2789): every worker
            # funnels its HTTP through one uvicorn process, so beyond the cap
            # the workers mostly contend. -j overrides.
            opts.jobs = min(8, max(2, (os.cpu_count() or 4) - 2))
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
            helpers.mark_aborted()
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
            helpers.mark_aborted()
            raise helpers.TestitAbort()
        return False


def import_module_for_testing(module_name, test_name, expected_root=None):
    """Dynamically import a test module.

    expected_root pins the import to a resolved package directory: an import
    that lands outside it means another same-named package on sys.path
    shadowed the one this record resolved to. That is reported and refused —
    running a chimera of two packages is strictly worse than failing.
    """
    name = f"{module_name}.{test_name}"
    try:
        module = import_module(name)
    except (ImportError, RuntimeError):
        print(f"  Failed to import test module: {name}")
        traceback.print_exc()
        return None
    if expected_root:
        module_file = getattr(module, "__file__", None) or ""
        expected = str(expected_root)
        if not module_file.startswith(expected.rstrip(os.sep) + os.sep):
            print(
                f"  Refusing to run {name}: import resolved to "
                f"{module_file or '<unknown>'} instead of the collected "
                f"package at {expected}. A same-named package elsewhere on "
                "sys.path is shadowing it — rename one of the packages.")
            return None
    return module


def _sort_key(name):
    prefix = name.split("_", 1)[0]
    return (int(prefix), name) if prefix.isdigit() else (float("inf"), name)


def _count_tests_in_file(file_path):
    """Count test functions in a file via the shared cached AST facts —
    one parse serves both this count and the isolation scan, cached across
    runs (maestro #2789)."""
    from testit import isolation
    return isolation.cached_file_facts(file_path).tests


def _discover_test_files(module_name, test_root, parent_test_root=None):
    """Find the module directory and return sorted list of (test_name, file_path).

    Root preference (consumer first, then repository) matches _module_record;
    callers holding a record should use _discover_record_files so discovery
    and execution agree on one resolved directory.
    """
    module_path = os.path.join(test_root, module_name)
    if not os.path.exists(module_path):
        if parent_test_root:
            module_path = os.path.join(parent_test_root, module_name)
        if not os.path.exists(module_path):
            return [], module_path
    return _list_module_files(module_path), module_path


def _list_module_files(module_path):
    """Sorted (test_name, file_path) pairs for one resolved module directory."""
    try:
        entries = os.listdir(module_path)
    except OSError:
        return []
    test_files = [f for f in entries
                  if f.endswith(".py") and f not in ["__init__.py", "setup.py"]
                  and not f.startswith("_")]
    result = []
    for test_file in sorted(test_files, key=_sort_key):
        test_name = test_file.rsplit('.', 1)[0]
        file_path = os.path.join(module_path, test_file)
        result.append((test_name, file_path))
    return result


def _discover_record_files(record):
    """Discovery for a resolved module record: exactly its directory, never a
    re-derived root."""
    if not os.path.exists(record.path):
        return []
    return _list_module_files(record.path)


def _record_unrunnable_file(module_name, test_name, expected_root, reason):
    """A test file that could not run at all is a FAILURE, never a silent skip.

    The refusal used to be a print() on a channel agents are told not to read,
    so a whole package could vanish from a run whose report still said passed
    (security-review finding, item #1839). Count the file's tests and land one
    error in the report so status goes red and failures[] names the file.
    """
    file_path = None
    if expected_root:
        candidate = os.path.join(expected_root, f"{test_name}.py")
        if os.path.exists(candidate):
            file_path = candidate
    total = _count_tests_in_file(file_path) if file_path else 0
    helpers._set_active_test(f"{module_name}:{test_name}:<import>")
    helpers._increment("total", max(total, 1))
    helpers._increment("failed")
    helpers._record_result(
        f"{test_name} (unrunnable)", status="error",
        detail=f"{module_name}.{test_name} did not run: {reason}")
    dfn = helpers._get_display_fn()
    if dfn:
        dfn("test_result", name=f"{test_name} (unrunnable)", status="error",
            detail=reason)


def run_module_tests_by_name(opts, module_name, test_name, expected_root=None):
    """Run all test functions in a specific test module in the order they appear."""
    module = import_module_for_testing(module_name, test_name, expected_root)
    if not module:
        _record_unrunnable_file(
            module_name, test_name, expected_root,
            "import failed or resolved to a shadowing package (see output above)")
        return
    skipped = run_module_setup(opts, module, test_name, module_name)
    if skipped:
        # Count all tests in this file as skipped so totals stay consistent
        prefix = "test_"
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
    prefix = "test_"

    functions = inspect.getmembers(module, inspect.isfunction)
    functions = sorted(
        functions,
        key=lambda func: inspect.getsourcelines(func[1])[1]
    )

    for func_name, func in functions:
        if func_name.startswith(prefix):
            if _abort_event.is_set():
                helpers.mark_aborted()
                raise helpers.TestitAbort()
            # Tier gate (maestro #2790): a test tagged into a bucket this run
            # did not select is a COUNTED skip, decided here before invoking so
            # the module still pays no setup for it. No @th.tier decorators
            # exist until Phase 3, so this is inert today.
            func_tier = getattr(func, "_tier", None)
            if func_tier and not getattr(opts, "select_all", False):
                selected = getattr(opts, "selected_tags", None) or set()
                if func_tier not in selected:
                    helpers.record_tier_skip(
                        module_name, test_name, func_name, func_tier)
                    continue
            # Track current test for the running display
            dfn = helpers._get_display_fn()
            if dfn:
                dfn("test_running", name=func_name)
            run_test(opts, module, func_name, module_name, test_name)

    if not helpers._get_display_fn():
        duration = time.time() - started
        print(f"{helpers.INDENT}---------\n{helpers.INDENT}run time: {duration:.2f}s")


def run_tests_for_record(opts, record):
    """Discover and run tests for one collected module record."""
    test_files = _discover_record_files(record)
    if not test_files:
        return

    for test_name, file_path in test_files:
        if _resume.active and not _resume.reached:
            if record.name != _resume.module or test_name != _resume.test_name:
                continue
            _resume.reached = True
        run_module_tests_by_name(opts, record.name, test_name,
                                 expected_root=record.path)


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
# django.conf drift detection
# ---------------------------------------------------------------------------
# BEST-EFFORT IN-RUN HYGIENE, not a guarantee. th.server_settings() writes
# overrides into var/django.conf and takes them back out again; a bug (or a run
# killed mid-context) can leave one behind, where it silently changes the
# behavior of every later run. Snapshotting the parsed config at both ends of a
# run catches the ones that survive to the end — it cannot catch a crash-strand,
# because the second snapshot never happens.
#
# Key NAMES only, ever. An override's VALUE may be a credential; it must not
# reach the console or the agent report.
def _snapshot_conf(conf_path=None):
    """Parsed key -> value map of var/django.conf, or None if unreadable."""
    try:
        from mojo.helpers.settings.parser import DjangoConfigLoader
        if conf_path is None:
            conf_path = paths.VAR_ROOT / "django.conf"
        context = {}
        DjangoConfigLoader(config_path=conf_path).load_config(context)
        return context
    except Exception:
        # No conf file, or a value this loader cannot parse — skip detection
        # rather than fail a test run over a diagnostic.
        return None


def _conf_drift(before, after):
    """Names of the keys that changed between two snapshots. Never values."""
    if before is None or after is None:
        return []
    drifted = set(before.keys()) ^ set(after.keys())
    for key in set(before.keys()) & set(after.keys()):
        try:
            if before[key] != after[key]:
                drifted.add(key)
        except Exception:
            drifted.add(key)
    return sorted(drifted)


def _report_conf_drift(drifted):
    if not drifted:
        return
    logit.color_print(
        "\n  !! var/django.conf CHANGED during this run: " + ", ".join(drifted),
        logit.ConsoleLogger.RED)
    logit.color_print(
        "     A th.server_settings() context did not restore cleanly. Those keys are "
        "now stranded and will affect every later run — inspect var/django.conf.",
        logit.ConsoleLogger.RED)
    logit.color_print(
        "     (values withheld on purpose — an override may be a credential)",
        logit.ConsoleLogger.YELLOW)


# ---------------------------------------------------------------------------
# Agent output
# ---------------------------------------------------------------------------
def _build_agent_report(opts, display=None, conf_drift=None):
    """Build the structured test report.

    Split out from _write_agent_report so the maestro reporter can obtain the
    same report without depending on --agent having written the file. Reads
    nothing off `opts` — anything the caller needs to add belongs at the call
    site, not in here.
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

        # Server error log tail. Logs live under VAR_ROOT/logs/, not VAR_ROOT —
        # this read silently found nothing for as long as the field has existed.
        try:
            log_path = os.path.join(paths.VAR_ROOT, "logs", "error.log")
            if os.path.exists(log_path):
                with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.readlines()
                entry["server_log_tail"] = "".join(lines[-20:])
        except Exception:
            pass

        failures.append(entry)

    module_tiers = getattr(opts, "_module_tiers", None) or {}

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
            if module_tiers.get(name):
                entry["tier"] = module_tiers[name]
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

    # Slowest individual tests — the input for deciding what to re-tier. Module
    # timing alone cannot tell you which test inside a slow module is the cost.
    slowest = sorted(
        (r for r in helpers.TEST_RUN.records if r.get("duration") is not None),
        key=lambda r: r["duration"],
        reverse=True,
    )[:25]
    slowest = [
        {
            "test_name": r.get("name"),
            "module": r.get("module"),
            "test_file": r.get("test_module"),
            "status": r.get("status"),
            "duration": r["duration"],
        }
        for r in slowest
    ]

    # Top-level rollup MUST equal the sum of the per-module values. TEST_RUN's
    # counters only see tests that actually executed, so a module skipped whole
    # (requires_extra / requires_apps) contributed nothing to total/skipped even
    # though its per-module entry counts its tests. That gap made every baseline
    # comparison drift by the size of the opt-in tier. See item #1127.
    totals = {"tests": 0, "passed": 0, "failed": 0, "skipped": 0}
    for entry in modules.values():
        for key in totals:
            totals[key] += entry.get(key, 0) or 0

    report = {
        "status": "passed" if helpers.TEST_RUN.failed == 0 else "failed",
        # The tier preset this run selected (maestro #2790) — core / framework /
        # all / a literal bucket. Distinguishes suite identities on the board.
        "preset": getattr(opts, "selected_preset", None),
        "total": totals["tests"],
        "passed": totals["passed"],
        "failed": totals["failed"],
        "skipped": totals["skipped"],
        # What actually executed this run, i.e. excluding whole-skipped modules.
        "ran": {
            "total": helpers.TEST_RUN.total,
            "passed": helpers.TEST_RUN.passed,
            "failed": helpers.TEST_RUN.failed,
            "skipped": helpers.TEST_RUN.skipped,
        },
        "duration": round(duration, 2),
        # Epoch seconds. The maestro reporter sends this as the run's start
        # time; an agent reading the file gets it for free.
        "started_at": helpers.TEST_RUN.started_at,
        "modules": modules,
        "slowest": slowest,
        "failures": failures,
        # Key names only — never the values (see _snapshot_conf).
        "conf_drift": list(conf_drift or []),
    }
    return report


def _write_agent_report(opts, display=None, conf_drift=None):
    """Write the structured test report to var/test_failures.json for LLM agents.

    This is the primary output channel for --agent mode. Agents should read
    this file instead of parsing terminal output. Returns the report it wrote.
    """
    report = _build_agent_report(opts, display=display, conf_drift=conf_drift)

    report_path = os.path.join(paths.VAR_ROOT, "test_failures.json")
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    return report


def _push_refused(opts):
    """Why this run must not be reported to maestro, or None when it may be.

    maestro answers "is this project green?" from the latest push per suite, so
    a run that is not the suite's verdict would overwrite a real result with a
    partial one.
    """
    if helpers.TEST_RUN.aborted or _abort_event.is_set():
        return "run stopped early"
    if opts.test_modules:
        return "run was limited with -t"
    if opts.ignore_modules:
        return "run skipped modules with --ignore"
    return None


# ---------------------------------------------------------------------------
# Parallel module runner
# ---------------------------------------------------------------------------
def _run_module_in_thread(opts_template, record, tracker):
    """Run all test files for a module record. Called from a thread."""
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
    test_files = _discover_record_files(record)
    try:
        for test_name, file_path in test_files:
            if _abort_event.is_set():
                helpers.mark_aborted()
                raise helpers.TestitAbort()
            run_module_tests_by_name(opts, record.name, test_name,
                                     expected_root=record.path)
    except helpers.TestitAbort:
        pass
    finally:
        tracker.set_current(None)
        tracker.finished_at = time.time()
        tracker.finished = True
        helpers._set_display_fn(None)
        if display_ref:
            display_ref.refresh()

    return record.name


# Module-level ref so threads can access the display
display_ref = None


ORIGIN_REPO = "django_mojo"
ORIGIN_CONSUMER = "consumer"


def _dir_has_python(path):
    """Whether a directory holds any .py file at all (maestro #2789).

    An unreadable directory raises: swallowing the OSError would silently
    drop a whole package from both the policy scan and collection, and the
    run would report green over tests that never ran."""
    return any(entry.endswith(".py") for entry in os.listdir(path))


def _previous_module_durations():
    """Per-module durations from the last run's report, or {} (maestro #2789)."""
    report_path = os.path.join(paths.VAR_ROOT, "test_failures.json")
    try:
        with open(report_path, "r", encoding="utf-8") as fh:
            modules = (json.load(fh) or {}).get("modules") or {}
        return {name: float(row.get("duration", 0) or 0)
                for name, row in modules.items()}
    except Exception:
        return {}


def _enforce_repository_policy(parent_test_root):
    """Fail-closed default-tier policy over django-mojo's own test tree.

    Runs before any import or worker, over the COMPLETE repository tree —
    targeted and direct-file runs do not bypass it. Consumer/application
    test roots are exempt and keep the historical selection contract.

    Blocking: hot-ring violations (see testit.isolation.HOT_CODES and
    SHARED_PATCH_PREFIXES) inside a default package, and any repository
    package without a readable literal TESTIT config declaring its state.
    Cold-ring violations (app-internal provider mocks) are advisory until
    the cold-ring migration (maestro item #1839 follow-up).

    Returns the advisory (cold) counts as (sites, packages).
    """
    from testit import isolation

    if not parent_test_root or not os.path.exists(parent_test_root):
        return 0, 0

    problems = []
    cold_sites = 0
    cold_packages = 0
    for name in sorted(os.listdir(parent_test_root)):
        package_path = os.path.join(parent_test_root, name)
        if not os.path.isdir(package_path) or name.startswith("__"):
            continue
        if not _dir_has_python(package_path):
            # Not a test package — most commonly a stale __pycache__-only
            # leftover from a branch switch (maestro #2789). Failing closed
            # on it used to block the whole run.
            continue
        config, state = _load_module_config_ex(package_path)
        # A `core` package is scanned under the strict contract (maestro #2790):
        # its `_`-prefixed helpers are scanned too and the stricter grammar
        # fires. No package declares tier="core" until Phase 3, so strict stays
        # False here and every scan is byte-identical to the pre-tier behavior.
        tier = config.get("tier")
        strict = tier == "core"
        scanned = isolation.scan_package(package_path, strict=strict)
        hot, cold = isolation.partition_violations(scanned.violations)
        if cold:
            cold_sites += len(cold)
            cold_packages += 1
        package_problems = isolation.evaluate_package_state(
            config, hot, origin=ORIGIN_REPO, has_config=(state == "ok"))
        package_problems += isolation.evaluate_cold_budget(
            config, cold, origin=ORIGIN_REPO, has_config=(state == "ok"))
        for problem in package_problems:
            problems.append(f"{name}: {problem}")
        # A serial opt-in package may carry mutation (its isolation is serial
        # execution) — legacy requires_extra+serial, or a new non-parallel
        # bucket (bug/extended/admin/edge/slow) declared serial.
        opt_in_serial = bool(config.serial) and (
            bool(config.requires_extra)
            or (tier and tier not in isolation.PARALLEL_TIERS))
        if hot and not opt_in_serial:
            problems.append(
                f"{name}: {len(hot)} blocking isolation violation(s):\n"
                + "\n".join(
                    f"    {row.file}:{row.line}: [{row.code}] {row.detail}"
                    for row in hot))

    if problems:
        sys.exit(
            "\nDEFAULT-TIER ISOLATION POLICY FAILED — no tests were run.\n"
            "Every repository test package must declare default_core=True "
            "(clean) or a nonempty requires_extra (opt-in, serial when it "
            "mutates). See .claude/rules/testing.md and "
            "docs/django_developer/testit/Overview.md.\n\n"
            + "\n".join(problems))
    return cold_sites, cold_packages


def _make_record(kind, name, origin, path, test_file=None):
    """One collected unit of work, with its ownership resolved up front.

    kind: "module" (a package directory) or "file" (a single -t pkg.file spec)
    name: the package name
    origin: ORIGIN_REPO for django-mojo's own tests/, ORIGIN_CONSUMER for the
        application test root
    path: the absolute package directory this record executes from
    test_file: the file's test name for kind="file", else None
    has_init: whether the resolved directory carries an __init__.py — the
        fail-closed policy state needs this distinction explicitly
    """
    return objict(
        kind=kind,
        name=name,
        origin=origin,
        path=str(path),
        test_file=test_file,
        has_init=os.path.exists(os.path.join(path, "__init__.py")),
    )


def _module_record(name, test_root, parent_test_root, kind="module", test_file=None):
    """Resolve one requested name to a record. Consumer root wins when both
    carry the name (the historical explicit-spec precedence); the path is
    recorded so every later stage uses this exact resolution instead of
    re-deriving its own."""
    consumer_path = os.path.join(test_root, name)
    if os.path.exists(consumer_path):
        return _make_record(kind, name, ORIGIN_CONSUMER, consumer_path, test_file)
    if parent_test_root:
        parent_path = os.path.join(parent_test_root, name)
        if os.path.exists(parent_path):
            return _make_record(kind, name, ORIGIN_REPO, parent_path, test_file)
    # Nonexistent either way — keep the consumer path so discovery reports
    # the same empty result it always has.
    return _make_record(kind, name, ORIGIN_CONSUMER, consumer_path, test_file)


def _collect_modules(opts, test_root, parent_test_root):
    """Collect the records to run, respecting filters and ignore lists.

    Returns objict records (see _make_record). A consumer package sharing a
    repository package's name is skipped with a loud warning instead of
    silently shadowing or duplicating it — one import name cannot bind two
    packages in one process, and the historical behavior (discovering the
    consumer's files while importing the repository's code) ran a chimera of
    the two.
    """
    records = []
    ignored = opts.ignore_modules or []

    if opts.test_modules:
        for test_spec in opts.test_modules:
            if '.' in test_spec:
                module_name, test_name = test_spec.split('.', 1)
                records.append(_module_record(
                    module_name, test_root, parent_test_root,
                    kind="file", test_file=test_name))
            else:
                if test_spec not in ignored:
                    records.append(_module_record(
                        test_spec, test_root, parent_test_root))
        return records

    repo_names = set()
    if parent_test_root and os.path.exists(parent_test_root):
        parent_test_modules = sorted([
            d for d in os.listdir(parent_test_root)
            if os.path.isdir(os.path.join(parent_test_root, d))
            and not d.startswith("__")
            and _dir_has_python(os.path.join(parent_test_root, d))
        ])
        if not opts.nomojo:
            for name in parent_test_modules:
                if name not in ignored:
                    records.append(_make_record(
                        "module", name, ORIGIN_REPO,
                        os.path.join(parent_test_root, name)))
                    repo_names.add(name)

    if not opts.onlymojo:
        app_test_root = test_root
        if os.path.exists(app_test_root):
            app_modules = sorted([
                d for d in os.listdir(app_test_root)
                if os.path.isdir(os.path.join(app_test_root, d))
                and not d.startswith("__")
            ])
            for name in app_modules:
                if name in ignored:
                    continue
                if name in repo_names:
                    print(
                        f"\n  !! consumer test package '{name}' shares a "
                        "repository package's name and is SKIPPED — rename it; "
                        "one import name cannot execute two packages in one "
                        "process")
                    continue
                records.append(_make_record(
                    "module", name, ORIGIN_CONSUMER,
                    os.path.join(app_test_root, name)))

    return records


def _count_record_tests(record):
    """Count total tests in a module record by scanning its test files."""
    total = 0
    for test_name, file_path in _discover_record_files(record):
        total += _count_tests_in_file(file_path)
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
    # Baseline for the end-of-run django.conf drift check (see _snapshot_conf).
    conf_before = _snapshot_conf()
    helpers.reset_test_run()
    helpers.STOP_ON_FAIL = bool(opts.stop)
    helpers.VERBOSE = opts.verbose or opts.errors
    # Resolved before the suite runs, for two reasons: a misconfiguration
    # should be reported now rather than ten minutes from now, and reporting a
    # run implies agent mode — without it no failure carries a file, line or
    # traceback to report.
    maestro_settings = testit.maestro.setup(opts)
    helpers.AGENT_MODE = bool(opts.agent) or bool(maestro_settings)
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

    if _should_reset_test_logs(opts):
        for reset_failure in _reset_test_logs():
            sys.stderr.write(f"Warning: could not reset test log {reset_failure}\n")

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
            all_records = _collect_modules(opts, test_root, parent_test_root)
            for record in all_records:
                if record.kind == "module":
                    add_from_directory(record.name, test_root)

        print_extra_flags(extras)
        return

    # Cross-run AST-fact cache: one parse serves the isolation scan and the
    # per-module test counts, and warm runs skip both (maestro #2789). Keyed
    # on the scanner's own source hash, so a grammar change invalidates it.
    isolation_cache_path = os.path.join(paths.VAR_ROOT, "isolation_scan_cache.json")
    try:
        from testit import isolation as _isolation_cache_mod
        _isolation_cache_mod.load_scan_cache(isolation_cache_path)
    except Exception:
        _isolation_cache_mod = None

    # Fail-closed repository isolation policy — before any import or worker.
    cold_sites, cold_packages = _enforce_repository_policy(parent_test_root)
    if cold_sites:
        print(
            f"  isolation advisory: {cold_sites} app-internal patch site(s) in "
            f"{cold_packages} package(s) remain outside the enforced ring "
            "(cold ring, maestro item #1839)")

    # Collect module records
    all_records = _collect_modules(opts, test_root, parent_test_root)

    # Determine if we use rich UI
    use_parallel = opts.jobs > 1 and not opts.verbose
    use_rich = HAS_RICH and not opts.plain and not opts.verbose and use_parallel

    # Load module configs and separate serial vs parallel
    parallel_modules = []   # records
    serial_modules = []     # records
    skipped_modules = []    # (name, reason, test_count)
    file_specs = []         # records with kind="file"

    # Resolve which tier buckets this run selects (maestro #2790). The bucket a
    # package declares is matched against the preset the runner selected; the
    # selected tags are also merged into extra_list so per-function
    # @requires_extra / @th.tier decorators satisfy on the same basis.
    selected_tags, select_all = _selected_tags(opts)
    opts.selected_tags = selected_tags
    opts.select_all = select_all
    opts.extra_list = sorted(set(opts.extra_list or []) | selected_tags)
    opts.selected_preset = _selected_preset_label(opts)
    time_budgets = {}   # record.name -> declared time_budget (maestro #2790)
    module_tiers = {}   # record.name -> sorted tier tags, for the report

    for record in all_records:
        if record.kind == "file":
            file_specs.append(record)
            continue

        config = _load_module_config(record.path)
        module_tiers[record.name] = sorted(_resolve_tags(config))

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
                    total = _count_record_tests(record)
                    skipped_modules.append((record.name, f"requires app: {missing_app}", total))
                    continue
            except Exception:
                pass

        # Tier selection (maestro #2790). A package runs when its declared
        # buckets intersect the selected tags, OR when it holds per-file /
        # per-function tests tagged into a selected bucket (Phase 3 curation).
        # An explicit `-t pkg` spec means "run this package regardless of tags".
        if not select_all and not opts.test_modules:
            pkg_tags = _resolve_tags(config)
            if not (pkg_tags & selected_tags) and not _record_has_selected_tag(
                    record, selected_tags):
                total = _count_record_tests(record)
                want = ", ".join(sorted(selected_tags)) or "(none)"
                skipped_modules.append(
                    (record.name,
                     f"not in selected tier(s): {want}", total))
                continue

        if config.time_budget:
            time_budgets[record.name] = config.time_budget

        if config.serial or opts.jobs <= 1:
            serial_modules.append(record)
        else:
            parallel_modules.append(record)

    opts._time_budgets = time_budgets
    opts._module_tiers = module_tiers

    # Longest-first (LPT) submission (maestro #2789): with package-granular
    # work units, alphabetical order regularly started a large module last and
    # let it pin the wall clock. Order by the previous run's per-module
    # durations; modules with no history sort first (they may be large).
    if len(parallel_modules) > 1:
        prev_durations = _previous_module_durations()
        parallel_modules.sort(
            key=lambda r: -prev_durations.get(r.name, float("inf")))

    # --- Execute ---
    # Slow-test stack-dump watchdog (maestro #2790). Off with -s/-v (serial
    # debugging already surfaces a hang).
    _SLOW_STACKS.clear()
    slow_watchdog = None
    if not opts.stop and not opts.verbose:
        slow_watchdog = _SlowTestWatchdog(_slow_dump_threshold())
        slow_watchdog.start()

    display = None

    if use_rich:
        display = _RichDisplay()
        display_ref = display

        # Add trackers for parallel modules
        for record in parallel_modules:
            display.add_module(record.name, _count_record_tests(record))

        # Add trackers for serial modules
        for record in serial_modules:
            display.add_module(record.name, _count_record_tests(record))

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
                    for record in parallel_modules:
                        tracker = display.trackers[record.name]
                        future = executor.submit(
                            _run_module_in_thread,
                            opts, record, tracker,
                        )
                        futures[future] = record.name

                    for future in as_completed(futures):
                        try:
                            future.result()
                        except Exception:
                            pass

            # Run serial modules sequentially
            for record in serial_modules:
                if _abort_event.is_set():
                    break
                tracker = display.trackers[record.name]

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
                    run_tests_for_record(opts, record)
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
        for record in file_specs:
            try:
                run_module_tests_by_name(opts, record.name, record.test_file,
                                         expected_root=record.path)
            except helpers.TestitAbort:
                pass

        # Run parallel modules in threads (no display callback — output interleaves)
        if parallel_modules:
            with ThreadPoolExecutor(max_workers=opts.jobs) as executor:
                futures = {}
                for record in parallel_modules:
                    tracker = _ModuleTracker(record.name, 0)
                    future = executor.submit(
                        _run_module_in_thread,
                        opts, record, tracker,
                    )
                    futures[future] = record.name
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception:
                        pass

        # Run serial modules sequentially
        for record in serial_modules:
            if _resume.active and not _resume.reached:
                if record.name != _resume.module:
                    continue
            try:
                run_tests_for_record(opts, record)
            except helpers.TestitAbort:
                break

        duration = time.time() - helpers.TEST_RUN.started_at
        _print_summary_plain(duration, skipped_modules)

    else:
        # Plain text sequential mode
        helpers._set_display_fn(None)
        display_ref = None

        # Run file specs first
        for record in file_specs:
            try:
                run_module_tests_by_name(opts, record.name, record.test_file,
                                         expected_root=record.path)
            except helpers.TestitAbort:
                break

        # Run all modules sequentially
        for record in parallel_modules + serial_modules:
            if _resume.active and not _resume.reached:
                if record.name != _resume.module:
                    continue
            try:
                run_tests_for_record(opts, record)
            except helpers.TestitAbort:
                break

        duration = time.time() - helpers.TEST_RUN.started_at
        _print_summary_plain(duration, skipped_modules)

    # Handle file specs in rich mode too
    if use_rich and file_specs:
        helpers._set_display_fn(None)
        for record in file_specs:
            try:
                run_module_tests_by_name(opts, record.name, record.test_file,
                                         expected_root=record.path)
            except helpers.TestitAbort:
                break

    if slow_watchdog is not None:
        slow_watchdog.stop()

    # Clear checkpoint on clean completion
    if helpers.TEST_RUN.failed == 0:
        clear_checkpoint()

    # Did a settings override strand itself in var/django.conf?
    drifted = _conf_drift(conf_before, _snapshot_conf())
    _report_conf_drift(drifted)

    # Structured report — built for every run so the tier budget (maestro
    # #2790) and the maestro reporter both see the same object. Written to disk
    # only in --agent mode.
    report = _build_agent_report(opts, display=display, conf_drift=drifted)
    report["budget_violations"] = _compute_budget_violations(
        opts, report, parent_test_root)
    with _SLOW_STACKS_LOCK:
        report["slow_stacks"] = list(_SLOW_STACKS)
    if report["slow_stacks"]:
        print(f"\n  {len(report['slow_stacks'])} slow test(s) captured a stack "
              "dump — see the agent report's slow_stacks / testit.log")
    _print_budget_violations(opts, report["budget_violations"])
    budget_failed = _budget_fails_run(opts, report["budget_violations"])

    if opts.agent:
        report_path = os.path.join(paths.VAR_ROOT, "test_failures.json")
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
        print(f"\n  Agent report: {report_path}")
        if helpers.TEST_RUN.failed > 0:
            print(f"  {helpers.TEST_RUN.failed} failure(s) — read the report for diagnostics")

    # Save results
    helpers.TEST_RUN.finished_at = time.time()
    helpers.save_results(os.path.join(paths.VAR_ROOT, "test_results.json"))

    # Persist the AST-fact cache for the next run (maestro #2789).
    if _isolation_cache_mod is not None:
        _isolation_cache_mod.save_scan_cache()

    # Release run lock
    _release_lock()

    # Report to maestro. After the lock release, so a slow network call never
    # blocks a waiting run; before the exit below, so a red run still reports.
    if maestro_settings:
        refused = _push_refused(opts)
        if refused:
            print(f"\n  Maestro: not reporting — {refused}, so this is not the suite's result")
        else:
            testit.maestro.report_run(maestro_settings, lambda: report)

    # Exit with failure status if any test failed, or the core-tier budget was
    # blown (maestro #2790).
    if helpers.TEST_RUN.failed > 0:
        sys.exit("  Tests failed!")
    if budget_failed:
        sys.exit("  Tier budget exceeded — see 'Tier budget' above.")


if __name__ == "__main__":
    opts = setup_parser()
    main(opts)
