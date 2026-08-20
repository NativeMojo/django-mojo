"""
The update script's one sanctioned way to read and report deploy state
(maestro item #1458, D4).

The self-stop trap is why this exists: the skeleton's update script stops the
job engine that is running the ``deploy_node`` job which shelled it, so Python
after the script call never executes on a node updating itself. The SCRIPT
must therefore report the terminal status — and it must never reimplement the
Redis key, TTL or compare-and-set conventions in bash. It calls this instead:

    manage.py deploy_status get
    manage.py deploy_status set deploying --sha <target-sha> --deployment <uuid>
    manage.py deploy_status set failed --sha <target-sha> --deployment <uuid> --detail <phase>
    manage.py deploy_status handoff --deployment <uuid>

``set`` is compare-and-set on the stamped SHA. Exit codes: 0 applied, 3 the
write was ignored because the deploy was superseded (distinct from argparse's
2, so the script can tell "stale, fine" from "I called this wrong").

``handoff`` is the other half of the same structural fact: the script is about
to stop the engine running this deployment's ``deploy_node`` job, so that job
would otherwise sit ``running`` behind a lease nobody will ever release. It
closes the job row and drops the in-flight entry, and does nothing else — no
lease, no evidence. Every successful update calls it, including the fleet runs
that deliberately report no status at all.
"""
import json
import os
import sys

from django.conf import settings as django_settings
from django.core.management.base import BaseCommand, CommandError

from mojo.apps.edge.services import deploy
from mojo.apps.edge.services import platform_deploy


