TESTIT = {
    "requires_apps": ["mojo.apps.account"],
    # Parallel-safe: tests pass per-request test-mode headers
    # (X-Mojo-Test-*-Handler, X-Mojo-Test-Allow-User-Registration, etc.)
    # instead of using th.server_settings(). No server reloads.
}
