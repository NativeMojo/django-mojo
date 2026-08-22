"""Fleet-only log retention and NLB health alarms."""

from mojo.deploy.provision import report, spec as spec_module


STEP = "telemetry"
LOG_KINDS = ("nginx", "app", "cloud-init")
RETENTION_DAYS = 90


def ensure_observability(clients, topology, observed, apply=False):
    findings, actions = [], []
    result = report.Result()
    logs = clients.get("logs")
    cloudwatch = clients.get("cloudwatch")
    root = f"/mojo/{topology.project}-{topology.fleet}"
    current_groups = observed.get("log_groups") or {}
    group_collisions = set(observed.get("log_group_collisions") or ())
    group_names = []
    for kind in LOG_KINDS:
        name = f"{root}/{kind}"
        group_names.append(name)
        current = current_groups.get(name)
        if name in group_collisions:
            findings.append(report.Finding(
                STEP, report.BLIND, "log_group.name_collision",
                f"{name} exists without exact fleet ownership tags",
                "rename the fleet resource or resolve the unowned collision; "
                "retention was not changed"))
            continue
        if not current:
            findings.append(report.missing(
                STEP, "log_group.missing", f"fleet log group {name} is absent",
                "apply creates only this migration-owned log group"))
            actions.append(report.Action(STEP, "create", name))
            if apply:
                created = report.safe(
                    findings, STEP, "logs.create_log_group",
                    lambda n=name: logs.create_log_group(
                        logGroupName=n, tags=_tags(topology)))
                if created is not None:
                    report.safe(
                        findings, STEP, "logs.put_retention_policy",
                        lambda n=name: logs.put_retention_policy(
                            logGroupName=n, retentionInDays=RETENTION_DAYS))
            continue
        if current.get("retentionInDays") != RETENTION_DAYS:
            findings.append(report.drift(
                STEP, "log_group.retention", f"{name} retention differs",
                f"apply sets {RETENTION_DAYS} days"))
            actions.append(report.Action(STEP, "modify", name,
                                         f"retention {RETENTION_DAYS} days"))
            if apply:
                report.safe(
                    findings, STEP, "logs.put_retention_policy",
                    lambda n=name: logs.put_retention_policy(
                        logGroupName=n, retentionInDays=RETENTION_DAYS))
        else:
            findings.append(report.existing(
                STEP, "log_group.ok", f"{name} retains {RETENTION_DAYS} days"))

    existing = {row.get("AlarmName"): row
                for row in observed.get("brownfield_alarms") or []}
    alarm_collisions = set(observed.get("alarm_collisions") or ())
    for alarm in _alarm_specs(topology, observed):
        if alarm["AlarmName"] in alarm_collisions:
            findings.append(report.Finding(
                STEP, report.BLIND, "alarm.name_collision",
                f"{alarm['AlarmName']} exists without exact fleet ownership tags",
                "rename the fleet alarm or resolve the unowned collision; it "
                "was not overwritten"))
            continue
        current = existing.get(alarm["AlarmName"])
        if current and _alarm_matches(current, alarm):
            findings.append(report.existing(
                STEP, "alarm.ok", f"{alarm['AlarmName']} matches"))
            continue
        status = report.missing if not current else report.drift
        findings.append(status(
            STEP, "alarm.missing" if not current else "alarm.drift",
            f"fleet alarm {alarm['AlarmName']} is absent or differs",
            "apply converges the migration-owned alarm"))
        actions.append(report.Action(
            STEP, "create" if not current else "modify", alarm["AlarmName"]))
        ready = alarm.pop("_ready", False)
        if apply and ready:
            report.safe(
                findings, STEP, "cloudwatch.put_metric_alarm",
                lambda request=dict(alarm): cloudwatch.put_metric_alarm(**request))

    result.set("fleet_log_groups", group_names)
    return findings, actions, result


def _alarm_specs(topology, observed):
    names = spec_module.names(topology)
    topic = topology.brownfield_manifest.get("alarm_topic_arn")
    actions = [topic] if topic else []
    common = {
        "Namespace": "AWS/NetworkELB", "Statistic": "Maximum",
        "Period": 60, "EvaluationPeriods": 3, "DatapointsToAlarm": 2,
        "ComparisonOperator": "GreaterThanThreshold",
        "Threshold": 0.0, "TreatMissingData": "missing",
        "AlarmActions": actions, "OKActions": actions,
        "Tags": [{"Key": key, "Value": value}
                 for key, value in _tags(topology).items()],
    }
    rows = []
    target_groups = observed.get("target_groups") or {}
    created_arns = observed.get("target_group_arns") or {}
    balancer = observed.get("balancer") or {}
    balancer_arn = (balancer.get("LoadBalancerArn")
                    or observed.get("balancer_arn"))
    for role in ("api", "certbot"):
        group = target_groups.get(role) or {}
        arn = group.get("TargetGroupArn") or created_arns.get(role)
        request = dict(common)
        request.update({
            "AlarmName": f"{topology.project}-{topology.fleet}-{role}-unhealthy",
            "AlarmDescription": f"Unhealthy {role} targets on {names['balancer']}",
            "MetricName": "UnHealthyHostCount",
            "Dimensions": [
                {"Name": "TargetGroup", "Value": (arn or "pending").split(
                    "targetgroup/", 1)[-1]},
                {"Name": "LoadBalancer",
                 "Value": (balancer_arn or "pending").split(
                     "loadbalancer/", 1)[-1]},
            ],
            "_ready": bool(arn and balancer_arn),
        })
        rows.append(request)
    return rows


def _alarm_matches(current, wanted):
    keys = ("Namespace", "MetricName", "Statistic", "Period",
            "EvaluationPeriods", "DatapointsToAlarm", "Threshold",
            "ComparisonOperator", "TreatMissingData", "Dimensions",
            "AlarmActions", "OKActions")
    return all(current.get(key) == wanted.get(key) for key in keys)


def _tags(topology):
    return {
        "managed-by": "django-mojo", "mojo:project": topology.project,
        "mojo:env": topology.env, "mojo:fleet": topology.fleet,
        "mojo:role": "telemetry",
    }
