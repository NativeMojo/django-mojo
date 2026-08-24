"""
edge background job handlers.

`install_generation` is published as a BROADCAST on the `edge` channel, so
every runner in the fleet converges its own node. That is the whole point:
nothing pushes to nodes, and a node that was down catches up on its next
convergence sweep (see cronjobs.py) without anyone maintaining an inventory.

An install is idempotent — a node already on the published generation does
nothing — so a duplicate broadcast, an overlapping cron sweep and a manual
trigger all cost one comparison. The one exception is a node with an
unfetchable release (`installed.json`'s `www_pending`): it re-installs the same
generation every sweep, on purpose, because retrying the fetch is the only
thing that heals it.
"""

from mojo.helpers import logit


def install_generation(job):
    """Converge this node onto the desired state for a pool.

    A failure raises: the Certificate/Vhost rows are already the durable truth,
    and the raise is what makes a node that could not converge visible in the
    jobs surface as well as in the incident it already reported.
    """
    from mojo.apps.edge import cronjobs
    from mojo.apps.edge.services import installer

    payload = job.payload or {}
    result = installer.install_pools(
        cronjobs.converge_pools(), force=bool(payload.get("force")))

    if not result.changed:
        return f"completed:unchanged={result.generation}"
    return (f"completed:generation={result.generation},"
            f"excluded={len(result.excluded or [])},"
            f"www_pending={len(result.www_pending or {})}")


def _converge_pools(pools, source):
    """Install every pool, isolating failures — one pool's failure must not
    stop convergence for the others; the install already reported its own
    incident."""
    from mojo.apps.edge.services import installer

    converged = []
    try:
        result = installer.install_pools(pools)
        if result.changed:
            converged = list(pools)
    except Exception as err:
        from mojo.apps.account.services.setup_safety import failure_metadata
        failure = failure_metadata(err, "edge.install")
        logit.error(
            f"edge: {source} combined convergence failed "
            f"(action={failure['action']}, error={failure['exception_class']})")
    return f"completed:converged={','.join(converged) if converged else 'none'}"


def converge(job):
    """Cron entry point — the same install, on a timer.

    Separate from `install_generation` only so the jobs surface distinguishes
    'something changed and we were told' from 'the periodic sweep'. A node that
    missed a broadcast, booted from an AMI, or had its runner stopped converges
    here.
    """
    pools = (job.payload or {}).get("pools") or ["default"]
    return _converge_pools(pools, "sweep")


def on_engine_start(engine):
    """Reconcile this node because it started, not because it was told to.

    Job-engine startup hook (maestro #1772). Fan-out resolves the runner
    roster at publish time, so a broadcast sent while this node's engine was
    restarting — which every deploy causes — quietly skips it. Rather than
    catching pushes better, the node re-derives its own desired state on
    boot: deliberately local, publishes nothing.

    Runs on the engine's worker pool; a failure here is logged by the engine
    and never prevents startup — the ten-minute sweep is still behind it.
    """
    from mojo.apps.edge import cronjobs
    from mojo.apps.edge.services import platform_deploy, webapp_auth_routes

    platform_deploy.finalize_post_restart()
    platform_deploy.record_rollback_outcomes()

    if not cronjobs.converge_enabled():
        return "disabled"
    # This node installs the repaired desired state immediately below. Every
    # other node also runs this startup hook after a platform deployment, and
    # the periodic sweep remains the crash backstop. Suppress the model-layer
    # broadcast here so startup retains its local, publish-nothing contract.
    auth_result = webapp_auth_routes.reconcile_all(publisher=lambda pool: None)
    logit.info(
        "edge: hosted-auth startup reconciliation "
        f"checked={auth_result['checked']} repaired={auth_result['repaired']} "
        f"failed={len(auth_result['failed'])}")
    logit.info(f"edge: startup convergence on {engine.runner_id}")
    return _converge_pools(cronjobs.converge_pools(), "startup")


# ----------------------------------------------------------------------
# WebApp release deploys
# ----------------------------------------------------------------------

def webapp_deploy_node(job):
    """Install and prove one WebApp deployment on this targeted runner."""
    from mojo.apps.edge.services import webapp_deploy

    return webapp_deploy.install_node(job)


