TESTIT = {
    # The live reader-wiring smoke test uses th.server_settings(DATABASE_READER_
    # HOST=...), a boot-time key that genuinely needs a server reload — legal
    # only in a serial/opt-in package. Moved out of the parallel test_db_reader
    # package (maestro #2791). The in-process router/middleware/config tests that
    # own the routing semantics stay in test_db_reader (default tier).
    "requires_apps": ["mojo.apps.account"],
    "requires_extra": ["extended"],
    "serial": True,
}
