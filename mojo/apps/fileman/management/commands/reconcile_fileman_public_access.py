from django.core.management.base import BaseCommand

from mojo.apps.fileman.models import FileManager


class Command(BaseCommand):
    help = (
        "Force a public-access audit for active user-scoped S3 FileManagers. "
        "Use after bucket/account policy changes made outside FileManager."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Inspect every manager without updating its audit metadata or is_public value",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        managers = FileManager.objects.filter(
            backend_type=FileManager.AWS_S3,
            user__isnull=False,
            group__isnull=True,
            is_active=True,
        ).order_by("pk")
        counts = {"public": 0, "private": 0, "unknown": 0, "failed": 0}

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN: audit results will not be persisted"))

        for manager in managers.iterator():
            try:
                manager.audit_is_public(force=True, persist=not dry_run)
                audit = getattr(manager, "_public_access_audit_result", {})
                status = audit.get("status", "unknown")
                counts[status] = counts.get(status, 0) + 1
                self.stdout.write(
                    f"FileManager {manager.pk} ({manager.name}): {status}"
                )
            except Exception as exc:
                counts["failed"] += 1
                self.stderr.write(
                    self.style.ERROR(f"FileManager {manager.pk} ({manager.name}): failed: {exc}")
                )

        prefix = "DRY RUN COMPLETE" if dry_run else "RECONCILIATION COMPLETE"
        self.stdout.write(self.style.SUCCESS(
            f"{prefix}: public={counts['public']} private={counts['private']} "
            f"unknown={counts['unknown']} failed={counts['failed']}"
        ))
