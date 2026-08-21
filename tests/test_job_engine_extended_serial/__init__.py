TESTIT = {
    # Patches shared mojo.apps.jobs internals in-process — unsafe under the
    # parallel default tier (maestro item #1839).
    "requires_apps": ["mojo.apps.jobs"],
    "requires_extra": ["extended"],
    "serial": True,  # JobEngine/Scheduler use signal handlers (main thread only)
}
