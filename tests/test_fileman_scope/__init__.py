"""Fileman storage scope: a manager on borrowed keys stays inside its group's
storage folder (maestro #7990).

Split out of test_fileman (maestro #7538): files inside one package run one
after another on one thread, so this core-tagged file kept test_fileman the
critical path of the core run. As its own package it runs in parallel with the
rest of fileman. Same tests, same tiers; no AWS is reached.
"""
TESTIT = {
    "tier": "framework",
}
