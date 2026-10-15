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


@schedule(minutes="30", hours="4")
def retire_cdn_signing_keys():
    """Daily: rotated CloudFront signing keys leave the key group once every
    link they signed has expired (maestro #8250)."""
    jobs.publish(
        func="mojo.apps.fileman.asyncjobs.retire_cdn_signing_keys",
        channel="cleanup",
        payload={},
    )
