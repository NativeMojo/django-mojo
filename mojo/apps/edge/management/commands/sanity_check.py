"""
Prove this node can actually serve, or exit non-zero naming what cannot
(maestro item #1458, D8).

Five checks, in order, stopping at the first failure. The last one is the one
that matters: a node whose Django loads, whose database answers, whose
migration graph is clean and whose Redis pings can still be unable to serve a
request — checks 1-4 pass on broken code, and an under-specified sanity check
drifts into a no-op that waves broken releases through. The canary deploy is
only as good as this command.

No AWS calls, no network beyond localhost: this runs on a node that may be
mid-deploy, and a sanity check must not depend on anything the deploy itself
could have broken remotely.
"""
import time

import requests
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


class Command(BaseCommand):
    help = (
        "Verify this node can serve: apps ready, database reachable, "
        "no unapplied migrations, Redis reachable, and one real request "
        "answered over the local socket. Exits non-zero on the first failure."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--url", default="http://127.0.0.1/api/version",
            help="Local probe URL for the real-request check "
                 "(default: the same probe post_deploy.sh uses).")
        parser.add_argument(
            "--timeout", type=float, default=5.0,
            help="Per-request timeout in seconds for the probe (default 5).")
        parser.add_argument(
            "--retries", type=int, default=10,
            help="Probe attempts before failing (default 10) — the app may "
                 "still be restarting when this runs.")
        parser.add_argument(
            "--delay", type=float, default=2.0,
            help="Seconds between probe attempts (default 2).")

    def handle(self, *args, **options):
        self._check("django apps", self._check_apps)
        self._check("database", self._check_database)
        self._check("migrations", self._check_migrations)
        self._check("redis", self._check_redis)
        self._check("local request", lambda: self._check_request(options))
        self.stdout.write(self.style.SUCCESS("sanity check passed"))

    def _check(self, name, func):
        try:
            func()
        except CommandError:
            raise
        except Exception as err:
            raise CommandError(f"sanity check failed: {name}: {err}")
        self.stdout.write(f"ok: {name}")

    def _check_apps(self):
        from django.apps import apps
        apps.check_apps_ready()

    def _check_database(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()

    def _check_migrations(self):
        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
        if plan:
            raise CommandError(
                f"sanity check failed: migrations: {len(plan)} unapplied "
                "migration(s) — the schema does not match this code")

    def _check_redis(self):
        from mojo.helpers.redis import get_client
        get_client().ping()

    def _check_request(self, options):
        url = options["url"]
        last_err = "no attempt made"
        for _ in range(max(1, options["retries"])):
            try:
                response = requests.get(url, timeout=options["timeout"])
                if response.status_code == 200:
                    return
                last_err = f"HTTP {response.status_code} from {url}"
            except requests.RequestException as err:
                last_err = str(err)
            time.sleep(options["delay"])
        raise CommandError(f"sanity check failed: local request: {last_err}")
