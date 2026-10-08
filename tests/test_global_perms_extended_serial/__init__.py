TESTIT = {
    # These tests write the protected MEMBER_PERMS_PROTECTION Setting row and
    # set the same key in the server's settings file (th.server_settings
    # reloads the server) — unsafe under the parallel default tier (maestro
    # item #1839).
    "requires_apps": ["mojo.apps.account"],
    "requires_extra": ["extended"],
    "serial": True,
}
