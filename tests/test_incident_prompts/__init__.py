TESTIT = {
    "requires_apps": ["mojo.apps.incident"],
    # Deliberately NOT requires_extra: ["slow"] — unlike tests/test_incident/,
    # these are fast static checks on prompt/doc accuracy and must run in the
    # default suite, where a re-introduced false claim would otherwise go
    # unnoticed until a --full pre-publish run. See maestro item #1122.
}
