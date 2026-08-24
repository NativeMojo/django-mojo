"""MojoSec sensor and shared-wire-contract tests."""

# Edge-deployment coverage: wholesale `edge` (maestro #2792). Opt-in serial, so
# its app-local provider mocks are exempt from the cold_budget ratchet.
TESTIT = {
    "tier": "edge",
    "serial": True,
}
