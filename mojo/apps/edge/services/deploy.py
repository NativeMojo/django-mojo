"""
Fleet code-deploy coordination — the state and the decisions.

The deploy state is two Redis keys, both TTL'd (maestro item #1458, D3):

- ``TARGET_KEY`` — what the fleet should converge onto: the pushed commit SHA
  and who asked for it. Last writer wins, on purpose: a push arriving mid-deploy
  overwrites the target and the orchestrator's chain check picks it up at its
  terminal, so the fleet converges on the newest commit exactly once.
- ``STATUS_KEY`` — what is happening right now (``migrating`` | ``deploying`` |
  ``failed``), stamped with the SHA of the deploy it belongs to. Arming is a
  Lua compare-and-set that lands only when nothing is armed **or** the armed
  lease is already terminal-``failed``; ``migrating``/``deploying`` are never
  stealable, so a same-second webhook race still starts exactly one deploy.
  Terminal writes are compare-and-set on the stamped SHA (a Lua script, so a
  ghost redelivery or a slow canary from a superseded deploy is ignored
  without needing to know it is stale).

The invariant is "never a second concurrent deploy **while one is live**" —
not "never a second deploy for the whole TTL". A ``failed`` lease describes a
deploy that is over; holding the plane shut behind it for the remaining TTL
buys nothing and costs every push in that window.

The TTL is load-bearing: a canary that dies hard would otherwise leave
``migrating`` set forever and wedge every future deploy. The multi-node
orchestrator clears the status at its terminal; the TTL is the backstop for
every crash before terminal intent. A single-runner replacement engine
finalizes its exact UUID lease from durable evidence and atomic local proof,
then resumes one queued target. A target whose deploy never started is not
left to the TTL backstop either — ``resume_stranded_target`` republishes it.

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


class FrameworkPinError(ValueError):
    """``EDGE_FRAMEWORK_VERSION`` cannot be turned into a version to install.

    Deliberately never carries the stored value: this becomes an operator-
    facing incident, and the setting is operator-written data. ``reason``
    distinguishes the two cases the incident wording separates — an unusable
    value, and ``hold`` on a fleet that has never converged.
    """

    def __init__(self, message, reason="unusable"):
        super().__init__(message)
        self.reason = reason


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
    "identity mismatch": "identity_mismatch",
    "identity invalidation": "identity_invalidation",
    "identity publish": "identity_publish",
    "identity sha mismatch": "identity_sha_mismatch",
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

# The value of EDGE_FRAMEWORK_VERSION that means "stay on the framework the
# fleet last converged onto" instead of naming a version.
FRAMEWORK_HOLD = "hold"

# A git commit id, abbreviated or full. The zero SHA of a branch deletion is
# hex-valid, so callers must also refuse it explicitly (see is_valid_sha).
SHA_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")
# PEP 440-shaped, loosely: enough to refuse shell metacharacters and whitespace
# before a version string enters a subprocess argv.
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._!+-]{0,63}$")

# How long one update-script run may take before the subprocess is killed.
# pip installs and migrations are the slow parts; 15 minutes is generous.
SCRIPT_TIMEOUT = 900
# TERM asks the shell transaction to roll back. Dependency restoration can
# legitimately take minutes, so give that recovery its own bounded window
# before the last-resort process-group kill.
ROLLBACK_GRACE_SECONDS = 900


def stderr_tail(raw, limit=10):
    """Return the bounded, per-line-redacted tail of deploy process output."""
    from mojo.apps.account.services.setup_safety import sanitize

    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8", "replace")
    lines = [line for line in str(raw or "").splitlines() if line.strip()]
    return [sanitize(line, max_bytes=1024) for line in lines[-limit:]]

# Version of the small on-disk deployment identity document. This is data
# shape, not an update-script permission gate.
DEPLOY_IDENTITY_SCHEMA = 2

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

# Arming is the same shape: set when NOTHING is armed, or when the armed lease
# is already terminal-failed. `migrating`/`deploying` are never stealable —
# that is the "one live deploy" invariant. A lease that will not decode is
# treated as absent: garbage in the key must not be able to wedge the plane.
_ARM_STATUS_LUA = """
local raw = redis.call('GET', KEYS[1])
if raw then
  local ok, cur = pcall(cjson.decode, raw)
  if ok and type(cur) == 'table' and cur['state'] ~= ARGV[3] then return 0 end
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[2]))
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


