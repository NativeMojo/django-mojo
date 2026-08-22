TESTIT = {
    "default_core": True,
    # incident is required for check_internal_threats(); account for
    # GeoLocatedIP. Both live in the generated test project.
    "requires_apps": ["mojo.apps.account", "mojo.apps.incident"],
    # Patch-free since item #2558: every test that patched shared module
    # attributes (geoip.config.*, geoip.PROVIDERS, threat_intel.*) either now
    # injects through the check_internal/check_external seams or moved to
    # tests/test_geoip_extended_serial/. The one global WRITER —
    # test_thresholds_are_db_tunable, which set the shared
    # GEOLOCATION_INTERNAL_ATTACKER_CONFIRMED_THRESHOLD Setting row and pushed
    # it to cache — moved there as well, so nothing left here retunes a
    # parallel module.
    #
    # Serial is retained as an execution choice only (this module seeds and
    # counts incident Events in bulk, and the package is small). It no longer
    # guards a mutation; drop it once a parallel run has confirmed the Event
    # seeding is as address-scoped as it reads.
    "serial": True,
}
