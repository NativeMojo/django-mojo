import json

from django.core.management.base import BaseCommand
from django.db.models import Count

from mojo.apps.fileman.models import FileManager
from mojo.apps.fileman.models.manager import SELECTION_ORDER


class Command(BaseCommand):
    help = (
        "List scopes (a group or a user) holding more than one active FileManager "
        "for the same purpose (`use`), and more than one active purpose-less "
        "default. Shows which id the lookups now pick: the default first, then "
        "the oldest. Read-only: writes nothing. New duplicates are refused on "
        "save; these predate that rule, and an operator decides what to do with them."
    )

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Print the rows as JSON")

    def handle(self, *args, **options):
        rows = self.find_duplicates()
        if options["json"]:
            self.stdout.write(json.dumps(rows))
            return
        if not rows:
            self.stdout.write("No duplicate file managers found.")
            return
        for row in rows:
            scope = f"group {row['group_id']}" if row["group_id"] is not None else f"user {row['user_id']}"
            managers = ", ".join(
                f"{m['id']}{' (default)' if m['is_default'] else ''}" for m in row["managers"])
            self.stdout.write(
                f"{scope} use={row['use'] or '(none)'}: {managers}; selected {row['selected_id']}")
        self.stdout.write(f"{len(rows)} scope(s) with duplicates.")

    def find_duplicates(self):
        active = FileManager.objects.filter(is_active=True).exclude(group=None, user=None)
        # A purpose manager: every active row for the scope and use counts.
        # Purpose-less: only defaults, since several non-defaults are normal.
        candidates = [
            active.exclude(use=""),
            active.filter(use="", is_default=True),
        ]
        rows = []
        for queryset in candidates:
            keys = (queryset.values("group_id", "user_id", "use")
                    .annotate(n=Count("id")).filter(n__gt=1)
                    .order_by("group_id", "user_id", "use"))
            for key in keys:
                managers = list(queryset.filter(
                    group_id=key["group_id"], user_id=key["user_id"], use=key["use"]
                ).order_by(*SELECTION_ORDER).values("id", "name", "is_default"))
                rows.append({
                    "group_id": key["group_id"],
                    "user_id": key["user_id"],
                    "use": key["use"],
                    "managers": managers,
                    "selected_id": managers[0]["id"],
                })
        return rows