class Command(BaseCommand):
    help = (
        "Read (get) or report (set) fleet deploy state. 'set' is "
        "compare-and-set on --sha; exit 3 means the write was ignored "
        "because the deploy was superseded."
    )

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["get", "set", "handoff"])
        parser.add_argument(
            "state", nargs="?", choices=[deploy.STATUS_DEPLOYING, deploy.STATUS_FAILED],
            help="Terminal state to report (set only).")
        parser.add_argument(
            "--sha", default=None,
            help="The target commit SHA this report belongs to (set only).")
        parser.add_argument(
            "--deployment", default=None,
            help="The platform deployment UUID this report belongs to (set only).")
        parser.add_argument(
            "--detail", default=None,
            help="Optional update.sh phase; arbitrary values become update_failed.")
        parser.add_argument(
            "--evidence", action="store_true", default=False,
            help="Attach the sanitized tail captured by update.sh (failed only).")

    def _read_var_line(self, name, limit=128):
        path = os.path.join(django_settings.PROJECT_ROOT, "var", name)
        try:
            with open(path, "r") as stream:
                return stream.read(limit).strip()
        except OSError:
            return ""

    def _failure_detail(self, phase, include_evidence):
        detail = {"phase": phase}
        # The rollback target is the pair of node-local facts update.sh
        # captured before anything moved. Both must validate or neither
        # travels — half a pair would read as a rollback promise the node
        # cannot keep.
        sha = self._read_var_line("previous_sha")
        framework = self._read_var_line("previous_framework")
        if (len(sha) == 40 and deploy.is_valid_sha(sha)
                and deploy.is_valid_version(framework)):
            detail["rollback_to"] = {"sha": sha, "framework": framework}
        if not include_evidence:
            return detail
        path = os.path.join(
            django_settings.PROJECT_ROOT, "var", "deploy_failure_output")
        try:
            with open(path, "rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - 65536), os.SEEK_SET)
                output = stream.read(65536)
        except OSError:
            output = b""
        tail = deploy.stderr_tail(output)
        if tail:
            detail["stderr_tail"] = tail
        return detail

    def _report_failure_incident(self, deployment_id, sha, runner_id, phase):
        from mojo.apps.incident import reporter

        try:
            event = reporter.report_event(
                f"deploy {deployment_id} ({sha}) failed on {runner_id} "
                f"(phase={phase})",
                title="Edge deploy node failed",
                category="edge_deploy", level=7)
            platform_deploy.add_link(
                deployment_id, "incident_events", event.pk)
        except Exception:
            # The deployment callback and rollback must not be blocked by a
            # secondary outage in the incident reporter.
            from mojo.helpers import logit
            logit.exception(
                f"edge deploy {deployment_id}: failed to report incident")

    def handle(self, *args, **options):
        if options["action"] == "get":
            self.stdout.write(json.dumps(dict(
                target=deploy.get_target(), status=deploy.get_status())))
            return

        if options["action"] == "handoff":
            # The tail of EVERY successful update, canary and fleet alike:
            # this node is about to stop the engine running its own deploy
            # job. Close that job's row while the engine still exists.
            # Touches no lease and writes no evidence — the terminal report
            # (or its deliberate absence, on a fleet run) is the deploy's
            # story; this is only the job row.
            deployment_id = platform_deploy.deployment_id(
                options.get("deployment"))
            if not deployment_id:
                raise CommandError(
                    "handoff requires --deployment <platform deployment UUID>")
            closed = platform_deploy.close_handoff_job(
                deployment_id, "completed")
            self.stdout.write(f"handoff: closed {closed} node job(s)")
            return

        state = options.get("state")
        sha = options.get("sha") or ""
        supplied_deployment = options.get("deployment")
        deployment_id = platform_deploy.deployment_id(supplied_deployment)
        if not state:
            raise CommandError("set requires a state: deploying or failed")
        if not deploy.is_valid_sha(sha):
            raise CommandError("set requires --sha <the target commit sha>")
        if not supplied_deployment:
            # The 1.9 updater installs this command before reporting its
            # canary result, but its in-flight argv and Redis lease predate
            # UUID ownership. Permit only that exact empty-UUID lease. A new
            # UUID-owned attempt can never be claimed through this bridge.
            armed = deploy.get_status() or {}
            if armed.get("sha") != sha or armed.get("deployment"):
                raise CommandError(
                    "set requires --deployment <platform deployment UUID>")
            if deploy.set_status(
                    state, sha, detail=options.get("detail"),
                    deployment_id=None):
                self.stdout.write(self.style.SUCCESS(
                    f"applied legacy upgrade: {state} ({sha})"))
                return
            self.stderr.write(
                f"ignored: the armed legacy deploy no longer belongs to {sha} "
                "(superseded, or nothing armed)")
            sys.exit(3)
        if not deployment_id:
            raise CommandError("set requires --deployment <platform deployment UUID>")
        record = platform_deploy.get(deployment_id)
        if record is None or record.sha != sha:
            raise CommandError("deployment UUID does not belong to --sha")

        runner_id = deploy.local_runner_id()
        failure_phase = deploy.failure_phase(options.get("detail"))
        failure_detail = self._failure_detail(
            failure_phase,
            state == deploy.STATUS_FAILED and options.get("evidence"))
        identity_v2 = (
            os.environ.get("MOJO_DEPLOY_IDENTITY_READY")
            == str(deploy.DEPLOY_CONTRACT))
        if state == deploy.STATUS_DEPLOYING:
            if identity_v2:
                # The v2 signal means update.sh has already atomically
                # published the candidate identity. Persist strict proof
                # before exposing success through the coordination lease.
                from mojo.apps.edge.services import readiness
                try:
                    proof = readiness.local_node_proof()
                except Exception:
                    raise CommandError("local node proof unavailable") from None
                if not platform_deploy.proof_matches(record, proof):
                    detail = {
                        "reason": "identity_mismatch",
                        "observed_sha": str(
                            (proof or {}).get("platform_sha") or "")[:40],
                        "observed_deployment": str(
                            (proof or {}).get("platform_deployment") or "")[:36],
                    }
                    if not platform_deploy.evidence(
                            deployment_id, runner_id, "identity_mismatch",
                            proof={}, detail=detail):
                        raise CommandError(
                            "local runner is not in the deployment roster")
                    if not deploy.set_status(
                            deploy.STATUS_FAILED, sha, detail="identity mismatch",
                            deployment_id=deployment_id):
                        self.stderr.write(
                            "ignored: the deployment identity mismatch no "
                            "longer owns the armed lease")
                        sys.exit(3)
                    platform_deploy.transition(
                        deployment_id, "failed",
                        {"phase": "identity_mismatch", "source": "node_report"})
                    raise CommandError("published deployment identity mismatch")
                if not platform_deploy.evidence(
                        deployment_id, runner_id, state, proof=proof, detail={}):
                    raise CommandError(
                        "local runner is not in the deployment roster")
            else:
                # Contract v1 called back before writing deploy_sha and
                # deployment_uuid. It may release a multi-node canary, but its
                # observation is explicitly not proof. A restarted v2 engine
                # finalizes the attempt from the now-readable legacy pair.
                if not platform_deploy.evidence(
                        deployment_id, runner_id, "identity_pending", proof={},
                        detail={"reason": "legacy_script_identity_order"}):
                    raise CommandError(
                        "local runner is not in the deployment roster")
        elif record.status != "failed":
            # Failure remains fail-safe even if this callback's process no
            # longer appears in the frozen roster. Evidence is best-effort;
            # the UUID/SHA CAS below is the authority for the terminal lease.
            # Do not overwrite a prior identity_mismatch refusal when the
            # updater subsequently reports its generic callback failure.
            platform_deploy.evidence(
                deployment_id, runner_id, state,
                detail=failure_detail)
        else:
            # The row is already failed: update.sh is reporting how the first
            # failure ENDED — "rollback failed" / "rollback impossible: no
            # previous state" (see deploy._FAILURE_PHASES) — or repeating
            # itself. A latest-per-runner evidence write would silently
            # swallow it; the append-only diagnosis journal is where the rest
            # of the story accumulates, with its own tail.
            kind = ("outcome" if failure_phase in (
                "rollback_failed", "rollback_impossible") else "failure")
            platform_deploy.record_diagnosis(
                deployment_id, runner_id, detail=failure_detail, kind=kind)

        # This node's deploy job is over either way — the script is making its
        # terminal report and will stop the engine running that job moments
        # from now. Close the row while there is still an engine to close it.
        job_state = ("failed" if state == deploy.STATUS_FAILED else "completed")
        if deploy.set_status(
                state, sha, detail=options.get("detail"),
                deployment_id=deployment_id):
            platform_deploy.close_handoff_job(deployment_id, job_state)
            if state == deploy.STATUS_FAILED:
                platform_deploy.transition(
                    deployment_id, "failed",
                    {"phase": failure_phase,
                     "source": "node_report"})
                self._report_failure_incident(
                    deployment_id, sha, runner_id, failure_phase)
            elif len(record.frozen_roster or []) <= 1 and identity_v2:
                platform_deploy.transition(
                    deployment_id, "verified",
                    {"source": "node_report", "sha": sha})
            elif len(record.frozen_roster or []) <= 1:
                platform_deploy.transition(
                    deployment_id, "fleet",
                    {"source": "legacy_identity_bridge", "sha": sha})
            self.stdout.write(self.style.SUCCESS(f"applied: {state} ({sha})"))
            return
        # Superseded: this report is stale, but the run that produced it is
        # still over and its engine is still about to die. Whoever owns the
        # lease now, nothing is coming back to finish THIS node's job row.
        platform_deploy.close_handoff_job(deployment_id, job_state)
        self.stderr.write(
            f"ignored: the armed deploy no longer belongs to {sha} "
            "(superseded, or nothing armed)")
        sys.exit(3)
