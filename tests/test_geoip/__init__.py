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
    # Serial dropped (maestro #2789): the precondition above was checked —
    # every Event query in this package filters on its own fixed TEST-NET-3
    # source_ip (203.0.113.x), no other parallel package uses those addresses,
    # and the whole-suite parallel run confirmed it.
}
