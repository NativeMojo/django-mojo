"""Regression for command-free system-Python integrity collection."""

import inspect
import sys

from testit import helpers as th


@th.tier("bug")
@th.django_unit_test()
def test_legacy_rpm_tier_is_command_free_system_python_fim(opts):
    from mojo.mojosec.collectors import rpm as system_python

    source = inspect.getsource(system_python)
    for forbidden in ("subprocess", "/usr/bin/rpm", "-Va", "-qa", "import rpm"):
        th.assert_true(
            forbidden not in source,
            f"the compatibility-named rpm tier must not retain command boundary {forbidden!r}",
        )

    root = "/usr/lib/python3.12/site-packages"
    regular = root + "/module.py"
    link = root + "/module-link.py"
    config = {
        "interpreter": sys.executable, "interval_seconds": 21600,
        "max_entries": 100, "max_packages": 10, "max_owner_queries": 100,
        "max_output_bytes": 65536, "timeout_seconds": 5,
        "max_file_bytes": 1024, "max_depth": 16,
    }
    roots_calls = []

    def roots_provider():
        roots_calls.append(True)
        return [root, "/tmp/attacker/site-packages"]

    walk_configs = []

    class Walker:
        def __init__(self, config, expected_changes_path, identity, tier,
                     hash_filter=None):
            walk_configs.append((config, hash_filter))

        def scan(self, previous):
            return {
                "complete": True,
                "snapshot": {
                    regular: {"kind": "file", "sha256": "c" * 64},
                    link: {"kind": "symlink", "target_sha256": "d" * 64},
                },
            }

    th.assert_true(
        system_python.probe_system_python_capability(
            config, roots_provider=roots_provider),
        "readiness must prove approved in-process roots without scanning or commands",
    )
    th.assert_eq(walk_configs, [],
                 "readiness must not run the descriptor walk")
    collector = system_python.SystemPythonCollector(
        config, {"name": "al2023-web-v2", "version": 2, "digest": "b" * 64},
        roots_provider=roots_provider, fim_factory=Walker,
    )
    scan = collector.scan(previous={})
    th.assert_true(scan["complete"],
                   "the descriptor-safe system-Python walk must complete")
    th.assert_eq(len(roots_calls), 2,
                 "readiness and the scan must each discover roots in process")
    th.assert_eq(walk_configs[0][1], None,
                 "the system-Python walk must not suppress any file hash")
    th.assert_eq(walk_configs[0][0]["max_file_bytes"], sys.maxsize,
                 "the compatibility file-size bound must not skip system-Python files")
    th.assert_eq(scan["snapshot"][regular]["sha256"], "c" * 64,
                 "every regular system-Python file must retain its hash")
    th.assert_eq(scan["snapshot"][link]["target_sha256"], "d" * 64,
                 "every system-Python symlink must retain its target hash")
    th.assert_eq(scan["tier"], "rpm",
                 "the persisted legacy tier identity must remain compatible")
