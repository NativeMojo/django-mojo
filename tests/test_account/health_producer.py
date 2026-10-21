"""Producer scheduling, bounded assembly and independent-source failures."""
import json
import time
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from testit import helpers as th


def _configuration():
    from mojo.apps.account.services import health_snapshot as hs
    identity = hs.resolve_identity(identity_reader=lambda: {"uuid": "d496818a-06c7-45f5-b165-8993d93b3a47", "slug": "testit"},
        get_static=lambda key, default=None: "testit" if key == "MOJO_ENVIRONMENT" else default)
    return {**identity, "config_revision": "b"*64, "services": [{"resource_id": "primary", "service_id": "primary", "origin": "https://example.com", "path": "/api/version"}],
        "services_complete": True, "services_omitted": 0, "channels": ["testit_health"], "channels_complete": True,
        "expected": {"configured": True, "complete": True, "spec": None, "sources": {}, "declarations": []}}


@th.django_unit_test()
def test_dispatch_coalesces_latest_pending_and_preserves_running_terminal_rows(opts):
    from mojo.apps.account.services import health_snapshot as hs
    from mojo.apps.jobs.models import Job
    configuration, now = _configuration(), datetime.now(timezone.utc)
    source = uuid.uuid4().hex*2
    configuration["source_id"] = source
    ids = []
    def publish(func, **params):
        row = Job.objects.create(id=uuid.uuid4().hex, func=func, payload=params["payload"], channel="testit_health", status="pending",
            idempotency_key=params["idempotency_key"], max_retries=params["max_retries"], max_exec_seconds=params["max_exec_seconds"], expires_at=params["expires_at"])
        ids.append(row.pk)
        return row.pk
    try:
        first = hs.enqueue_snapshot(now=now, configuration_reader=lambda end: configuration, publisher=publish, mirror=lambda *args: None)
        later = hs.enqueue_snapshot(now=now+timedelta(seconds=300), configuration_reader=lambda end: configuration, publisher=publish, mirror=lambda *args: None)
        assert first["job_id"] == later["job_id"] and len(ids) == 1, "scheduler replay accumulated stale pending producer slots"
        row = Job.objects.get(pk=ids[0])
        assert row.payload["slot"] == int((now+timedelta(seconds=300)).timestamp())//300 and row.max_retries == 0 and row.max_exec_seconds == 60, "coalesced producer did not retain only the latest bounded slot"
        row.status = "running"
        row.save(update_fields=["status"])
        third = hs.enqueue_snapshot(now=now+timedelta(seconds=600), configuration_reader=lambda end: configuration, publisher=publish, mirror=lambda *args: None)
        row.refresh_from_db()
        assert len(ids) == 2 and row.status == "running" and third["job_id"] != first["job_id"], "coalescing mutated a running producer or failed to keep one latest pending slot"
    finally:
        Job.objects.filter(pk__in=ids).delete()


