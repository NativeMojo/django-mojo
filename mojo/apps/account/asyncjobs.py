from mojo.helpers import dates


def prune_notifications(job):
    from mojo.apps.account.models.notification import Notification
    Notification.objects.filter(expires_at__lt=dates.utcnow()).delete()
