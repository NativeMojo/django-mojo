import os
import tempfile
from unittest import mock

from testit import helpers as th


@th.unit_test("audit policy is selective and architecture complete")
def test_render_policy_is_selective(opts):
    from mojo.deploy.audit import render_policy

    text = render_policy(1000)
    for arch in ("b64", "b32"):
        th.assert_in(f"-a always,exit -F arch={arch} -S execve,execveat -F euid=0", text,
                     f"root exec capture must include {arch}")
        th.assert_in(f"-a always,exit -F arch={arch} -S execve,execveat -F auid=1000", text,
                     f"application execution capture must include {arch}")
    th.assert_in("-w /usr/bin/sudo -p x -k mojosec-sudo", text,
                 "sudo executable execution must have an exact path watch")
    th.assert_true("fork" not in text and "clone" not in text,
                   "the policy must not record fork-only process activity")
    th.assert_true("task,never" not in text,
                   "the AL2023 task-never seed must not survive managed policy")


@th.unit_test("audit health sidecar is strict and gap detecting")
def test_health_sidecar_validation(opts):
    from mojo.deploy.audit import validate_health

    health = {
        "schema": "mojosec.audit-health", "version": 1,
        "boot_id": "a" * 32, "generation": "b" * 64,
        "rules_sha256": "c" * 64, "sequence": 7,
        "enabled": 1, "failure": 1, "rate_limit": 0,
        "backlog_limit": 8192, "backlog": 0, "lost": 0,
        "updated_at": 1000.0,
    }
    result = validate_health(health, now=1001.0, previous=None)
    th.assert_true(result["healthy"], "a fresh exact healthy epoch should enable proof")
    gap = dict(health, sequence=9, updated_at=1002.0)
    result = validate_health(gap, now=1002.0, previous=health)
    th.assert_true(not result["healthy"], "a sequence gap must disable suppression")
    lost = dict(health, sequence=8, lost=1, updated_at=1002.0)
    result = validate_health(lost, now=1002.0, previous=health)
    th.assert_true(not result["healthy"], "new audit loss must disable suppression")


@th.unit_test("audit inventory rejects unknown rule sources")
def test_inventory_only_admits_known_seed_or_managed(opts):
    from mojo.deploy.audit import AuditError, inventory_sources

    with tempfile.TemporaryDirectory() as root:
        rules = os.path.join(root, "rules.d")
        os.mkdir(rules)
        seed = os.path.join(rules, "audit.rules")
        with open(seed, "w", encoding="utf-8") as handle:
            handle.write("-D\n-a task,never\n")
        found = inventory_sources(rules, generated_path="", active_rules="-a task,never\n")
        th.assert_eq(found["state"], "seed", "the exact AL2023 seed should be admitted")
        with open(seed, "a", encoding="utf-8") as handle:
            handle.write("-w /tmp -p wa\n")
        with th.assert_raises(AuditError):
            inventory_sources(rules, generated_path="", active_rules="-a task,never\n")
