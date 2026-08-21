TESTIT = {
    # Deploy-plane tests that patch shared mojo.apps.jobs / mojo.apps.incident
    # surfaces and mutate django.conf.settings / os.environ / protected Setting
    # rows — unsafe under the parallel default tier (maestro item #1839).
    "requires_extra": ["extended"],
    "serial": True,
}