def webapp_deploy_orchestrate(job):
    """Wait for active runners and restore prior state on partial failure."""
    from mojo.apps.edge.services import webapp_deploy

    deployment_id = (job.payload or {}).get("deployment")
    return webapp_deploy.orchestrate(deployment_id)


def webapp_onboarding_advance(job):
    """Advance one leased onboarding operation from durable intent."""
    from mojo.apps.edge.services import webapp_onboarding

    operation_id = (job.payload or {}).get("operation")
    return webapp_onboarding.advance(operation_id, owner=job.pk)


# ----------------------------------------------------------------------
# fleet code deploy (maestro item #1458)
# ----------------------------------------------------------------------
#
# Project-owned update scripts return after nginx and the candidate API pass.
# The parent records the result, lets its job finish normally, then detaches an
# engine recycle. The orchestrator still uses a canary and updates itself last;
# deploy jobs remain non-retrying because repeating a host transaction is not a
# safe generic job retry.

# Seconds between DEPLOY_STATUS polls while the canary proves the release.
# A module constant so tests can shrink it.
DEPLOY_POLL_INTERVAL = 3.0


def _publish_deploy_node(runner_id, sha, framework, migrate, deployment_id,
                         api_cohort=True):
    from mojo.apps import jobs
    from mojo.apps.edge.services import deploy
    from mojo.apps.edge.services import platform_deploy

    try:
        job_id = jobs.publish(
            func=deploy.DEPLOY_NODE_JOB,
            # `runner` names the ONE node this row was published for. The
            # channel already says it, but a channel is routing — it is what
            # the engine claimed from, and `runner_id` is then overwritten by
            # whichever engine did the claiming. The payload is the published
            # intent and nothing rewrites it, so it is what
            # platform_deploy.close_handoff_job matches on to close its own
            # row and never a fleet peer's.
            payload=dict(
                sha=sha, framework=framework, migrate=bool(migrate),
                deployment=str(deployment_id), runner=str(runner_id),
                api_cohort=bool(api_cohort)),
            channel=runner_id,
            max_retries=0,
            expires_in=deploy.canary_timeout())
    except Exception:
        platform_deploy.evidence(
            deployment_id, runner_id, "publish_failed",
            detail={"migrate": bool(migrate)})
        raise
    platform_deploy.evidence(
        deployment_id, runner_id, "dispatched",
        detail={"migrate": bool(migrate), "job": str(job_id)})
    return job_id


