TESTIT = {
    "default_core": True,
    "requires_apps": ["mojo.apps.account"],
    # No longer serial: the th.server_settings()/in-process fan-out cap test
    # moved to tests/test_metrics_extended_serial (maestro item #1839).
}
