TESTIT = {
    # Threat-intel tests that mock.patch shared geoip module attributes
    # (threat_intel thresholds/dry-run/spies, geoip config/PROVIDERS/detection
    # for the geolocate_ip end-to-end paths) — process-global, unsafe under
    # the parallel default tier (maestro item #2558).
    # requires_apps copied verbatim from tests/test_geoip/__init__.py.
    "requires_apps": ["mojo.apps.account", "mojo.apps.incident"],
    "requires_extra": ["extended"],
    "serial": True,
}