def framework_version_pin():
    """The operator's framework hold, normalized, read live from the database.

    NOT a ``settings.get_static`` read, and the exception to this module's
    "never ``settings.get``" rule is deliberate: the whole point of this
    setting is that an operator can freeze the fleet's framework version from
    the Admin portal without shipping a deploy. It is therefore a **protected**
    system setting, whose only writer proves an active literal superuser —
    a generic ``manage_settings`` grant cannot reach it, which is exactly the
    property the get_static rule protects for the other EDGE_DEPLOY_* keys.

    Re-normalizing what the validator already normalized is defense in depth:
    a row written before the validator existed must not become a deploy argv.
    """
    from mojo.apps.account.models import Setting
    from mojo.apps.edge import settings_validators

    # Setting.get_from_db, not system_settings.get_value: get_value re-parses
    # the stored string as JSON, so a number-shaped pin ("1.12", "2") would
    # come back as a float/int the validator refuses — every deploy refusing
    # while the portal reports the pin as saved.
    raw, found = Setting.get_from_db(settings_validators.FRAMEWORK_VERSION_KEY)
    try:
        return settings_validators.framework_pin(
            settings_validators.FRAMEWORK_VERSION_KEY, raw if found else "")
    except ValueError as err:
        raise FrameworkPinError(str(err)) from None


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

    Lands when nothing is armed, or when the armed lease is already terminal
    ``failed`` — a finished deploy is not a reason to refuse the next one, and
    refusing for the rest of the TTL is how a single failed deploy used to
    swallow every push behind it. A ``migrating`` or ``deploying`` lease is
    never stolen: of two same-second webhooks exactly one still starts a
    deploy and the other has merely recorded its target.

    ``force=True`` is the orchestrator's chain re-arm, where the old status is
    deliberately replaced whatever it says. Returns whether the arm landed.
    """
    payload = json.dumps(dict(
        state=STATUS_MIGRATING, sha=sha,
        deployment=str(deployment_id) if deployment_id else "",
        at=dates.utcnow().isoformat()))
    client = get_client()
    if force:
        return bool(client.set(STATUS_KEY, payload, ex=status_ttl()))
    return bool(client.eval(
        _ARM_STATUS_LUA, 1, STATUS_KEY, payload, status_ttl(), STATUS_FAILED))


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
    """The ONE django-mojo version this deploy installs, resolved once (D6).

    Not "the newest" — newest is only the DEFAULT. ``EDGE_FRAMEWORK_VERSION``
    is the operator's hold, and it decides which of three branches runs:

    - **unset** — resolve the newest published release from PyPI. The original
      behavior, and still the common case.
    - **``hold``** — the framework version of the last CONVERGED deploy, so a
      commit ships without also moving the framework.
    - **a version** — exactly that version, with no PyPI request at all, so a
      pinned fleet still deploys while PyPI is unreachable.

    Raises on any failure — C1's whole point is that a silently skipped
    upgrade is the failure mode, so a deploy that cannot resolve the version
    fails rather than letting each node resolve "latest" at its own moment.
    The corollary is what the pin adds: an operator who asked NOT to take
    latest must never be handed latest as a fallback, so an unusable hold
    raises ``FrameworkPinError`` instead of falling through to PyPI.
    """
    from mojo.apps.edge.services import platform_deploy

    pin = framework_version_pin()
    if pin == FRAMEWORK_HOLD:
        version = platform_deploy.last_converged_framework()
        if not is_valid_version(version or ""):
            raise FrameworkPinError(
                "EDGE_FRAMEWORK_VERSION is 'hold' but this fleet has no "
                "converged deployment to hold at",
                reason="hold_without_converged")
        return version
    if pin:
        # Validated on write and re-validated on read; PyPI is never asked
        # whether the version exists — pip is what discovers that, on the
        # node, with its own message.
        return pin
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


def resume_stranded_target():
    """Republish the orchestrate job for a target whose deploy never started.

    The wedge this heals: something armed the lease and then died before —
    or instead of — publishing an orchestrator (a queue outage, a runner that
    vanished between arm and publish, an orchestrate job that expired
    undelivered). The target is recorded, the durable row still says
    ``requested``, and nothing is going to move it. Before this, the fleet sat
    on the old commit until a human pushed again.

    Every guard is a reason NOT to act, and one publish per sweep at most:

    - a live lease (``migrating``/``deploying``) means a deploy IS running;
    - the target must name a valid sha and a deployment UUID;
    - that row must exist, carry the same sha, and still be exactly
      ``requested`` — a row already at canary/fleet/terminal is not stranded;
    - ``arm_status`` must land. That is the atomic claim: two reconcilers on
      two nodes race here and exactly one wins, which is the same rule the
      webhook receiver uses.

    Returns the resumed sha, or None when nothing needed resuming.
    """
    from mojo.apps import jobs
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import platform_deploy

    status = get_status()
    if status and status.get("state") != STATUS_FAILED:
        return None
    target = get_target() or {}
    sha = target.get("sha") or ""
    row_id = platform_deploy.deployment_id(target.get("deployment"))
    if not is_valid_sha(sha) or not row_id:
        return None
    row = platform_deploy.get(row_id)
    if (row is None or row.sha != sha
            or row.status != PlatformDeployment.STATUS_REQUESTED):
        return None
    if not arm_status(sha, deployment_id=row.pk):
        return None
    try:
        jobs.publish(
            func=DEPLOY_ORCHESTRATE_JOB,
            payload=dict(sha=sha, deployment=str(row.pk)),
            channel=DEPLOY_CHANNEL,
            max_retries=0,
            expires_in=canary_timeout())
    except Exception:
        # Same rollback as request_deploy: never leave a claimed lease with
        # no orchestrator behind it.
        platform_deploy.transition(
            row.pk, "failed", {"reason": "orchestrator_publish_failed"})
        try:
            clear_status(row.pk)
        except Exception:
            pass
        return None
    platform_deploy.transition(
        row.pk, "requested",
        {"queue": "orchestrator_republished", "reason": "stranded_target"})
    logger.info(f"deploy {sha}: resumed a stranded target")
    return sha


def _run(argv, popen=None, kill_group=None, timeout=None):
    """Run one update process group and reap every child on timeout."""
    import os
    import signal

    popen = popen or subprocess.Popen
    kill_group = kill_group or os.killpg
    timeout = SCRIPT_TIMEOUT if timeout is None else timeout
    environment = os.environ.copy()
    environment["MOJO_DEPLOY_PARENT_STATUS"] = "1"
    process = popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=environment, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as err:
        kill_group(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=ROLLBACK_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            kill_group(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            argv, timeout, output=stdout or err.output,
            stderr=stderr or err.stderr) from None
    return subprocess.CompletedProcess(
        argv, process.returncode, stdout=stdout, stderr=stderr)
