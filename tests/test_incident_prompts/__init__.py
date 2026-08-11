TESTIT = {
    "requires_apps": ["mojo.apps.incident"],
    # Deliberately NOT requires_extra: ["slow"] — unlike tests/test_incident/,
    # these are fast incident-rule correctness and prompt/doc accuracy
    # regressions that must run in the default suite, where a re-introduced
    # execution or documentation bug would otherwise go unnoticed until a
    # --all pre-publish run. See maestro items #1122 and #1124.
}
