"""
Sysinfo Task - Broadcast-execute callable for collecting host info from runners.

Used by jobs.get_sysinfo() to gather system information from one or all runners
via broadcast_execute / execute_on_runner.
"""
from mojo.helpers.sysinfo import get_host_info


def collect_sysinfo(data):
    """
    Collect host system info from the current runner process.

    Called on each runner via broadcast_execute or execute_on_runner.
    The `data` argument is passed by the jobs control channel but is unused.

    Returns:
        dict: Result of sysinfo.get_host_info() — OS, CPU, memory, disk, network.
    """
    return get_host_info()

def collect_diagnostics(data):
    """Fixed checked collector. Local receipt evidence has activation scope only."""
    import socket
    from datetime import datetime, timezone
    from mojo.helpers.sysinfo import get_host_diagnostics
    from mojo.apps.edge.services.readiness import local_node_proof
    result = {"hostname": socket.gethostname().lower(),
              "observed_at": datetime.now(timezone.utc).isoformat(),
              "current": get_host_diagnostics(), "proof": None,
              "activation": {"status": "unavailable", "reason": "activation_unpublished",
                             "scope": "configuration_activation"}}
    try:
        result["proof"] = local_node_proof()
    except Exception:
        result["proof_status"] = "node_proof_unavailable"
    # Read bounded local files only. The full activation report performs
    # subprocess/dependency probes; on-demand host collection never starts those.
    revision = (data or {}).get("revision")
    if revision:
        try:
            from mojo.deploy import fleet_config_node, fleet_config_role
            receipt = fleet_config_node.read_receipt()
            digest = fleet_config_node.current_digest()
            role = fleet_config_role.read(revision, digest)
            if receipt.get("installed_revision") == revision and role:
                result["activation"] = {
                    "status": "ok", "reason": None, "scope": "configuration_activation",
                    "revision": revision, "receipt_status": receipt.get("status"),
                    "request_service_required": role["request_service_required"],
                    "observed_at": result["observed_at"],
                    "health": "unknown", "health_reason": "receipt_is_not_current_service_proof"}
            else:
                result["activation"]["reason"] = "activation_unbound"
        except Exception:
            result["activation"]["reason"] = "activation_unavailable"
    return result
