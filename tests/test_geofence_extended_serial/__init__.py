TESTIT = {
    # These tests write protected GEOFENCE_*/MOJO_TEST_MODE Setting rows via
    # /api/settings and mutate django.conf.settings in-process — unsafe under
    # the parallel default tier (maestro item #1839).
    "requires_apps": ["mojo.apps.account"],
    "requires_extra": ["extended"],
    "serial": True,
}
