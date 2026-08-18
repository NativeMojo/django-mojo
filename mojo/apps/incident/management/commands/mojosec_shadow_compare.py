import json

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Sum

from mojo.helpers import dates


class Command(BaseCommand):
    help = "Read-only bounded MojoSec Event/receipt/shadow-case canary comparison"

    def add_arguments(self, parser):
        parser.add_argument("--installation-key", type=int, required=True)
        parser.add_argument("--vhost", type=int, required=True)
        parser.add_argument("--hours", type=int, default=24)
        parser.add_argument("--max-cases", type=int, default=500)
        parser.add_argument("--min-compression", type=float, default=2.0)

    def handle(self, *args, **options):
        from mojo.apps.incident.models import MojoSecCase, MojoSecReceipt

        hours = options["hours"]
        if not 1 <= hours <= 168:
            raise CommandError("--hours must be 1-168")
        if not 1 <= options["max_cases"] <= 100000:
            raise CommandError("--max-cases must be 1-100000")
        if not 1 <= options["min_compression"] <= 1000000000:
            raise CommandError("--min-compression must be 1-1000000000")
        since = dates.utcnow() - dates.timedelta(hours=hours)
        resource_id = f"vhost:{options['vhost']}"
        cases = MojoSecCase.objects.filter(
            installation_key_id=options["installation_key"],
            resource_id=resource_id, last_seen__gte=since)
        totals = cases.aggregate(
            cases=Count("id"), occurrences=Sum("occurrence_count"),
            receipts=Sum("receipt_count"), projected_events=Sum("projected_event_count"),
            overflows=Sum("overflow_count"))
        linked_receipts = MojoSecReceipt.objects.filter(
            api_key_id=options["installation_key"], mojosec_case__in=cases,
            case_contributed_at__gte=since).count()
        occurrences = totals["occurrences"] or 0
        case_count = totals["cases"] or 0
        compression = round(occurrences / case_count, 2) if case_count else 0
        result = {
            "schema": "mojosec.shadow-comparison",
            "version": 1,
            "installation_key_id": options["installation_key"],
            "resource_id": resource_id,
            "hours": hours,
            "cases": case_count,
            "occurrences": occurrences,
            "case_receipts": totals["receipts"] or 0,
            "linked_receipts": linked_receipts,
            "projected_events": totals["projected_events"] or 0,
            "overflows": totals["overflows"] or 0,
            "compression_ratio": compression,
            "bounds": {
                "receipt_fidelity": linked_receipts == (totals["receipts"] or 0),
                "case_cardinality": case_count <= options["max_cases"],
                "compression": compression >= options["min_compression"],
            },
        }
        self.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")))
        if not all(result["bounds"].values()):
            raise CommandError("MojoSec shadow canary bounds were not met")
