TESTIT = {
    # Whole package is correct-but-not-critical feature coverage (#2792).
    "tier": "extended",
    "requires_apps": ["mojo.apps.account"],
    # Uses X-Mojo-Test-Bouncer-Require-Token header instead of
    # th.server_settings() — no server reloads.
}
