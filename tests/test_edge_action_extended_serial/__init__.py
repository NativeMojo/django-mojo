TESTIT = {
    # These tests patch.dict os.environ (clear=True) around in-process calls
    # — unsafe under the parallel default tier (maestro item #1839).
    "requires_extra": ["extended"],
    "serial": True,
}
