"""Command-line entry point for the MojoSec host sensor."""

import argparse
import json
import sys

from .config import (
    CANONICAL_CONFIG_PATH, ConfigError, load_config, load_effective_config,
)
from .collectors.rpm import RpmError, probe_rpm_capability
from .output import read_status


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m mojo.mojosec",
        description="MojoSec host security sensor",
    )
    parser.add_argument("--config", default=CANONICAL_CONFIG_PATH,
                        help="strict JSON config (default: %(default)s)")
    parser.add_argument("command", nargs="?", default="run",
                        choices=("run", "once", "check", "status",
                                 "baseline-preview", "baseline-initialize",
                                 "baseline-initialize-tier",
                                 "baseline-rollback"))
    parser.add_argument("tier", nargs="?", default="",
                        help="tier to seed, for baseline-initialize-tier")
    parser.add_argument("--confirm-digest", default="",
                        help="exact profile digest required for baseline mutation "
                             "(baseline-initialize-tier: the tier's graph digest)")
    parser.add_argument("--reason", default="operator",
                        help="bounded baseline initialization reason")
    return parser


def _print_json(value, output):
    output.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def main(argv=None, *, stdout=None, stderr=None):
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    args = build_parser().parse_args(argv)
    # Deferred so `--help` and the import graph stay as light as they were.
    from .store import StoreError
    try:
        if args.config == CANONICAL_CONFIG_PATH:
            config = load_effective_config(args.config)
        else:
            config = load_config(args.config)
        if args.command == "check":
            rpm = config.get("collectors", {}).get("rpm", {})
            if rpm.get("enabled") is True:
                probe_rpm_capability(rpm)
            _print_json({"ok": True, "sensor_id": config["sensor_id"],
                         "version": config["version"]}, stdout)
            return 0
        if args.command == "status":
            _print_json(read_status(config["status_path"]), stdout)
            return 0

        from .runtime import Runtime
        runtime = Runtime(config)
        if args.command in ("baseline-preview", "baseline-initialize"):
            scans = runtime.preview_integrity()
            identity = runtime.profile_identity
            preview = {
                "profile": identity,
                "complete": all(scan["complete"] for scan in scans.values()),
                "tiers": {
                    tier: {
                        "complete": scan["complete"], "entries": len(scan["snapshot"]),
                        "duration": scan.get("duration", 0),
                        "bounds": scan.get("bounds", {}),
                        "packages": scan.get("packages", 0),
                        "rpm_anomalies": scan.get("anomalies", 0),
                    } for tier, scan in scans.items()
                },
            }
            if args.command == "baseline-initialize":
                if not identity or args.confirm_digest != identity["digest"]:
                    raise ValueError("baseline initialization requires the exact previewed digest")
                if not preview["complete"]:
                    raise ValueError("baseline initialization refuses an incomplete tier")
                runtime.initialize_integrity(scans, reason=args.reason)
                preview["initialized"] = True
            _print_json(preview, stdout)
            runtime.store.close()
        elif args.command == "baseline-initialize-tier":
            # Seeding ONE tier, for the re-enrollment ceremony: a node whose
            # content roots changed has a content baseline key with nothing
            # behind it, and its first scan would otherwise alarm on an entire
            # tenant estate. The store refuses to overwrite an already-seeded
            # tier, so this can only ever create a baseline, never launder one.
            if not args.tier:
                raise ValueError("baseline tier initialization requires a tier name")
            scan = runtime.preview_integrity_tier(args.tier)
            collector = runtime.integrity_collectors[args.tier]
            if not args.confirm_digest or args.confirm_digest != collector.graph_digest:
                raise ValueError(
                    "tier initialization requires the exact resolved tier graph digest")
            if not scan["complete"]:
                raise ValueError("tier initialization refuses an incomplete tier")
            result = runtime.initialize_integrity_tier(
                args.tier, scan, reason=args.reason)
            _print_json({
                "initialized": True, "tier": args.tier,
                "profile": runtime.profile_identity,
                "graph_digest": collector.graph_digest,
                "entries": result["entries"],
                "baseline_key": result["baseline_key"],
                "superseded": result["superseded"],
            }, stdout)
            runtime.store.close()
        elif args.command == "baseline-rollback":
            if not args.confirm_digest:
                raise ValueError("baseline rollback requires an exact prior digest")
            identity = runtime.store.rollback_fim_profile(args.confirm_digest)
            _print_json({"rolled_back": True, "profile": identity}, stdout)
            runtime.store.close()
        elif args.command == "once":
            runtime.run_once()
        else:
            runtime.run()
        return 0
    except (ConfigError, OSError, RpmError, StoreError, ValueError) as err:
        # StoreError covers the baseline refusals (already-seeded tier, absent
        # rollback history). They are operator errors with a clear message, not
        # crashes, and the converge-driven ceremony needs a clean exit code.
        stderr.write(f"mojosec: {err}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
