TESTIT = {
    # Mutates process-global state (os.environ, sys.modules, the settings
    # singleton, django.conf.settings) or patches shared testit surfaces —
    # unsafe under the parallel default tier (maestro item #1839).
    "requires_extra": ["extended"],
    "serial": True,
}
