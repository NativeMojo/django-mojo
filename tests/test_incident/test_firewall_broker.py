import io
import subprocess
import sys
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
        "expected_permanent_set": "mojo_blocked",
        "cidrs": ["192.0.2.1/24", "192.0.2.9/24"],
    }, function="mojo.apps.incident.asyncjobs.broadcast_sync_ipset")
    th.assert_eq(built["cidrs"], ["192.0.2.0/24"],
                 "the broker must canonicalize networks before root execution")
    th.assert_in("create blocked_tmp hash:net", built["stdin"],
                 "restore text must be generated inside the root broker")
    th.assert_true("argv_digest" in built and "stdin_digest" in built,
                   "root receipts need exact semantic input digests")


@th.unit_test("broker refuses IPv6 with a typed compatibility error")
def test_ipv6_refused_before_build(opts):
    from mojo.deploy.firewall_broker import BrokerError, build_operation

    with th.assert_raises(BrokerError) as raised:
        build_operation({
            "operation": "ip.normalize", "source": "2001:db8::1",
            "present": True,
        }, function="mojo.apps.incident.asyncjobs.broadcast_reconcile_firewall_ip")
    th.assert_eq(raised.exception.code, "unsupported_family",
                 "IPv6 refusal must stay machine-readable")


@th.unit_test("compound GeoLocatedIP repair is one closed broker operation")
def test_compound_geolocated_build(opts):
    from mojo.deploy.firewall_broker import build_operation

    built = build_operation({
        "operation": "geolocated.normalize", "source": "192.0.2.8",
        "expected_permanent_set": "mojo_blocked",
        "cidrs": ["192.0.2.8", "198.51.100.9/32"],
        "temporary_present": False,
    }, function=(
        "mojo.apps.incident.asyncjobs.broadcast_reconcile_geolocated_ip"))
    th.assert_eq(built["source"], "192.0.2.8/32",
                 "compound direct-IP input was not canonicalized")
    th.assert_eq(built["cidrs"], ["192.0.2.8/32", "198.51.100.9/32"],
                 "compound permanent set was not canonicalized")


@th.unit_test("status reports exact duplicate multiplicity")
def test_ip_status_exact_multiplicity(opts):
    from mojo.deploy import firewall_broker as broker

    rules = (
        "-A INPUT -s 192.0.2.8/32 -j DROP\n"
        "-A INPUT -s 192.0.2.8/32 -j DROP\n"
        "-A FORWARD -s 192.0.2.8/32 -j DROP\n")
    child = {"ok": True, "pid": 1, "start_ticks": 1,
             "returncode": 0}
    with mock.patch.object(broker, "_read_rules", return_value=(child, rules)), \
            mock.patch.object(broker, "_forwarding_required", return_value=True):
        unused, observed = broker._ip_status("192.0.2.8/32")
    th.assert_eq((observed["input_count"], observed["forward_count"]), (2, 1),
                 "duplicate rules must remain visible to normalization")
    th.assert_true(not observed["present"],
                   "duplicate multiplicity cannot be reported as desired truth")


@th.unit_test("IP normalization removes duplicates before establishing one rule")
def test_ip_normalize_repairs_duplicate_rules(opts):
    from mojo.deploy import firewall_broker as broker

    initial = (
        "-A INPUT -s 192.0.2.8/32 -j DROP\n"
        "-A INPUT -s 192.0.2.8/32 -j DROP\n")
    final = "-A INPUT -s 192.0.2.8/32 -j DROP\n"
    read_child = {"ok": True, "pid": 1, "start_ticks": 1,
                  "returncode": 0}
    child = {"ok": True, "pid": 2, "start_ticks": 2,
             "returncode": 0}
    commands = []

    def run(argv, *args, **kwargs):
        commands.append(argv)
        return dict(child), "", ""

    with mock.patch.object(
            broker, "_read_rules",
            side_effect=[(read_child, initial), (read_child, final)]), \
            mock.patch.object(broker, "_run_child", side_effect=run), \
            mock.patch.object(broker, "_forwarding_required", return_value=False):
        unused, observed, ok = broker._normalize_ip("192.0.2.8/32", True)
    deletes = [argv for argv in commands if argv[1] == "-D"]
    inserts = [argv for argv in commands if argv[1] == "-I"]
    th.assert_eq((len(deletes), len(inserts)), (2, 1),
                 "normalization did not repair exact duplicate multiplicity")
    th.assert_true(ok and observed["present"],
                   "repair did not re-observe exact desired truth")


