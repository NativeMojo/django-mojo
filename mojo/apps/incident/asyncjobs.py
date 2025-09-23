from mojo.apps.incident.models import Event
from django.utils import timezone
from datetime import timedelta
from django.conf import settings

# Default: check once an hour at minute 0 (can be overridden in settings)
INCIDENT_EVENT_PRUNE_DAYS = getattr(settings, "INCIDENT_EVENT_PRUNE_DAYS", 30)


def prune_events(job):
    qset = Event.objects.filter(
        created__lt=timezone.now() - timedelta(days=INCIDENT_EVENT_PRUNE_DAYS),
        level__lt=6)
    qset.delete()


def example(job):
    job.add_log("This is an example job")
