TESTIT = {
    # Exhaustive Admin settings/platform/system-setup matrices: they patch
    # production module attributes and write protected Setting rows, which is
    # unsafe under the parallel default tier (maestro item #1839).
    "requires_apps": ["mojo.apps.account"],
    "requires_extra": ["extended"],
    "serial": True,
}
