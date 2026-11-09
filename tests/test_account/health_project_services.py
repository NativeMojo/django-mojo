"""Trusted project callbacks share the existing publication and freshness rules."""
import copy
import json
import time
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from testit import helpers as th

NOW = datetime(2026, 10, 22, 2, 0, tzinfo=timezone.utc)
MODULE = "test_project.health"


def _declaration(*checks):
    return {"module": MODULE, "checks": list(checks or [{"id": "mcp", "impact": "availability"}])}


def _project(declared=None, values=None, loader=None, clock=None):
    from mojo.apps.account.services import project_health as ph
    declared = declared if declared is not None else _declaration()
    module = SimpleNamespace(configuration=lambda checks, end, budget: values if values is not None else {
        row["id"]: {"origin": "https://example.com"} for row in checks}, collect=lambda *args: None)
    return ph.freeze(10, get_static=lambda key, default=None: declared,
                     loader=loader or (lambda name: module), clock=clock or (lambda: 0))


def _configuration(project=None):
    from mojo.apps.account.services import health_evidence as he, system_settings
    cursor = SimpleNamespace(execute=lambda *args: None, fetchone=lambda: ({
        system_settings.INSTALLATION_UUID: "d496818a-06c7-45f5-b165-8993d93b3a47",
        system_settings.INSTALLATION_SLUG: "testit-project-health"}, []))
    return he.source_configuration(time.monotonic() + 3, cursor_context=lambda end: nullcontext(cursor),
        get_static=lambda key, default=None: {"MOJO_ENVIRONMENT": "testit", "JOBS_CHANNELS": ["testit_project"]}.get(key, default),
        project_reader=lambda end: project)


def _components(project, results=None, previous=None):
    from mojo.apps.account.services import health_evidence as he
    config = _configuration(project)
    return [row for row in he.build_components(config, {}, {}, {"rows": [], "sources": {},
        "expected": {"complete": True}, "roster_complete": True}, {}, project_results=results,
        previous=previous, now=NOW) if row["kind"] == "service"]


def _publication(project, results=None):
    from mojo.apps.account.services import health_evidence as he, health_snapshot as hs
    config = _configuration(project)
    identity = {key: config[key] for key in ("source_id", "source_identity")}
    return hs.publication(identity, 1, config["config_revision"], _components(project, results),
        generated_at=he.timestamp(NOW), published_at=he.timestamp(NOW), roster_revision=None)


def _result(outcome="pass", reason=None):
    return {"outcome": outcome, "observed_at": NOW.isoformat() if outcome != "unknown" else None, "reason": reason}


@th.django_unit_test()
def test_default_off_retains_existing_revision_input_and_limits(opts):
    from mojo.apps.account.services import health_evidence as he, health_snapshot as hs, project_health as ph
    from mojo.apps.aws.services import fleet_health
    assert ph.freeze(10, get_static=lambda key, default=None: default) is None, "absent static setting enabled project checks"
    config = _configuration()
    original = {"roster_scope": fleet_health.roster_scope(config["expected"]), "services": config["services"],
                "channels": config["channels"], "bindings": config["bindings"]}
    assert "project_services" not in config and config["config_revision"] == hs.configuration_revision(config, original), "default-off changed existing configuration hashes"
    assert hs.MAX_COMPONENTS == 37 and hs.MAX_BYTES == 512 * 1024 and hs.MAX_SLOTS == 2048 and he.MAX_CONDITIONS == 1024, "project support widened unrelated publication caps"
    assert he.CycleBudget(10, clock=lambda: 0).limits["project_services"] == 8, "project callback accounting has no closed eight-check ceiling"


@th.django_unit_test()
def test_invalid_declarations_remain_one_bounded_unknown_obligation(opts):
    for declared in ({}, {"module": MODULE, "checks": []}, _declaration(*[{"id": "mcp", "impact": "availability"}] * 2),
                     _declaration({"id": "project-checks", "impact": "operational"}),
                     _declaration({"id": "MixedCase", "impact": "availability"}),
                     _declaration({"id": "a" * 65, "impact": "availability"}),
                     _declaration({"id": "mcp", "impact": "availability", "module": "untrusted.path"}),
                     _declaration(*[{"id": "check-%d" % index, "impact": "availability"} for index in range(9)])):
        project = _project(declared)
        rows = _components(project)
        assert len(rows) == 1 and rows[0]["component_id"] == "service:project-checks", "malformed roster silently disappeared or escaped its bounded sentinel"
        assert rows[0]["status"] == "unknown" and not rows[0]["coverage"]["complete"], "invalid project roster established healthy coverage"


