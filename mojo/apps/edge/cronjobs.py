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


# The cron field values MUST be strings. `minutes=10` raises a TypeError inside
# the matcher and the job then silently never runs — see mojo/helpers/cron.py.
@schedule(minutes="*/10")
def converge_edge():
    """Queue a convergence sweep on every runner in the fleet."""
    return jobs.publish(
        func=CONVERGE_JOB,
        payload={"pools": converge_pools()},
        channel=EDGE_CHANNEL,
        broadcast=True)