def deploy_orchestrate(job):
    """Drive one fleet deploy: canary first, fleet on proof, self last (D4).

    The target snapshot taken at the top is the deploy — decisions never
    re-read it. A webhook overwriting the target mid-deploy is picked up by
    the chain check at the terminal, which is what makes "converge on the
    newest commit, once" true instead of aspirational.
    """
    import time as _time

    from mojo.apps import jobs
    from mojo.apps.edge.services import deploy
    from mojo.apps.edge.services import platform_deploy
    from mojo.apps.incident import reporter

    payload = job.payload or {}
    deployment_id = platform_deploy.deployment_id(payload.get("deployment"))
    record = platform_deploy.get(deployment_id)
    if record is None or not deploy.is_valid_sha(record.sha):
        logit.warn("edge deploy: orchestrate fired with no usable target")
        return "no_target"
    sha = record.sha
    me = job.runner_id or deploy.local_runner_id()
    runners = list(record.frozen_roster or [])
    raw_api = (record.detail or {}).get("api_roster")
    api_runners = list(raw_api) if isinstance(raw_api, list) else list(runners)
    api_runners = [runner for runner in api_runners if runner in set(runners)]

    target = deploy.get_target()
    status = deploy.get_status()
    if (not target or target.get("deployment") != deployment_id or not status
            or status.get("deployment") != deployment_id):
        platform_deploy.transition(
            deployment_id, "superseded", {"reason": "target_moved_before_start"})
        if target and target.get("deployment") != deployment_id:
            return _deploy_terminal(
                sha, me, framework=None, released=False,
                deployment_id=deployment_id)
        return "superseded"

    if not runners:
        event = reporter.report_event(
            f"deploy {sha}: frozen edge runner roster was empty",
            title="Edge deploy has no proven runner roster",
            category="edge_deploy", level=7)
        platform_deploy.add_link(deployment_id, "incident_events", event.pk)
        return _deploy_terminal(
            sha, me, framework=None, released=False,
            deployment_id=deployment_id)

    if not api_runners:
        event = reporter.report_event(
            f"deploy {sha}: frozen deployment roster has no API runner",
            title="Edge deploy has no API migration canary",
            category="edge_deploy", level=7)
        platform_deploy.add_link(deployment_id, "incident_events", event.pk)
        return _deploy_terminal(
            sha, me, framework=None, released=False,
            deployment_id=deployment_id)

    try:
        framework = deploy.resolve_framework_version()
    except deploy.FrameworkPinError as err:
        # The operator's own hold is unusable. This is NOT a provider failure
        # and must not read like one: the cure is a settings change, not a
        # retry, so the incident names the setting and says which way it is
        # broken. The stored value never travels — it is operator-written data
        # on an operator-facing surface.
        message = (
            f"deploy {sha}: EDGE_FRAMEWORK_VERSION is set to 'hold' but this "
            "fleet has no converged deployment to hold at"
            if getattr(err, "reason", "") == "hold_without_converged" else
            f"deploy {sha}: EDGE_FRAMEWORK_VERSION is not a usable version "
            "— refusing to deploy")
        event = reporter.report_event(
            message, title="Edge deploy framework pin is unusable",
            category="edge_deploy", level=7)
        platform_deploy.add_link(deployment_id, "incident_events", event.pk)
        return _deploy_terminal(
            sha, me, framework=None, released=False,
            deployment_id=deployment_id)
    except Exception as err:
        # C1: a deploy that cannot pin the framework version fails loudly
        # rather than letting each node resolve "latest" at its own moment.
        from mojo.apps.account.services.setup_safety import failure_metadata
        failure = failure_metadata(err, "edge.deploy.framework_resolution")
        event = reporter.report_event(
            f"deploy {sha}: framework version resolution failed "
            f"(error={failure['exception_class']})",
            title="Edge deploy failed before it started",
            category="edge_deploy", level=7)
        platform_deploy.add_link(deployment_id, "incident_events", event.pk)
        return _deploy_terminal(
            sha, me, framework=None, released=False,
            deployment_id=deployment_id)

    platform_deploy.set_framework(deployment_id, framework)
    platform_deploy.transition(deployment_id, "canary", {"runner": me})
    if len(runners) <= 1:
        # Single-runner fleet: this node IS the canary. Fire-and-forget — the
        # script records terminal intent, this engine dies with the update,
        # and the replacement engine proves/finalizes the exact UUID lease.
        _publish_deploy_node(
            me, sha, framework, migrate=True, deployment_id=deployment_id,
            api_cohort=True)
        logit.info(f"edge deploy {sha}: single runner, updating locally")
        return f"single:{sha}"

    canary = deploy.pick_canary(api_runners, me)
    if canary is None:
        # This orchestrator is the only API node in a mixed fleet. It cannot
        # block waiting on a job addressed to its own engine. Return, let that
        # job run, and let the replacement engine release the specialized
        # cohort from durable proof.
        _publish_deploy_node(
            me, sha, framework, migrate=True, deployment_id=deployment_id,
            api_cohort=True)
        logit.info(f"edge deploy {sha}: sole API canary queued locally")
        return f"single-api:{sha}"
    _publish_deploy_node(
        canary, sha, framework, migrate=True, deployment_id=deployment_id,
        api_cohort=True)
    logit.info(f"edge deploy {sha}: canary {canary} told to migrate")

    deadline = _time.time() + deploy.canary_timeout()
    outcome = None
    while _time.time() < deadline:
        status = deploy.get_status()
        if not status or status.get("deployment") != deployment_id:
            # The lease is gone or belongs to someone else: this deploy has
            # been superseded and is no longer the one driving the fleet.
            # Waiting out the canary timeout to then declare the canary
            # "silent" would file a false incident and, worse, chain a fresh
            # orchestrate on top of the deploy that took the lease. Leave
            # quietly instead — no incident, no chain, no self-update.
            platform_deploy.transition(
                deployment_id, "superseded", {"reason": "lease_superseded"})
            logit.info(
                f"edge deploy {sha}: lease superseded mid-canary, standing down")
            return f"superseded:{sha}"
        if (status.get("sha") == sha
                and status.get("state") in deploy.TERMINAL_STATES):
            outcome = status
            break
        _time.sleep(DEPLOY_POLL_INTERVAL)

    released = bool(outcome and outcome.get("state") == deploy.STATUS_DEPLOYING)
    if released:
        platform_deploy.transition(
            deployment_id, "fleet", {"canary": canary, "proven": True})
        # The release is proven. Tell the START-of-deploy snapshot, minus the
        # canary and me — a re-read here would miss nodes whose engines are
        # cycling and silently leave them on the old release.
        for runner_id in runners:
            if runner_id in (canary, me):
                continue
            _publish_deploy_node(
                runner_id, sha, framework, migrate=False,
                deployment_id=deployment_id,
                api_cohort=runner_id in set(api_runners))
        logit.info(f"edge deploy {sha}: released to {len(runners) - 2} fleet node(s)")
    else:
        detail = deploy.failure_phase((outcome or {}).get("detail")) if outcome else (
            "canary reported failure" if outcome else
            f"canary did not report within {deploy.canary_timeout()}s")
        event = reporter.report_event(
            f"deploy {sha}: canary {canary} did not prove the release: {detail}",
            title="Edge deploy canary failed",
            category="edge_deploy", level=7)
        platform_deploy.add_link(deployment_id, "incident_events", event.pk)

    return _deploy_terminal(
        sha, me, framework=framework, released=released,
        deployment_id=deployment_id)


