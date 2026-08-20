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
from django.core.management.base import BaseCommand, CommandError
from django.db.migrations.executor import MigrationExecutor

from mojo.apps.edge.services import sanity


class Command(BaseCommand):
    help = (
        "Verify this node can serve: apps ready, database reachable, "
        "no unapplied migrations, Redis reachable, and one real request "
        "answered over the local socket. Exits non-zero on the first failure."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--url", default=sanity.LOCAL_PROBE,
            help="Local probe URL for the real-request check "
                 "(default: the same probe post_deploy.sh uses). HTTPS on the "
                 "loopback by default and unverified there — see "
                 "sanity._verify_for.")
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
        options["migration_executor_cls"] = MigrationExecutor
        try:
            sanity.run(
                options, stop_on_failure=True,
                on_pass=lambda name: self.stdout.write(f"ok: {name}"))
        except sanity.SanityFailure as exc:
            raise CommandError(
                f"sanity check failed: {exc.check}: {exc.detail}")
        self.stdout.write(self.style.SUCCESS("sanity check passed"))
