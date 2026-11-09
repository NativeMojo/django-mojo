"""Preview/install MojoSec defaults and explicitly repair legacy visibility."""
import json

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Preview MojoSec rules; --apply requires the preview's --expected-revision."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--repair-visibility", action="store_true")
        parser.add_argument("--expected-revision")

    def handle(self, *args, **options):
        from mojo.apps.incident.services.mojosec_defaults import setup_defaults

        if options["apply"] and not options["expected_revision"]:
            raise CommandError("Run the preview first and pass --expected-revision")
        try:
            result = setup_defaults(
                apply=options["apply"],
                repair_visibility=options["repair_visibility"],
                expected_revision=options["expected_revision"])
        except ValueError as err:
            raise CommandError(str(err)) from err
        self.stdout.write(json.dumps(result, sort_keys=True, indent=2))
