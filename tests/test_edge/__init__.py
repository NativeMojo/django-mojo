TESTIT = {
    # The parallel half of the edge suite (maestro #2792). The tests that
    # reload the shared test server (th.server_settings), mutate global hosting
    # settings, or patch shared surfaces moved to the serial sibling
    # `tests/test_edge_serial`; what remains here is parallel-safe and runs in
    # the framework preset's parallel ring. The files still run sequentially
    # WITHIN this package (no file_parallel): `_helpers.cleanup()` sweeps every
    # edge-/up-/app* row and the files declare conflicting global EDGE_POOLS, so
    # they may not run beside each other.
    "default_core": True,
    "cold_budget": 29,
}
