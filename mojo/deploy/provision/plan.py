"""The dependency graph, and the two functions everything else calls.

    findings, actions, run = plan.observe(clients, spec)   # read-only
    findings, actions, run = plan.apply(clients, spec)     # converge

`run` is an objict:

    run.steps       {step name: objict(status, depends_on, blocked_by, values)}
    run.observed    the merged account shape, with each step's resolved values
                    folded in — this is how `nodes` learns the subnet ids that
                    `network` created a moment ago
    run.worst       the highest-severity finding status in the whole run
    run.blocking    True when something failed or could not be seen

THE EDGES ARE REAL, not an ordering. "Blocked" has to mean something for a step
whose failed dependency is four steps back — `dns` does not care that `network`
ran, it cares that `nodes` produced an address — and an ordered list cannot
express that. Each step declares what it needs; the walk is a topological sort
over those declarations, and `_ordered()` refuses a graph with a cycle or a
dependency that does not exist rather than silently running in file order.

THREE WAYS A STEP DOES NOT RUN, and they mean different things:

    DISABLED   the topology does not want it. `balancer` on the micro preset,
               `dns` with no domain. A disabled step blocks nothing — `dns`
               depends on `balancer` and still runs on micro, pointing at the
               node's own address instead.
    BLOCKED    something it depends on FAILED. Running anyway would mean making
               AWS calls with values that were never resolved.
    SKIPPED    something it depends on is PENDING. Not a failure: Aurora takes
               ten minutes, and the next `apply` picks this up.

WHICH IS WHY A FRESH ACCOUNT TAKES MORE THAN ONE `apply`. The first run creates
the network, the bucket, the secrets, the database and the cache, and stops
there because the database has no endpoint yet. The second, ten minutes later,
writes the stage-1 payload and launches the nodes. The third attaches the
balancer. Every run is safe to interrupt and safe to repeat, and running against
a converged account creates nothing at all — that property is worth more than
finishing in one pass.

THERE IS NO STATE FILE. Resume is re-observation. A state file would be a second
source of truth about an account that can be changed from four other places, and
the first time it disagreed with reality it would be worse than useless.
"""

from objict import objict

from mojo.deploy.provision import (balancer, data, discover, dns, encryption,
                                   identity, network, nodes, observability,
                                   report, storage)
from mojo.deploy.provision import spec as spec_module


OK = "OK"
PENDING = "PENDING"
FAILED = "FAILED"
BLOCKED = "BLOCKED"
SKIPPED = "SKIPPED"
DISABLED = "DISABLED"

# A dependency in one of these states stops its dependents dead.
STOPPING = (FAILED, BLOCKED)
# A dependency in one of these means "not yet" — try again next run.
WAITING = (PENDING, SKIPPED)


class Step:
    """One node in the graph.

    `run` is any `ensure_*(clients, spec, observed, apply=False)`.
    `enabled` decides whether the topology wants this step at all; it takes the
    spec and nothing else, so the answer never depends on what AWS said.
    """

    def __init__(self, name, run, depends_on=(), enabled=None, description=""):
        self.name = name
        self.run = run
        self.depends_on = tuple(depends_on)
        self._enabled = enabled
        self.description = description

    def enabled(self, spec):
        return True if self._enabled is None else bool(self._enabled(spec))

    def __repr__(self):
        return f"<Step {self.name}>"


def _ensure_account(clients, spec, observed, apply=False):
    """Confirm there is a usable credential before anything is built with it.

    Cheap, and it converts the single most common bootstrap failure — expired
    or wrong-account credentials — from a confusing error eight resources in
    into the first line of the report.
    """
    findings, actions = [], []
    result = report.Result()
    account_id = observed.get("account_id")
    if not account_id:
        findings.append(report.Finding(
            "account", report.BLIND, "account.unknown",
            "sts:GetCallerIdentity did not answer, so the account this would "
            "build in is unknown",
            "check the credential is present, active and not expired"))
        return findings, actions, result
    findings.append(report.existing(
        "account", "account.ok",
        f"account {account_id} in {spec.region} as {observed.get('caller_arn')}"))
    result.set("account_id", account_id)
    return findings, actions, result


