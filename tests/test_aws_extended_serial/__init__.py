TESTIT = {
    # AWS setup/receiver tests that mutate django.conf.settings, reset
    # protected installation Setting rows, and patch shared mojo.apps.incident
    # surfaces — unsafe under the parallel default tier (maestro item #1839).
    "requires_extra": ["extended"],
    "serial": True,
}
