"""Moved from tests/test_deploy/audit.py (maestro #2558).

These convergence/rollback orchestration tests mock `mojo.deploy.audit`
internals (`_load_state`, `_restore_inventory`, `_run`, `audit.os`, ...) —
process-global patches of production module attributes, unsafe under the
parallel default tier. The pure audit-policy, health, and inventory contracts
remain in the default-tier tests/test_deploy/audit.py.
"""

import json
import os
import tempfile
from unittest import mock

from testit import helpers as th


@th.unit_test("Audit rollback distinguishes immediate generation from permanent prior")
def test_transactional_restore_generations(opts):
    from mojo.deploy import audit

    state = {
        "schema": "mojosec.audit-state", "version": 1,
        "previous": {"name": "managed-generation-n"},
        "prior": {"name": "original-al2023-seed"},
        "rules_dir": "/etc/audit/rules.d", "managed_path": audit.MANAGED_PATH,
    }
    restored = []
    with mock.patch.object(audit, "_load_state", return_value=(state, b"state")), \
            mock.patch.object(audit, "_restore_inventory",
                              side_effect=lambda value, *args: restored.append(value)), \
            mock.patch.object(audit.os, "unlink") as unlink:
        audit.restore_immediate("/state")
        th.assert_eq(restored[-1], state["previous"],
                     "late install failure must restore the immediately replaced generation")
        th.assert_true(not unlink.called,
                       "retry state must survive an immediate transactional rollback")
        audit.restore_prior("/state")
        th.assert_eq(restored[-1], state["prior"],
                     "deliberate off/downgrade must restore the original inventory")
        unlink.assert_called_once_with("/state")

    with mock.patch.object(audit, "_load_state", return_value=(state, b"state")), \
            mock.patch.object(audit, "_restore_inventory",
                              side_effect=audit.AuditError("reload failed")), \
            mock.patch.object(audit.os, "unlink") as unlink:
        with th.assert_raises(audit.AuditError):
            audit.restore_prior("/state")
        th.assert_true(not unlink.called,
                       "permanent rollback state may be consumed only after verified restore")


@th.unit_test("managed Audit upgrade late failure restores state bytes and retries")
def test_managed_upgrade_failure_retry(opts):
    from mojo.deploy import audit

    old_policy = audit.MANAGED_MARKER + "\n-D\n-b 8192\n"
    inventory = {
        "state": "managed",
        "sources": [{"path": "/rules/70-mojosec.rules", "content": old_policy}],
        "generated": None, "active_rules": "old-active",
        "inventory_sha256": "1" * 64,
    }
    original = {"state": "seed", "sources": [], "active_rules": "-a task,never"}
    old_state = {
        "schema": "mojosec.audit-state", "version": 1,
        "generation": audit._sha256(old_policy.encode()), "prior": original,
    }
    healthy = ("enabled 1\nfailure 1\nrate_limit 0\nbacklog_limit 8192\n"
               "backlog 0\nlost 0\n")
    unhealthy = healthy.replace("lost 0", "lost 1")
    writes = []
    with tempfile.TemporaryDirectory() as root, \
            mock.patch.object(audit, "LOCK_PATH", os.path.join(root, "audit.lock")), \
            mock.patch.object(audit.os, "geteuid", return_value=0), \
            mock.patch.object(audit, "inventory_sources", return_value=inventory), \
            mock.patch.object(audit, "_load_state",
                              return_value=(old_state, b"old-state-bytes\n")), \
            mock.patch.object(audit, "_active_rules",
                              side_effect=["old-active", "old-active", "new-active"]), \
            mock.patch.object(audit, "_run",
                              side_effect=["", unhealthy]), \
            mock.patch.object(audit, "_restore_inventory") as restore, \
            mock.patch.object(audit, "_atomic_write",
                              side_effect=lambda path, payload, mode:
                              writes.append((path, payload, mode))):
        with th.assert_raises(audit.AuditError):
            audit.converge(1000, rules_dir="/rules", generated_path="/generated",
                           managed_path="/rules/70-mojosec.rules",
                           state_path="/state")
    restore.assert_called_once_with(inventory, "/rules", "/rules/70-mojosec.rules")
    th.assert_eq(writes[-1][1], b"old-state-bytes\n",
                 "late managed-upgrade failure must restore exact prior state bytes")

    writes = []
    active = "-a always,exit -k mojosec-root-exec\n-k mojosec-app-exec\n-w /usr/bin/sudo -k mojosec-sudo\n"
    installed_inventory = dict(inventory, sources=[{
        "path": "/rules/70-mojosec.rules", "content": audit.render_policy(1000)}])
    with tempfile.TemporaryDirectory() as root, \
            mock.patch.object(audit, "LOCK_PATH", os.path.join(root, "audit.lock")), \
            mock.patch.object(audit.os, "geteuid", return_value=0), \
            mock.patch.object(audit, "inventory_sources",
                              side_effect=[inventory, inventory, installed_inventory]), \
            mock.patch.object(audit, "_load_state",
                              return_value=(old_state, b"old-state-bytes\n")), \
            mock.patch.object(audit, "_active_rules",
                              side_effect=["old-active", "old-active", active]), \
            mock.patch.object(audit, "_run", side_effect=["", healthy]), \
            mock.patch.object(audit, "_atomic_write",
                              side_effect=lambda path, payload, mode:
                              writes.append((path, payload, mode))):
        result = audit.converge(
            1000, rules_dir="/rules", generated_path="/generated",
            managed_path="/rules/70-mojosec.rules", state_path="/state")
    installed = json.loads(next(payload for path, payload, mode in writes
                                if path == "/state").decode())
    th.assert_eq(installed["prior"], original,
                 "retry must retain the original pre-feature rollback inventory")
    th.assert_eq(installed["previous"], inventory,
                 "retry must retain the immediately replaced managed generation")
    th.assert_eq(result["rules_sha256"], audit._sha256(active.encode()),
                 "retry must verify and report the newly active generation")