@th.django_unit_test()
def test_module_configuration_failure_retains_exact_declared_ids(opts):
    declared = _declaration({"id": "mcp", "impact": "availability"}, {"id": "sites-dns", "impact": "operational"})
    def unavailable(name):
        raise ImportError("private provider data")
    for loader in (unavailable, lambda name: SimpleNamespace(configuration=None, collect=None)):
        project = _project(declared, loader=loader)
        rows = _components(project)
        assert {row["service_id"] for row in rows} == {"mcp", "sites-dns"}, "module failure omitted declared service obligations"
        assert all(row["status"] == "unknown" and row["observed_at"] is None for row in rows), "module failure manufactured a valid observation"
        assert "private provider data" not in json.dumps(rows), "import exception details escaped project evidence"
    invalid_module = _project({**declared, "module": "https://untrusted.example/check"}, loader=unavailable)
    assert invalid_module["module"] is None and len(invalid_module["checks"]) == 2, "invalid executable name was accepted or removed its valid roster"


@th.django_unit_test()
def test_configuration_bounds_exact_ids_and_actual_routing_fingerprint(opts):
    first = _project(values={"mcp": {"origin": "https://one.example"}})
    second = _project(values={"mcp": {"origin": "https://two.example"}})
    assert first["fingerprint"] != second["fingerprint"], "actual routing configuration did not change project identity"
    assert _configuration(first)["config_revision"] != _configuration(second)["config_revision"], "project configuration was excluded from publication revision"
    for values in ({}, {"mcp": {}, "extra": {}}, {"mcp": "x" * 4097}, {"mcp": float("nan")},
                   {"mcp": {1: "invalid JSON key"}}, {"mcp": object()}):
        project = _project(values=values)
        assert project["reason"] == "project_configuration_unavailable" and project["checks"][0]["config"] is None, "malformed/nonfinite/oversize/missing config became usable evidence"
    eight = _declaration(*[{"id": "check-%d" % index, "impact": "availability"} for index in range(8)])
    total = _project(eight, values={row["id"]: "x" * 4094 for row in eight["checks"]})
    assert total["reason"] == "project_configuration_unavailable", "eight individually bounded configs escaped the total32KiB cap"
    elapsed = [0]
    def late(checks, end, budget):
        elapsed[0] = end
        return {"mcp": {}}
    project = _project(loader=lambda name: SimpleNamespace(configuration=late, collect=lambda *args: None), clock=lambda: elapsed[0])
    assert project["reason"] == "project_configuration_late", "configuration returning at its absolute deadline was accepted"


@th.django_unit_test()
def test_collect_uses_frozen_check_actual_deadline_and_shared_budget(opts):
    from mojo.apps.account.services import health_evidence as he, project_health as ph
    project, elapsed, calls = _project(values={"mcp": {"paths": ["/mcp"]}}), [2], []
    budget = he.CycleBudget(20, clock=lambda: elapsed[0])
    def collect(check, end, actual_budget):
        calls.append((end, actual_budget))
        with th.assert_raises(TypeError):
            check["id"] = "other"
        with th.assert_raises(TypeError):
            check["config"]["paths"][0] = "/other"
        return _result()
    module = SimpleNamespace(configuration=lambda *args: {}, collect=collect)
    result = ph.collect(project, project["checks"][0], 6, budget, loader=lambda name: module,
                        clock=lambda: elapsed[0], now=lambda: NOW)
    assert result == _result() and calls == [(6, budget)], "callback did not use the actual remaining acquisition deadline/shared budget"
    assert budget.counts == {"project_services": 1}, "callback execution was not charged once to its project allowance"
    ph.collect(project, project["checks"][0], 20, budget, loader=lambda name: module, clock=lambda: elapsed[0], now=lambda: NOW)
    assert calls[-1][0] == 7, "callback deadline exceeded five seconds per check"


