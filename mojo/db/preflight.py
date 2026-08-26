"""Ordinary-connection validation for a candidate API database pool."""

from collections import namedtuple


PoolPreflightResult = namedtuple(
    "PoolPreflightResult",
    "enabled required_connections budget max_connections nodes workers max_size destination",
)


class PoolPreflightError(RuntimeError):
    """A pool candidate cannot be proven safe for activation."""


def _positive_int(value, name):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PoolPreflightError(f"{name} must be a positive integer")
    return value


def validate_pool_preflight(plan, observed):
    """Validate candidate settings against facts from the default destination."""
    if not plan.get("enabled"):
        return PoolPreflightResult(False, 0, 0, 0, 0, 0, 0, "disabled")
    if not plan.get("valid"):
        raise PoolPreflightError("invalid pool plan: " + "; ".join(plan.get("errors") or ()))
    if plan.get("role") != "preflight" or plan.get("launcher") != "preflight":
        raise PoolPreflightError("pool preflight must run as role=preflight launcher=preflight")
    if tuple(plan.get("aliases") or ()) != ("default",):
        raise PoolPreflightError("pool preflight supports only the default alias")
    if observed.get("connection_is_pooled"):
        raise PoolPreflightError("preflight default connection must be ordinary, not pooled")

    expected_nodes = _positive_int(plan.get("node_count"), "configured node count")
    expected_workers = _positive_int(plan.get("api_workers"), "configured API worker count")
    rendered_nodes = _positive_int(observed.get("rendered_nodes"), "rendered node count")
    live_nodes = _positive_int(observed.get("live_nodes"), "live node count")
    rendered_workers = _positive_int(observed.get("rendered_workers"), "rendered API worker count")
    live_workers = _positive_int(observed.get("live_workers"), "live API worker count")
    if rendered_nodes != expected_nodes or live_nodes != expected_nodes:
        raise PoolPreflightError(
            f"node topology mismatch: configured={expected_nodes} rendered={rendered_nodes} live={live_nodes}")
    if rendered_workers != expected_workers or live_workers != expected_workers:
        raise PoolPreflightError(
            "API worker topology mismatch: "
            f"configured={expected_workers} rendered={rendered_workers} live={live_workers}")

    expected_destination = dict(plan.get("destination") or {})
    observed_destination = observed.get("destination") or {}
    for key in ("engine", "host", "port", "name"):
        if str(observed_destination.get(key, "")) != str(expected_destination.get(key, "")):
            raise PoolPreflightError(f"default destination {key} does not match the captured plan")
    if str(observed.get("database_name", "")) != str(expected_destination.get("name", "")):
        raise PoolPreflightError("connected database name does not match DATABASES.default")
    expected_port = str(expected_destination.get("port", "") or "5432")
    if str(observed.get("database_port", "")) != expected_port:
        raise PoolPreflightError("connected database port does not match DATABASES.default")

    max_connections = _positive_int(observed.get("max_connections"), "database max_connections")
    max_size = _positive_int(plan.get("options", {}).get("max_size"), "pool max_size")
    required = expected_nodes * expected_workers * max_size
    budget = max_connections * 60 // 100
    if required > budget:
        raise PoolPreflightError(
            f"pool requires {required} connections but the 60% database budget is {budget}")
    return PoolPreflightResult(
        True, required, budget, max_connections, expected_nodes,
        expected_workers, max_size, expected_destination.get("host", ""),
    )


def inspect_default_connection(connection):
    """Read capacity and destination facts through one ordinary default alias."""
    settings_dict = connection.settings_dict
    pool = getattr(connection, "pool", None)
    with connection.cursor() as cursor:
        cursor.execute("SHOW max_connections")
        max_connections = int(cursor.fetchone()[0])
        cursor.execute("SELECT current_database(), inet_server_port()")
        database_name, database_port = cursor.fetchone()
    return {
        "connection_is_pooled": pool is not None,
        "max_connections": max_connections,
        "database_name": database_name,
        "database_port": database_port,
        "destination": {
            "engine": settings_dict.get("ENGINE", ""),
            "host": settings_dict.get("HOST", ""),
            "port": str(settings_dict.get("PORT", "") or ""),
            "name": settings_dict.get("NAME", ""),
        },
    }


def run_default_preflight(plan, connection, topology):
    """Inspect default once, merge operator-proven topology, and validate."""
    if not plan.get("enabled"):
        return validate_pool_preflight(plan, {})
    if not plan.get("valid"):
        raise PoolPreflightError("invalid pool plan: " + "; ".join(plan.get("errors") or ()))
    if plan.get("role") != "preflight" or plan.get("launcher") != "preflight":
        raise PoolPreflightError("pool preflight must run as role=preflight launcher=preflight")
    if tuple(plan.get("aliases") or ()) != ("default",):
        raise PoolPreflightError("pool preflight supports only the default alias")
    observed = inspect_default_connection(connection)
    observed.update(topology)
    return validate_pool_preflight(plan, observed)
