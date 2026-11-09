"""Fileman role and generic-key REST routes: platform credentials stay with the
platform (see 14_test_manager_role_settings in test_fileman for the settings
side).

Split out of test_fileman (maestro #7538): files inside one package run one
after another on one thread, so this core-tagged file kept test_fileman the
critical path of the core run. As its own package it runs in parallel with the
rest of fileman. Same tests, same tiers; no AWS is reached.
"""
TESTIT = {
    "tier": "framework",
}
