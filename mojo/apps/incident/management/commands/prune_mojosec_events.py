"""Count, then prune, aged per-receipt MojoSec Events."""

import json

from django.core.management.base import BaseCommand, CommandError

from mojo.helpers import dates

MINIMUM_DAYS = 7


class Command(BaseCommand):
    help = "Dry-run or prune per-receipt MojoSec Events older than the receipt retention"

    def add_arguments(self, parser):
        parser.add_argument("--older-than-days", type=int, default=None)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--max-events", type=int, default=None)

    def handle(self, *args, **options):
        from mojo.apps.incident.services import mojosec, mojosec_retention

        days = options["older_than_days"]
        if days is None:
            days = mojosec.receipt_retention_days()
        if days < MINIMUM_DAYS:
            raise CommandError(f"--older-than-days must be {MINIMUM_DAYS} or more")
        maximum = options["max_events"]
        if maximum is not None and maximum <= 0:
            raise CommandError("--max-events must be positive")
        if options["apply"] and maximum is None:
            raise CommandError("--apply requires --max-events")

        now = dates.utcnow()
        cutoff = mojosec_retention.cutoff_for(days=days, now=now)
        result = {
            "schema": "mojosec.event-prune",
            "version": 1,
            "mode": "apply" if options["apply"] else "dry-run",
            "older_than_days": days,
        }
        result.update(mojosec_retention.summarize(cutoff))
        if options["apply"]:
            result["deleted"] = mojosec_retention.prune_projected_events(
                now=now, days=days, limit=maximum, max_batches=maximum)
        self.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return None