def _deploy_terminal(sha, me, framework, released, deployment_id):
    """The orchestrator's terminal, in D4's order: chain check, clear status,
    then — on a released deploy only — update self, fire-and-forget."""
    from mojo.apps import jobs
    from mojo.apps.edge.services import deploy
    from mojo.apps.edge.services import platform_deploy

    current = deploy.get_target()
    if (current and deploy.is_valid_sha(current.get("sha") or "")
            and current.get("deployment") != deployment_id):
        # A push landed while this deploy ran. Re-arm and chain a fresh
        # orchestrate — the new deploy moves everyone, including this node,
        # so the stale self-update is skipped.
        platform_deploy.transition(
            deployment_id, "superseded",
            {"next_deployment": current.get("deployment")})
        live = deploy.get_status()
        if live and live.get("deployment") == current.get("deployment"):
            # Somebody already claimed the new target's lease — the reconciler
            # resuming a stranded target, or a receiver that armed it while
            # this deploy was finishing. That deploy is running; a force re-arm
            # and a second orchestrate would double-drive it.
            logit.info(
                f"edge deploy {sha}: target {current['sha']} is already armed, "
                "leaving it to its own orchestrator")
            return f"chained:{current['sha']}"
        deploy.arm_status(
            current["sha"], force=True,
            deployment_id=current.get("deployment"))
        jobs.publish(
            func=deploy.DEPLOY_ORCHESTRATE_JOB,
            payload=dict(
                sha=current["sha"], deployment=current.get("deployment")),
            channel=deploy.DEPLOY_CHANNEL,
            max_retries=0,
            expires_in=deploy.canary_timeout())
        logit.info(f"edge deploy {sha}: target moved to {current['sha']}, chained")
        return f"chained:{current['sha']}"

    deploy.clear_status(deployment_id)
    if released and framework:
        # Canary proof releases the fleet, but it is not fleet convergence.
        # Keep the row active until the delayed reconciler collects UUID/SHA
        # proof from every restarted runner.
        platform_deploy.transition(
            deployment_id, "fleet", {"canary_proven": True,
                                      "verification": "pending"})
        record = platform_deploy.get(deployment_id)
        roster = list(record.frozen_roster or []) if record else []
        raw_api = (record.detail or {}).get("api_roster") if record else None
        api_roster = list(raw_api) if isinstance(raw_api, list) else list(roster)
        if me in set(roster):
            _publish_deploy_node(
                me, sha, framework, migrate=False,
                deployment_id=deployment_id,
                api_cohort=me in set(api_roster))
        return f"released:{sha}"
    platform_deploy.transition(
        deployment_id, "failed", {"reason": "canary_not_proven"})
    return f"failed:{sha}"


