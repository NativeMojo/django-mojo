# Feature-internal assistant coverage: the package default is `extended`
# (maestro #2792). Security boundaries are pulled back to `core` per-test /
# per-file (SSRF/model-tool/RCE-guard/approval/MCP-gate/AI-access-flags),
# regressions to `bug`, cloud tools to `admin`/`edge`. Opt-in tier, so the
# app-local provider mocks are exempt from the cold_budget ratchet.
TESTIT = {
    "tier": "extended",
    "requires_apps": ["mojo.apps.assistant"],
}
