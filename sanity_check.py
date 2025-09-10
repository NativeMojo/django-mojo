#!/usr/bin/env python3
import json
import shutil
import subprocess
import sys
from collections import defaultdict
from typing import List, Dict, Any

RULES = {
    "reportUnusedImport",
    "reportMissingImports",
    "reportMissingModuleSource",
}

def pick_pyright_command() -> List[str]:
    # Prefer a globally installed `pyright`; fallback to `npx pyright`
    if shutil.which("pyright"):
        return ["pyright"]
    if shutil.which("npx"):
        return ["npx", "pyright"]
    raise SystemExit("Pyright not found. Install with `npm i -g pyright` or use `npx pyright`.")

def run_pyright(paths: List[str]) -> Dict[str, Any]:
    cmd = pick_pyright_command() + ["--outputjson"] + (paths or ["."]
                                                        )
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # Pyright returns non-zero when it finds issues; still parse stdout as JSON.
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        sys.stderr.write("Failed to parse Pyright JSON output.\n\nSTDOUT:\n")
        sys.stderr.write(proc.stdout + "\n\nSTDERR:\n")
        sys.stderr.write(proc.stderr + "\n")
        raise

def filter_diagnostics(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    diags = data.get("generalDiagnostics", [])
    out = []
    for d in diags:
        rule = d.get("rule")
        if rule in RULES:
            out.append(d)
    return out

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
    # Usage:
    #   python pyright_imports_report.py                # current directory
    #   python pyright_imports_report.py src pkg tests  # specific paths
    paths = sys.argv[1:]
    data = run_pyright(paths)
    diags = filter_diagnostics(data)
    print_report(diags)
    # Exit with non-zero if any issues found (useful for CI)
    sys.exit(1 if diags else 0)

if __name__ == "__main__":
    main()
