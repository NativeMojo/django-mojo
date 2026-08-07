"""
The update script's one sanctioned way to read and report deploy state
(maestro item #1458, D4).

The self-stop trap is why this exists: the skeleton's update script stops the
job engine that is running the ``deploy_node`` job which shelled it, so Python
after the script call never executes on a node updating itself. The SCRIPT
must therefore report the terminal status — and it must never reimplement the
Redis key, TTL or compare-and-set conventions in bash. It calls this instead:

    manage.py deploy_status get
    manage.py deploy_status set deploying --sha <target-sha>
    manage.py deploy_status set failed --sha <target-sha> --detail "why"

``set`` is compare-and-set on the stamped SHA. Exit codes: 0 applied, 3 the
write was ignored because the deploy was superseded (distinct from argparse's
2, so the script can tell "stale, fine" from "I called this wrong").
"""
import json
import sys

from django.core.management.base import BaseCommand, CommandError

from mojo.apps.edge.services import deploy


class Command(BaseCommand):
    help = (
        "Read (get) or report (set) fleet deploy state. 'set' is "
        "compare-and-set on --sha; exit 3 means the write was ignored "
        "because the deploy was superseded."
    )

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["get", "set"])
        parser.add_argument(
            "state", nargs="?", choices=[deploy.STATUS_DEPLOYING, deploy.STATUS_FAILED],
            help="Terminal state to report (set only).")
        parser.add_argument(
            "--sha", default=None,
            help="The target commit SHA this report belongs to (set only).")
        parser.add_argument(
            "--detail", default=None,
            help="Optional failure detail; travels into the deploy incident.")

    def handle(self, *args, **options):
        if options["action"] == "get":
            self.stdout.write(json.dumps(dict(
                target=deploy.get_target(), status=deploy.get_status())))
            return

        state = options.get("state")
        sha = options.get("sha") or ""
        if not state:
            raise CommandError("set requires a state: deploying or failed")
        if not deploy.is_valid_sha(sha):
            raise CommandError("set requires --sha <the target commit sha>")
        if deploy.set_status(state, sha, detail=options.get("detail")):
            self.stdout.write(self.style.SUCCESS(f"applied: {state} ({sha})"))
            return
        self.stderr.write(
            f"ignored: the armed deploy no longer belongs to {sha} "
            "(superseded, or nothing armed)")
        sys.exit(3)
