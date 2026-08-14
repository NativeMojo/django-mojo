"""
Fleet code-deploy coordination — the state and the decisions.

The deploy state is two Redis keys, both TTL'd (maestro item #1458, D3):

- ``TARGET_KEY`` — what the fleet should converge onto: the pushed commit SHA
  and who asked for it. Last writer wins, on purpose: a push arriving mid-deploy
  overwrites the target and the orchestrator's chain check picks it up at its
  terminal, so the fleet converges on the newest commit exactly once.
- ``STATUS_KEY`` — what is happening right now (``migrating`` | ``deploying`` |
  ``failed``), stamped with the SHA of the deploy it belongs to. Arming is
  ``SET NX`` so a same-second webhook race starts exactly one deploy; terminal
  writes are compare-and-set on the stamped SHA (a Lua script, so a ghost
  redelivery or a slow canary from a superseded deploy is ignored without
  needing to know it is stale).

The TTL is load-bearing: a canary that dies hard would otherwise leave
``migrating`` set forever and wedge every future deploy. The multi-node
orchestrator clears the status at its terminal; the TTL is the backstop for
every crash in between, and the only cleaner on a single-runner fleet.

Redis remains ephemeral coordination, while ``edge.PlatformDeployment`` is the
durable UUID-addressed attempt journal. A Redis flush can release coordination,
but the five-minute reconciler closes the abandoned durable attempt as unknown
instead of erasing its history.

Every ``EDGE_DEPLOY_*`` setting is read with ``settings.get_static`` — never
``settings.get``. ``settings.get`` resolves a DB-backed ``Setting`` row first,
and ``Setting`` is REST-writable, which would make the update-script argv, the
deploy branch and the PyPI URL attacker-controlled data for any holder of a
global ``manage_settings`` grant (same reasoning as ``installer.py``).
"""
import json
import re
import subprocess

import requests

from mojo.helpers import dates
from mojo.helpers import logit
from mojo.helpers.redis import get_client
from mojo.helpers.settings import settings

logger = logit.get_logger("edge", "edge.log")


class DeploymentCoordinationError(RuntimeError):
    """A fixed, non-provider failure safe for endpoint classification."""

TARGET_KEY = "edge:deploy:target"
STATUS_KEY = "edge:deploy:status"

STATUS_MIGRATING = "migrating"
STATUS_DEPLOYING = "deploying"
STATUS_FAILED = "failed"
TERMINAL_STATES = (STATUS_DEPLOYING, STATUS_FAILED)

# Only update.sh's fixed phase labels may cross from process execution into
# Redis, incidents, or the durable deployment journal.  Exception and command
# output can echo credentials, so an arbitrary --detail is never persisted.
_FAILURE_PHASES = {
    "git fetch": "git_fetch",
    "git clean": "git_clean",
    "post_deploy (migrate)": "post_deploy_migrate",
    "sanity_check": "sanity_check",
    "deploy_status report": "deploy_status_report",
    "post_deploy": "post_deploy",
    "rollback failed": "rollback_failed",
    "rollback impossible: no previous state": "rollback_impossible",
}

# Phases the NODE decides, not the script: deploy_node reports these when the
# update script never got far enough to report anything itself. Kept separate
# from _FAILURE_PHASES because they have no script-owned label to map from —
# deploy_node passes the classification straight through.
_NODE_FAILURE_PHASES = {
    "exec_failed", "script_timeout", "preflight_failed", "unconfigured",
}

DEPLOY_CHANNEL = "default"
DEPLOY_ORCHESTRATE_JOB = "mojo.apps.edge.asyncjobs.deploy_orchestrate"
DEPLOY_NODE_JOB = "mojo.apps.edge.asyncjobs.deploy_node"

# A git commit id, abbreviated or full. The zero SHA of a branch deletion is
# hex-valid, so callers must also refuse it explicitly (see is_valid_sha).
SHA_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")
# PEP 440-shaped, loosely: enough to refuse shell metacharacters and whitespace
# before a version string enters a subprocess argv.
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._!+-]{0,63}$")

# How long one update-script run may take before the subprocess is killed.
# pip installs and migrations are the slow parts; 15 minutes is generous.
SCRIPT_TIMEOUT = 900

