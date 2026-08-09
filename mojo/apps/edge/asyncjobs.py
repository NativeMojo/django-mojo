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
    from mojo.apps.edge.services import installer

    payload = job.payload or {}
    pool = payload.get("pool") or "default"
    result = installer.install(pool=pool, force=bool(payload.get("force")))

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
    for pool in pools:
        try:
            result = installer.install(pool=pool)
            if result.changed:
                converged.append(pool)
        except Exception as err:
            logit.error(f"edge: {source} convergence failed for pool {pool}: {err}")
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

    if not cronjobs.converge_enabled():
        return "disabled"
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


# ----------------------------------------------------------------------
# fleet code deploy (maestro item #1458)
# ----------------------------------------------------------------------
#
# One structural fact drives everything here: the update script stops the job
# engine running it (the skeleton's `bin/jobman stop`, SIGTERM then SIGKILL),
# so on any node updating ITSELF, Python after the script call never executes.
# Hence:
#   - the SCRIPT reports terminal status (via the deploy_status management
#     command), never deploy_node;
#   - the orchestrator delegates the canary to another node, updates itself
#     LAST and fire-and-forget;
#   - every deploy job is published max_retries=0 with an expiry — a dead
#     job's redelivery would re-run a whole update, possibly concurrently
#     with the still-running orphaned script.

# Seconds between DEPLOY_STATUS polls while the canary proves the release.
# A module constant so tests can shrink it.
DEPLOY_POLL_INTERVAL = 3.0


def _publish_deploy_node(runner_id, sha, framework, migrate):
    from mojo.apps import jobs
    from mojo.apps.edge.services import deploy

    jobs.publish(
        func=deploy.DEPLOY_NODE_JOB,
        payload=dict(sha=sha, framework=framework, migrate=bool(migrate)),
        channel=runner_id,
        max_retries=0,
        expires_in=deploy.canary_timeout())


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
    from mojo.apps.incident import reporter

    target = deploy.get_target()
    if not target or not deploy.is_valid_sha(target.get("sha") or ""):
        logit.warn("edge deploy: orchestrate fired with no usable target")
        deploy.clear_status()
        return "no_target"
    sha = target["sha"]
    me = job.runner_id or deploy.local_runner_id()

    try:
        framework = deploy.resolve_framework_version()
    except Exception as err:
        # C1: a deploy that cannot pin the framework version fails loudly
        # rather than letting each node resolve "latest" at its own moment.
        reporter.report_event(
            f"deploy {sha}: framework version resolution failed: {err}",
            title="Edge deploy failed before it started",
            category="edge_deploy", level=7)
        return _deploy_terminal(sha, me, framework=None, released=False)

    runners = deploy.alive_runner_ids(jobs.get_runners())
    if len(runners) <= 1:
        # Single-runner fleet: this node IS the canary. Fire-and-forget — the
        # script reports status, and this engine dies with the update. The
        # status tail is cleaned only by its TTL (D3, documented bound).
        _publish_deploy_node(me, sha, framework, migrate=True)
        logit.info(f"edge deploy {sha}: single runner, updating locally")
        return f"single:{sha}"

    canary = deploy.pick_canary(runners, me)
    _publish_deploy_node(canary, sha, framework, migrate=True)
    logit.info(f"edge deploy {sha}: canary {canary} told to migrate")

    deadline = _time.time() + deploy.canary_timeout()
    outcome = None
    while _time.time() < deadline:
        status = deploy.get_status()
        if (status and status.get("sha") == sha
                and status.get("state") in deploy.TERMINAL_STATES):
            outcome = status
            break
        _time.sleep(DEPLOY_POLL_INTERVAL)

    released = bool(outcome and outcome.get("state") == deploy.STATUS_DEPLOYING)
    if released:
        # The release is proven. Tell the START-of-deploy snapshot, minus the
        # canary and me — a re-read here would miss nodes whose engines are
        # cycling and silently leave them on the old release.
        for runner_id in runners:
            if runner_id in (canary, me):
                continue
            _publish_deploy_node(runner_id, sha, framework, migrate=False)
        logit.info(f"edge deploy {sha}: released to {len(runners) - 2} fleet node(s)")
    else:
        detail = (outcome or {}).get("detail") or (
            "canary reported failure" if outcome else
            f"canary did not report within {deploy.canary_timeout()}s")
        reporter.report_event(
            f"deploy {sha}: canary {canary} did not prove the release: {detail}",
            title="Edge deploy canary failed",
            category="edge_deploy", level=7)

    return _deploy_terminal(sha, me, framework=framework, released=released)


