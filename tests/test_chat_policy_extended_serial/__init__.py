TESTIT = {
    "requires_apps": ["mojo.apps.chat", "mojo.apps.realtime"],
    "requires_extra": ["extended"],
    # th.server_settings() reloads the shared live server.
    "serial": True,
}
