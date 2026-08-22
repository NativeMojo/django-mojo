"""The isolated brownfield DAG: dependencies, identity, nodes, edge, telemetry."""

import hashlib
import json

from objict import objict

from mojo.deploy.provision import (brownfield_discover, brownfield_identity,
                                   brownfield_observability,
                                   brownfield_policy, balancer, nodes, plan,
                                   report, spec as spec_module)


class DependencyDriftError(RuntimeError):
    pass


STEPS = (
    ("identity", brownfield_identity.ensure_identity),
    ("nodes", nodes.ensure_nodes),
    ("balancer", balancer.ensure_balancer),
    ("telemetry", brownfield_observability.ensure_observability),
)


def observe(clients, topology):
    return _prepare(clients, topology)


def apply(clients, topology, expected_digest, expected_action_digest):
    """Re-observe, compare the preview digest, then reach mutations."""
    if not expected_digest:
        raise DependencyDriftError(
            "brownfield apply requires the dependency digest printed by its "
            "immediately preceding preview")
    if not expected_action_digest:
        raise DependencyDriftError(
            "brownfield apply requires the action digest printed by its "
            "immediately preceding preview")
    findings, actions, run = _prepare(clients, topology)
    actual = run.observed.get("dependency_digest")
    if actual != expected_digest:
        raise DependencyDriftError(
            f"dependency digest changed between preview and apply "
            f"({expected_digest} -> {actual}); nothing was mutated")
    actual_actions = run.observed.get("action_digest")
    if actual_actions != expected_action_digest:
        raise DependencyDriftError(
            f"preview action set changed between confirmation and apply "
            f"({expected_action_digest} -> {actual_actions}); nothing was "
            f"mutated")
    if run.blocking:
        return findings, actions, run
    return _walk(clients, topology, run.observed, apply=True,
                 dependency_findings=[finding for finding in findings
                                      if finding.step == "dependencies"])


def _prepare(clients, topology):
    problems = spec_module.validate_names(topology)
    if problems:
        findings = [report.Finding(
            "validate", report.BLIND, "fleet.invalid", problem,
            "fix the fleet manifest; nothing was mutated")
                    for problem in problems]
        return findings, [], _failed_run(findings, problems)
    dependency_findings, observed = brownfield_discover.observe(
        clients, topology)
    if report.Report(dependency_findings).is_blocking():
        run = _new_run(observed)
        run.steps.dependencies = objict(
            status=plan.FAILED, depends_on=[], blocked_by=[], values={})
        for name, _ensure in STEPS:
            run.steps[name] = objict(status=plan.BLOCKED,
                                     depends_on=["dependencies"],
                                     blocked_by=["dependencies"], values={})
        return _finish(dependency_findings, [], run)
    return _walk(clients, topology, observed, apply=False,
                 dependency_findings=dependency_findings)


def _walk(clients, topology, observed, apply=False,
          dependency_findings=None):
    findings = list(dependency_findings or ())
    actions = []
    run = _new_run(observed)
    run.steps.dependencies = objict(
        status=plan.OK, depends_on=[], blocked_by=[],
        values={"dependency_digest": observed.get("dependency_digest")})

    if not apply:
        preview_observed = objict(observed)
        previous = "dependencies"
        for name, ensure in STEPS:
            step_findings, step_actions, result = ensure(
                clients, topology, preview_observed, False)
            findings.extend(step_findings)
            actions.extend(step_actions)
            values = result.as_dict()
            preview_observed.update(values)
            run.steps[name] = objict(
                status=plan.outcome(step_findings),
                depends_on=[previous],
                blocked_by=[], values=values)
            previous = name
        brownfield_policy.validate_actions(
            actions, allowed_targets=_allowed_action_targets(topology))
        preview_observed.action_digest = _action_digest(actions)
        run.observed = preview_observed
        return _finish(findings, actions, run)

    previous = "dependencies"
    for index, (name, ensure) in enumerate(STEPS):
        step_findings, step_actions, result = ensure(
            clients, topology, observed, True)
        findings.extend(step_findings)
        actions.extend(step_actions)
        values = result.as_dict()
        observed.update(values)
        status = plan.outcome(step_findings)
        run.steps[name] = objict(status=status, depends_on=[previous],
                                 blocked_by=[], values=values)
        if status in plan.STOPPING:
            for blocked_name, _blocked_ensure in STEPS[index + 1:]:
                run.steps[blocked_name] = objict(
                    status=plan.BLOCKED, depends_on=[name],
                    blocked_by=[name], values={})
            break
        previous = name
    run.observed = observed
    return _finish(findings, actions, run)


def _allowed_action_targets(topology):
    """The exact structured action boundary for this declaration."""
    manifest = topology.brownfield_manifest
    allowed = {}

    def add(step, verb, values):
        allowed.setdefault((step, verb), set()).update(
            value for value in values if value)

    node_names = [row["name"] for row in manifest["nodes"]["items"]]
    add("nodes", "create", node_names)

    for declaration in manifest["nodes"]["profiles"].values():
        managed = declaration.get("managed")
        if not managed:
            continue
        add("identity", "create",
            (managed["role_name"], managed["profile_name"]))
        add("identity", "modify", (managed["role_name"],))
        add("identity", "attach", (managed["role_name"],))

    balancer = manifest["load_balancer"]
    edge_names = (balancer["name"], balancer["api_target_group"],
                  balancer["certbot_target_group"])
    eips = [f"{subnet_id} elastic IP"
            for subnet_id in balancer["subnet_ids"]]
    add("balancer", "create", edge_names + ("TCP:443", "TCP:80")
        + tuple(eips))
    add("balancer", "modify", edge_names)
    add("balancer", "register", ("api", "certbot"))

    log_groups = [f"/mojo/{topology.project}-{topology.fleet}/{kind}"
                  for kind in brownfield_observability.LOG_KINDS]
    alarms = [
        f"{topology.project}-{topology.fleet}-{role}-unhealthy"
        for role in ("api", "certbot")]
    add("telemetry", "create", log_groups + alarms)
    add("telemetry", "modify", log_groups + alarms)
    return allowed


def _action_digest(actions):
    rows = [action.as_dict() for action in actions]
    body = json.dumps(rows, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _new_run(observed):
    return objict(steps=objict(), observed=observed, worst=report.PASS,
                  blocking=False, validated=True, problems=[])


def _failed_run(findings, problems):
    run = _new_run(objict())
    run.validated = False
    run.problems = list(problems)
    for name in ("dependencies",) + tuple(row[0] for row in STEPS):
        run.steps[name] = objict(status=plan.BLOCKED, depends_on=[],
                                 blocked_by=["validate"], values={})
    return _finish(findings, [], run)[2]


def _finish(findings, actions, run):
    summary = report.Report(findings, actions)
    run.worst = summary.worst()
    run.blocking = summary.is_blocking()
    return findings, actions, run
