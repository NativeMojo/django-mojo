import io
from unittest import mock

from testit import helpers as th


@th.unit_test("broker rejects duplicate unknown and raw command input")
def test_strict_request(opts):
    from mojo.deploy.firewall_broker import BrokerError, parse_request

    for payload in (
            b'{"operation":"rules.read","operation":"rules.read"}',
            b'{"operation":"rules.read","argv":["/bin/sh"]}',
            b'{"operation":"rules.read","stdin":"restore"}'):
        with th.assert_raises(BrokerError):
            parse_request(payload)


@th.unit_test("broker canonicalizes networks and constructs restore itself")
def test_restore_construction(opts):
    from mojo.deploy.firewall_broker import build_operation

    built = build_operation({
        "operation": "set.replace", "set_name": "blocked",
        "cidrs": ["192.0.2.1/24", "2001:db8::1/64"],
    }, function="mojo.apps.incident.asyncjobs.broadcast_sync_ipset")
    th.assert_eq(built["cidrs"], ["192.0.2.0/24", "2001:db8::/64"],
                 "the broker must canonicalize networks before root execution")
    th.assert_in("create blocked_tmp hash:net", built["stdin"],
                 "restore text must be generated inside the root broker")
    th.assert_true("argv_digest" in built and "stdin_digest" in built,
                   "root receipts need exact semantic input digests")


@th.unit_test("broker function-operation matrix is closed")
def test_function_matrix(opts):
    from mojo.deploy.firewall_broker import BrokerError, build_operation

    with th.assert_raises(BrokerError):
        build_operation({"operation": "rule.insert", "chain": "INPUT",
                         "source": "192.0.2.8"}, function="evil.module.call")


@th.unit_test("firewall backend uses exact noninteractive empty-argv broker command")
def test_firewall_invocation(opts):
    from mojo.apps.incident import firewall
    from mojo.apps.jobs.execution_context import execution

    completed = mock.Mock(returncode=0, stdout='{"ok":true,"present":false}\n', stderr="")
    with execution("job-1", "mojo.apps.incident.asyncjobs.broadcast_block_ip", 1,
                   "default", "runner-1"):
        with mock.patch.object(firewall, "_check_user", return_value=True), \
                mock.patch.object(firewall.subprocess, "run", return_value=completed) as run:
            result = firewall.is_blocked("192.0.2.8")
    th.assert_true(not result, "semantic rules read should return broker presence")
    th.assert_eq(run.call_args.args[0], ["/usr/bin/sudo", "-n", "--",
                                        "/usr/local/sbin/mojo-firewall-broker"],
                 "application sudo must execute only the empty-argv broker command")


@th.unit_test("broker production limits and empty-argv sudoers are exact")
def test_broker_limits_and_sudoers(opts):
    from mojo.deploy import firewall_broker as broker

    th.assert_eq(
        (broker.MAX_REQUEST_BYTES, broker.MAX_CIDRS, broker.MAX_RESTORE_BYTES,
         broker.MAX_OUTPUT_BYTES, broker.MAX_RULES_OUTPUT_BYTES,
         broker.SCALAR_TIMEOUT_SECONDS, broker.BULK_TIMEOUT_SECONDS,
         broker.ADDRESS_SPACE_BYTES),
        (16 * 1024 * 1024, 250000, 24 * 1024 * 1024, 64 * 1024,
         8 * 1024 * 1024, 15, 120, 256 * 1024 * 1024),
        "the reviewed production resource envelope must not drift")
    th.assert_eq(
        broker.render_sudoers(),
        'ec2-user ALL=(root) NOPASSWD: /usr/local/sbin/mojo-firewall-broker ""\n',
        "sudoers must authorize exactly the broker with an empty argument vector")


@th.unit_test("semantic firewall read does not use substring matching")
def test_semantic_rules_read(opts):
    from mojo.deploy.firewall_broker import _rules_contain_source

    payload = "-A INPUT -s 192.0.2.80/32 -j DROP\n-A OUTPUT -s 192.0.2.8/32 -j ACCEPT\n"
    th.assert_true(not _rules_contain_source(payload, "192.0.2.8/32"),
                   "a source substring or non-DROP rule must not decide blocked status")
    th.assert_true(_rules_contain_source(
        payload + "-A INPUT -s 192.0.2.8 -j DROP\n", "192.0.2.8/32"),
        "canonical exact source and DROP target should decide blocked status")
