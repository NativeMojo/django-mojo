TESTIT = {
    # Tests that mutate process-wide state — django.conf.settings (maestro
    # item #1839) or a shared model's RestMeta attributes (item #2558) —
    # unsafe under the parallel default tier.
    "requires_extra": ["extended"],
    "serial": True,
}
