"""Real producer SQL accounting with test-owned primary/Redis boundaries."""
import json
import time
from threading import Event
from types import SimpleNamespace
from unittest import mock

from testit import helpers as th


def _produce(order, project_queries=2):
    from mojo.apps.account.services import health_evidence as he, health_snapshot as hs, project_health as ph, system_settings
    from test_account.health_snapshot import _identity
    now, connections, state, stored, budgets = he.utcnow(), [], {}, {}, []
    native_done, project_done = Event(), Event()
    config_sql = "SELECT (SELECT jsonb_object_agg"
    stored_config = {system_settings.INSTALLATION_UUID: "d496818a-06c7-45f5-b165-8993d93b3a47",
                     system_settings.INSTALLATION_SLUG: "testit"}
    class Connection:
        autocommit = True
        def __init__(self):
            self.sql, self.closed, self.cursor_closed, self.result = [], False, False, None
        def cursor(self):
            return self
        def execute(self, sql, params=None):
            self.sql.append(sql)
            if sql.startswith(config_sql):
                self.result = (stored_config, [])
            elif "pg_stat_database" in sql:
                self.result = ({"reachable": True, "incarnation": "one"}, [], [], he.utcnow(), 1)
            elif sql.startswith("SELECT id, value, is_secret"):
                self.result = (1, json.dumps(state), False)
            elif sql.startswith("UPDATE"):
                state.clear()
                state.update(json.loads(params[0]))
            elif sql.startswith("SELECT value"):
                self.result = (json.dumps(state),)
            return self
        def fetchone(self):
            return self.result
        def close(self):
            if self.cursor_closed:
                self.closed = True
            self.cursor_closed = True
    def connect(deadline):
        value = Connection()
        connections.append(value)
        return value
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
            if script == hs._READ_LUA:
                return ["", "missing", ""]
            if script == hs._RESERVE_LUA:
                stored[keys[1]] = values[1]
            elif script == hs._PUBLISH_LUA:
                stored[keys[2]] = values[2]
            return 1
    def collect(check, deadline, budget):
        if order == "native_first":
            assert native_done.wait(2), "native-first fixture did not finish the grouped reader"
        connection = connect(deadline)
        try:
            for _ in range(project_queries):
                budget.take("db_queries")
                connection.execute("SELECT 1")
            return {"outcome": "pass", "observed_at": he.timestamp(he.utcnow()), "reason": None}
        finally:
            connection.close()
            connection.close()
            project_done.set()
    module = SimpleNamespace(configuration=lambda *args: {"mcp": {}}, collect=collect)
    get_static = lambda key, default=None: {"MOJO_ENVIRONMENT": "production", "JOBS_CHANNELS": ["testit_budget"]}.get(key, default)
    real_config = he.source_configuration
    def configuration(deadline, **kwargs):
        budget = kwargs["budget"]
        if not budgets:
            budgets.append((budget, deadline))
        return real_config(deadline, **kwargs, get_static=get_static, project_reader=lambda end: ph.freeze(end,
            get_static=lambda key, default=None: {"module": "test_project.health", "checks": [{"id": "mcp", "impact": "availability"}]},
            loader=lambda name: module, budget=budget))
    def native():
        if order == "project_first":
            assert project_done.wait(2), "project-first fixture did not finish its credential queries"
        try:
            budget, deadline = budgets[0]
            return he.database_and_job_facts(["testit_budget"], deadline, budget=budget)
        finally:
            native_done.set()
    readers = {"native": native, "redis": lambda: {"native": {}, "queues": {}}, "managed": lambda: {"series": []},
        "project_loader": lambda name: module, "probe": lambda *args: {},
        "fleet": {"inventory": lambda *args: {"complete": True, "instances": {}}, "heartbeats": lambda end: [],
            "scheduler": lambda end: {}, "identity_cache": SimpleNamespace(read=lambda *args, **kwargs: None,
                write=lambda *args, **kwargs: True), "recent": lambda *args: {"nodes": {}}}}
    with mock.patch.object(he, "_primary_connection", side_effect=connect), mock.patch.object(he, "source_configuration", side_effect=configuration):
        result = hs.produce_snapshot(SimpleNamespace(payload={"source_id": _identity()["source_id"], "slot": int(now.timestamp()) // 300}),
            readers=readers, connect=lambda **kwargs: Redis(), now=now)
    publication = json.loads(next(value for key, value in stored.items() if key.endswith(":current")))
    return result, publication, connections


@th.django_unit_test()
def test_core_and_two_query_project_fit_actual_sql_budget_in_either_order(opts):
    for order in ("native_first", "project_first"):
        result, publication, connections = _produce(order)
        service = next(row for row in publication["components"] if row["component_id"] == "service:mcp")
        database = next(row for row in publication["components"] if row["component_id"] == "database:default")
        native = next(row for row in database["evidence"] if row["slot_id"] == "native")
        assert service["availability"] == "up", f"{order}: core SQL starved the actual two-query project callback"
        assert native["status"] == "ok" and native["value"]["reachable"] is True, f"{order}: project collection starved the grouped native SELECT"
        assert result["status"] == "published" and result["accounting"]["db_queries"] == 14, f"{order}: production-shaped collection/publication must cost14 of16 SQL operations"
        assert sum(len(row.sql) for row in connections) == 14, f"{order}: reported accounting must equal the actual SQL trace"
        initial = next(row for row in connections if row.sql and row.sql[0].startswith("SELECT (SELECT jsonb_object_agg"))
        grouped = next(row for row in connections if any("pg_stat_database" in sql for sql in row.sql))
        assert len(initial.sql) == len(grouped.sql) == 1 and initial.autocommit and grouped.autocommit, f"{order}: standalone readers must use real autocommit SELECTs"
        transactions = [row.sql for row in connections if row.sql and row.sql[0] == "BEGIN"]
        assert len(transactions) == 2 and all(len(sql) == 5 and sql[-1] == "COMMIT" for sql in transactions), f"{order}: reserve/publication transactions lost their five-operation fences"
        assert all(any("FOR UPDATE" in sql for sql in transaction) for transaction in transactions), f"{order}: durable generation/publication row locks were removed"
        assert any(any("FOR SHARE" in sql for sql in transaction) for transaction in transactions), f"{order}: publication no longer locks the configuration recheck"
        assert all(row.closed and row.cursor_closed for row in connections), f"{order}: the cycle leaked private DB resources"


@th.django_unit_test()
def test_genuine_shared_sql_exhaustion_preserves_unknown_and_finalization(opts):
    result, publication, connections = _produce("native_first", project_queries=5)
    service = next(row for row in publication["components"] if row["component_id"] == "service:mcp")
    check = service["evidence"][0]
    assert check["reason"] == "not_collected_budget" and service["status"] == "unknown", "genuine SQL exhaustion must remain a bounded Unknown, never a pass"
    assert result["status"] == "published" and result["accounting"]["db_queries"] == 16, "project exhaustion spent the five SQL operations reserved for finalization"
    assert sum(len(row.sql) for row in connections) == 16 and all(row.closed for row in connections), "budget refusal hid SQL operations or leaked private resources"
