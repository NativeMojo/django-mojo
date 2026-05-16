TESTIT = {
    "requires_apps": ["mojo.apps.account"],
    # Now uses X-Mojo-Test-Bouncer-Require-Token header instead of
    # th.server_settings() — parallel-safe, no server reloads.
}
