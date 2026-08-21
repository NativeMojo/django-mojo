TESTIT = {
    "default_core": True,
    # Edge integration tests intentionally replace process-wide incident
    # reporter hooks and mutate global hosting settings such as
    # EDGE_RELEASE_BUCKETS. Running the module beside other test threads lets
    # those temporary values leak across module boundaries.
    "serial": True,
}
