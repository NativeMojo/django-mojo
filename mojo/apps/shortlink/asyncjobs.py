from datetime import timedelta
from mojo.helpers import dates, logit


GRACE_PERIOD_DAYS = 7


def prune_expired_shortlinks(job):
    """Delete expired, unprotected shortlinks that have been expired for 7+ days."""
    from mojo.apps.shortlink.models import ShortLink

    cutoff = dates.utcnow() - timedelta(days=GRACE_PERIOD_DAYS)
    qset = ShortLink.objects.filter(
        expires_at__lt=cutoff,
        is_protected=False,
    )
    count = qset.count()
    if count > 0:
        qset.delete()
        logit.info(f"shortlink: pruned {count} expired links")
    return f"completed:deleted={count}"
