from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from mojo.apps.fileman.models import FileManager


class Command(BaseCommand):
    help = (
        "Force a public-access audit for active S3 FileManagers. By default only "
        "user-scoped managers are checked. --groups also checks group-scoped "
        "managers, and --manager checks only the named ones. A user-scoped "
        "manager is corrected in both directions. A group-scoped manager is only "
        "ever corrected from public to private. "
        "Use after bucket/account policy changes made outside FileManager."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Inspect every manager without updating its audit metadata or is_public value",
        )
        parser.add_argument(
            "--groups",
            action="store_true",
            help="Also check group-scoped managers (corrected from public to private only)",
        )
        parser.add_argument(
            "--manager",
            action="append",
            type=int,
            dest="managers",
            metavar="PK",
            help="Check only this manager id (repeatable); overrides --groups",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        include_groups = options.get("groups", False)
        manager_ids = list(dict.fromkeys(options.get("managers") or []))
        self.counts = {
            "public": 0, "private": 0, "unknown": 0, "failed": 0,
            "changed": 0, "unverified_public": 0, "skipped": 0,
        }
        counts = self.counts

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN: audit results will not be persisted"))

        for manager in self.select_managers(manager_ids, include_groups):
            try:
                self.check_manager(manager, dry_run)
            except Exception as exc:
                counts["failed"] += 1
                self.stderr.write(
                    self.style.ERROR(f"FileManager {manager.pk} ({manager.name}): failed: {exc}")
                )

        if not manager_ids and not include_groups:
            unchecked = FileManager.objects.filter(
                backend_type=FileManager.AWS_S3,
                group__isnull=False,
                is_active=True,
            ).count()
            if unchecked:
                self.stdout.write(self.style.WARNING(
                    f"{unchecked} group-scoped S3 managers not checked (use --groups)"
                ))

        prefix = "DRY RUN COMPLETE" if dry_run else "RECONCILIATION COMPLETE"
        changed_label = "would_change" if dry_run else "changed"
        self.stdout.write(self.style.SUCCESS(
            f"{prefix}: public={counts['public']} private={counts['private']} "
            f"unknown={counts['unknown']} failed={counts['failed']} "
            f"{changed_label}={counts['changed']} "
            f"unverified_public={counts['unverified_public']} skipped={counts['skipped']}"
        ))

    def select_managers(self, manager_ids, include_groups):
        """Yield the managers to check. System managers are never selected."""
        if manager_ids:
            found = FileManager.objects.in_bulk(manager_ids)
            for pk in manager_ids:
                manager = found.get(pk)
                reason = self.skip_reason(manager)
                if reason:
                    name = f" ({manager.name})" if manager else ""
                    self.counts["skipped"] += 1
                    self.stdout.write(f"FileManager {pk}{name}: skipped ({reason})")
                    continue
                yield manager
            return

        scope = Q(user__isnull=False, group__isnull=True)
        if include_groups:
            scope |= Q(group__isnull=False)
        managers = FileManager.objects.filter(
            scope,
            backend_type=FileManager.AWS_S3,
            is_active=True,
        ).order_by("pk")
        yield from managers.iterator()

    def skip_reason(self, manager):
        if manager is None:
            return "not found"
        if not manager.is_active:
            return "inactive"
        if manager.backend_type != FileManager.AWS_S3:
            return "not an S3 manager"
        if manager.user_id is None and manager.group_id is None:
            return "system manager"
        return None

    def check_manager(self, manager, dry_run):
        counts = self.counts
        before = manager.is_public
        note = ""
        if manager.group_id is None:
            manager.audit_is_public(force=True, persist=not dry_run)
            audit = getattr(manager, "_public_access_audit_result", {})
            status = audit.get("status", "unknown")
            after = status == "public" if status in ("public", "private") else before
        else:
            # A group manager never gets the object-level probe that confirms a
            # personal manager's policy answer, so policy evidence may only move
            # it toward signed URLs: public -> private, never the reverse.
            manager.audit_is_public(force=True, persist=False)
            audit = getattr(manager, "_public_access_audit_result", {})
            status = audit.get("status", "unknown")
            after = False if (status == "private" and before) else before
            if not dry_run:
                updates = {"public_access_audit": audit, "modified": timezone.now()}
                if after != before:
                    updates["is_public"] = False
                FileManager.objects.filter(pk=manager.pk).update(**updates)
            if status == "unknown" and before:
                counts["unverified_public"] += 1
                note = " (is_public True kept, not verified)"
            elif status == "public" and not before:
                note = " (policy is public, is_public False kept)"

        if after != before:
            counts["changed"] += 1
            verb = "would change is_public" if dry_run else "is_public"
            note = f" ({verb} {before} -> {after})"
        counts[status] = counts.get(status, 0) + 1
        self.stdout.write(f"FileManager {manager.pk} ({manager.name}): {status}{note}")
