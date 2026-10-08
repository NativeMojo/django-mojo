TESTIT = {
    # Tests that delete the shared Bouncer signature cache key and switch
    # BOUNCER_LEARN_ENABLED off in django.conf: unsafe under the parallel
    # default tier (maestro items #1839, #7392).
    "requires_apps": ["mojo.apps.account"],
    "requires_extra": ["extended"],
    "serial": True,
}
