TESTIT = {
    # These tests mock.patch shared production objects
    # (mojo.apps.incident.services.maestro_sync) around in-process handler
    # calls — unsafe under the parallel default tier (maestro item #1839).
    "requires_apps": ["mojo.apps.incident", "mojo.apps.jobs"],
    "requires_extra": ["extended"],
    "serial": True,
}
