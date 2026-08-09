"""Bootstrap a first WebApp and its GitHub deployment credential."""

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = (
        "Create or resolve a WebApp and mint its MOJO_DEPLOY_KEY. "
        "Use --token-only when piping directly to GitHub.")

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument(
            "--webapp", type=int,
            help="Existing WebApp id (the normal portal bootstrap path).")
        target.add_argument(
            "--slug",
            help="Create or resolve this slug; requires --group, --vhost, --bucket.")
        parser.add_argument("--group", type=int, help="Owning Group id for --slug.")
        parser.add_argument("--vhost", type=int, help="Existing Vhost id for --slug.")
        parser.add_argument("--bucket", help="Allowed release bucket for --slug.")
        parser.add_argument(
            "--rotate", action="store_true",
            help="Explicitly revoke and replace an already-linked key.")
        parser.add_argument(
            "--token-only", action="store_true",
            help="Write only the raw token to stdout; diagnostics go to stderr.")

    @transaction.atomic
    def handle(self, *args, **options):
        from mojo.apps.account.models import Group
        from mojo.apps.edge.models import Vhost, WebApp
        from mojo.apps.edge.services import webapp_keys

        try:
            if options.get("webapp") is not None:
                forbidden = [
                    name for name in ("group", "vhost", "bucket")
                    if options.get(name) is not None]
                if forbidden:
                    raise CommandError(
                        "--webapp cannot be combined with "
                        + ", ".join(f"--{name}" for name in forbidden))
                web_app = WebApp.objects.select_related("api_key", "group").filter(
                    pk=options["webapp"]).first()
                if web_app is None:
                    raise CommandError("WebApp not found")
                created = False
            else:
                missing = [
                    name for name in ("group", "vhost", "bucket")
                    if not options.get(name)]
                if missing:
                    raise CommandError(
                        "--slug requires "
                        + ", ".join(f"--{name}" for name in missing))
                group = Group.objects.filter(pk=options["group"]).first()
                if group is None:
                    raise CommandError("Group not found")
                vhost = Vhost.objects.filter(pk=options["vhost"]).first()
                if vhost is None:
                    raise CommandError("Vhost not found")

                web_app = WebApp.objects.select_related("api_key", "group").filter(
                    group=group, slug=options["slug"]).first()
                created = web_app is None
                if created:
                    web_app = WebApp(
                        group=group,
                        slug=options["slug"],
                        vhost=vhost,
                        bucket=options["bucket"],
                        prefix="pending")
                    web_app.save()
                    web_app.prefix = web_app.storage_prefix()
                    web_app.save(update_fields=["prefix", "modified"])
                elif (web_app.vhost_id != vhost.pk
                      or web_app.bucket != options["bucket"]):
                    raise CommandError(
                        "an existing WebApp has this group and slug but its "
                        "vhost or bucket differs")

            web_app, api_key, token, rotated = webapp_keys.link(
                web_app, rotate=options["rotate"])
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        if options["token_only"]:
            self.stderr.write(
                f"WebApp {web_app.pk} ({web_app.slug}) ready; "
                f"MOJO_WEBAPP_ID={web_app.pk}")
            self.stdout.write(token)
            return

        self.stdout.write(json.dumps({
            "webapp": web_app.pk,
            "slug": web_app.slug,
            "api_key": api_key.pk,
            "token": token,
            "created": created,
            "rotated": rotated,
        }, sort_keys=True))
