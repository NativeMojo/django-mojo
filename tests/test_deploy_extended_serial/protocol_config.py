"""Moved from tests/test_mojosec/protocol_config.py (maestro #2558).

These CLI-check tests mock `mojo.mojosec.__main__` attributes
(`load_effective_config`, `load_config`, `probe_rpm_capability`) —
process-global patches of production module attributes, unsafe under the
parallel default tier. The protocol, config-loading, and descriptor-safety
contracts stay in the default-tier tests/test_mojosec/protocol_config.py.
"""

import io
from unittest import mock

from testit import helpers as th


@th.django_unit_test()
def test_cli_uses_effective_loader_only_for_exact_canonical_path(opts):
    import mojo.mojosec.__main__ as cli

    config = {
        "sensor_id": "prod-web-i-0123456789abcdef0",
        "version": 1,
    }
    output = io.StringIO()
    with mock.patch.object(cli, "load_effective_config", return_value=config) as effective, \
            mock.patch.object(cli, "load_config", return_value=config) as desired:
        th.assert_eq(cli.main(["check"], stdout=output), 0,
                     "the omitted service path must use the canonical effective artifact")
        th.assert_eq(cli.main(
            ["--config", cli.CANONICAL_CONFIG_PATH, "check"], stdout=output), 0,
                     "the explicit service path must use the canonical effective artifact")
        alias = "/etc/mojosec/./config.json"
        th.assert_eq(cli.main(["--config", alias, "check"], stdout=output), 0,
                     "an alternate spelling remains valid only as caller policy")

    th.assert_eq(effective.call_count, 2,
                 "only omitted and exact canonical paths may use effective loading")
    desired.assert_called_once_with(alias)


@th.django_unit_test()
def test_cli_check_probes_enabled_rpm_binding_capability(opts):
    import mojo.mojosec.__main__ as cli

    config = {
        "sensor_id": "prod-web-i-0123456789abcdef0", "version": 1,
        "collectors": {"rpm": {
            "enabled": True, "interpreter": "/usr/bin/python3",
            "max_output_bytes": 65536, "timeout_seconds": 5,
        }},
    }
    with mock.patch.object(cli, "load_effective_config", return_value=config), \
            mock.patch.object(cli, "probe_rpm_capability") as probe:
        th.assert_eq(cli.main(["check"], stdout=io.StringIO()), 0,
                     "a healthy installed-file binding probe must pass readiness")
    probe.assert_called_once_with(config["collectors"]["rpm"])

    with mock.patch.object(cli, "load_effective_config", return_value=config), \
            mock.patch.object(
                cli, "probe_rpm_capability",
                side_effect=cli.RpmError("RPMDBI_INSTFILENAMES unavailable")):
        th.assert_eq(cli.main(["check"], stderr=io.StringIO()), 2,
                     "a missing or incompatible system RPM binding must fail check")
