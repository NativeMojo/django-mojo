"""
docit_kb scheduled work.

Every function here is a THIN DISPATCHER: it publishes a job and returns. The
real work belongs on a job runner, which is where retries, timeouts and job
visibility live — a cron function runs synchronously on whatever process the
cron matcher fires on, so anything slow or database-bound occupies that process
for its whole duration. Same shape as every other app's cronjobs module.
"""

from mojo.decorators.cron import schedule
from mojo.apps import jobs
from mojo.helpers import logit
from mojo.helpers.settings import settings


RECONCILE_JOB = "mojo.apps.docit_kb.asyncjobs.reconcile_embeddings"
# One sweep per cron period; must match the @schedule interval below.
RECONCILE_SWEEP_BUCKET_SEC = 900


# The cron field values MUST be strings. `minutes=15` raises a TypeError inside
# the matcher and the job then silently never runs — see mojo/helpers/cron.py
# and the same warning in mojo/apps/dnsman/cronjobs.py.
@schedule(minutes="*/15")
def reconcile_embeddings():
    """
    Queue the sweep that heals pages whose knowledge-base chunks fell behind.

    The embed publish on Page.save is fire-and-forget, so a queue outage drops
    re-embeds silently; the sweep is what notices and re-queues them.

    The sweep-level idempotency key is best-effort multi-host dedupe — two
    hosts straddling a bucket boundary may double-fire, which is harmless
    because the sweep's own per-page hourly keys dedupe the real work. A sweep
    that fails is simply retried by the next tick, so this dispatcher does not
    retry. The whole body is guarded: cron's run_now() is unguarded, and a
    raising dispatcher aborts every later app's cron tick.
    """
    from django.utils import timezone

    try:
        if not settings.get("DOCIT_KB_RECONCILE_ENABLED", True, kind="bool"):
            return None
        bucket = int(timezone.now().timestamp() // RECONCILE_SWEEP_BUCKET_SEC)
        return jobs.publish(
            RECONCILE_JOB, {}, max_retries=0,
            idempotency_key=f"docit_kb.recon.sweep:{bucket}")
    except Exception as err:
        logit.error(f"docit_kb: reconcile sweep dispatch failed: {err}")
        return None
