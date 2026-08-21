TESTIT = {
    "default_core": True,
    "requires_apps": ["mojo.apps.account"],
    # These tests patch module-level key accessors (token_manager.crypto_keys,
    # assess.crypto_keys). Test modules run as threads in ONE process, and
    # test_public_messages mints real bouncer tokens in-process — a parallel
    # run would sign under the patched fake key. Serial modules run after all
    # parallel modules complete, so the patch window is exclusive.
    "serial": True,
}
