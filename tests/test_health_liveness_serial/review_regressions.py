"""Real producer wiring and checked logical-node identity regressions."""
import json
import time
from tempfile import TemporaryDirectory
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from testit import helpers as th
from .liveness import NOW, Redis, _runner


class Client(Redis):
    def __init__(self):
        super().__init__()
        self.commands = []
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def get(self, key):
        self.commands.append(("get", key))
        return super().get(key)
    def set(self, key, value, **kwargs):
        self.commands.append(("set", key))
        return super().set(key, value, **kwargs)
    def eval(self, script, count, *args):
        from mojo.apps.account.services import health_snapshot as hs
        self.commands.append(("eval", script))
        keys, values = args[:count], args[count:]
        if script == hs._READ_LUA:
            return ["", "missing", ""]
        if script == hs._RESERVE_LUA:
            self.data[keys[1]] = values[1]
        elif script == hs._PUBLISH_LUA:
            self.data[keys[2]] = values[2]
        return 1


@th.django_unit_test()
def test_actual_snapshot_producer_reads_cached_detector_on_shared_redis_budget(opts):
    from mojo.apps.account.services import health_snapshot as hs, health_evidence as he
    from mojo.apps.incident.services import health_liveness as hl
    from test_account.health_producer import _configuration
    now, client, options = he.utcnow(), Client(), []
    configuration = _configuration()
    collected = hl.collect(client=client, roster=lambda *args, **kwargs: [],
                           channels=["edge"], expected=["node-a"], now=now)
    client.commands.clear()
    def connect(**kwargs):
        options.append(kwargs)
        return client
    readers = {"native": lambda: {}, "redis": lambda: {"native": {}, "queues": {}},
        "managed": lambda: {"series": []}, "probe": lambda *args: {},
        "fleet": {"inventory": lambda *args: {"complete": True, "instances": {}},
            "heartbeats": lambda end: [], "recent": lambda *args: {"nodes": {}},
            "identity_cache": SimpleNamespace(read=lambda *args, **kwargs: None,
                write=lambda *args, **kwargs: True)}}
    result = hs.produce_snapshot(SimpleNamespace(payload={"source_id": configuration["source_id"],
        "slot": int(now.timestamp())//300}), readers=readers, connect=connect,
        configuration_reader=lambda end: configuration, reserve=lambda *args: 1,
        guard=lambda *args: nullcontext(), now=now)
    publication = json.loads(next(value for key, value in client.data.items() if key.endswith(":current")))
    jobman = next(row for row in publication["components"] if row["kind"] == "jobman")
    detector = next(row for row in jobman["evidence"] if row["slot_id"] == "scheduler_detector")
    assert detector["observed_at"] == collected["observed_at"] and detector["status"] == "ok", "the actual producer's scheduler reader bypassed the independent cache"
    assert ("get", hl._key("detector")) in client.commands, "produce_snapshot did not read actual independent detector evidence"
    assert next(row for row in jobman["conditions"] if row["rule"] == "scheduler_available")["outcome"] == "fail", "the producer discarded a known independent scheduler outage"
    # The result captures counters at publication; finally releases its lease
    # afterward. Exclude that actual cleanup command, never acquisition reads.
    publication_commands = [command for command in client.commands if command != ("eval", hs._RELEASE_LUA)]
    assert result["status"] == "published" and result["accounting"]["redis_commands"] == len(publication_commands), "the producer's detector read bypassed shared Redis command accounting"
    assert all(option["read_from_replicas"] is False and option["timeout"] <= .5 and option["deadline"] > time.monotonic() for option in options), "the producer's detector read lost primary transport bounds"


def _logical_setup(hosts):
    from mojo.apps.aws.services import fleet_health as fh
    from mojo.apps.incident.services import health_liveness as hl
    topology = {"nodes": ["logical-edge"], "pools": ["app"]}
    get_static = lambda key, default=None: {"JOBS_CHANNELS": ["edge"], "MOJO_ENVIRONMENT": "production"}.get(key, default)
    expected = fh._read_expected({"platform": True, "aws": True, "jobs": True}, time.monotonic()+2,
        get_static=get_static, topology_reader=lambda key: topology, spec_reader=lambda: None)
    client = Client()
    runners = [dict(_runner(host, host+"-100"), started=NOW.isoformat()) for host in hosts]
    proofs = [{"host": host, "started": NOW.isoformat(),
               "proof": {"node_id": "logical-edge", "observed_at": NOW.isoformat()}} for host in hosts]
    rows = fh.reconcile(expected["declarations"], runners=runners, proofs=proofs, now=NOW)
    cache = fh.IdentityRoster(connect=lambda **kwargs: nullcontext(client), clock=lambda: NOW.timestamp())
    assert cache.write({"platform": True, "aws": True, "jobs": True}, fh._revision(rows), rows,
        time.monotonic()+2, scope=fh.roster_scope(expected)), "logical fixture could not write the actual validated shared identity cache"
    def collect(rows, now=NOW):
        from mojo.apps.account.services import health_evidence as he
        from mojo.apps.aws.services import infra_setup
        cursor = SimpleNamespace(execute=lambda *args: None, fetchone=lambda: (json.dumps(topology),))
        with TemporaryDirectory() as root, \
                mock.patch.object(hl.settings, "get_static", side_effect=get_static), \
                mock.patch.object(he, "primary_cursor", return_value=nullcontext(cursor)), \
                mock.patch.object(infra_setup, "_project_root", return_value=root):
            return hl.collect(client=client, roster=lambda *args, **kwargs: rows, now=now)
    return client, runners, collect


@th.django_unit_test()
def test_logical_edge_node_uses_validated_alias_instead_of_false_runner_down(opts):
    client, runners, collect = _logical_setup(["host-a"])
    result = collect(runners)
    assert result["runners"]["healthy"] is True and not result["runners"]["missing"], "logical EDGE_NODE_ID was mistaken for a physical runner hostname"


@th.django_unit_test()
def test_ambiguous_logical_alias_remains_unknown_with_live_runners(opts):
    client, runners, collect = _logical_setup(["host-a", "host-b"])
    result = collect(runners)
    assert result["runners"]["healthy"] is None and result["runners"]["status"] == "unknown", "ambiguous logical aliases created a false Runner Down or healthy result"


@th.django_unit_test()
def test_unproved_logical_alias_is_not_inferred_from_live_only_roster(opts):
    client, runners, collect = _logical_setup([])
    result = collect([dict(_runner("host-a", "host-a-100"), started=NOW.isoformat())])
    assert result["runners"]["healthy"] is None and result["runners"]["status"] == "unknown", "a live-only roster invented the physical alias of a logical node"


@th.django_unit_test()
def test_established_logical_alias_detects_disappearance_after_alias_cache_expiry(opts):
    client, runners, collect = _logical_setup(["host-a"])
    first = collect(runners)
    lost = collect([], NOW+timedelta(seconds=180))
    assert first["runners"]["healthy"] is True, "the established logical alias fixture did not start healthy"
    assert lost["runners"]["healthy"] is False and lost["runners"]["missing"] == [{"hostname": "host-a", "channels": ["edge"]}], "expiring current alias evidence erased an established missing physical runner"
    restarted = [dict(_runner("host-a", "host-a-200"), started=(NOW+timedelta(seconds=180)).isoformat(),
                      last_heartbeat=(NOW+timedelta(seconds=180)).isoformat())]
    unproved = collect(restarted, NOW+timedelta(seconds=180))
    assert unproved["runners"]["healthy"] is None, "a predecessor's logical alias proved the identity of an unchecked replacement incarnation"
