"""Parallel-safe readiness and declared fleet contracts."""
import json
import subprocess
from testit import helpers as th


@th.django_unit_test()
def test_readiness_requires_effective_application_uid(opts):
    from mojo.apps.incident.services import firewall_readiness as ready
    calls = []
    result = ready.probe(euid=0, app_uid=1000, transport=lambda: calls.append(True))
    th.assert_eq(result["code"], "broker_wrong_user", "root must not advertise application capability")
    th.assert_eq(calls, [], "wrong effective uid must not invoke sudo")


@th.django_unit_test()
def test_readiness_strict_bounded_status(opts):
    from mojo.apps.incident.services import firewall_readiness as ready
    good = {"ok": True, "schema": "mojo.firewall.broker", "version": 1,
            "permanent_set_name": "mojo_blocked"}
    result = ready.probe(euid=1000, app_uid=1000, permanent_name="mojo_blocked",
                         transport=lambda: (0, json.dumps(good).encode()))
    th.assert_true(result["ready"], "valid enrolled broker must be ready without MojoSec")
    for payload in (b'{}', b'{"ok":true,"ok":false}', b'x' * 4097):
        result = ready.probe(euid=1000, app_uid=1000,
                             transport=lambda: (0, payload))
        th.assert_true(not result["ready"], "malformed or oversized broker proof must fail closed")


@th.django_unit_test()
def test_expected_fleet_is_explicit_and_subset_never_final(opts):
    from mojo.apps.incident.services import firewall_truth as truth
    for value in (None, [], "host-a", ["host-a", "host-a"], ["bad host"]):
        with th.assert_raises(truth.FirewallTruthError):
            truth.expected_hosts(value)
    row = {"hostname": "host-a", "runner_id": "host-a-engine", "started": "2026-09-09T12:00:00+00:00",
           "channels": ["firewall", "host-a-engine"],
           "capabilities": {"firewall_reconcile": 1, "execute_checked": 2}}
    from mojo.apps.jobs.manager import JobManager
    manager = JobManager.__new__(JobManager)
    manager.get_runners_bounded = lambda *args, **kwargs: [row]
    result = truth.firewall_roster(rows=[row], expected=["host-a", "host-b"], manager=manager)
    th.assert_eq(result["expected_hosts"], ["host-a", "host-b"], "declared fleet must not shrink")
    th.assert_eq(result["unavailable_hosts"], ["host-b"], "offline expected host must remain unavailable")
    th.assert_eq(len(result["selected"]), 1, "healthy subset must remain repairable")
    dispatches = []
    def dispatch(func, payload, **kwargs):
        dispatches.append(kwargs["roster"])
        return {"status": "verified"}
    manager.broadcast_execute_checked = dispatch
    checked = truth._dispatch_firewall("example.firewall", {}, 1, None,
                                      manager=manager, expected=["host-a", "host-b"])
    th.assert_eq(dispatches, [[row]], "only healthy expected hosts may receive repair")
    th.assert_eq(checked["status"], "partial", "healthy-subset success must never finalize fleet truth")
    th.assert_eq(checked["unavailable_hosts"], ["host-b"], "dispatch evidence must preserve absent expected host")


@th.django_unit_test()
def test_disabled_ipv6_tombstone_still_removes_safe_set(opts):
    from mojo.apps.incident.services.firewall_truth import canonical_operator_ipset, FirewallTruthError
    th.assert_eq(canonical_operator_ipset("old_v6", ["2001:db8::/32"], False),
                 ("old_v6", []), "disabled historical IPv6 row must produce an absence tombstone")
    with th.assert_raises(FirewallTruthError):
        canonical_operator_ipset("bad;name", [], False)
    with th.assert_raises(FirewallTruthError):
        canonical_operator_ipset("old_v6", ["2001:db8::/32"], True)


@th.django_unit_test()
def test_transient_readiness_and_terminal_structural_failures(opts):
    from mojo.apps.incident.services import firewall_readiness as ready
    from mojo.apps.incident.asyncjobs import FirewallSyncRetry
    def timed_out():
        raise subprocess.TimeoutExpired(ready.STATUS_ARGV, 3)
    result = ready.probe(euid=1000, app_uid=1000, transport=timed_out)
    th.assert_true(result["transient"], "timeout must remain retryable")
    th.assert_true(FirewallSyncRetry("broker_timeout").retryable, "job timeout needs durable retry")
    th.assert_true(not FirewallSyncRetry("broker_wrong_user").retryable, "wrong-user job must fail terminally")
    th.assert_true(not FirewallSyncRetry("broker_malformed_response").retryable, "malformed authority must fail terminally")
