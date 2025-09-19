from mojo.apps.logit.models import Log
from django.utils import timezone
from datetime import timedelta
from django.conf import settings

# Default: check once an hour at minute 0 (can be overridden in settings)
LOGIT_PRUNE_DAYS = getattr(settings, "LOGIT_PRUNE_DAYS", 90)
PRUNE_KINDS = ["request", "response"]

def prune_logit_logs(job):
    qset = Log.objects.filter(created__lt=timezone.now() - timedelta(days=LOGIT_PRUNE_DAYS), kind__in=PRUNE_KINDS)
    qset.delete()
