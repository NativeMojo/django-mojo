TESTIT = {
    # These tests mock.patch the shared settings singleton
    # (mojo.helpers.settings.settings.get) and other process-wide surfaces
    # around in-process service calls — unsafe under the parallel default
    # tier (maestro item #1839).
    "requires_apps": ["mojo.apps.assistant"],
    "requires_extra": ["extended"],
    "serial": True,
}
