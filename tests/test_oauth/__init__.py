TESTIT = {
    "requires_apps": ["mojo.apps.account"],
    # ALLOWED_REDIRECT_URLS + GITHUB_CLIENT_ID are pinned in test project
    # settings; no per-test server_settings reload needed. Parallel-safe.
}