STEPS = (
    Step("account", _ensure_account, (),
         description="who am I, and where"),
    Step("config_bucket", storage.ensure_config_bucket, ("account",),
         description="the bucket everything else is read from"),
    Step("secrets", storage.ensure_secrets, ("config_bucket",),
         description="generated once, read back forever"),
    # The edge plane's two prerequisites. Both depend only on the account, so
    # an environment gets them on its first apply rather than discovering at
    # WebApp-onboarding time that it cannot hold a certificate.
    Step("encryption", encryption.ensure_key, ("account",),
         description="the KMS key KSMSecrets models encrypt against"),
    Step("releases_bucket", storage.ensure_releases_bucket, ("account",),
         description="where WebApp artifacts are published and fetched"),
    Step("network", network.ensure_vpc, ("account",),
         description="VPC, subnets, gateway, routes, S3 endpoint"),
    Step("security_groups", network.ensure_security_groups, ("network",),
         description="who may talk to the nodes, the database and the cache"),
    # The key pair depends on the secrets object, not on the network: a
    # generated private key has to land somewhere in the same step it is
    # created, and that somewhere is the secrets object.
    Step("key_pair", identity.ensure_key_pair, ("secrets",),
         description="how an operator reaches a node"),
    Step("node_role", identity.ensure_node_role, ("account",),
         description="what a node is allowed to do"),
    Step("db", data.ensure_database, ("network", "security_groups", "secrets"),
         description="Aurora PostgreSQL"),
    Step("cache", data.ensure_cache, ("network", "security_groups", "secrets"),
         description="Valkey replication group"),
    Step("stage1_payload", storage.ensure_stage1_payload,
         ("secrets", "db", "cache"),
         description="the endpoints a booting node reads"),
    # The scripts and the application tarball a booting node downloads. It
    # depends only on the bucket, so a not-yet-published version pin or a
    # broken worktree is discovered on the FIRST run — before Aurora exists,
    # let alone a node.
    #
    # Enabled by `project_root`, which is where `git archive HEAD` is taken
    # from. Only the CLI sets it, because only the CLI is standing in the
    # project; a caller that is not — the portal, a test — has nothing to
    # archive, and `nodes` refuses to launch rather than starting an instance
    # with no payload to boot from.
    Step("bootstrap_payload", storage.ensure_bootstrap_payload,
         ("config_bucket",),
         enabled=lambda spec: bool(getattr(spec, "project_root", None)),
         description="stage1.sh, the app tarball, the agent config"),
    Step("nodes", nodes.ensure_nodes,
         ("network", "security_groups", "key_pair", "node_role",
          "stage1_payload", "bootstrap_payload"),
         description="the EC2 instances"),
    Step("balancer", balancer.ensure_balancer, ("network", "nodes"),
         enabled=spec_module.wants_balancer,
         description="NLB, target groups, listeners"),
    Step("observability", observability.ensure_observability, ("account",),
         description="CloudTrail, GuardDuty, the log groups"),
    # Depends on both. Whichever one produced an address is what the record
    # points at, and on a preset with no balancer the disabled step blocks
    # nothing.
    Step("dns", dns.ensure_dns, ("nodes", "balancer"),
         enabled=lambda spec: bool(spec.domain),
         description="hosted zone and A records"),
)

BY_NAME = {step.name: step for step in STEPS}


def _ordered(steps=STEPS):
    """Topological order, or an exception naming what is wrong with the graph.

    Deliberately strict. A dependency on a step that does not exist, or a cycle,
    is a programming error in this file and should stop the process at import-
    adjacent time — not degrade into "ran in whatever order the tuple happened
    to be in", which is the failure mode that makes a DAG worth nothing.
    """
    known = {step.name for step in steps}
    for step in steps:
        unknown = [name for name in step.depends_on if name not in known]
        if unknown:
            raise ValueError(
                f"step {step.name!r} depends on unknown step(s): "
                f"{', '.join(unknown)}")

    ordered, placed = [], set()
    remaining = list(steps)
    while remaining:
        ready = [step for step in remaining
                 if all(name in placed for name in step.depends_on)]
        if not ready:
            stuck = ", ".join(sorted(step.name for step in remaining))
            raise ValueError(f"dependency cycle among: {stuck}")
        for step in ready:
            ordered.append(step)
            placed.add(step.name)
        remaining = [step for step in remaining if step.name not in placed]
    return ordered


