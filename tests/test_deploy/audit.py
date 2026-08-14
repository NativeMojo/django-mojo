import os
import json
import tempfile
import time
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
        th.assert_in(f"-F euid=0 -F auid!=1000", text,
                     "application-origin root exec must not overlap the root rule")
        th.assert_in("-F exe!=/usr/bin/sudo -k mojosec-app-exec", text,
                     "the exact sudo watch must not overlap the application rule")
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
    result = validate_health(health, now=1001.5, previous=health)
    th.assert_true(result["healthy"],
                   "reading the same still-fresh sidecar twice is not a sequence gap")
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


@th.unit_test("audit inventory adopts an empty rules.d with inert active rules")
def test_inventory_adopts_empty_rules_dir(opts):
    from mojo.deploy.audit import AuditError, inventory_sources

    with tempfile.TemporaryDirectory() as root:
        rules = os.path.join(root, "rules.d")
        os.mkdir(rules)
        found = inventory_sources(rules, generated_path="", active_rules="-a never,task\n")
        th.assert_eq(found["state"], "seed",
                     "an empty rules.d with inert active rules is a never-adopted "
                     "node and must adopt exactly like the pristine seed (item 2002)")
        with th.assert_raises(AuditError):
            inventory_sources(rules, generated_path="", active_rules="-w /etc -p wa\n")


@th.unit_test("audit inventory admits the stock AL2023 seed with its comments")
def test_inventory_admits_commented_stock_seed(opts):
    from mojo.deploy.audit import AuditError, inventory_sources

    # Byte-for-byte shape of the file the distro actually ships (observed in
    # production, item 2002): the seed directives wrapped in comments and
    # blank lines. Classification must read effective directives, not bytes.
    stock = (
        "## This set of rules is to suppress the performance effects of the\n"
        "## audit system. The result is that you only get hardwired events.\n"
        "-D\n"
        "\n"
        "## This suppresses syscall auditing for all tasks started\n"
        "## with this rule in effect.  Remove it if you need syscall\n"
        "## auditing.\n"
        "-a task,never\n"
        "\n"
    )
    with tempfile.TemporaryDirectory() as root:
        rules = os.path.join(root, "rules.d")
        os.mkdir(rules)
        with open(os.path.join(rules, "audit.rules"), "w", encoding="utf-8") as handle:
            handle.write(stock)
        found = inventory_sources(rules, generated_path="", active_rules="-a never,task\n")
        th.assert_eq(found["state"], "seed",
                     "the distro-shipped seed file must classify as seed despite "
                     "its comments and blank lines (item 2002)")
        with open(os.path.join(rules, "extra.rules"), "w", encoding="utf-8") as handle:
            handle.write("# watch passwd\n-w /etc/passwd -p wa\n")
        with th.assert_raises(AuditError):
            inventory_sources(rules, generated_path="", active_rules="-a never,task\n")


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


@th.unit_test("package downgrade flushes every held broker event before old v3 starts")
def test_downgrade_flush_pending(opts):
    from mojo.deploy.audit import flush_pending_firewall
    from mojo.mojosec.events import observation
    from mojo.mojosec.store import Store

    aggregation = {"window_seconds": 60, "flush_count": 10,
                   "max_aggregates": 100, "critical_reserve_aggregates": 10}
    delivery = {"max_spool_events": 100, "critical_reserve_events": 10,
                "retry_min_seconds": 1, "retry_max_seconds": 60}
    with tempfile.TemporaryDirectory() as root:
        store = Store(root, "sensor", aggregation, delivery,
                      local_only_diagnostic_path=os.path.join(root, "missing"))
        candidate = observation(
            "auth.sudo_command", "high", "Privileged sudo command executed",
            attributes={"actor": "ec2-user", "target_user": "root",
                        "command": "/usr/local/sbin/mojo-firewall-broker",
                        "command_path": "/usr/local/sbin/mojo-firewall-broker"},
            fingerprint_values=("downgrade",), aggregate=False,
            observed_at="2026-08-13T20:00:00Z")
        store.db.execute("BEGIN IMMEDIATE")
        store.hold_firewall_observation(candidate, now=time.time())
        store.db.execute("COMMIT")
        store.close()

        th.assert_eq(flush_pending_firewall(os.path.join(root, "state.sqlite3")), 1,
                     "stable helper must convert held proof to ordinary evidence")
        reopened = Store(root, "sensor", aggregation, delivery,
                         local_only_diagnostic_path=os.path.join(root, "missing"))
        th.assert_eq(reopened.stats()["provenance"]["pending_firewall"], 0,
                     "an older v3 process must never inherit an invisible held candidate")
        events = reopened.pending_batch(10, 65536)
        th.assert_eq(len(events), 1,
                     "the exact held command must survive restart as an ordinary Event")
        th.assert_eq(events[0]["attributes"]["command_path"],
                     "/usr/local/sbin/mojo-firewall-broker",
                     "downgrade conversion must preserve admin evidence")
        reopened.close()


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
