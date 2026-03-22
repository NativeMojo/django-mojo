from mojo.helpers import dates


def prune_notifications(job):
    from mojo.apps.account.models.notification import Notification
    Notification.objects.filter(expires_at__lt=dates.utcnow()).delete()


def refresh_bouncer_sig_cache(job):
    """Scheduled job: rebuild Redis signature cache from active BotSignature records."""
    from mojo.apps.account.services.bouncer.learner import refresh_sig_cache
    refresh_sig_cache()
