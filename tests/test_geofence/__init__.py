TESTIT = {
    "requires_apps": ["mojo.apps.account"],
    # Serial because tests toggle GEOFENCE_* settings via th.server_settings()
    # for per-scenario configuration; reloads must not race with parallel modules.
    "serial": True,
}
