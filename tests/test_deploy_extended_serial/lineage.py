"""Moved from tests/test_mojosec/lineage.py (maestro #2558).

This test stubs `mojo.mojosec.store.PENDING_OPERATION_CAP` — a process-global
patch of a production module attribute, unsafe under the parallel default
tier. The remaining pending-firewall hold/flush lineage contracts stay in the
default-tier tests/test_mojosec/lineage.py.
"""

import os
import tempfile
import time
from unittest import mock

from testit import helpers as th


@th.unit_test("pending firewall capacity pressure fails open before eviction")
def test_pending_firewall_capacity_fail_open(opts):
    from mojo.mojosec import store as store_module
    from mojo.mojosec.events import observation

    aggregation = {"window_seconds": 60, "flush_count": 10,
                   "max_aggregates": 100, "critical_reserve_aggregates": 10}
    delivery = {"max_spool_events": 100, "critical_reserve_events": 10,
                "retry_min_seconds": 1, "retry_max_seconds": 60}
    candidate = observation(
        "auth.sudo_command", "high", "Privileged sudo command executed",
        attributes={"actor": "ec2-user", "target_user": "root",
                    "command": "/usr/local/sbin/mojo-firewall-broker",
                    "command_path": "/usr/local/sbin/mojo-firewall-broker"},
        fingerprint_values=("capacity",), aggregate=False,
        observed_at="2026-08-13T20:00:00Z")
    with tempfile.TemporaryDirectory() as root:
        store = store_module.Store(root, "sensor", aggregation, delivery,
                                   local_only_diagnostic_path=os.path.join(root, "missing"))
        store.db.execute("BEGIN IMMEDIATE")
        with mock.patch.object(store_module, "PENDING_OPERATION_CAP", 0):
            store.hold_firewall_observation(candidate, now=time.time())
        store.db.execute("COMMIT")
        th.assert_eq(store.db.execute(
            "SELECT COUNT(*) FROM pending_firewall").fetchone()[0], 0,
            "cap pressure may delete a candidate only after fail-open enqueue")
        th.assert_eq(store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1,
                     "cap pressure must retain an ordinary Event")
        th.assert_eq(store.stats()["provenance"]["pending_cap_flush"], 1,
                     "cap fail-open must be visible in pressure telemetry")
        store.close()