@th.django_unit_test()
def test_collect_rejects_late_missing_malformed_and_unsanitized_results(opts):
    from mojo.apps.account.services import health_evidence as he, project_health as ph
    project = _project()
    for raw in (None, {**_result(), "provider_body": "private"}, _result("unknown") | {"observed_at": NOW.isoformat()},
                _result() | {"observed_at": "2026-10-22T02:00:00"}, _result() | {"reason": "raw provider secret"},
                _result() | {"observed_at": (NOW + timedelta(seconds=31)).isoformat()}, {"outcome": "pass"}):
        module = SimpleNamespace(configuration=lambda *args: {}, collect=lambda *args: raw)
        result = ph.collect(project, project["checks"][0], 10, he.CycleBudget(10, clock=lambda: 0),
            loader=lambda name: module, clock=lambda: 0, now=lambda: NOW)
        assert result["outcome"] == "unknown" and result["observed_at"] is None, "invalid callback result produced causal evidence"
        assert "private" not in json.dumps(result) and "raw provider secret" not in json.dumps(result), "untrusted callback detail escaped the fixed result contract"
    elapsed = [0]
    def late(*args):
        elapsed[0] = 5
        return _result("fail")
    module = SimpleNamespace(configuration=lambda *args: {}, collect=late)
    result = ph.collect(project, project["checks"][0], 10, he.CycleBudget(10, clock=lambda: elapsed[0]),
        loader=lambda name: module, clock=lambda: elapsed[0], now=lambda: NOW)
    assert result == {"outcome": "unknown", "observed_at": None, "reason": "project_check_late"}, "late failing result fabricated a causal outage"
    exhausted = he.CycleBudget(10, limits={"project_services": 0}, clock=lambda: 0)
    result = ph.collect(project, project["checks"][0], 10, exhausted, clock=lambda: 0, now=lambda: NOW)
    assert result["reason"] == "not_collected_budget", "exhausted project allowance silently dropped its obligation"


