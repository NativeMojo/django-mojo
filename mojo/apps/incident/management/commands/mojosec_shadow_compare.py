import json

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Sum

from mojo.helpers import dates


# Cutting an installation to authoritative silences per-receipt Events for
# these exact categories — the preflight lists what stops firing.
ROUTED_CATEGORIES = (
    "mojosec.web.probe", "mojosec.web.denied", "mojosec.web.error",
    "mojosec.fim.change",
)


class Command(BaseCommand):
    help = "Read-only bounded MojoSec Event/receipt/case canary comparison"

    def add_arguments(self, parser):
        parser.add_argument("--installation-key", type=int, required=True)
        parser.add_argument("--vhost", type=int, default=None)
        parser.add_argument("--sensor", default="")
        parser.add_argument("--hours", type=int, default=24)
        parser.add_argument("--max-cases", type=int, default=500)
        parser.add_argument("--min-compression", type=float, default=2.0)

    def handle(self, *args, **options):
        from mojo.apps.incident.models import (
            MojoSecCase, MojoSecReceipt, RuleSet)
        from mojo.apps.incident.services import mojosec_correlation

        hours = options["hours"]
        if not 1 <= hours <= 168:
            raise CommandError("--hours must be 1-168")
        if not 1 <= options["max_cases"] <= 100000:
            raise CommandError("--max-cases must be 1-100000")
        if not 1 <= options["min_compression"] <= 1000000000:
            raise CommandError("--min-compression must be 1-1000000000")
        sensor = options["sensor"]
        if sensor and len(sensor) > 128:
            raise CommandError("--sensor must be at most 128 characters")
        now = dates.utcnow()
        since = now - dates.timedelta(hours=hours)
        future_bound = now + dates.timedelta(
            seconds=mojosec_correlation.future_skew_seconds())
        cases = MojoSecCase.objects.filter(
            installation_key_id=options["installation_key"],
            last_seen__gte=since, last_seen__lte=future_bound)
        resource_id = None
        if options["vhost"] is not None:
            resource_id = f"vhost:{options['vhost']}"
            cases = cases.filter(resource_id=resource_id)
        if sensor:
            cases = cases.filter(sensor_id=sensor)
        totals = cases.aggregate(
            cases=Count("id"), occurrences=Sum("occurrence_count"),
            receipts=Sum("receipt_count"), projected_events=Sum("projected_event_count"),
            overflows=Sum("overflow_count"))
        linked = MojoSecReceipt.objects.filter(
            api_key_id=options["installation_key"], mojosec_case__in=cases,
            case_contributed_at__gte=since)
        linked_receipts = linked.count()
        suppressed_events = linked.filter(
            case_routed=True,
            publish_state=MojoSecReceipt.PUBLISH_PUBLISHED).count()
        deployment_cases = [
            {"sensor_id": row["sensor_id"], "deployment_id": row["deployment_id"],
             "cases": row["count"]}
            for row in cases.filter(family="deployment").values(
                "sensor_id", "deployment_id").annotate(
                count=Count("id")).order_by("-count")[:32]
        ]
        silenced_rule_sets = [
            {"name": name, "category": category, "handler": (handler or "")[:96]}
            for name, category, handler in RuleSet.objects.filter(
                category__in=ROUTED_CATEGORIES, is_active=True,
            ).order_by("category", "priority").values_list(
                "name", "category", "handler")[:32]
        ]
        occurrences = totals["occurrences"] or 0
        case_count = totals["cases"] or 0
        compression = round(occurrences / case_count, 2) if case_count else 0
        result = {
            "schema": "mojosec.shadow-comparison",
            "version": 2,
            "installation_key_id": options["installation_key"],
            "resource_id": resource_id,
            "sensor_id": sensor or None,
            "hours": hours,
            "cases": case_count,
            "occurrences": occurrences,
            "case_receipts": totals["receipts"] or 0,
            "linked_receipts": linked_receipts,
            "projected_events": totals["projected_events"] or 0,
            "overflows": totals["overflows"] or 0,
            "suppressed_events": suppressed_events,
            "deployment_cases": deployment_cases,
            "silenced_rule_sets": silenced_rule_sets,
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
