TESTIT = {
    # These tests patch the shared settings singleton
    # (mojo.helpers.settings.settings.get) for in-process calls — unsafe
    # under the parallel default tier (maestro item #1839).
    "requires_extra": ["extended"],
    "serial": True,
}
