TESTIT = {
    # Tests that mutate django.conf.settings process-wide — unsafe under the
    # parallel default tier (maestro item #1839).
    "requires_apps": ["mojo.apps.realtime"],
    "requires_extra": ["extended"],
    "serial": True,
}
