TESTIT = {
    # handler404 only runs when DEBUG is off, so this test flips DEBUG on the
    # server via th.server_settings() — a reload, legal only in a serial/opt-in
    # package. Moved out of the parallel test_error_pages package (maestro
    # #2791); the in-process negotiation.py rule coverage and the non-reload live
    # tests stay in test_error_pages (default tier).
    "requires_apps": ["mojo.apps.account", "mojo.apps.incident"],
    "requires_extra": ["extended"],
    "serial": True,
}
