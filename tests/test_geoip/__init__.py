TESTIT = {
    "default_core": True,
    # incident is required for check_internal_threats(); account for
    # GeoLocatedIP. Both live in the generated test project.
    "requires_apps": ["mojo.apps.account", "mojo.apps.incident"],
    # Patch-free since item #2558: every test that patched shared module
    # attributes (geoip.config.*, geoip.PROVIDERS, threat_intel.*) either now
    # injects through the check_internal/check_external seams or moved to
    # tests/test_geoip_extended_serial/. Serial is kept for an execution
    # reason: test_thresholds_are_db_tunable writes the global
    # GEOLOCATION_INTERNAL_ATTACKER_CONFIRMED_THRESHOLD Setting row (and
    # pushes it to cache), which would retune any parallel module that calls
    # check_internal_threats() while the window is open.
    "serial": True,
}
