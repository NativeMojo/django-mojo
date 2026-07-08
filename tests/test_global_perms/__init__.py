TESTIT = {
    # The escalation sweep hits endpoints across several apps; require the ones
    # whose global-effect endpoints this module asserts on. All are installed in
    # the standard testproject (the baseline suite runs their test modules).
    "requires_apps": [
        "mojo.apps.account",
        "mojo.apps.jobs",
        "mojo.apps.incident",
    ],
}
