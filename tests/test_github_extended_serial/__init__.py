TESTIT = {
    # Tests that mutate django.conf.settings process-wide — unsafe under the
    # parallel default tier (maestro item #1839).
    "requires_apps": ["mojo.apps.github"],
    "requires_extra": ["extended"],
    "serial": True,
}
