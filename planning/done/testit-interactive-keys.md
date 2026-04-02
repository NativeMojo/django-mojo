# Testit Runner: Interactive Keyboard Controls + Module Filter Fix

**Type**: request
**Status**: done
**Date**: 2026-04-02
**Priority**: medium

## Description

Add interactive keyboard controls during the Rich UI test run, and fix `__pycache__` appearing as a test module in the progress display.

## Context

The Rich UI display currently provides real-time visual feedback but accepts no keyboard input. When a test hangs or you want to bail out mid-run, the only option is Ctrl+C which kills the process without a summary. Interactive keys would make the runner much more usable during development.

Separately, `__pycache__` directories are picked up by `_collect_modules` because it only checks `os.path.isdir()` without filtering non-test directories.

## Acceptance Criteria

### Interactive keys (Rich UI mode only)
- `q` — Graceful quit: set an abort flag, let the current test finish, skip remaining, print summary + agent report as normal
- `f` — Toggle fail-fast: enable stop-on-first-failure mid-run (one-way toggle, cannot un-set)
- `r` — Show running: display which specific test function is currently executing per module (helps diagnose hangs)
- `v` — Toggle verbose: start printing test names to a log area below the table
- Keys are shown in a hint line below the progress table (e.g., `[q]uit  [f]ail-fast  [r]unning  [v]erbose`)
- Only active in Rich UI mode — plain/verbose/serial modes ignore this entirely

### __pycache__ fix
- `_collect_modules` filters out `__pycache__` (and any other dunder directories) when scanning test directories

## Investigation

**What exists**:
- `_RichDisplay` class (runner.py:728) — manages the Live panel with per-module progress
- `TestitAbort` exception (helpers.py:47) — already used by `--stop` to halt execution early
- `helpers.STOP_ON_FAIL` flag — checked after each test, can be set mid-run for `f` key
- `_run_module_in_thread` (runner.py:974) — each thread checks `TestitAbort`, natural place to check abort flag
- `_ModuleTracker` (runner.py:691) — tracks per-module state, could be extended to track current test name
- `_collect_modules` (runner.py:1010) — scans `os.listdir()` for test directories, no `__pycache__` filter
- `-q` flag is already taken (`--quick`), so the quit key is keyboard-only, not a CLI flag

**What changes**:
- `testit/runner.py` — all changes are here:
  - Add `_KeyboardListener` class: background thread using `tty`/`termios` for non-blocking stdin reads (macOS/Linux only)
  - Add shared abort flag (e.g., `threading.Event`) checked between tests in `_run_module_in_thread` and serial loops
  - Extend `_RichDisplay._build_table()` to show hint line and optionally show running test names
  - Extend `_ModuleTracker` to track `current_test` name (set before each test, cleared after)
  - Filter `__pycache__` and dunder dirs in `_collect_modules`

**Constraints**:
- `termios`/`tty` are Unix-only — fine for this project (macOS target), but the listener should be wrapped in a try/except so it degrades gracefully
- Thread safety: the abort event and STOP_ON_FAIL flag are already thread-safe patterns (Event, simple bool)
- Rich `Live` context manager owns the terminal — keyboard listener must not conflict with it
- Must not break `--agent`, `--plain`, or `--verbose` modes

**Related files**:
- `testit/runner.py` — all implementation
- `testit/helpers.py` — `TestitAbort`, `STOP_ON_FAIL`, display callback system

## Tests Required

- `__pycache__` filtering: create a `__pycache__` dir in test root, verify `_collect_modules` excludes it
- Interactive keys are hard to unit test (terminal input) — manual verification is acceptable
- Existing test suite must still pass with no behavioral changes when no keys are pressed

## Out of Scope

- Windows support (`msvcrt`) — not needed for this project
- Interactive keys in plain/verbose/serial modes
- Pause/resume (too complex for the threading model)
- CLI flag for quit (`-q` is taken, and the use case is interactive-only anyway)

## Plan

**Status**: done
**Planned**: 2026-04-02

### Objective
Add interactive keyboard controls (`q`/`f`/`r`/`v`) to the Rich UI test runner and filter dunder directories from module discovery.

### Steps
1. `testit/runner.py` — Filter `__pycache__` in `_collect_modules` list comprehensions
2. `testit/runner.py` — Add module-level `_abort_event = threading.Event()`; check it in `run_module_tests` and `_run_module_in_thread` before each test
3. `testit/runner.py` — Extend `_ModuleTracker` with `current_test` field
4. `testit/runner.py` — Add `_KeyboardListener` daemon thread class (tty/termios, degrades gracefully)
5. `testit/runner.py` — Extend `_RichDisplay._build_table()` with hint line and optional running-test column
6. `testit/runner.py` — Wire up listener start/stop in `main()` rich block

### Design Decisions
- `threading.Event` for abort: explicitly thread-safe, no lock needed
- Check abort between tests, not mid-test: avoids dirty server state
- `r` toggles a persistent display mode, not a one-shot
- Hint line lives inside the Rich table to avoid stdout flicker
- `v` toggles showing test names in table, not switching to plain print (Live owns stdout)

### Edge Cases
- Not a TTY: check `sys.stdin.isatty()` before starting listener
- No `termios`: try/except at import, listener becomes no-op
- Quit during parallel: all threads check `_abort_event`, drain naturally
- Terminal restore: finally block around `tty.setraw()` ensures termios attrs restored

### Testing
- `__pycache__` filter: run full suite, confirm gone from display
- Keyboard controls: manual verification
- Regression: existing suite passes unchanged

### Docs
- None needed — internal tooling