# Terminal status writes are compare-and-set on the stamped SHA, atomically —
# a get-then-set in Python would leave a window where a ghost writer with a
# superseded SHA lands between the read and the write.
_CAS_STATUS_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local cur = cjson.decode(raw)
if cur['sha'] ~= ARGV[1] then return 0 end
if (cur['deployment'] or '') ~= ARGV[2] then return 0 end
redis.call('SET', KEYS[1], ARGV[3], 'EX', tonumber(ARGV[4]))
return 1
"""

_CLEAR_STATUS_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local cur = cjson.decode(raw)
if (cur['deployment'] or '') ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""


def status_ttl():
    return int(settings.get_static("EDGE_DEPLOY_STATUS_TTL", 900))


def canary_timeout():
    return int(settings.get_static("EDGE_DEPLOY_CANARY_TIMEOUT", 600))


def deploy_branch():
    return settings.get_static("EDGE_DEPLOY_BRANCH", "main")


def pypi_url():
    return settings.get_static(
        "EDGE_PYPI_URL", "https://pypi.org/pypi/django-mojo/json")


def deploy_script_argv():
    """The update-script argv, or None when this node is not deploy-configured.

    There is deliberately NO default: a box that has not set
    ``EDGE_DEPLOY_SCRIPT`` in its settings file (dev laptops, test
    environments) must refuse to run a deploy rather than sudo-execute a
    script path guessed by the framework.
    """
    argv = settings.get_static("EDGE_DEPLOY_SCRIPT", None, kind="list")
    if not argv:
        return None
    return list(argv)


def is_valid_sha(value):
    if not isinstance(value, str) or not SHA_PATTERN.match(value):
        return False
    # A branch-deletion push carries the all-zero SHA, which is hex-valid.
    return value.strip("0") != ""


def is_valid_version(value):
    return isinstance(value, str) and bool(VERSION_PATTERN.match(value))


def failure_phase(value):
    """Map script-owned labels to a fixed non-secret failure classification."""
    value = str(value or "")
    allowed = (set(_FAILURE_PHASES.values()) | _NODE_FAILURE_PHASES
               | {"git_reset", "update_failed"})
    if value in allowed:
        return value
    if value.startswith("git reset to "):
        return "git_reset"
    return _FAILURE_PHASES.get(value, "update_failed")


# ----------------------------------------------------------------------
# state
# ----------------------------------------------------------------------

def _loads(raw):
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def set_target(sha, actor=None, deployment_id=None):
    """Record what the fleet should be on. Last writer wins (D3)."""
    payload = dict(
        sha=sha, actor=actor or "unknown",
        deployment=str(deployment_id) if deployment_id else "",
        at=dates.utcnow().isoformat())
    get_client().set(TARGET_KEY, json.dumps(payload), ex=status_ttl())
    return payload


def get_target():
    return _loads(get_client().get(TARGET_KEY))


def get_status():
    return _loads(get_client().get(STATUS_KEY))


def arm_status(sha, force=False, deployment_id=None):
    """Arm ``migrating`` for one deploy.

    ``SET NX`` by default, so of two same-second webhooks exactly one starts a
    deploy and the other has merely recorded its target. ``force=True`` is the
    orchestrator's chain re-arm, where the old status is deliberately replaced.
    Returns whether the arm landed.
    """
    payload = json.dumps(dict(
        state=STATUS_MIGRATING, sha=sha,
        deployment=str(deployment_id) if deployment_id else "",
        at=dates.utcnow().isoformat()))
    client = get_client()
    if force:
        return bool(client.set(STATUS_KEY, payload, ex=status_ttl()))
    return bool(client.set(STATUS_KEY, payload, ex=status_ttl(), nx=True))


def set_status(state, sha, detail=None, deployment_id=None):
    """Terminal status write, compare-and-set on the stamped SHA (D3).

    Returns True when applied, False when the armed status no longer belongs
    to ``sha`` (superseded deploy, ghost redelivery) or nothing is armed at
    all — the write is ignored in both cases.
    """
    if state not in TERMINAL_STATES:
        raise ValueError(f"not a terminal deploy state: {state!r}")
    deployment_id = str(deployment_id) if deployment_id else ""
    payload = json.dumps(dict(
        state=state, sha=sha, deployment=deployment_id,
        detail=failure_phase(detail) if state == STATUS_FAILED else "",
        at=dates.utcnow().isoformat()))
    result = get_client().eval(
        _CAS_STATUS_LUA, 1, STATUS_KEY, sha, deployment_id, payload, status_ttl())
    return bool(result)


def clear_status(deployment_id=None):
    """Delete only the UUID-owned lease; legacy empty leases stay compatible."""
    deployment_id = str(deployment_id) if deployment_id else ""
    return bool(get_client().eval(
        _CLEAR_STATUS_LUA, 1, STATUS_KEY, deployment_id))


# ----------------------------------------------------------------------
# decisions
# ----------------------------------------------------------------------

def alive_runner_ids(runners):
    """The runner ids worth talking to, sorted for determinism.

    ``jobs.get_runners()`` keeps reporting a dead runner for one heartbeat TTL
    (~15s) with ``alive: False`` — publishing a deploy to one guarantees a
    timed-out canary or a node incident for a box that no longer exists.
    """
    ids = set()
    for runner in runners or []:
        if runner.get("alive") and runner.get("runner_id"):
            ids.add(runner["runner_id"])
    return sorted(ids)


def pick_canary(runner_ids, me):
    """Lowest alive runner id excluding self; None for a single-runner fleet.

    Deterministic on purpose: a random canary makes a failed deploy
    irreproducible, and "the canary is always node X" is worth having while
    debugging at 3am.
    """
    for runner_id in sorted(runner_ids or []):
        if runner_id != me:
            return runner_id
    return None


def local_runner_id():
    """This box's runner id — the fallback when a job row carries none

    (``th.run_pending_jobs`` executes handlers without an engine claim, so
    ``job.runner_id`` is only stamped in production)."""
    from mojo.apps.jobs import ENGINE_CHANNEL_SUFFIX
    from mojo.apps.jobs.job_engine import host_channel
    return f"{host_channel()}{ENGINE_CHANNEL_SUFFIX}"


def resolve_framework_version():
    """Resolve the newest django-mojo version, once per deploy (D6).

    Raises on any failure — C1's whole point is that a silently skipped
    upgrade is the failure mode, so a deploy that cannot resolve the version
    fails rather than letting each node resolve "latest" at its own moment.
    """
    response = requests.get(pypi_url(), timeout=10)
    response.raise_for_status()
    version = (response.json().get("info") or {}).get("version")
    if not is_valid_version(version or ""):
        raise ValueError(f"unusable version from PyPI: {version!r}")
    return version


def request_deploy(sha, actor=None, source="external", created_by=None,
                   idempotency_key=None, source_delivery="", retry_of=None,
                   return_deployment=False, links=None):
    """The uniform receiver rule (D3), shared by the webhook and the manual
    endpoint: always record the target; publish an orchestrate job only when
    this call armed the status. Returns True when a deploy was started, False
    when the target was recorded onto a deploy already in flight.
    """
    from mojo.apps import jobs
    from mojo.apps.edge.services import platform_deploy

    row, replayed = platform_deploy.create(
        sha, actor=actor or "", source=source, created_by=created_by,
        idempotency_key=idempotency_key, source_delivery=source_delivery,
        retry_of=retry_of, links=links)
    if row.status == "failed" and row.detail.get("reason") == "roster_unavailable":
        raise DeploymentCoordinationError("edge runner roster unavailable")
    if replayed:
        started = any(
            (item.get("detail") or {}).get("queue") == "orchestrator_published"
            for item in (row.transitions or []) if isinstance(item, dict))
        return (started, row) if return_deployment else started

    try:
        set_target(sha, actor=row.actor, deployment_id=row.pk)
    except Exception:
        platform_deploy.transition(row.pk, "failed", {"reason": "coordination_unavailable"})
        raise DeploymentCoordinationError("deploy coordination unavailable") from None
    platform_deploy.transition(
        row.pk, "requested", {"coordination": "target_recorded"})
    if not arm_status(sha, deployment_id=row.pk):
        logger.info(f"deploy {sha}: recorded onto an in-flight deploy")
        return (False, row) if return_deployment else False
    # State first, publish second — a published job with no state would be a
    # deploy with no coordination. max_retries=0: a redelivered orchestrator
    # double-driving the protocol is worse than a bounded wedge (the status
    # TTL). The NX arm above is what dedupes GitHub's own webhook retries.
    try:
        jobs.publish(
            func=DEPLOY_ORCHESTRATE_JOB,
            payload=dict(sha=sha, deployment=str(row.pk)),
            channel=DEPLOY_CHANNEL,
            max_retries=0,
            expires_in=canary_timeout())
    except Exception:
        platform_deploy.transition(
            row.pk, "failed", {"reason": "orchestrator_publish_failed"})
        try:
            clear_status(row.pk)
        except Exception:
            pass
        raise DeploymentCoordinationError("deployment queue unavailable") from None
    platform_deploy.transition(
        row.pk, "requested", {"queue": "orchestrator_published"})
    logger.info(f"deploy {sha}: started by {row.actor or 'unknown'}")
    return (True, row) if return_deployment else True


def _run(argv):
    """Run the update script. The single seam the deploy tests replace.

    Deliberately dumb, exactly like the installer's: no shell, no string
    interpolation, no cwd. The argv base comes from a get_static setting and
    the appended values are pattern-validated before they get here.
    """
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=SCRIPT_TIMEOUT)