def _node_deploy_failed(deployment_id, sha, job, migrate, phase, message,
                        detail=None, title="Edge deploy node failed"):
    """Report ONE node's deploy failure on every surface that has to see it.

    Called for every parent-observed failure: unconfigured, an unexecutable
    script, a timeout, or a nonzero exit. Assumes
    nothing about how far the deploy got — in particular it does not require
    that `deploy._run` was ever reached.

    Order matters: incident and durable evidence first, and the deploy LEASE
    only when this node was migrating. A fleet node's failure must never touch
    the deploy status — the release is already proven and one node falling
    behind is not a fleet failure. When the migrating node's CAS lands, the
    durable row is closed too. The legacy management command uses the same
    state transition during the one-generation adoption bridge.
    """
    from mojo.apps.edge.services import deploy
    from mojo.apps.edge.services import platform_deploy
    from mojo.apps.incident import reporter

    event = reporter.report_event(
        message, title=title, category="edge_deploy", level=7)
    platform_deploy.add_link(deployment_id, "incident_events", event.pk)
    platform_deploy.evidence(
        deployment_id, job.runner_id or deploy.local_runner_id(),
        "failed", detail=detail or {"phase": phase})
    if not migrate:
        return
    if deploy.set_status(
            deploy.STATUS_FAILED, sha, detail=phase,
            deployment_id=deployment_id):
        platform_deploy.transition(
            deployment_id, "failed", {"phase": phase, "source": "node_report"})


def _recycle_engine_after_return():
    """Detach the engine recycle so the current job can finish normally."""
    import os
    import subprocess
    import sys

    from django.conf import settings as django_settings

    # Unit-test engines must never terminate themselves. Production projects
    # run DEBUG=False; the detached process is then adopted outside this job's
    # process group before it stops and starts the engine.
    if django_settings.DEBUG:
        return
    root = django_settings.PROJECT_ROOT
    command = (
        'sleep 2; "$1" -m mojo.deploy.jobman stop engine --root "$2" '
        '--grace 2; sleep 1; "$1" -m mojo.deploy.jobman start engine '
        '--root "$2"')
    subprocess.Popen(
        ["bash", "-c", command, "deploy-recycle", sys.executable, root],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
        env=os.environ.copy())


