"""Narrow, audit-preserving repair for the September 2026 broker flood."""

import datetime
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, F, Max, Min, Q
from django.utils import dateparse, timezone


BROKER_CATEGORY = "mojosec.auth.sudo_command"
BROKER_PATH = "/usr/local/sbin/mojo-firewall-broker"
PROOF_STATUS = "missing"
BATCH_SIZE = 5000


def _utc_timestamp(value, option):
    parsed = dateparse.parse_datetime(value)
    if (parsed is None or not timezone.is_aware(parsed) or
            parsed.utcoffset() != datetime.timedelta(0)):
        raise CommandError(f"{option} must be an aware UTC timestamp")
    return parsed.astimezone(datetime.timezone.utc)


def _fingerprint_events(sensor, since, before):
    from mojo.apps.incident.models import Event

    return Event.objects.filter(
        scope="mojosec",
        category=BROKER_CATEGORY,
        created__gte=since,
        created__lt=before,
        metadata__mojosec__sensor_id=sensor,
        metadata__mojosec__kind="auth.sudo_command",
        metadata__mojosec__evidence__command_path=BROKER_PATH,
        metadata__mojosec__evidence__proof_status=PROOF_STATUS,
    )


def _classified_events(sensor, since, before):
    from mojo.apps.incident.models import MojoSecReceipt

    terminal = (
        MojoSecReceipt.HANDLER_NONE,
        MojoSecReceipt.HANDLER_DISPATCHED,
        MojoSecReceipt.HANDLER_DEAD,
    )
    receipt_filter = Q(
        mojosec_receipts__publish_state=MojoSecReceipt.PUBLISH_PUBLISHED,
        mojosec_receipts__handler_state__in=terminal,
        mojosec_receipts__incident__isnull=True,
    )
    return _fingerprint_events(sensor, since, before).annotate(
        receipt_count=Count("mojosec_receipts", distinct=True),
        safe_receipt_count=Count(
            "mojosec_receipts", filter=receipt_filter, distinct=True),
    )


def _safe_events(sensor, since, before):
    return _classified_events(sensor, since, before).filter(
        incident__isnull=True,
        receipt_count__gt=0,
        receipt_count=F("safe_receipt_count"),
    )


def _delete_safe_batch(sensor, since, before, ids):
    """Lock and revalidate both sides of the audit relation before delete."""
    from mojo.apps.incident.models import Event, MojoSecReceipt

    expected = sorted(ids)
    with transaction.atomic():
        locked = sorted(Event.objects.select_for_update().filter(
            pk__in=expected).values_list("pk", flat=True))
        list(MojoSecReceipt.objects.select_for_update().filter(
            event_id__in=expected).order_by("pk").values_list("pk", flat=True))
        revalidated = sorted(_safe_events(sensor, since, before).filter(
            pk__in=expected).values_list("pk", flat=True))
        if locked != expected or revalidated != expected:
            raise CommandError(
                "refusing batch: Event/receipt safety changed after selection")
        unused_total, by_model = Event.objects.filter(pk__in=expected).delete()
        removed = by_model.get(Event._meta.label, 0)
        if removed != len(expected):
            raise CommandError("Event deletion count changed during apply")
    return removed


class Command(BaseCommand):
    help = "Dry-run or prune the exact missing-proof firewall-broker MojoSec flood"

    def add_arguments(self, parser):
        parser.add_argument("--sensor", required=True)
        parser.add_argument("--since", required=True)
        parser.add_argument("--before", required=True)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--max-events", type=int, default=None)

    def handle(self, *args, **options):
        sensor = options["sensor"]
        if not sensor or len(sensor) > 128:
            raise CommandError("--sensor must be 1-128 characters")
        since = _utc_timestamp(options["since"], "--since")
        before = _utc_timestamp(options["before"], "--before")
        if since >= before:
            raise CommandError("--since must be earlier than --before")
        if before > timezone.now():
            raise CommandError("--before may not be in the future")
        maximum = options["max_events"]
        if maximum is not None and maximum <= 0:
            raise CommandError("--max-events must be positive")
        if options["apply"] and maximum is None:
            raise CommandError("--apply requires --max-events")

        matched = _fingerprint_events(sensor, since, before)
        safe = _safe_events(sensor, since, before)
        matched_count = matched.count()
        safe_count = safe.count()
        unsafe_count = matched_count - safe_count
        bounds = matched.aggregate(
            first_id=Min("pk"), last_id=Max("pk"),
            first_created=Min("created"), last_created=Max("created"))
        result = {
            "schema": "mojosec.broker-flood-prune",
            "version": 1,
            "mode": "apply" if options["apply"] else "dry-run",
            "sensor_id": sensor,
            "since": since.isoformat(),
            "before": before.isoformat(),
            "matched": matched_count,
            "safe": safe_count,
            "unsafe": unsafe_count,
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
        }
        if not options["apply"]:
            self.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")))
            return None
        if unsafe_count:
            raise CommandError(
                f"refusing apply: {unsafe_count} linked or nonterminal match(es)")
        if safe_count > maximum:
            raise CommandError(
                f"refusing apply: {safe_count} safe matches exceed --max-events={maximum}")

        upper_pk = safe.aggregate(value=Max("pk"))["value"]
        deleted = 0
        while upper_pk is not None:
            ids = list(_safe_events(sensor, since, before).filter(
                pk__lte=upper_pk).order_by("pk").values_list("pk", flat=True)[:BATCH_SIZE])
            if not ids:
                break
            deleted += _delete_safe_batch(sensor, since, before, ids)
        result["deleted"] = deleted
        self.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return None