@th.unit_test("incompatible set families remain observable for repair")
def test_incompatible_set_family_status(opts):
    from mojo.deploy import firewall_broker as broker

    payload = (
        "create blocked hash:net family inet6 hashsize 1024 maxelem 65536\n"
        "add blocked 2001:db8::/32\n")
    set_type, family, members, digest = broker._parse_set_save(
        payload, "blocked")
    th.assert_eq((set_type, family), ("hash:net", "inet6"),
                 "wrong-family truth was hidden from the repair path")
    th.assert_eq(members, ["2001:db8::/32"],
                 "incompatible membership should be observed, never executed")
    th.assert_true(len(digest) == 64, "status digest was not bounded")


@th.unit_test("wrong-family sets are destroyed and recreated before success")
def test_normalize_repairs_incompatible_set(opts):
    from mojo.apps.incident.services.firewall_truth import network_digest
    from mojo.deploy import firewall_broker as broker

    before = {
        "name": "blocked", "present": False, "exists": True,
        "type": "hash:net", "family": "inet6", "count": 1,
        "digest": "0" * 64, "input_count": 1, "forward_count": 0,
        "forwarding_required": False,
    }
    after = {
        "name": "blocked", "present": True, "exists": True,
        "type": "hash:net", "family": "inet", "count": 1,
        "digest": network_digest(["192.0.2.0/24"]),
        "input_count": 1, "forward_count": 0,
        "forwarding_required": False,
    }
    child = {"ok": True, "pid": 3, "start_ticks": 3,
             "returncode": 0}
    with mock.patch.object(
            broker, "_set_status",
            side_effect=[([], before), ([], after)]), \
            mock.patch.object(
                broker, "_normalize_rules", side_effect=[[], []]), \
            mock.patch.object(
                broker, "_run_child", return_value=(child, "", "")) as run, \
            mock.patch.object(
                broker, "_replace_set", return_value=child) as replace:
        unused, observed, ok = broker._normalize_set(
            "blocked", ["192.0.2.0/24"], True)
    run.assert_called_once_with([broker.IPSET, "destroy", "blocked"])
    replace.assert_called_once_with("blocked", ["192.0.2.0/24"])
    th.assert_true(ok and observed == after,
                   "incompatible set repair was not re-observed")


@th.unit_test("the root broker host lock fails closed when already owned")
def test_host_lock_busy_is_typed(opts):
    import stat
    from mojo.deploy import firewall_broker as broker

    info = mock.Mock(st_uid=0, st_mode=stat.S_IFREG | 0o600)
    with mock.patch.object(broker.os, "open", return_value=7), \
            mock.patch.object(broker.os, "fstat", return_value=info), \
            mock.patch.object(
                broker.fcntl, "flock", side_effect=BlockingIOError), \
            mock.patch.object(broker.os, "close") as close:
        with th.assert_raises(broker.BrokerError) as raised:
            broker._acquire_host_lock()
    th.assert_eq(raised.exception.code, "host_busy",
                 "concurrent root mutation did not return a typed refusal")
    close.assert_called_once_with(7)


@th.unit_test("global broker preconditions always use broker-wide codes")
def test_global_broker_preconditions_are_retryable(opts):
    import stat
    from mojo.deploy import firewall_broker as broker

    with mock.patch.object(broker.os, "geteuid", return_value=1000), \
            th.assert_raises(broker.BrokerError) as caller:
        broker._verify_caller()
    th.assert_eq(caller.exception.code, "broker_caller_invalid",
                 "caller verification could repeat once per desired object")

    unsafe = mock.Mock(st_uid=1, st_mode=stat.S_IFREG | 0o600)
    with mock.patch.object(broker.os, "open", return_value=7), \
            mock.patch.object(broker.os, "fstat", return_value=unsafe), \
            mock.patch.object(broker.os, "close") as close, \
            th.assert_raises(broker.BrokerError) as locked:
        broker._acquire_host_lock()
    th.assert_eq(locked.exception.code, "broker_host_lock_unsafe",
                 "unsafe host-lock metadata was treated as object-local")
    close.assert_called_once_with(7)


