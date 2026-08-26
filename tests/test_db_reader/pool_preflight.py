"""Pure capacity and ordinary-connection preflight contracts."""

from testit import helpers as th


def _plan(max_size=7):
    return {
        "enabled": True,
        "valid": True,
        "errors": (),
        "role": "preflight",
        "launcher": "preflight",
        "aliases": ("default",),
        "options": {"min_size": 1, "max_size": max_size, "timeout": 5},
        "api_workers": 4,
        "node_count": 2,
        "destination": {
            "engine": "django.db.backends.postgresql",
            "host": "writer.internal",
            "port": "5432",
            "name": "mojoland",
        },
    }


def _observed(**overrides):
    observed = {
        "connection_is_pooled": False,
        "rendered_nodes": 2,
        "live_nodes": 2,
        "rendered_workers": 4,
        "live_workers": 4,
        "max_connections": 100,
        "database_name": "mojoland",
        "database_port": 5432,
        "destination": dict(_plan()["destination"]),
    }
    observed.update(overrides)
    return observed


def _expect_error(plan, observed, message):
    from mojo.db.preflight import PoolPreflightError, validate_pool_preflight

    try:
        validate_pool_preflight(plan, observed)
    except PoolPreflightError:
        return
    raise AssertionError(message)


@th.unit_test("pool preflight: disabled mode succeeds without database facts")
def test_disabled_preflight(opts):
    from mojo.db.preflight import validate_pool_preflight

    result = validate_pool_preflight({"enabled": False}, {})
    assert result.enabled is False and result.required_connections == 0, \
        f"disabled preflight must be a zero-cost success, got {result!r}"


@th.unit_test("pool preflight: exact 60 percent capacity boundary passes")
def test_capacity_boundary(opts):
    from mojo.db.preflight import validate_pool_preflight

    result = validate_pool_preflight(_plan(), _observed())
    assert result.required_connections == 56 and result.budget == 60, \
        f"two nodes x four workers x seven connections must fit 60/100, got {result!r}"

    exact = validate_pool_preflight(_plan(max_size=6), _observed(max_connections=80))
    assert exact.required_connections == exact.budget == 48, \
        f"the exact 60 percent boundary must pass, got {exact!r}"
    _expect_error(
        _plan(max_size=8), _observed(),
        "64 requested connections must fail a 60-connection budget",
    )


@th.unit_test("pool preflight: topology and destination drift fail closed")
def test_topology_and_destination_failures(opts):
    _expect_error(_plan(), _observed(live_nodes=1), "live node drift must fail")
    _expect_error(_plan(), _observed(rendered_workers=3), "rendered worker drift must fail")
    _expect_error(_plan(), _observed(connection_is_pooled=True), "pooled preflight must fail")
    _expect_error(_plan(), _observed(database_name="other"), "database name drift must fail")
    wrong_destination = dict(_plan()["destination"])
    wrong_destination["host"] = "reader.internal"
    _expect_error(
        _plan(), _observed(destination=wrong_destination),
        "configured destination drift must fail",
    )


class _Cursor:
    def __init__(self):
        self.query = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, query):
        self.query = query

    def fetchone(self):
        if self.query == "SHOW max_connections":
            return (100,)
        return ("mojoland", 5432)


class _Connection:
    pool = None
    settings_dict = dict(_plan()["destination"])
    settings_dict["ENGINE"] = settings_dict.pop("engine")
    settings_dict["HOST"] = settings_dict.pop("host")
    settings_dict["PORT"] = settings_dict.pop("port")
    settings_dict["NAME"] = settings_dict.pop("name")

    def cursor(self):
        return _Cursor()


@th.unit_test("pool preflight: facts come through the ordinary default connection")
def test_default_connection_inspection(opts):
    from mojo.db.preflight import run_default_preflight

    topology = {
        "rendered_nodes": 2,
        "live_nodes": 2,
        "rendered_workers": 4,
        "live_workers": 4,
    }
    result = run_default_preflight(_plan(), _Connection(), topology)
    assert result.enabled is True and result.max_connections == 100, \
        f"ordinary default inspection must feed the capacity proof, got {result!r}"