@th.django_unit_test()
def test_project_pool_publishes_independent_callback_outcomes(opts):
    from mojo.apps.account.services import health_evidence as he, health_snapshot as hs
    project = _project(_declaration({"id": "mcp", "impact": "availability"}, {"id": "sites-dns", "impact": "operational"}))
    config, now, stored, callback_threads = _configuration(project), he.utcnow(), {}, []
    class Redis:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def set(self, key, value, **kwargs):
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
            return 1
    def collect(check, end, budget):
        from threading import current_thread
        callback_threads.append(current_thread().name)
        if check["id"] == "sites-dns":
            raise RuntimeError("private resolver exception")
        return {"outcome": "fail", "observed_at": now.isoformat(), "reason": "protocol_invalid"}
    module = SimpleNamespace(configuration=lambda *args: {}, collect=collect)
    readers = {"native": lambda: {}, "redis": lambda: {"native": {}, "queues": {}}, "managed": lambda: {"series": []},
        "project_loader": lambda name: module, "probe": lambda *args: {},
        "fleet": {"inventory": lambda *args: {"complete": True, "instances": {}}, "heartbeats": lambda end: [],
            "scheduler": lambda end: {}, "identity_cache": SimpleNamespace(read=lambda *args, **kwargs: None,
                write=lambda *args, **kwargs: True), "recent": lambda *args: {"nodes": {}}}}
    result = hs.produce_snapshot(SimpleNamespace(payload={"source_id": config["source_id"], "slot": int(now.timestamp()) // 300}),
        readers=readers, connect=lambda **kwargs: Redis(), configuration_reader=lambda end: config,
        reserve=lambda *args: 1, guard=lambda *args: nullcontext(), now=now)
    value = json.loads(next(raw for key, raw in stored.items() if key.endswith(":current")))
    services = {row["resource_id"]: row for row in value["components"] if row["kind"] == "service"}
    assert result["status"] == "published" and result["accounting"]["project_services"] == 2, "project checks escaped the ordinary bounded producer/accounting path"
    assert len(callback_threads) == 2 and all("ThreadPoolExecutor" in name for name in callback_threads), "project callbacks did not use the existing source worker pool"
    assert services["mcp"]["availability"] == "down" and services["sites-dns"]["status"] == "unknown", "one failed callback discarded independent proved service failure"
    assert "private resolver exception" not in json.dumps(value), "raw callback exceptions reached the cached publication"


def _produce_acquisition(project, collect, readers, *, config=None, clock=None):
    from mojo.apps.account.services import health_evidence as he, health_snapshot as hs
    config, now, stored = config or _configuration(project), he.utcnow(), {}
    trace = {"configuration": [], "guard": [], "opened": 0, "closed": 0}
    class Redis:
        def __enter__(self):
            trace["opened"] += 1
            return self
        def __exit__(self, *args):
            trace["closed"] += 1
        def set(self, key, value, **kwargs):
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
            return 1
    def configuration(end):
        trace["configuration"].append(end)
        return config
    def guard(source_id, generation, revision, end):
        trace["guard"].append(end)
        return nullcontext()
    module = SimpleNamespace(configuration=lambda *args: {}, collect=collect)
    sources = {"native": lambda: {}, "redis": lambda: {"native": {}, "queues": {}},
        "managed": lambda: {"series": []}, "project_loader": lambda name: module,
        "probe": lambda *args: {}, "fleet": {
            "inventory": lambda *args: {"complete": True, "instances": {}}, "heartbeats": lambda end: [],
            "scheduler": lambda end: {}, "identity_cache": SimpleNamespace(read=lambda *args, **kwargs: None,
                write=lambda *args, **kwargs: True), "recent": lambda *args: {"nodes": {}}}}
    sources.update(readers)
    result = hs.produce_snapshot(SimpleNamespace(payload={"source_id": config["source_id"], "slot": int(now.timestamp()) // 300}),
        readers=sources, connect=lambda **kwargs: Redis(), configuration_reader=configuration,
        reserve=lambda *args: 1, guard=guard, now=now, monotonic=clock)
    publication = json.loads(next(raw for key, raw in stored.items() if key.endswith(":current")))
    return result, publication, trace


@th.django_unit_test()
def test_project_cleanup_and_other_sources_finish_before_fleet_acquisition(opts):
    from threading import Event, current_thread
    from mojo.apps.account.services import health_evidence as he
    project = _project(_declaration({"id": "mcp", "impact": "availability"}, {"id": "sites-dns", "impact": "operational"}))
    entered = Event()
    finished = {key: Event() for key in ("native", "redis", "http", "mcp", "sites-dns")}
    waits, fleet_starts, workers = [], [], []
    def source(name):
        workers.append(current_thread())
        try:
            waits.append(entered.wait(2))
            if name == "redis":
                raise RuntimeError("private redis exception")
            return {"reachable": False, "latency_seconds": 0, "observed_at": he.timestamp(he.utcnow())}
        finally:
            finished[name].set()
    def collect(check, end, budget):
        workers.append(current_thread())
        entered.set()
        try:
            if check["id"] == "sites-dns":
                raise RuntimeError("private project exception")
            return {"outcome": "fail", "observed_at": he.timestamp(he.utcnow()), "reason": "protocol_invalid"}
        finally:
            finished[check["id"]].set()
    def managed():
        workers.append(current_thread())
        fleet_starts.append({key: event.is_set() for key, event in finished.items()})
        return {"series": []}
    result, value, trace = _produce_acquisition(project, collect, {
        "native": lambda: source("native"), "redis": lambda: source("redis"),
        "probe": lambda *args: source("http"), "managed": managed})
    assert len(fleet_starts) == 1 and all(fleet_starts[0].values()), "fleet acquisition overlapped unfinished source work or project cleanup"
    assert all(waits), "the original nonfleet sources lost concurrency with project checks"
    components = {row["component_id"]: row for row in value["components"]}
    assert components["service:mcp"]["availability"] == "down" and components["api:primary"]["availability"] == "down", "waiting flattened independently proved project or HTTP failures"
    assert components["service:sites-dns"]["status"] == components["redis:default"]["status"] == "unknown", "a phase-one exception skipped collection or became healthy"
    assert result["status"] == "published" and result["accounting"]["project_services"] == 2, "phase-one exceptions prevented independent fleet collection/publication or changed project accounting"
    assert len({worker.name.rsplit("_", 1)[0] for worker in workers}) == 1 and all(not worker.is_alive() for worker in workers), "acquisition created another pool or left source workers running"
    assert trace["opened"] == trace["closed"] and "private" not in json.dumps(value), "the cycle leaked cache resources or private exception text"


@th.django_unit_test()
def test_default_off_keeps_fleet_ahead_of_queued_http_probes(opts):
    from threading import Event
    from mojo.apps.account.services import health_snapshot as hs
    from mojo.apps.aws.services import fleet_health
    config, fleet_entered, waits, calls = _configuration(), Event(), [], []
    config["services"] = [{**config["services"][0], "resource_id": "probe-%d" % index,
        "service_id": "probe-%d" % index} for index in range(4)]
    config["config_revision"] = hs.configuration_revision(config, {
        "roster_scope": fleet_health.roster_scope(config["expected"]), "services": config["services"],
        "channels": config["channels"], "bindings": config["bindings"]})
    def source(value):
        waits.append(fleet_entered.wait(2))
        return value
    def managed():
        calls.append("fleet")
        fleet_entered.set()
        return {"series": []}
    result, value, trace = _produce_acquisition(None, None, {
        "native": lambda: source({}), "redis": lambda: source({"native": {}, "queues": {}}),
        "probe": lambda *args: source({}), "managed": managed}, config=config)
    assert calls == ["fleet"] and len(waits) == 6 and all(waits), "default-off fleet moved behind HTTP work or was collected more than once"
    assert result["status"] == "published" and "project_services" not in result["accounting"], "default-off scheduling introduced project collection"
    assert trace["opened"] == trace["closed"], "default-off acquisition leaked cache resources"


@th.django_unit_test()
def test_deferred_fleet_keeps_original_deadline_and_independent_failure(opts):
    from mojo.apps.account.services import health_evidence as he
    from mojo.helpers.bounded_transport import remaining
    project, base, observed, deadlines, external_calls, budgets = _project(), time.monotonic(), he.timestamp(he.utcnow()), [], [], []
    clock = [base]
    def collect(check, end, budget):
        deadlines.append(("project", end))
        budgets.append(budget)
        clock[0] = base + 4
        return {"outcome": "fail", "observed_at": observed, "reason": "protocol_invalid"}
    def managed():
        clock[0] = base + 50
        return {"series": []}
    def inventory(spec, end):
        deadlines.append(("fleet", end))
        remaining(end, clock=lambda: clock[0])
        external_calls.append("inventory")
        return {"complete": True, "instances": {}}
    result, value, trace = _produce_acquisition(project, collect, {
        "managed": managed, "fleet": {"inventory": inventory, "heartbeats": lambda end: [],
            "scheduler": lambda end: {}, "identity_cache": SimpleNamespace(read=lambda *args, **kwargs: None,
                write=lambda *args, **kwargs: True), "recent": lambda *args: {"nodes": {}}}}, clock=lambda: clock[0])
    components = {row["component_id"]: row for row in value["components"]}
    assert deadlines == [("project", base + 5), ("fleet", base + 50)], "a new wave reset the five-second callback or absolute collection deadline"
    assert trace["configuration"] == [base + 50] and trace["guard"] == [base + 60], "acquisition or publication gained a second producer deadline"
    assert not external_calls and components["fleet:installation"]["availability"] == "unknown", "expired remaining fleet time performed I/O or fabricated availability"
    assert components["service:mcp"]["availability"] == "down", "late independent fleet failure erased a valid project failure"
    sample = components["service:mcp"]["evidence"][0]
    assert sample["observed_at"] == observed and sample["fresh_until"] == (he.parse_time(observed) + timedelta(seconds=420)).isoformat(), "waiting renewed or replaced the actual project observation"
    assert budgets[0].deadline == base + 60 and budgets[0].limits == he.CycleBudget.LIMITS and budgets[0].reserved == {"redis_commands": 4, "db_queries": 5}, "deferral changed shared operation caps or finalization reserves"
    assert result["status"] == "published" and trace["opened"] == trace["closed"], "expired fleet evidence prevented bounded publication or cache cleanup"
