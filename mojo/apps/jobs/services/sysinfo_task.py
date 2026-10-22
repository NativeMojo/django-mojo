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
    """Fixed checked collector with separate activation and current serving proof."""
    from datetime import datetime, timezone
    from mojo.apps.jobs.job_engine import host_channel
    from mojo.helpers.sysinfo import get_host_diagnostics
    from mojo.apps.edge.services.readiness import local_node_proof
    result = {"hostname": host_channel(),
              "observed_at": datetime.now(timezone.utc).isoformat(),
              "current": get_host_diagnostics(), "proof": None,
              "activation": {"status": "unavailable", "reason": "activation_unpublished",
                             "scope": "configuration_activation"},
              "request_service": {"status": "unknown", "reason": "current_service_proof_unavailable"}}
    try:
        result["proof"] = local_node_proof()
    except Exception:
        result["proof_status"] = "node_proof_unavailable"
    # Reuse the fixed local serving proof, never the full convergence report
    # (which also performs jobs/scheduler/topic activation checks).
    revision = (data or {}).get("revision")
    if revision:
        try:
            from mojo.deploy import fleet_config_node, fleet_config_role
            receipt = fleet_config_node.read_receipt()
            digest = fleet_config_node.current_digest()
            role = fleet_config_role.read(revision, digest)
            if (receipt.get("installed_revision") == revision and digest
                    and receipt.get("installed_digest") == digest and role):
                result["activation"] = {
                    "status": "ok", "reason": None, "scope": "configuration_activation",
                    "revision": revision, "receipt_status": receipt.get("status"),
                    "request_service_required": role["request_service_required"],
                    "request_service_profile": role.get("request_service_profile"),
                    "observed_at": result["observed_at"],
                    "health": "unknown", "health_reason": "receipt_is_not_current_service_proof"}
                if role["request_service_required"] is True:
                    profile = role["request_service_profile"]
                    proof = fleet_config_node._serving_proof(revision, profile)
                    if proof.get("error_code") or type(proof.get("healthy")) is not bool:
                        result["request_service"]["reason"] = proof.get("error_code") or "proof_invalid"
                    else:
                        result["request_service"] = {
                            "status": "ok", "reason": None, "profile": profile,
                            "observed_at": datetime.now(timezone.utc).isoformat(),
                            **{key: proof.get(key) for key in ("node", "loaded_revision", "pid", "healthy")}}
            else:
                result["activation"]["reason"] = "activation_unbound"
        except Exception:
            result["activation"]["reason"] = "activation_unavailable"
    return result
