TESTIT = {
    "default_core": True,
    "requires_apps": ["mojo.apps.realtime"],
    # The ten-sockets-at-once cap test moved to the extended_serial sibling
    # (maestro #2789); the files left here open one socket at a time. Serial
    # is retained only because ~28 th.server_settings() writers still live in
    # parallel default-tier packages — each reload stalls open websockets past
    # their 5s timeouts. Drop this flag when those writers are gone
    # (maestro #2791 removes them and owns the flip).
    "serial": True,
}
