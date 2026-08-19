"""Scheduled AWS maintenance checks.

`cron.load_app_cron()` imports this module automatically because
`mojo.apps.aws` is in INSTALLED_APPS. It swallows only ImportError, so any
other import-time exception here breaks cron loading fleet-wide — keep
module-level work to imports.
"""

from mojo.decorators.cron import schedule
from mojo.apps import jobs
from mojo.helpers.settings import settings


VERSION_DRIFT_JOB = "mojo.apps.aws.asyncjobs.check_version_drift"
INFRA_DRIFT_JOB = "mojo.apps.aws.asyncjobs.check_infra_drift"


def _setting(name, default=None):
    """Module-local settings read so tests can patch it without touching the
    process-wide settings object."""
    try:
        return settings.get_static(name, default)
    except Exception:
        return default


# Daily at 07:00 — inventory managed-service versions for major-version drift.
#
# Daily, not monthly, deliberately: run_scheduled_functions has no per-function
# try, so one sibling raising aborts the whole batch, and find_scheduled_functions
# is pure wall-clock matching with no last-run state and no catch-up. A monthly
# single-minute window would be the most miss-sensitive schedule in the repo
# guarding the slowest-moving deadline — one bad tick would cost 30 days. The
# scan is a handful of read-only API calls and the job is idempotent.
@schedule(minutes="0", hours="7")
def check_version_drift(force=False, verbose=False, now=None):
    # Read inside the function, never at import: a module-level read is frozen
    # at process start and cannot be exercised by a test.
    if not _setting("AWS_VERSION_DRIFT_ENABLED", False):
        return
    jobs.publish(func=VERSION_DRIFT_JOB, channel="cleanup", payload={})


# Daily at 07:20 — offset from the version-drift scan above so the two AWS
# reads never share a scheduler tick.
#
# The body is a settings read plus a publish and nothing else, deliberately:
# run_scheduled_functions wraps the WHOLE loop in one try with a re-raise, so
# anything that raises here aborts every other scheduled function on this tick,
# fleet-wide. The AWS calls belong in the job, where a failure costs one job.
@schedule(minutes="20", hours="7")
def check_infra_drift(force=False, verbose=False, now=None):
    # Read inside the function, never at import: a module-level read is frozen
    # at process start and cannot be exercised by a test.
    if not _setting("AWS_INFRA_DRIFT_ENABLED", False):
        return
    jobs.publish(func=INFRA_DRIFT_JOB, channel="cleanup", payload={})
