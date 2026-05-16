TESTIT = {
    "requires_apps": ["mojo.apps.account"],
    # Parallel-safe: tests pass per-request test-mode headers
    # (X-Mojo-Test-Geo, X-Mojo-Test-Geofence-System, etc.) instead of using
    # th.server_settings(). No server reloads, no cross-module collisions.
}
