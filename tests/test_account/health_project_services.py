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
