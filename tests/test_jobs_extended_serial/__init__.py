TESTIT = {
    # Patches shared mojo.apps.jobs / jobs-handler module attributes in-process
    # — unsafe under the parallel default tier (maestro item #1839).
    "requires_apps": ["mojo.apps.jobs"],
    "requires_extra": ["extended"],
    "serial": True,
}
