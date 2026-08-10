"""
edge scheduled work.

A thin dispatcher, same shape as every other app's cronjobs module: publish and
return. The install itself is filesystem- and subprocess-bound and must not
occupy whatever process the cron matcher fires on.

**The sweep is what makes convergence a property rather than a hope.** The
broadcast in `services/certs.py` and the one this app publishes on a vhost
change are both best-effort: a node that was down, booted from an AMI, or had
its runner stopped never saw them. This timer is how it catches up without
anyone maintaining a node inventory.
"""

from mojo.decorators.cron import schedule
from mojo.apps import jobs
from mojo.helpers.settings import settings


CONVERGE_JOB = "mojo.apps.edge.asyncjobs.converge"
EDGE_CHANNEL = "edge"


def converge_pools():
    return settings.get("EDGE_POOLS", ["default"], kind="list") or ["default"]


def converge_enabled():
    """Whether this deployment runs the vhost-convergence sweep at all.

    `get_static` (settings-file-only) on purpose: this is deploy-time posture,
    and a DB-writable `Setting` row must not be able to switch fleet
    convergence off. Default True — existing edge deployments see no change.
    Deployments that install this app ONLY for the fleet-deploy plane (the
    django-mojo-skeleton pattern: `deploy_node`, `migrate_locked`,
    `deploy_status`, the webhook) set it False so the sweep never publishes
    onto an `edge` channel none of their runners consume.
    """
    return bool(settings.get_static("EDGE_CONVERGE_ENABLED", True))


# The cron field values MUST be strings. `minutes=10` raises a TypeError inside
# the matcher and the job then silently never runs — see mojo/helpers/cron.py.
@schedule(minutes="*/10")
def converge_edge():
    """Queue a convergence sweep on every runner in the fleet."""
    if not converge_enabled():
        return "disabled"
    return jobs.publish(
        func=CONVERGE_JOB,
        payload={"pools": converge_pools()},
        channel=EDGE_CHANNEL,
        broadcast=True)


@schedule(minutes="*/5")
def reconcile_platform_deployments():
    """Close durable attempts whose Redis coordination lease disappeared."""
    from mojo.apps.edge.services import platform_deploy
    return platform_deploy.reconcile_stale()
