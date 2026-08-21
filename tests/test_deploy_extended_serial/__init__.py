TESTIT = {
    # These tests mutate process-global state around in-process calls —
    # os.environ (MOJO_PROJECT_ROOT, HOME; maestro item #1839) and
    # mock.patch of production module attributes in mojo.deploy.* /
    # mojo.mojosec.* (maestro item #2558) — unsafe under the parallel
    # default tier.
    "requires_extra": ["extended"],
    "serial": True,
}