@th.unit_test("broker function-operation matrix is closed")
def test_function_matrix(opts):
    from mojo.deploy import firewall_broker as broker
    from mojo.mojosec.store import _BROKER_FUNCTION_OPERATIONS

    with th.assert_raises(broker.BrokerError):
        broker.build_operation({"operation": "rule.insert", "chain": "INPUT",
                                "source": "192.0.2.8"},
                               function="evil.module.call")
    th.assert_eq(
        _BROKER_FUNCTION_OPERATIONS, broker._FUNCTION_OPERATIONS,
        "MojoSec governance drifted from the broker's closed authority matrix")


@th.unit_test("broker refuses configured aggregate collision for operator sets")
def test_dynamic_reserved_set_collision(opts):
    from mojo.deploy import firewall_broker as broker

    request = {
        "operation": "set.replace", "set_name": "configured_reserved",
        "expected_permanent_set": "configured_reserved",
        "cidrs": ["192.0.2.0/24"],
    }
    with mock.patch.object(
            broker, "_root_permanent_set_name",
            return_value="configured_reserved"), \
            th.assert_raises(broker.BrokerError) as raised:
        broker.build_operation(
            request,
            function="mojo.apps.incident.asyncjobs.broadcast_sync_ipset")
    th.assert_eq(raised.exception.code, "reserved_set_name",
                 "operator lifecycle could overwrite the configured aggregate")


@th.unit_test("broker independently closes the framework operator namespace")
def test_framework_operator_namespace_is_refused(opts):
    from mojo.deploy import firewall_broker as broker

    request = {
        "operation": "set.normalize", "set_name": "mojo_forged_operator",
        "expected_permanent_set": "mojo_blocked", "cidrs": [],
        "present": False,
    }
    with mock.patch.object(
            broker, "_root_permanent_set_name", return_value="mojo_blocked"), \
            th.assert_raises(broker.BrokerError) as raised:
        broker.build_operation(
            request, function="mojo.apps.incident.asyncjobs.sync_firewall")
    th.assert_eq(raised.exception.code, "reserved_set_name",
                 "framework namespace reached an operator broker operation")


@th.unit_test("caller fields cannot redefine the root permanent namespace")
def test_forged_permanent_namespace_is_refused(opts):
    from mojo.deploy import firewall_broker as broker

    request = {
        "operation": "permanent.normalize",
        "expected_permanent_set": "attacker_chosen",
        "cidrs": ["192.0.2.0/24"],
    }
    with mock.patch.object(
            broker, "_root_permanent_set_name", return_value="mojo_blocked"), \
            th.assert_raises(broker.BrokerError) as raised:
        broker.build_operation(
            request, function="mojo.apps.incident.asyncjobs.sync_firewall")
    th.assert_eq(raised.exception.code, "permanent_set_config_mismatch",
                 "a caller-controlled field redefined the root-owned set")


@th.unit_test("configured and default broker namespaces fail closed on mismatch")
def test_root_and_application_namespace_mismatch_is_refused(opts):
    from mojo.deploy import firewall_broker as broker

    request = {
        "operation": "set.normalize", "set_name": "operator_set",
        "expected_permanent_set": "mojo_blocked", "cidrs": [],
        "present": False,
    }
    with mock.patch.object(
            broker, "_root_permanent_set_name",
            return_value="configured_root_set"), \
            th.assert_raises(broker.BrokerError) as raised:
        broker.build_operation(
            request, function="mojo.apps.incident.asyncjobs.sync_firewall")
    th.assert_eq(raised.exception.code, "permanent_set_config_mismatch",
                 "an app/default mismatch reached an operator mutation")


