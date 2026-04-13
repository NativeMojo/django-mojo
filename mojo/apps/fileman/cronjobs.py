from mojo.decorators.cron import schedule
from mojo.apps import jobs


@schedule(minutes="0", hours="4")
def cleanup_expired_files():
    """Daily cleanup of files with expired metadata.expires_at."""
    jobs.publish(
        func="mojo.apps.fileman.asyncjobs.cleanup_expired_files",
        channel="cleanup",
        payload={},
    )
