TESTIT = {
    # The whole flow over the wire needs BASE_URL and the resource enable
    # switch set on the running server, which means server_settings() and the
    # global Setting rows behind it — process-wide state that is unsafe under
    # the parallel default tier (maestro item #1839).
    "requires_apps": ["mojo.apps.account", "mojo.apps.assistant"],
    "requires_extra": ["extended"],
    "serial": True,
}
