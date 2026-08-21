TESTIT = {
    # These tests mutate django.conf.settings (METRICS_FANOUT_MAX_CHILDREN)
    # in-process — unsafe under the parallel default tier (maestro item #1839).
    "requires_apps": ["mojo.apps.account"],
    "requires_extra": ["extended"],
    "serial": True,
}
