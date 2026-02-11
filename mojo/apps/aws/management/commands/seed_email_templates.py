import json
import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Seed DB EmailTemplate rows from JSON seed files shipped with mojo.apps.aws."

    def add_arguments(self, parser):
        parser.add_argument(
            "--path",
            dest="path",
            default=None,
            help="Optional path to a directory containing seed JSON files. "
                 "Defaults to mojo/apps/aws/seeds/email_templates.",
        )
        parser.add_argument(
            "--update-existing",
            dest="update_existing",
            action="store_true",
            default=False,
            help="Update subject/html/text/metadata on existing templates. "
                 "By default existing templates are left unchanged.",
        )
        parser.add_argument(
            "--dry-run",
            dest="dry_run",
            action="store_true",
            default=False,
            help="Show what would change but do not write to the database.",
        )

    def handle(self, *args, **options):
        from mojo.apps.aws.models import EmailTemplate

        seed_dir = options.get("path") or self._default_seed_dir()
        update_existing = bool(options.get("update_existing"))
        dry_run = bool(options.get("dry_run"))

        if not os.path.isdir(seed_dir):
            raise CommandError("Seed directory not found: %s" % seed_dir)

        seed_files = self._list_seed_files(seed_dir)
        if not seed_files:
            self.stdout.write(self.style.WARNING("No seed files found in %s" % seed_dir))
            return

        plan = []
        for fpath in seed_files:
            payload = self._load_seed_file(fpath)
            name = (payload.get("name") or "").strip()
            if not name:
                raise CommandError("Seed file is missing required key 'name': %s" % fpath)

            plan.append({
                "file": fpath,
                "name": name,
                "subject_template": payload.get("subject_template", "") or "",
                "html_template": payload.get("html_template", "") or "",
                "text_template": payload.get("text_template", "") or "",
                "metadata": payload.get("metadata", {}) or {},
            })

        self.stdout.write("Seeding %d email template(s) from %s" % (len(plan), seed_dir))
        self.stdout.write("Mode: %s%s" % (
            "DRY RUN" if dry_run else "WRITE",
            " (update-existing enabled)" if update_existing else " (existing templates unchanged)",
        ))

        created = 0
        updated = 0
        skipped = 0

        ctx = transaction.atomic() if not dry_run else self._noop_context()
        with ctx:
            for item in plan:
                existing = EmailTemplate.objects.filter(name=item["name"]).first()
                if existing is None:
                    created += 1
                    self._print_action("CREATE", item["name"], item["file"])
                    if not dry_run:
                        EmailTemplate.objects.create(
                            name=item["name"],
                            subject_template=item["subject_template"],
                            html_template=item["html_template"],
                            text_template=item["text_template"],
                            metadata=item["metadata"],
                        )
                    continue

                if not update_existing:
                    skipped += 1
                    self._print_action("SKIP", item["name"], item["file"], reason="already exists")
                    continue

                changes = self._compute_changes(existing, item)
                if not changes:
                    skipped += 1
                    self._print_action("SKIP", item["name"], item["file"], reason="no changes")
                    continue

                updated += 1
                self._print_action("UPDATE", item["name"], item["file"], reason=", ".join(changes))
                if not dry_run:
                    existing.subject_template = item["subject_template"]
                    existing.html_template = item["html_template"]
                    existing.text_template = item["text_template"]
                    existing.metadata = item["metadata"]
                    existing.save(update_fields=["subject_template", "html_template", "text_template", "metadata", "modified"])

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Done. created=%d updated=%d skipped=%d" % (created, updated, skipped)))

    def _default_seed_dir(self):
        """
        Resolve the default seed directory reliably.

        This command ships inside mojo.apps.aws. We find the aws app directory by
        importing the module and using its file path, rather than relying on
        relative traversal from this commands directory (which can be brittle in
        some packaging/layout scenarios).
        """
        try:
            import mojo.apps.aws as aws_app
        except Exception as e:
            raise CommandError("Unable to import mojo.apps.aws to resolve seed directory: %s" % str(e))

        aws_dir = os.path.dirname(os.path.abspath(aws_app.__file__))  # .../mojo/apps/aws
        seed_dir = os.path.join(aws_dir, "seeds", "email_templates")
        return seed_dir

    def _list_seed_files(self, seed_dir):
        files = []
        for name in os.listdir(seed_dir):
            if not name.endswith(".json"):
                continue
            full = os.path.join(seed_dir, name)
            if os.path.isfile(full):
                files.append(full)
        files.sort()
        return files

    def _load_seed_file(self, fpath):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            raise CommandError("Invalid JSON in %s: %s" % (fpath, str(e)))
        except Exception as e:
            raise CommandError("Failed to read %s: %s" % (fpath, str(e)))

    def _compute_changes(self, existing, item):
        changes = []
        if (existing.subject_template or "") != (item.get("subject_template") or ""):
            changes.append("subject_template")
        if (existing.html_template or "") != (item.get("html_template") or ""):
            changes.append("html_template")
        if (existing.text_template or "") != (item.get("text_template") or ""):
            changes.append("text_template")
        if (existing.metadata or {}) != (item.get("metadata") or {}):
            changes.append("metadata")
        return changes

    def _print_action(self, action, name, fpath, reason=None):
        msg = "%-6s %s" % (action, name)
        if reason:
            msg += " (%s)" % reason
        msg += "  <- %s" % os.path.basename(fpath)

        if action == "CREATE":
            self.stdout.write(self.style.SUCCESS(msg))
        elif action == "UPDATE":
            self.stdout.write(self.style.WARNING(msg))
        else:
            self.stdout.write(msg)

    class _noop_context:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False