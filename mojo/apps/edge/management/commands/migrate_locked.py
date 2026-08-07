"""
``migrate`` under a real Postgres advisory lock (maestro item #1458, D5).

The lock must live in the same process — and therefore the same database
session — as the migration it serialises. An advisory lock is session-scoped,
and the job engine calls ``close_old_connections()`` at the top of every job
execution, so a lock taken in an orchestrating job would be gone before the
migration ran, passing its own test while serialising nothing. A management
command holds one connection across both.

This also closes the case a canary deploy alone cannot: someone running
``post_deploy.sh`` by hand on another box mid-deploy races the deploy's
migration, and the old ``var/allow_migrate`` flag file cannot see that
process. The Postgres lock can — the second ``migrate_locked`` exits non-zero
instead of migrating concurrently.

Non-blocking on purpose (``pg_try_advisory_lock``, not ``pg_advisory_lock``):
the blocking form would queue a second migration behind the first rather than
refuse it, and a queued surprise migration is exactly what a deploy script
must never produce.
"""
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

# Distinct from the dnsman advisory-lock namespaces (certs: 1421, acme: 1422)
# so a deploy migration and a certificate issuance can never collide.
LOCK_NAMESPACE = 1423
LOCK_ID = 1


class Command(BaseCommand):
    help = (
        "Run database migrations while holding a Postgres advisory lock, "
        "exiting non-zero instead of running when another migration holds it."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--noinput", action="store_true",
            help="Pass through to migrate: never prompt for input.")

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            # Same bypass as the dnsman issue lock: django-mojo supports
            # PostgreSQL in production; this keeps isolated unit-test
            # databases functional without pretending to provide a
            # cross-process guarantee on unsupported engines.
            self.stderr.write(self.style.WARNING(
                f"advisory locks unsupported on {connection.vendor}; "
                "migrating WITHOUT cross-process serialisation"))
            self._migrate(options)
            return

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s, %s)", [LOCK_NAMESPACE, LOCK_ID])
            acquired = bool(cursor.fetchone()[0])
        if not acquired:
            raise CommandError(
                "another process holds the migration lock — refusing to run "
                "migrate concurrently")
        try:
            self._migrate(options)
        finally:
            # Released even when the migration fails: a failed migrate must
            # leave the lock free for the retry, not wedge every later deploy.
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s, %s)", [LOCK_NAMESPACE, LOCK_ID])

    def _migrate(self, options):
        kwargs = {}
        if options.get("noinput"):
            kwargs["interactive"] = False
        call_command("migrate", **kwargs)
        self.stdout.write(self.style.SUCCESS("migrations complete"))
