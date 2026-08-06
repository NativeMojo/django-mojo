"""
edge background job handlers.

`install_generation` is published as a BROADCAST on the `edge` channel, so
every runner in the fleet converges its own node. That is the whole point:
nothing pushes to nodes, and a node that was down catches up on its next
convergence sweep (see cronjobs.py) without anyone maintaining an inventory.

An install is idempotent — a node already on the published generation does
nothing — so a duplicate broadcast, an overlapping cron sweep and a manual
trigger all cost one comparison.
"""

from mojo.helpers import logit


def install_generation(job):
    """Converge this node onto the desired state for a pool.

    A failure raises: the Certificate/Vhost rows are already the durable truth,
    and the raise is what makes a node that could not converge visible in the
    jobs surface as well as in the incident it already reported.
    """
    from mojo.apps.edge.services import installer

    payload = job.payload or {}
    pool = payload.get("pool") or "default"
    result = installer.install(pool=pool, force=bool(payload.get("force")))

    if not result.changed:
        return f"completed:unchanged={result.generation}"
    return (f"completed:generation={result.generation},"
            f"excluded={len(result.excluded or [])}")


def converge(job):
    """Cron entry point — the same install, on a timer.

    Separate from `install_generation` only so the jobs surface distinguishes
    'something changed and we were told' from 'the periodic sweep'. A node that
    missed a broadcast, booted from an AMI, or had its runner stopped converges
    here.
    """
    from mojo.apps.edge.services import installer

    pools = (job.payload or {}).get("pools") or ["default"]
    converged = []
    for pool in pools:
        try:
            result = installer.install(pool=pool)
            if result.changed:
                converged.append(pool)
        except Exception as err:
            # One pool's failure must not stop the sweep for the others — the
            # install already reported its own incident.
            logit.error(f"edge: convergence sweep failed for pool {pool}: {err}")
    return f"completed:converged={','.join(converged) if converged else 'none'}"
