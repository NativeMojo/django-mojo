TESTIT = {
    # MojoSecrets v2 (#1060). Violation-free: keys reach the code through the
    # per-instance _secrets_key_list seam and rewrap(keys=...), never settings.
    # Serial for an execution reason: rewrap_secrets locks and swaps rows.
    "tier": "framework",
    "serial": True,
    "requires_apps": ["mojo.apps.account"],
}
