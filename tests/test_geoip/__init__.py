TESTIT = {
    # incident is required for check_internal_threats(); account for
    # GeoLocatedIP. Both live in the generated test project.
    "requires_apps": ["mojo.apps.account", "mojo.apps.incident"],
    # Serial: these tests patch module attributes (geoip.config.PRIMARY_PROVIDER,
    # geoip.PROVIDERS, threat_intel.*) that are process-global. The runner
    # executes modules as threads in one process, so a parallel module calling
    # geolocate_ip() would see our stubs.
    "serial": True,
}