@th.unit_test("firewall backend uses exact noninteractive empty-argv broker command")
def test_firewall_invocation(opts):
    from mojo.apps.incident import firewall
    from mojo.apps.jobs.execution_context import execution

    completed = mock.Mock(returncode=0, stdout='{"ok":true,"present":false}\n', stderr="")
    with execution("job-1", "mojo.apps.incident.asyncjobs.broadcast_block_ip", 1,
                   "default", "runner-1"):
        with mock.patch.object(firewall, "_check_user", return_value=True), \
                mock.patch("mojo.apps.incident.services.firewall_readiness.probe",
                        return_value={"ready": True, "code": "ready"}), \
                mock.patch.object(firewall.subprocess, "run", return_value=completed) as run:
            result = firewall.is_blocked("192.0.2.8")
    th.assert_true(not result, "semantic rules read should return broker presence")
    th.assert_eq(run.call_args.args[0], ["/usr/bin/sudo", "-n", "--",
                                        "/usr/local/sbin/mojo-firewall-broker"],
                 "application sudo must execute only the empty-argv broker command")


@th.unit_test("firewall client returns typed bounded transport failures")
def test_firewall_transport_failures_are_typed(opts):
    from mojo.apps.incident import firewall
    from mojo.apps.jobs.execution_context import execution

    secret = "198.51.100.201/32"
    malformed = mock.Mock(returncode=1, stdout="not-json", stderr="secret-stderr")
    invalid = mock.Mock(returncode=0, stdout='{"result":"unknown"}', stderr="")
    refused = mock.Mock(
        returncode=1,
        stdout='{"ok":false,"error":{"code":"broker_resource_limit_unavailable"}}',
        stderr="")
    poisoned = mock.Mock(
        returncode=1,
        stdout='{"ok":false,"error":{"code":"broker_timeout\\nsecret-log-line"}}',
        stderr="")
    poisoned_zero = mock.Mock(
        returncode=0,
        stdout='{"ok":false,"error":{"code":"broker_bad\\nsecret-job-line"}}',
        stderr="")
    scenarios = (
        (subprocess.TimeoutExpired([firewall.BROKER], 20), "broker_timeout"),
        (OSError("secret-startup-path"), "broker_start_failed"),
        (malformed, "broker_malformed_response"),
        (invalid, "broker_invalid_response"),
        (refused, "broker_resource_limit_unavailable"),
        (poisoned, "broker_invalid_response"),
        (poisoned_zero, "broker_invalid_response"),
    )
    with execution(
            "job-1", "mojo.apps.incident.asyncjobs.sync_firewall", 1,
            "default", "runner-1"):
        for outcome, expected in scenarios:
            with mock.patch.object(firewall, "_check_user", return_value=True), \
                    mock.patch("mojo.apps.incident.services.firewall_readiness.probe",
                            return_value={"ready": True, "code": "ready"}), \
                    mock.patch.object(firewall.logit, "error") as logged:
                if isinstance(outcome, BaseException):
                    run = mock.patch.object(
                        firewall.subprocess, "run", side_effect=outcome)
                else:
                    run = mock.patch.object(
                        firewall.subprocess, "run", return_value=outcome)
                with run:
                    result = firewall._broker_request(
                        "ip.normalize", source=secret, present=True)
            th.assert_eq(
                result["error"]["code"], expected,
                "a broker transport failure lost its stable machine code")
            rendered = repr(logged.call_args_list)
            th.assert_true(
                secret not in rendered and "secret-startup-path" not in rendered and
                "secret-stderr" not in rendered and "not-json" not in rendered and
                "secret-log-line" not in rendered and "secret-job-line" not in rendered,
                "broker diagnostics leaked request, exception, stderr, or response content")


