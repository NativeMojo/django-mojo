TESTIT = {
    # Patches shared jobs attributes or exercises process-global runner
    # discovery/control channels — unsafe under the parallel default tier
    # (maestro item #1839).
    "requires_apps": ["mojo.apps.jobs"],
    "requires_extra": ["extended"],
    "serial": True,
}
