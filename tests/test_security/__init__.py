TESTIT = {
    "requires_apps": ["mojo.apps.account"],
    "serial": True,  # bouncer.py uses server_settings()
    "requires_extra": ["slow"],  # opt-in: run with --extra slow
}
