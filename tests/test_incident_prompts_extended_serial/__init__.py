TESTIT = {
    # These tests mock.patch the shared RuleSet model class
    # (mojo.apps.incident.models.RuleSet.run_handler) around in-process event
    # publishing — unsafe under the parallel default tier (maestro item #1839).
    "requires_apps": ["mojo.apps.incident"],
    "requires_extra": ["extended"],
    "serial": True,
}
