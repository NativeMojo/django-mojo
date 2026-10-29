TESTIT = {
    # th.server_settings rewrites the shared server's settings and reloads it:
    # unsafe under the parallel default tier (maestro item #1839).
    "requires_extra": ["extended"],
    "serial": True,
}
