TESTIT = {
    "tier": "extended",
    "requires_apps": ["mojo.apps.account"],
    # These tests patch module-level key accessors (token_manager.crypto_keys,
    # assess.crypto_keys) process-wide. Test modules run as threads in ONE
    # process, and test_public_messages mints real bouncer tokens in-process —
    # a parallel run would sign under the patched fake key. As an opt-in serial
    # bucket the patch window is exclusive: serial modules run after all
    # parallel modules complete. Opt-in buckets are exempt from cold_budget.
    "serial": True,
}
