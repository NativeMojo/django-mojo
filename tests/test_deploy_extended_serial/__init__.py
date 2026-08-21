TESTIT = {
    # These tests mutate os.environ (MOJO_PROJECT_ROOT, HOME) around
    # in-process calls — unsafe under the parallel default tier
    # (maestro item #1839).
    "requires_extra": ["extended"],
    "serial": True,
}
