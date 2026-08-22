TESTIT = {
    "default_core": True,
    "cold_budget": 2,
    "requires_apps": ["mojo.apps.account"],
    # No th.server_settings() calls in this module; serial flag was stale.
    # Setup uses unique pk-scoped cleanup, parallel-safe with other modules.
}