def deploy_node(job):
    """Run the update script on THIS node (D4).

    Success returns only after the script's nginx and exact-200 checks. This
    parent then records evidence/status and schedules the engine recycle.
    Every failure path routes through `_node_deploy_failed`.
    """
    import errno as _errno
    import os
    import subprocess

    from mojo.apps.edge.services import deploy
    from mojo.apps.edge.services import platform_deploy

    payload = job.payload or {}
    sha = payload.get("sha") or ""
    framework = payload.get("framework") or ""
    migrate = bool(payload.get("migrate"))
    api_cohort = bool(payload.get("api_cohort", True))
    deployment_id = payload.get("deployment") or ""
    deployment_id = platform_deploy.deployment_id(deployment_id)
    if not deployment_id:
        raise RuntimeError("refusing platform deploy without deployment UUID")
    record = platform_deploy.get(deployment_id)
    if record is None or record.sha != sha:
        raise RuntimeError("refusing platform deploy with mismatched deployment UUID")

    runner = job.runner_id or deploy.local_runner_id()
    try:
        node_type = deploy.local_node_type()
    except ValueError:
        _node_deploy_failed(
            deployment_id, sha, job, migrate, phase="preflight_failed",
            message=f"deploy {deployment_id} ({sha}): local node type is invalid")
        raise RuntimeError("EDGE_DEPLOY_NODE_TYPE is invalid") from None
    if api_cohort != (node_type == "api"):
        _node_deploy_failed(
            deployment_id, sha, job, migrate, phase="preflight_failed",
            message=(
                f"deploy {deployment_id} ({sha}): routing cohort does not "
                f"match local node type {node_type} on {runner}"))
        raise RuntimeError("deployment cohort does not match local node type")
    if migrate and node_type != "api":
        _node_deploy_failed(
            deployment_id, sha, job, migrate, phase="preflight_failed",
            message=f"deploy {deployment_id} ({sha}): non-API node refused migration")
        raise RuntimeError("only API nodes may migrate")
    argv_base = deploy.deploy_script_argv()
    if not argv_base:
        _node_deploy_failed(
            deployment_id, sha, job, migrate, phase="unconfigured",
            message=(
                f"deploy {deployment_id} ({sha}): EDGE_DEPLOY_SCRIPT is not configured on this node "
                "— refusing to deploy"),
            title="Edge deploy node unconfigured")
        raise RuntimeError("EDGE_DEPLOY_SCRIPT is not configured")
    # An explicitly-pathed script that is not executable fails identically on
    # every node in the fleet, so catch it here rather than letting each node
    # discover it as an OSError from exec. Bare command names are SKIPPED:
    # os.access does no PATH resolution, so probing "sudo" would refuse every
    # deploy on the documented ["sudo", "-n", "..."] configuration. The OSError
    # catch below is the backstop for those.
    if os.sep in argv_base[0] and not os.access(argv_base[0], os.X_OK):
        _node_deploy_failed(
            deployment_id, sha, job, migrate, phase="preflight_failed",
            message=(
                f"deploy {deployment_id} ({sha}): EDGE_DEPLOY_SCRIPT is not "
                f"executable on {runner}: {argv_base[0]} — the update script "
                "must ship committed 100755 (git update-index --chmod=+x)"))
        raise RuntimeError("EDGE_DEPLOY_SCRIPT is not executable")
    # Defense-in-depth before anything enters a subprocess argv. There is no
    # shell, but argv hygiene is cheap and the values crossed a webhook.
    if not deploy.is_valid_sha(sha):
        raise RuntimeError(f"refusing to deploy an invalid sha: {sha!r}")
    if not deploy.is_valid_version(framework):
        raise RuntimeError(f"refusing to deploy an invalid framework version: {framework!r}")

    argv = list(argv_base) + [
        "--sha", sha, "--framework", framework,
        "--deployment", deployment_id, "--node-type", node_type]
    if migrate:
        argv.append("--migrate")
    logit.info(
        f"edge deploy {deployment_id} ({sha}): running update script "
        f"(migrate={migrate}, node_type={node_type})")
    try:
        result = deploy._run(argv)
    except OSError as err:
        # The script never started: wrong mode, wrong interpreter, gone
        # between the preflight and here. errno names are framework-owned
        # vocabulary; the OSError's own message is not and never travels.
        code = _errno.errorcode.get(err.errno, "unknown")
        _node_deploy_failed(
            deployment_id, sha, job, migrate, phase="exec_failed",
            message=(
                f"deploy {deployment_id} ({sha}): update script could not be "
                f"executed ({code}): {argv_base[0]} on {runner}"))
        raise RuntimeError(f"update script could not be executed: {code}") from None
    except subprocess.TimeoutExpired as err:
        # A killed script leaves nothing but its output, so the bounded
        # redacted tail is the whole diagnosis — evidence only, never the
        # incident.
        _node_deploy_failed(
            deployment_id, sha, job, migrate, phase="script_timeout",
            message=(
                f"deploy {deployment_id} ({sha}): update script timed out on "
                f"{runner} after {deploy.SCRIPT_TIMEOUT}s "
                "(phase=script_timeout)"),
            detail={"phase": "script_timeout",
                    "stderr_tail": deploy.stderr_tail(err.stderr)})
        raise RuntimeError("update script timed out") from None
    if result.returncode != 0:
        # stdout/stderr may contain echoed credentials. Only the bounded,
        # per-line sanitized tail becomes durable evidence; nothing raw
        # reaches an incident or Redis.
        _node_deploy_failed(
            deployment_id, sha, job, migrate, phase="update_script",
            message=(
                f"deploy {deployment_id} ({sha}): update script failed on "
                f"{job.runner_id or 'this node'} "
                f"(phase=update_script, exit={result.returncode})"),
            detail={"phase": "update_script", "exit": result.returncode,
                    "stderr_tail": deploy.stderr_tail(result.stderr)})
        raise RuntimeError(f"update script exited {result.returncode}")

    # The script returns only after nginx accepted the candidate and its API
    # answered HTTP 200. Record that terminal result here, after the process
    # has returned, without asking candidate Django to coordinate its deploy.
    if not platform_deploy.evidence(
            deployment_id, runner, deploy.STATUS_DEPLOYING,
            detail={"node_type": node_type}):
        raise RuntimeError("local runner is not in the deployment roster")
    if migrate and not deploy.set_status(
            deploy.STATUS_DEPLOYING, sha, deployment_id=deployment_id):
        raise RuntimeError("canary deploy no longer owns the armed lease")
    if node_type == "api":
        _recycle_engine_after_return()
    return f"completed:{sha}"
