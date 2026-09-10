"""Settings-free enrollment and exact read-only broker status contracts."""
import os
import tempfile
import json
import subprocess
import sys
from testit import helpers as th


@th.django_unit_test()
def test_status_request_has_no_context_or_extra_fields(opts):
    from mojo.deploy import firewall_broker as broker
    th.assert_eq(broker.parse_request(b'{"operation":"broker.status"}'),
                 {"operation": "broker.status"}, "status must not need JobEngine context")
    for value in (b'{"operation":"broker.status","context":{}}',
                  b'{"operation":"broker.status","argv":[]}',
                  b'{"operation":"broker.status","operation":"broker.status"}'):
        with th.assert_raises(broker.BrokerError):
            broker.parse_request(value)


@th.django_unit_test()
def test_enroll_converge_off_preserves_kernel_and_identity(opts):
    from mojo.deploy import firewall_deploy as deploy
    with tempfile.TemporaryDirectory() as root:
        for directory in ("etc/sudoers.d", "usr/local/sbin"):
            os.makedirs(os.path.join(root, directory))
        calls = []
        lifecycle = deploy.Lifecycle(root=root, owner_uid=os.getuid(),
                                     validator=lambda path: calls.append(path))
        th.assert_eq(lifecycle.check()["status"], "unenrolled", "fresh host must start unenrolled")
        lifecycle.enroll("mojo_blocked")
        th.assert_eq(lifecycle.check()["status"], "ready", "clean host enrollment must install authority")
        os.unlink(root + deploy.BROKER_PATH)
        lifecycle.converge()
        th.assert_eq(lifecycle.check()["status"], "ready", "legacy MojoSec cleanup must be repairable")
        lifecycle.off()
        th.assert_true(not os.path.exists(root + deploy.SUDOERS_PATH), "off must revoke sudo authority")
        th.assert_true(os.path.exists(root + deploy.BROKER_CONFIG_PATH), "off must retain permanent-set identity")
        th.assert_true(calls, "sudoers must be validated before activation")


@th.django_unit_test()
def test_failed_enrollment_rolls_back_every_managed_file(opts):
    from mojo.deploy import firewall_deploy as deploy
    def invalid(path):
        raise ValueError("invalid sudoers")
    with tempfile.TemporaryDirectory() as root:
        for directory in ("etc/sudoers.d", "usr/local/sbin"):
            os.makedirs(os.path.join(root, directory))
        lifecycle = deploy.Lifecycle(root=root, owner_uid=os.getuid(), validator=invalid)
        with th.assert_raises(ValueError):
            lifecycle.enroll("mojo_blocked")
        for path in (deploy.BROKER_PATH, deploy.SUDOERS_PATH, deploy.CONFIG_PATH, deploy.BROKER_CONFIG_PATH):
            th.assert_true(not os.path.exists(root + path), "failed enrollment must restore absent files")


@th.django_unit_test()
def test_status_main_never_uses_mutation_lock_or_dispatch(opts):
    script = '''
import io, json, sys
from mojo.deploy import firewall_broker as b
b._verify_caller = lambda: None
b._install_address_space_limit = lambda: None
b.broker_status = lambda: {"ok": True, "schema": "mojo.firewall.broker", "version": 1, "permanent_set_name": "mojo_blocked"}
receipts = []
b._start_ticks = lambda pid: 42
b.syslog.openlog = lambda **kwargs: None
b.syslog.syslog = lambda priority, message: receipts.append(json.loads(message))
def forbidden(*args):
    raise RuntimeError("status entered mutation machinery")
b._acquire_host_lock = forbidden
b.execute = forbidden
sys.stdin = io.TextIOWrapper(io.BytesIO(b'{"operation":"broker.status"}'))
code = b.main([])
print(json.dumps(receipts), file=sys.stderr)
sys.exit(code)
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=10)
    th.assert_eq(result.returncode, 0, "status must complete without mutation dispatch or host lock")
    th.assert_true(json.loads(result.stdout)["ok"], "status must return a bounded successful proof")
    receipts = json.loads(result.stderr)
    th.assert_eq([item["kind"] for item in receipts], ["begin", "result"],
                 "status must publish one non-mutating proof pair")
    th.assert_true(all(item["operation"] == "broker.status" and not item["children"]
                       for item in receipts),
                   "readiness proof must not claim a firewall child mutation")
    from mojo.mojosec.lineage import firewall_receipt
    parsed = [firewall_receipt({"SYSLOG_IDENTIFIER": "mojo-firewall-broker",
                                "_UID": "0", "_PID": str(item["broker_pid"]),
                                "_BOOT_ID": "a" * 32, "_AUDIT_SESSION": "9",
                                "_TTY": "", "_EXE": "/usr/bin/python3.12",
                                "MESSAGE": json.dumps(item)})
              for item in receipts]
    th.assert_true(all(parsed), "the sensor must accept both root-authored status receipts")


@th.django_unit_test()
def test_rollback_retains_firewall_reconvergence_after_legacy_cleanup(opts):
    from mojo.deploy import mojosec_refresh as refresh
    from mojo.deploy import firewall_deploy as deploy
    with tempfile.TemporaryDirectory() as state:
        previous = os.path.join(state, "previous_post.sh")
        refresh.durable_write(previous, b'#!/bin/bash\nexit 0\n', owner_uid=os.getuid())
        refresh.retain(state, owner_uid=os.getuid())
        with open(previous) as handle:
            wrapper = handle.read()
        with open(os.path.join(state, "firewall_deploy.py")) as handle:
            retained = handle.read()
        th.assert_true(wrapper.index('bash "$state/previous_post.original.sh"') <
                       wrapper.index('"$state/firewall_deploy.py" converge'),
                       "N-1 reconvergence must run after old activation cleanup")
        th.assert_in("class Lifecycle:", retained, "rollback must retain a settings-free lifecycle implementation")
        th.assert_true("from mojo" not in retained, "retained lifecycle must survive package downgrade")


@th.django_unit_test()
def test_lifecycle_rejects_symlink_and_identity_replacement(opts):
    from mojo.deploy import firewall_deploy as deploy
    with tempfile.TemporaryDirectory() as root:
        for directory in ("etc/sudoers.d", "usr/local/sbin"):
            os.makedirs(os.path.join(root, directory))
        lifecycle = deploy.Lifecycle(root=root, owner_uid=os.getuid(), validator=lambda path: None)
        lifecycle.enroll("mojo_blocked")
        with th.assert_raises(ValueError):
            lifecycle.enroll("another_name")
        th.assert_eq(lifecycle.identity(), "mojo_blocked", "failed enrollment must preserve sole permanent identity")
        os.unlink(root + deploy.CONFIG_PATH)
        os.symlink(root + deploy.BROKER_CONFIG_PATH, root + deploy.CONFIG_PATH)
        with th.assert_raises(OSError):
            lifecycle.off()
