#!/usr/bin/env python3
import argparse
import json
import shutil
import subprocess
import sys
from collections import defaultdict
from typing import List, Dict, Any, Optional

RULES = {
    "reportUnusedImport",
    "reportMissingImports",
    "reportMissingModuleSource",
}

def pick_pyright_command(force_direct: bool = False) -> List[str]:
    """
    Prefer a globally installed `pyright`; otherwise use `npx --yes pyright`.
    Use `force_direct=True` to avoid npx even if pyright isn't found (will error).
    """
    if shutil.which("pyright"):
        return ["pyright"]
    if force_direct:
        raise SystemExit("`pyright` not found in PATH.")
    if shutil.which("npx"):
        # `--yes` prevents the initial interactive prompt that can cause 'hangs'
        return ["npx", "--yes", "pyright"]
    raise SystemExit("Neither `pyright` nor `npx` found. Install pyright (`npm i -g pyright`) or npm.")

def run_pyright(paths: List[str], verbose: bool, timeout: Optional[int], force_direct: bool = False) -> Dict[str, Any]:
    cmd = pick_pyright_command(force_direct=force_direct) + ["--outputjson"]
    if verbose:

    cmd += (paths or ["." ])
    if verbose:
        print(f"[pyright-imports] Running: {' '.join(cmd)}", flush=True)

    try:
        # Show Pyright's progress in real-time when verbose by letting stderr inherit the console.
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,   # always capture JSON
            stderr=None if verbose else subprocess.PIPE,  # stream progress when verbose
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(f"Timed out waiting for Pyright after {timeout} seconds. "
                         f"Try narrowing paths or increasing --timeout.")

    # Pyright returns non-zero when it finds issues; we still parse stdout as JSON.
    stdout = proc.stdout if proc.stdout is not None else ""
    if not stdout.strip():
        if verbose:
            # If verbose, Pyright may have printed progress to stderr only; still need JSON on stdout.
            raise SystemExit("Pyright produced no JSON on stdout. Ensure it’s not being mixed with stderr.")
        else:
            sys.stderr.write("[pyright-imports] No output on stdout; stderr was:\n")
            if proc.stderr:
                sys.stderr.write(proc.stderr + "\n")
            raise SystemExit("Failed to retrieve JSON from Pyright. Run with --verbose to see progress.")

    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        sys.stderr.write("[pyright-imports] Failed to parse Pyright JSON output.\n\nSTDOUT:\n")
        sys.stderr.write(stdout + "\n")
        if not verbose and proc.stderr:
            sys.stderr.write("\nSTDERR:\n" + proc.stderr + "\n")
        raise

def filter_diagnostics(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    diags = data.get("generalDiagnostics", [])
    return [d for d in diags if d.get("rule") in RULES]

def print_report(diags: List[Dict[str, Any]]):
    if not diags:
        print("No unused or missing imports found.")
        return

    grouped = defaultdict(list)
    for d in diags:
        grouped[d.get("file", "<unknown>")].append(d)

    total = 0
    for file, items in sorted(grouped.items()):
        print(f"\n{file}")
        for d in sorted(
            items,
            key=lambda x: (
                x.get("range", {}).get("start", {}).get("line", 0),
                x.get("range", {}).get("start", {}).get("character", 0),
            ),
        ):
            rng = d.get("range", {})
            start = rng.get("start", {})
            line = start.get("line", 0) + 1
            col = start.get("character", 0) + 1
            rule = d.get("rule", "")
            msg = d.get("message", "").strip()
            print(f"  {line}:{col}  {rule}: {msg}")
            total += 1

    print(f"\nFound {total} import issues across {len(grouped)} files.")

def main():
    parser = argparse.ArgumentParser(description="Report unused/missing imports from Pyright.")
    parser.add_argument("paths", nargs="*", help="Paths to analyze (default: current directory)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Stream Pyright stderr (no pyright --verbose)")
    parser.add_argument("--timeout", type=int, default=None, help="Timeout in seconds for Pyright")
    parser.add_argument("--force-direct", action="store_true",
                        help="Require direct `pyright` (do not fallback to npx)")
    args = parser.parse_args()

    # Run pyright
    data = run_pyright(args.paths, verbose=args.verbose, timeout=args.timeout, force_direct=args.force_direct)

    # Filter and report
    diags = filter_diagnostics(data)
    print_report(diags)

    # Exit with non-zero if any issues found (useful for CI)
    sys.exit(1 if diags else 0)

if __name__ == "__main__":
    main()
