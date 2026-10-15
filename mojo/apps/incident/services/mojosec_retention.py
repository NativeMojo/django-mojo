"""Age-bound retention for per-receipt MojoSec Events.

Every receipt projected outside a grouped case writes one Event with
``model_name="mojosec_sensor"``. The generic ``prune_events`` keeps every
level >= 6 row, and the noisiest MojoSec kinds are level 8, so those rows
were never removed. This module removes them once they are older than the
receipt retention, keeping any Event an Incident holds or whose receipt is
not finished. The daily job and ``prune_mojosec_events`` share one selector.
The daily job deletes only when ``MOJOSEC_EVENT_PRUNE_ENABLED`` is on and
otherwise logs that it is off; the command is not gated.
"""

from django.db import transaction
from django.db.models import Count, Max, Min

from mojo.helpers import dates
from mojo.helpers.settings import settings

SCOPE = "mojosec"
MODEL_NAME = "mojosec_sensor"


def _receipt_states():
    from mojo.apps.incident.models import MojoSecReceipt

    # Positive state lists: a negated filter such as "publish state is not
    # published" would also exclude Events whose receipt is already gone.
    unfinished_publish = (MojoSecReceipt.PUBLISH_PENDING, MojoSecReceipt.PUBLISH_DEAD)
    unfinished_handler = (
        MojoSecReceipt.HANDLER_PENDING, MojoSecReceipt.HANDLER_QUEUED,
        MojoSecReceipt.HANDLER_FAILED)
    return unfinished_publish, unfinished_handler


def cutoff_for(days=None, now=None):
    from mojo.apps.incident.services import mojosec

    if days is None:
        days = mojosec.receipt_retention_days()
    return (now or dates.utcnow()) - dates.timedelta(days=days)


def _aged_events(cutoff):
    from mojo.apps.incident.models import Event

    return Event.objects.filter(
        scope=SCOPE, model_name=MODEL_NAME, created__lt=cutoff)


def projected_events(cutoff):
    """Per-receipt Events older than ``cutoff`` that may be deleted."""
    unfinished_publish, unfinished_handler = _receipt_states()
    return _aged_events(cutoff).filter(incident__isnull=True).exclude(
        mojosec_receipts__publish_state__in=unfinished_publish,
    ).exclude(
        mojosec_receipts__handler_state__in=unfinished_handler,
    )


def kept_counts(cutoff):
    """Aged per-receipt Events the selector keeps, counted by reason."""
    from django.db.models import Q

    unfinished_publish, unfinished_handler = _receipt_states()
    aged = _aged_events(cutoff)
    unfinished = aged.filter(incident__isnull=True).filter(
        Q(mojosec_receipts__publish_state__in=unfinished_publish) |
        Q(mojosec_receipts__handler_state__in=unfinished_handler),
    ).values("pk").distinct()
    return {
        "has_incident": aged.filter(incident__isnull=False).count(),
        "receipt_not_finished": unfinished.count(),
    }


def summarize(cutoff):
    """Counts for a dry run: what would be deleted and what is kept."""
    selected = projected_events(cutoff)
    by_category = {
        row["category"]: row["count"]
        for row in selected.order_by().values("category").annotate(
            count=Count("pk"))
    }
    bounds = selected.aggregate(
        first_id=Min("pk"), last_id=Max("pk"),
        first_created=Min("created"), last_created=Max("created"))
    return {
        "cutoff": cutoff.isoformat(),
        "would_delete": sum(by_category.values()),
        "by_category": by_category,
        "range": {
            "first_id": bounds["first_id"],
            "last_id": bounds["last_id"],
            "first_created": (
                bounds["first_created"].isoformat()
                if bounds["first_created"] else None),
            "last_created": (
                bounds["last_created"].isoformat()
                if bounds["last_created"] else None),
        },
        "kept": kept_counts(cutoff),
    }


def prune_projected_events(job=None, now=None, batch=1000, max_batches=100,
                           days=None, limit=None, enabled=None):
    """Delete aged per-receipt Events; with a job, obey the daily switch.

    1.35.0 and 1.35.1 queued the 08:35 job under this path, and the job
    engine calls ``func(job)``, so a call with a job goes through
    ``daily_prune``. The command passes no job and stays ungated.
    """
    if job is not None:
        return daily_prune(job=job, now=now, days=days, enabled=enabled)
    return _prune_projected_events(
        job=job, now=now, batch=batch, max_batches=max_batches, days=days,
        limit=limit)


def _prune_projected_events(job=None, now=None, batch=1000, max_batches=100,
                            days=None, limit=None):
    """Delete aged per-receipt Events in bounded batches; return the count.

    Walks ids upward so a kept prefix is not re-read on every batch. Each
    batch, in its own transaction, locks its rows with the selector
    (``FOR UPDATE SKIP LOCKED``) and deletes only the rows it locked. The
    lock re-checks the selector against the newest row version, so an
    Incident linked after the ids were read keeps its Event; a row another
    transaction holds is skipped until the next run, so the job never waits
    on a writer. ``limit`` caps the total deleted (the command's
    ``--max-events``).
    """
    from mojo.apps.incident.models import Event

    cutoff = cutoff_for(days=days, now=now)
    deleted = 0
    last_id = 0
    capped = False
    for unused_index in range(max_batches):
        size = batch if limit is None else min(batch, limit - deleted)
        if size <= 0:
            capped = True
            break
        ids = list(projected_events(cutoff).filter(pk__gt=last_id).order_by(
            "pk").values_list("pk", flat=True)[:size])
        if not ids:
            break
        last_id = ids[-1]
        with transaction.atomic():
            locked = list(projected_events(cutoff).filter(pk__in=ids)
                          .select_for_update(skip_locked=True)
                          .values_list("pk", flat=True))
            unused_total, by_model = Event.objects.filter(
                pk__in=locked).delete()
        deleted += by_model.get(Event._meta.label, 0)
    else:
        capped = True
    if job is not None and hasattr(job, "add_log"):
        message = f"Pruned {deleted} per-receipt MojoSec Event rows"
        if capped and projected_events(cutoff).filter(pk__gt=last_id).exists():
            message += "; more remain past the batch ceiling"
        job.add_log(message)
    return deleted


def deletion_enabled():
    return settings.get_static("MOJOSEC_EVENT_PRUNE_ENABLED", False, kind="bool")


def daily_prune(job=None, now=None, days=None, enabled=None):
    """The 08:35 job: prune only when ``MOJOSEC_EVENT_PRUNE_ENABLED`` is on.

    While it is off, run no query, log that nothing was deleted and name the
    count-only preview a person reads before turning deletion on.
    """
    if enabled is None:
        enabled = deletion_enabled()
    if enabled:
        return _prune_projected_events(job=job, now=now, days=days)
    if job is not None and hasattr(job, "add_log"):
        job.add_log(
            "MojoSec Event prune is off (MOJOSEC_EVENT_PRUNE_ENABLED); deleted "
            "nothing. Preview with: manage.py prune_mojosec_events")
    return 0
