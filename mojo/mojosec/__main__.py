"""Command-line entry point for the MojoSec host sensor."""

import argparse
import json
import os
import sys

from .config import ConfigError, check_file_security, load_config
from .output import read_status


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m mojo.mojosec",
        description="MojoSec host security sensor",
    )
    parser.add_argument("--config", default="/opt/api/var/mojosec.json",
                        help="strict JSON config (default: %(default)s)")
    parser.add_argument("command", nargs="?", default="run",
                        choices=("run", "once", "check", "status"))
    return parser


def _print_json(value):
    sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        problems = check_file_security(args.config, require_root=os.geteuid() == 0)
        if problems:
            raise ConfigError("config security check failed: " + "; ".join(problems))
        if args.command == "check":
            _print_json({"ok": True, "sensor_id": config["sensor_id"],
                         "version": config["version"]})
            return 0
        if args.command == "status":
            _print_json(read_status(config["status_path"]))
            return 0

        from .runtime import Runtime
        runtime = Runtime(config)
        if args.command == "once":
            runtime.run_once()
        else:
            runtime.run()
        return 0
    except (ConfigError, OSError, ValueError) as err:
        sys.stderr.write(f"mojosec: {err}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
