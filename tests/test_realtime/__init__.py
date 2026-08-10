TESTIT = {
    "requires_apps": ["mojo.apps.realtime"],
    # Connection-limit coverage deliberately keeps ten sockets open together.
    # A concurrent server_settings writer degrades after 60s and restarts the
    # worker, invalidating the server-side count this module is measuring.
    "serial": True,
}
