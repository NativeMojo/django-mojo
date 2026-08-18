"""Report a non-release-critical deploy warning without vetoing deployment.

The shell passes only one of the fixed phase names below. Raw command output
never enters incidents; it remains in the deploy's private bounded log tail.
This command intentionally exits successfully if incident reporting is down:
an observability outage cannot turn housekeeping into an application outage.
"""
from django.core.management.base import BaseCommand


PHASES = {
    "auxiliary_systemd",
    "cron",
    "deployment_control_plane",
    "log_ownership",
    "mojosec",
    "mojosec_helper",
    "nginx_security",
    "retired_config",
    "timers",
}


class Command(BaseCommand):
    help = "File a fixed-phase noncritical deployment warning incident."

    def add_arguments(self, parser):
        parser.add_argument("phase", choices=sorted(PHASES))

    def handle(self, *args, **options):
        phase = options.get("phase")
        if phase not in PHASES:
            return

        from mojo.apps.edge.services import deploy, platform_deploy
        from mojo.apps.incident import reporter

        status = deploy.get_status() or {}
        deployment_id = platform_deploy.deployment_id(status.get("deployment"))
        sha = str(status.get("sha") or "")[:40]
        runner_id = deploy.local_runner_id()
        try:
            event = reporter.report_event(
                f"deploy housekeeping warning on {runner_id} (phase={phase})",
                title="Edge deploy housekeeping warning",
                category="edge_deploy", level=5)
            if deployment_id and platform_deploy.get(deployment_id) is not None:
                platform_deploy.add_link(
                    deployment_id, "incident_events", event.pk)
            self.stdout.write(self.style.WARNING(
                f"reported deploy warning: {phase} ({sha or 'manual'})"))
        except Exception:
            from mojo.helpers import logit
            logit.exception(
                f"edge deploy warning: failed to report phase={phase}")
            self.stderr.write(
                f"deploy warning incident unavailable: phase={phase}")
