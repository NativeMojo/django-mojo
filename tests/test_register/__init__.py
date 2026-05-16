TESTIT = {
    "requires_apps": ["mojo.apps.account"],
    # Serial because tests use th.server_settings() to wire handler dotted-paths
    # per scenario; reloads must not race with parallel modules.
    "serial": True,
}