def observe(clients, spec, steps=STEPS):
    """Read the account and evaluate every step without changing anything.

    Returns `(findings, actions, run)`. The actions are what `apply` WOULD do —
    that is the whole point of the dry run, and it is what the CLI shows before
    asking for a yes.
    """
    return _walk(clients, spec, apply=False, steps=steps)


def apply(clients, spec, steps=STEPS):
    """Converge the account toward the spec.

    Step 0 is name validation, and it runs BEFORE the first AWS call. A target
    group name two characters too long is worth finding in a millisecond; found
    after the VPC, the subnets and an encrypted Aurora cluster already exist, it
    is a bill and a manual cleanup, because nothing in this package deletes.
    """
    return _walk(clients, spec, apply=True, steps=steps)


def _walk(clients, spec, apply=False, steps=STEPS):
    findings, actions = [], []
    run = objict(steps=objict(), observed=discover.blank(), worst=report.PASS,
                 blocking=False, validated=True, problems=[])

    problems = spec_module.validate_names(spec)
    if problems:
        run.validated = False
        run.problems = list(problems)
        for problem in problems:
            findings.append(report.Finding(
                "validate", report.MANUAL, "names.invalid", problem,
                "fix the spec and re-run — nothing was created"))
        for step in steps:
            run.steps[step.name] = objict(
                status=BLOCKED, depends_on=list(step.depends_on),
                blocked_by=["validate"], values={})
        return _finish(findings, actions, run)

    observed_findings, observed = discover.observe(clients, spec)
    findings.extend(observed_findings)
    run.observed = observed

    for step in _ordered(steps):
        if not step.enabled(spec):
            run.steps[step.name] = objict(
                status=DISABLED, depends_on=list(step.depends_on),
                blocked_by=[], values={})
            continue

        stopped = [name for name in step.depends_on
                   if run.steps.get(name, objict()).get("status") in STOPPING]
        if stopped:
            findings.append(report.Finding(
                step.name, report.BLOCKED, f"{step.name}.blocked",
                f"{step.name} did not run because {', '.join(stopped)} failed",
                "fix the reported failure above and re-run"))
            run.steps[step.name] = objict(
                status=BLOCKED, depends_on=list(step.depends_on),
                blocked_by=stopped, values={})
            continue

        waiting = [name for name in step.depends_on
                   if run.steps.get(name, objict()).get("status") in WAITING]
        if waiting:
            findings.append(report.pending(
                step.name,
                f"{step.name}.waiting",
                f"{step.name} is waiting on {', '.join(waiting)}"))
            run.steps[step.name] = objict(
                status=SKIPPED, depends_on=list(step.depends_on),
                blocked_by=waiting, values={})
            continue

        step_findings, step_actions, result = step.run(
            clients, spec, observed, apply)
        findings.extend(step_findings)
        actions.extend(step_actions)
        values = result.as_dict()
        # Fold what this step resolved into the observation, so the steps after
        # it read real ids rather than re-deriving names and hoping.
        observed.update(values)
        run.steps[step.name] = objict(
            status=outcome(step_findings), depends_on=list(step.depends_on),
            blocked_by=[], values=values)

    return _finish(findings, actions, run)


def outcome(findings):
    """A step's status, from what it reported.

    BLIND is FAILED, not a warning: it means the call was not made, so nothing
    downstream may assume this step's values exist.
    """
    statuses = {finding.status for finding in findings}
    if statuses & set(report.BLOCKING_STATUSES):
        return FAILED
    if report.PENDING in statuses:
        return PENDING
    return OK


def _finish(findings, actions, run):
    summary = report.Report(findings, actions)
    run.worst = summary.worst()
    run.blocking = summary.is_blocking()
    return findings, actions, run
