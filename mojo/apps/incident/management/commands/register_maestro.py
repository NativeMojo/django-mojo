from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Validate MAESTRO_API_KEY and register this deployment's callback URL"

    def handle(self, *args, **options):
        from mojo.apps.incident.services import maestro_sync

        try:
            result = maestro_sync.register()
        except Exception as err:
            reason = getattr(err, "reason", None) or str(err)
            raise CommandError(reason)

        workspace = result["workspace"]
        board = result["default_board"]
        self.stdout.write(self.style.SUCCESS(
            "Maestro registered: "
            f"integration={result['integration_id']} "
            f"workspace={workspace['name'] or workspace['id']} ({workspace['id']}) "
            f"default_board={board['name'] or board['id']} ({board['id']})"
        ))
