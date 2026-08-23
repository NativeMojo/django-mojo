TESTIT = {
    "default_core": True,
    "requires_apps": ["mojo.apps.realtime"],
    # No longer serial (maestro #2791). The ten-sockets-at-once cap test already
    # moved to the extended_serial sibling (maestro #2789), and the only reason
    # this package stayed serial afterward — parallel-tier th.server_settings()
    # writers whose reloads stalled open websockets past their 5s timeouts — is
    # gone: #2791 removed every server_settings() from the parallel tier, so no
    # reload can interrupt these sockets during the parallel phase anymore.
}