def _deploy_terminal(sha, me, framework, released):
    """The orchestrator's terminal, in D4's order: chain check, clear status,
    then — on a released deploy only — update self, fire-and-forget."""
    from mojo.apps import jobs
    from mojo.apps.edge.services import deploy

    current = deploy.get_target()
    if current and deploy.is_valid_sha(current.get("sha") or "") and current["sha"] != sha:
        # A push landed while this deploy ran. Re-arm and chain a fresh
        # orchestrate — the new deploy moves everyone, including this node,
        # so the stale self-update is skipped.
        deploy.arm_status(current["sha"], force=True)
        jobs.publish(
            func=deploy.DEPLOY_ORCHESTRATE_JOB,
            payload=dict(sha=current["sha"]),
            channel=deploy.DEPLOY_CHANNEL,
            max_retries=0,
            expires_in=deploy.canary_timeout())
        logit.info(f"edge deploy {sha}: target moved to {current['sha']}, chained")
        return f"chained:{current['sha']}"

    deploy.clear_status()
    if released and framework:
        _publish_deploy_node(me, sha, framework, migrate=False)
        return f"released:{sha}"
    return f"failed:{sha}"


def deploy_node(job):
    """Run the update script on THIS node (D4).

    On a full successful run this function never returns — the script's tail
    stops the engine executing it. Every observable outcome is therefore
    either the script's own deploy_status report, or the failure path below,
    which IS reachable: the script dies before its engine-stopping tail
    whenever install, migrate or sanity fails, and that is exactly when an
    incident must be filed.
    """
    from mojo.apps.edge.services import deploy
    from mojo.apps.incident import reporter

    payload = job.payload or {}
    sha = payload.get("sha") or ""
    framework = payload.get("framework") or ""
    migrate = bool(payload.get("migrate"))

    argv_base = deploy.deploy_script_argv()
    if not argv_base:
        reporter.report_event(
            f"deploy {sha}: EDGE_DEPLOY_SCRIPT is not configured on this node "
            "— refusing to deploy",
            title="Edge deploy node unconfigured",
            category="edge_deploy", level=7)
        raise RuntimeError("EDGE_DEPLOY_SCRIPT is not configured")
    # Defense-in-depth before anything enters a subprocess argv. There is no
    # shell, but argv hygiene is cheap and the values crossed a webhook.
    if not deploy.is_valid_sha(sha):
        raise RuntimeError(f"refusing to deploy an invalid sha: {sha!r}")
    if not deploy.is_valid_version(framework):
        raise RuntimeError(f"refusing to deploy an invalid framework version: {framework!r}")

    argv = list(argv_base) + ["--sha", sha, "--framework", framework]
    if migrate:
        argv.append("--migrate")
    logit.info(f"edge deploy {sha}: running update script (migrate={migrate})")
    result = deploy._run(argv)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "")[-800:]
        reporter.report_event(
            f"deploy {sha}: update script failed on {job.runner_id or 'this node'} "
            f"(exit {result.returncode}): {tail}",
            title="Edge deploy node failed",
            category="edge_deploy", level=7)
        raise RuntimeError(f"update script exited {result.returncode}")
    return f"completed:{sha}"