@th.unit_test("aggregate client declares config but cannot choose root target")
def test_permanent_client_request_has_no_target_field(opts):
    import json
    from mojo.apps.incident import firewall
    from mojo.apps.jobs.execution_context import execution

    completed = mock.Mock(
        returncode=0, stdout='{"ok":true,"observed":{}}\n', stderr="")
    with execution(
            "job-1", "mojo.apps.incident.asyncjobs.sync_firewall", 1,
            "default", "runner-1"), \
            mock.patch.object(firewall, "_check_user", return_value=True), \
            mock.patch("mojo.apps.incident.services.firewall_readiness.probe",
                       return_value={"ready": True, "code": "ready"}), \
            mock.patch.object(
                firewall.subprocess, "run", return_value=completed) as run:
        firewall.normalize_permanent_ipset([])
    payload = json.loads(run.call_args.kwargs["input"])
    th.assert_eq(payload["operation"], "permanent.normalize",
                 "aggregate client used the wrong broker authority")
    th.assert_true("set_name" not in payload and "reserved_set_name" not in payload,
                   "unprivileged aggregate caller still selected root identity")
    th.assert_eq(payload["expected_permanent_set"], "mojo_blocked",
                 "application configuration assertion was not carried")


@th.unit_test("broker production limits and empty-argv sudoers are exact")
def test_broker_limits_and_sudoers(opts):
    from mojo.deploy import firewall_broker as broker

    th.assert_eq(
        (broker.MAX_REQUEST_BYTES, broker.MAX_CIDRS, broker.MAX_RESTORE_BYTES,
         broker.MAX_OUTPUT_BYTES, broker.MAX_RULES_OUTPUT_BYTES,
         broker.SCALAR_TIMEOUT_SECONDS, broker.BULK_TIMEOUT_SECONDS,
         broker.ADDRESS_SPACE_GROWTH_BYTES, broker.MAX_ADDRESS_SPACE_BYTES),
        (16 * 1024 * 1024, 250000, 24 * 1024 * 1024, 64 * 1024,
         8 * 1024 * 1024, 15, 120, 256 * 1024 * 1024,
         768 * 1024 * 1024),
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


@th.unit_test("broker kills output overflow without unbounded communicate buffering")
def test_bounded_child_output(opts):
    from mojo.deploy import firewall_broker as broker

    with mock.patch.object(broker, "_start_ticks", return_value=1234):
        with th.assert_raises(broker.BrokerChildError):
            broker._run_child(
                [sys.executable, "-c", "import sys;sys.stdout.write('x'*70000)"],
                stdout_limit=64 * 1024)


@th.unit_test("multi-step broker receipts bind every ordered child")
def test_multistep_child_receipts(opts):
    from mojo.deploy import firewall_broker as broker

    context = {
        "execution_id": "exec-1", "job_id": "job-1",
        "function": "mojo.apps.incident.asyncjobs.broadcast_ipset_add_blocked",
        "attempt": 1, "channel": "default", "runner": "runner-1",
        "broadcast": False,
    }
    request = {"operation": "permanent.rule_ensure",
               "expected_permanent_set": "mojo_blocked",
               "context": context}
    children = [
        ({"pid": 101, "start_ticks": 1001, "exe": broker.IPTABLES,
          "argv_digest": "a" * 64, "returncode": 1, "ok": False}, "", ""),
        ({"pid": 102, "start_ticks": 1002, "exe": broker.IPTABLES,
          "argv_digest": "b" * 64, "returncode": 0, "ok": True}, "", ""),
    ]
    receipts = []
    with mock.patch.object(broker, "_run_child", side_effect=children), \
            mock.patch.object(broker, "_receipt",
                              side_effect=lambda kind, op, ctx, built, **values:
                              receipts.append(dict(kind=kind, **values)) or values):
        result = broker.execute(request)
    recorded = receipts[-1]["children"]
    th.assert_eq([item["pid"] for item in recorded], [101, 102],
                 "result receipt must preserve every subprocess in execution order")
    th.assert_true(all(item["ok"] for item in recorded) and result["ok"],
                   "the expected rule-miss step and successful insert are both semantic success")
    th.assert_eq([item["argv_digest"] for item in recorded], ["a" * 64, "b" * 64],
                 "each child must retain its own exact argv digest")
