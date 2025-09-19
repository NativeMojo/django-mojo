from mojo.apps.jobs.models import Job
from django.utils import timezone
from datetime import timedelta
from django.conf import settings


def prune_jobs(job):
    qset = Job.objects.filter(
        created__lt=timezone.now() - timedelta(days=7))
    qset.delete()
