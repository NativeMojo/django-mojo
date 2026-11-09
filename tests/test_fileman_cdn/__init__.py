"""Fileman CDN links, provisioning and link lifetime (maestro #8250, #8487).

Split out of test_fileman (maestro #7538): files inside one package run one
after another on one thread, so these core-tagged CDN files made test_fileman
the critical path of the core run. As their own package they run in parallel
with the rest of fileman. Same tests, same tiers; no AWS is reached.
"""
TESTIT = {
    "tier": "framework",
}
