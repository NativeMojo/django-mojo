# Feature-internal maestro-board coverage: wholesale `extended` (maestro
# #2792). Opt-in serial, so its app-local mocks are exempt from the
# cold_budget ratchet.
TESTIT = {
    "tier": "extended",
    "serial": True,
    "requires_apps": ["mojo.apps.incident", "mojo.apps.jobs"],
}