@th.django_unit_test()
def test_producer_keeps_independent_api_failure_when_native_collector_fails(opts):
    from mojo.apps.account.services import health_snapshot as hs, health_evidence as he
    configuration, now, stored = _configuration(), he.utcnow(), {}
    class Redis:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def set(self, key, value, **kwargs):
            if kwargs.get("nx") and key in stored:
                return False
            stored[key] = value
            return True
        def get(self, key):
            return stored.get(key)
        def eval(self, script, count, *args):
            keys, values = args[:count], args[count:]
            if script == hs._RESERVE_LUA:
                stored[keys[1]] = values[1]
            elif script == hs._READ_LUA:
                return ["", "missing", ""]
            elif script == hs._PUBLISH_LUA:
                stored[keys[2]] = values[2]
                stored[keys[4]] = values[4]
            elif script == hs._RELEASE_LUA:
                stored.pop(keys[0], None)
            return 1
    def broken():
        raise RuntimeError("native database unavailable")
    readers = {"native": broken, "redis": lambda: {"native": {}, "queues": {}}, "managed": lambda: {"series": []},
        "probe": lambda service, end: {"observed_at": he.timestamp(now), "reachable": False, "http_status": 503, "latency_seconds": 0.1},
        "fleet": {"inventory": lambda spec, end: {"complete": True, "instances": {}}, "heartbeats": lambda end: [],
            "scheduler": lambda end: {"status": "ok", "observed_at": None},
            "identity_cache": SimpleNamespace(read=lambda *args, **kwargs: None, write=lambda *args, **kwargs: True),
            "recent": lambda *args: {"nodes": {}}}}
    result = hs.produce_snapshot(SimpleNamespace(payload={"source_id": configuration["source_id"], "slot": int(now.timestamp())//300}),
        readers=readers, connect=lambda **kwargs: Redis(), configuration_reader=lambda end: configuration,
        reserve=lambda *args: 1, guard=lambda *args: nullcontext(), now=now)
    publication = json.loads(next(value for key, value in stored.items() if key.endswith(":current")))
    api = next(row for row in publication["components"] if row["kind"] == "api")
    assert result["status"] == "published" and api["availability"] == "down", "one collector exception discarded independently proved HTTP unavailability"
    assert publication["collection_status"] == "partial" and result["accounting"]["api_services"] == 1, "partial source collection lost its bounded accounting or claimed complete health"


@th.django_unit_test()
def test_maximum_roster_packing_preserves_omitted_obligations_under_all_caps(opts):
    from mojo.apps.account.services import health_snapshot as hs, health_evidence as he
    configuration, now = _configuration(), datetime.now(timezone.utc)
    configuration.update(services_omitted=2, services_complete=False)
    fleet = {"rows": [], "evidence": {}, "expected": {"complete": True}, "roster_complete": True,
             "inventory": {"observed_at": he.timestamp(now)}, "sources": {}}
    for index in range(128):
        node = "node-%d" % index
        fleet["rows"].append({"node_id": node, "expected": True, "identity_status": "resolved", "provider_state": "running"})
        sample = {"status": "ok", "observed_at": he.timestamp(now), "percent": 1}
        mounts = [{**sample, "path": "/data%d" % mount, "inodes": {"status": "ok", "observed_at": he.timestamp(now), "total": 10, "free": 9}} for mount in range(16)]
        fleet["evidence"][node] = {"current": {"cpu": sample, "memory": sample, "filesystems": {**sample, "mounts": mounts}}, "trends": {}}
    components = he.build_components(configuration, {}, {}, fleet, {}, now=now)
    value = hs._fit_publication({key: configuration[key] for key in ("source_id", "source_identity")}, 1, configuration["config_revision"], components,
        generated_at=he.timestamp(now), published_at=he.timestamp(now), roster_revision=None, configuration=configuration)
    assert len(he.canonical(value).encode()) <= hs.MAX_BYTES and sum(len(row["evidence"]) for row in value["components"]) <= hs.MAX_SLOTS, "maximum expected roster could not publish within byte/slot caps"
    assert sum(len(row["conditions"]) for row in value["components"]) <= he.MAX_CONDITIONS, "packing exceeded the typed causal condition cap"
    assert value["coverage"]["expected_total"] is None and not value["coverage"]["complete"], "truncated service enumeration established a known complete denominator"
    fleet_component = next(row for row in value["components"] if row["kind"] == "fleet")
    assert fleet_component["coverage"]["omitted_total"] > 0 and fleet_component["coverage"]["truncated"], "packing silently replaced required filesystem obligations with a smaller roster"


@th.django_unit_test()
def test_pending_producer_reconciles_a_lost_queue_mirror_after_commit(opts):
    from mojo.apps.account.services import health_snapshot as hs
    from mojo.apps.jobs.models import Job
    from mojo.apps.jobs.keys import JobKeys
    from mojo.helpers.redis import get_bounded_connection
    configuration, now = _configuration(), datetime.now(timezone.utc)
    configuration["source_id"] = uuid.uuid4().hex*2
    channel, ids = "testit_health_"+uuid.uuid4().hex[:12], []
    key = JobKeys().queue(channel)
    def lost_publish(func, **params):
        row = Job.objects.create(id=uuid.uuid4().hex, func=func, channel=channel, payload=params["payload"],
            status="pending", idempotency_key=params["idempotency_key"], expires_at=params["expires_at"])
        ids.append(row.pk)
        # Simulate death between durable commit and the original Redis mirror.
        return row.pk
    try:
        with get_bounded_connection(timeout=0.5, deadline=time.monotonic()+2) as client:
            client.delete(key)
        first = hs.enqueue_snapshot(now=now, configuration_reader=lambda end: configuration, publisher=lost_publish)
        second = hs.enqueue_snapshot(now=now+timedelta(seconds=300), configuration_reader=lambda end: configuration, publisher=lost_publish)
        with get_bounded_connection(timeout=0.5, deadline=time.monotonic()+2) as client:
            queued = [str(value) for value in client.lrange(key, 0, 3)]
        assert first["job_id"] == second["job_id"] and queued == ids, "latest pending producer remained stranded after its original queue mirror was lost"
        row = Job.objects.get(pk=ids[0])
        assert row.last_error == "" and row.payload["slot"] == int((now+timedelta(seconds=300)).timestamp())//300, "confirmed remirror failed to preserve latest slot or clear uncertainty"
    finally:
        Job.objects.filter(pk__in=ids).delete()
        with get_bounded_connection(timeout=0.5, deadline=time.monotonic()+2) as client:
            client.delete(key)


@th.django_unit_test()
def test_counted_acquisition_exhaustion_keeps_publication_headroom(opts):
    from mojo.apps.account.services import health_snapshot as hs, health_evidence as he
    configuration, now, stored, bounds = _configuration(), he.utcnow(), {}, {}
    configuration["channels"] = ["testit_channel_%d" % index for index in range(32)]
    class Pipeline:
        def __init__(self):
            self.values = []
        def get(self, key):
            self.values.append(None)
            return self
        def llen(self, key):
            self.values.append(0)
            return self
        def zcard(self, key):
            self.values.append(0)
            return self
        def zrange(self, *args, **kwargs):
            self.values.append([])
            return self
        def execute(self):
            return self.values
        def reset(self):
            pass
    class Redis:
        def __init__(self, end):
            self.deadline = end
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def set(self, key, value, **kwargs):
            stored[key] = value
            return True
        def get(self, key):
            return stored.get(key)
        def ping(self):
            return True
        def info(self):
            return {"run_id": "one", "maxmemory": 100, "used_memory": 1}
        def pipeline(self, **kwargs):
            return Pipeline()
        def zrangebyscore(self, *args, **kwargs):
            return ["runner-%d" % index for index in range(128)]
        def zcount(self, *args):
            return 0
        def eval(self, script, count, *args):
            keys, values = args[:count], args[count:]
            if script == hs._RESERVE_LUA:
                stored[keys[1]] = values[1]
            elif script == hs._READ_LUA:
                return ["", "missing", ""]
            elif script == hs._PUBLISH_LUA:
                stored[keys[2]] = values[2]
                bounds["publication"] = self.deadline
            return 1
    def probe(service, end):
        bounds["acquisition"] = end
        return {"observed_at": he.timestamp(now), "reachable": False, "http_status": 503}
    readers = {"native": lambda: {}, "managed": lambda: {"series": []}, "probe": probe,
        "fleet": {"inventory": lambda *args: {"complete": True, "instances": {}}, "recent": lambda *args: {"nodes": {}}}}
    result = hs.produce_snapshot(SimpleNamespace(payload={"source_id": configuration["source_id"], "slot": int(now.timestamp())//300}),
        readers=readers, connect=lambda **kwargs: Redis(kwargs["deadline"]), configuration_reader=lambda end: configuration,
        reserve=lambda *args: 1, guard=lambda *args: nullcontext(), now=now)
    publication = json.loads(next(value for key, value in stored.items() if key.endswith(":current")))
    assert result["status"] == "published" and 253 <= result["accounting"]["redis_commands"] <= 256, "acquisition command exhaustion consumed the reserved atomic-publication allowance"
    assert bounds["publication"]-bounds["acquisition"] == 10, "source collection had no reserved finalization time before the whole-cycle deadline"
    assert next(row for row in publication["components"] if row["kind"] == "api")["availability"] == "down" and not publication["coverage"]["complete"], "budget exhaustion discarded completed independent evidence or established complete health"
