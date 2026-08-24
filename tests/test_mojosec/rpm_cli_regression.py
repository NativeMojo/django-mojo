"""Regression for RPM CLI ownership without Python RPM bindings."""

import json

from testit import helpers as th


def _inventory(root, path):
    return (
        "<mojosec-rpm-package>"
        '<rpmTag name="Nevra"><string>rpm-4.16.1-1.amzn2023.x86_64</string></rpmTag>'
        '<rpmTag name="Sha256header"><string>' + "a" * 64 + "</string></rpmTag>"
        '<rpmTag name="Installtid"><integer>42</integer></rpmTag>'
        '<rpmTag name="Installtime"><integer>1720000000</integer></rpmTag>'
        "<mojosec-rpm-files>"
        '<mojosec-rpm-file><rpmTag name="Filenames"><string>/usr/bin/rpm</string>'
        '</rpmTag><rpmTag name="Filestates"><integer>0</integer></rpmTag>'
        '</mojosec-rpm-file>'
        '<mojosec-rpm-file><rpmTag name="Filenames"><string>' + path + "</string>"
        '</rpmTag><rpmTag name="Filestates"><integer>0</integer></rpmTag>'
        '</mojosec-rpm-file>'
        "</mojosec-rpm-files>"
        "</mojosec-rpm-package>\n"
    )


@th.tier("bug")
@th.django_unit_test()
def test_rpm_cli_probe_and_scan_do_not_import_python_rpm(opts):
    from mojo.mojosec.collectors.rpm import RpmCollector, probe_rpm_capability

    root = "/usr/local/lib/python3.12/site-packages"
    path = root + "/example.py"
    config = {
        "interpreter": "/usr/bin/python-without-rpm", "interval_seconds": 21600,
        "max_entries": 100, "max_packages": 10, "max_owner_queries": 100,
        "max_output_bytes": 65536, "timeout_seconds": 5,
        "max_file_bytes": 1024, "max_depth": 16,
    }
    calls = []

    def runner(argv, accepted=(0,)):
        calls.append(list(argv))
        if argv[0] == config["interpreter"]:
            th.assert_true("import rpm" not in argv[-1],
                           "site-root discovery must never import the Python rpm module")
            return 0, json.dumps([root]), ""
        if argv[:2] == ["/usr/bin/rpm", "-qa"]:
            return 0, _inventory(root, path), ""
        if argv[:2] == ["/usr/bin/rpm", "--verify"]:
            return 0, "", ""
        raise AssertionError(f"unexpected RPM regression command: {argv!r}")

    th.assert_true(probe_rpm_capability(config, runner=runner),
                   "the RPM CLI inventory must prove readiness without Python bindings")
    collector = RpmCollector(
        config, {"name": "al2023-web-v2", "version": 2, "digest": "b" * 64},
        runner=runner,
    )
    scan = collector.scan(shared_snapshot={
        path: {"kind": "file", "mode": 0o644, "uid": 0, "gid": 0,
               "size": 4, "sha256": "c" * 64},
    })
    th.assert_true(scan["complete"],
                   "a stable injected RPM CLI inventory must complete the scan")
    th.assert_eq(scan["snapshot"][path]["rpm_owner"],
                 "rpm-4.16.1-1.amzn2023.x86_64",
                 "the exact normal-state inventory owner must select RPM verification")
    th.assert_true("sha256" not in scan["snapshot"][path],
                   "an RPM-owned file must not retain duplicate SHA-256 coverage")
    th.assert_eq(sum(argv[:2] == ["/usr/bin/rpm", "-qa"] for argv in calls), 3,
                 "the probe and scan must use one inventory each, with a stable rescan")
