TESTIT = {
    "requires_apps": ["mojo.apps.account"],
    "serial": True,  # flips BOUNCER_ALLOW_ANY_ORIGIN via server_settings()
    # Deliberately NOT requires_extra: these are the regression guards for a
    # CORS bypass flag. They must run in the suite that gates every commit,
    # unlike tests/test_security/ which is opt-in behind --extra slow.
}
