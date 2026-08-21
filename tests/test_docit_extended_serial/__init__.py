TESTIT = {
    # Tests that mutate django.conf.settings process-wide — unsafe under the
    # parallel default tier (maestro item #1839). The source package
    # (tests/test_docit) declares no requires_apps, so none is declared here.
    "requires_extra": ["extended"],
    "serial": True,
}
