"""Parallel-safe readiness and declared fleet contracts."""
import json
import subprocess
import sys
from pathlib import Path
from types import FunctionType, SimpleNamespace
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


@th.django_unit_test()
def test_aggregate_marker_survives_retry_and_releases_at_terminal_boundary(opts):
    from mojo.apps.incident import asyncjobs
    for code, attempt, expected_deletes in (
            ("expected_hosts_unavailable", 1, 0),
            ("expected_hosts_unavailable", 8, 1),
            ("broker_wrong_user", 1, 1),
            (None, 1, 1)):
        deleted = []
        redis = SimpleNamespace(eval=lambda *args: deleted.append(args))
        def aggregate(job):
            if code:
                raise asyncjobs.FirewallSyncRetry(code)
            return True
        # Copy this function's globals so the real wrapper runs against local
        # boundaries without patching the process-shared incident namespace.
        namespace = dict(asyncjobs.aggregate_firewall_truth.__globals__,
                         _aggregate_firewall_truth=aggregate, _raw_redis=lambda: redis)
        wrapper = FunctionType(asyncjobs.aggregate_firewall_truth.__code__, namespace)
        job = SimpleNamespace(payload={"generation": 1, "aggregate_token": "owned"},
                              attempt=attempt, max_retries=8)
        try:
            wrapper(job)
        except asyncjobs.FirewallSyncRetry:
            pass
        th.assert_eq(len(deleted), expected_deletes,
                     f"aggregate marker lifecycle must match retry boundary: {code}, attempt={attempt}")


@th.django_unit_test()
def test_aggregation_needs_only_fleet_evidence_and_invalidates_lost_capability(opts):
    # Shared-model/provider substitutions live in their own Django process.
    script = '''
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from contextlib import ExitStack
from mojo.apps.incident import asyncjobs as a
from mojo.apps.incident.services import firewall_truth as t
from mojo.apps.incident.services import firewall_readiness as r
for lose_capability in (False, True):
    updates = []
    ipsets, geos = MagicMock(), MagicMock()
    row = SimpleNamespace(pk=101, modified="revision", name="testset")
    ipsets.objects.order_by.return_value = [row]
    ipsets.objects.filter.return_value.update.side_effect = lambda **values: updates.append(values) or 1
    geos.objects.filter.return_value.order_by.return_value = []
    geos.objects.filter.return_value.update.return_value = 0
    roster_calls = []
    def roster():
        roster_calls.append(True)
        if lose_capability and len(roster_calls) == 3:
            raise t.FirewallTruthError("expected_hosts_unavailable", "capability disappeared")
        return [{"host": "host-a", "started": "same"}]
    boundaries = {
        "exact_compatible_roster": roster,
        "acquire_desired_state": lambda: SimpleNamespace(redis=None),
        "release_desired_state": lambda lease: None,
        "read_desired_generation": lambda redis: 1,
        "desired_state_is_current": lambda lease: True,
        "permanent_snapshot": lambda: {"name": "mojo_blocked", "cidrs": [], "fingerprint": "a" * 64},
        "read_fences": lambda redis, targets: {target: 1 for target in targets},
        "ipset_snapshot_from_row": lambda row: {"desired": {"name": row.name}, "fingerprint": "b" * 64},
        "aggregate_observations": lambda *args, **kwargs: {"ok": True},
    }
    with ExitStack() as stack:
        stack.enter_context(patch("mojo.apps.incident.models.IPSet", ipsets))
        stack.enter_context(patch("mojo.apps.account.models.GeoLocatedIP", geos))
        stack.enter_context(patch.object(r, "require_ready", side_effect=AssertionError("evidence-only aggregate consulted local broker")))
        for name, value in boundaries.items():
            stack.enter_context(patch.object(t, name, value))
        job = SimpleNamespace(add_log=lambda message: None)
        try:
            result = a._aggregate_firewall_truth(job)
            assert not lose_capability and result is True, "complete fleet evidence must finalize on any consumer"
        except a.FirewallSyncRetry as error:
            assert lose_capability and error.code == "expected_hosts_unavailable", "capability loss must retry"
        assert updates, "the aggregate must exercise publication"
        if lose_capability:
            assert updates[-1]["last_synced"] is None, "post-publication roster loss must invalidate success"
            assert "expected_hosts_unavailable" in updates[-1]["sync_error"], "invalidation must preserve the lost-capability reason"
'''
    manage = Path(__file__).resolve().parents[2] / "testproject/manage.py"
    result = subprocess.run([sys.executable, str(manage), "shell", "-c", script],
                            capture_output=True, text=True, timeout=30)
    th.assert_eq(result.returncode, 0,
                 "aggregation availability regression failed: " + result.stderr[-2000:])


@th.django_unit_test()
def test_firewall_channel_parameters_reject_other_channels(opts):
    from mojo.apps.incident.services import firewall_truth as truth
    with th.assert_raises(truth.FirewallTruthError):
        truth.exact_compatible_runner_roster("default")
    result = truth.reconcile_ip("203.0.113.4", True, channel="default")
    th.assert_eq(result["error"]["code"], "invalid_firewall_channel",
                 "legacy channel argument must fail explicitly before dispatch")
