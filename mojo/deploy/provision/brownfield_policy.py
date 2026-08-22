"""Closed AWS mutation and preview-action boundary for brownfield fleets."""


class BrownfieldCallBlocked(RuntimeError):
    pass


# Metadata reads are exact per service. A generic ``get_*`` exception would
# also authorize s3.GetObject and secretsmanager.GetSecretValue, which crosses
# the central brownfield rule: this process may observe metadata, never secret
# or object bodies.
ALLOWED_READS = {
    "sts": frozenset(("get_caller_identity",)),
    "ec2": frozenset((
        "describe_addresses", "describe_availability_zones",
        "describe_images", "describe_instances", "describe_key_pairs",
        "describe_route_tables", "describe_security_groups",
        "describe_subnets", "describe_vpcs",
    )),
    "rds": frozenset(("describe_db_clusters", "describe_db_subnet_groups")),
    "elasticache": frozenset((
        "describe_cache_subnet_groups", "describe_replication_groups",
    )),
    "s3": frozenset((
        "get_bucket_location", "head_object", "list_objects_v2",
    )),
    "kms": frozenset(("describe_key", "get_key_policy", "list_grants")),
    "sns": frozenset(("get_topic_attributes",)),
    "iam": frozenset((
        "get_instance_profile", "get_role", "get_role_policy",
        "list_attached_role_policies", "list_role_policies",
    )),
    "elbv2": frozenset((
        "describe_listeners", "describe_load_balancer_attributes",
        "describe_load_balancers", "describe_tags", "describe_target_groups",
        "describe_target_health",
    )),
    "logs": frozenset(("describe_log_groups", "list_tags_log_group")),
    "cloudwatch": frozenset(("describe_alarms", "list_tags_for_resource")),
}

ALLOWED_MUTATIONS = {
    "ec2": frozenset(("run_instances", "allocate_address")),
    "elbv2": frozenset((
        "create_target_group", "modify_target_group",
        "create_load_balancer", "modify_load_balancer_attributes",
        "create_listener", "register_targets",
    )),
    "iam": frozenset((
        "create_role", "put_role_policy", "create_instance_profile",
        "add_role_to_instance_profile", "attach_role_policy",
    )),
    "logs": frozenset(("create_log_group", "put_retention_policy")),
    "cloudwatch": frozenset(("put_metric_alarm",)),
}

ALLOWED_ACTIONS = {
    "identity": frozenset(("create", "modify", "attach")),
    "nodes": frozenset(("create",)),
    "balancer": frozenset(("create", "modify", "register")),
    "telemetry": frozenset(("create", "modify")),
}

PROHIBITED_TARGET_MARKERS = (
    "route53", "hosted zone", "certificate", "acm", "aurora", "database",
    "cache", "valkey", "vpc", "route table", "internet gateway", "kms",
    "current eip", "preserved eip", "live instance", "releaseaddress",
)


class MutationPolicy:
    """Attribute-level positive allowlist used by ``discover.Clients``."""

    def authorize(self, service, method):
        if method in ALLOWED_READS.get(service, ()):
            return
        if method in ALLOWED_MUTATIONS.get(service, ()):
            return
        raise BrownfieldCallBlocked(
            f"{service}.{method} is not in the brownfield preparation "
            f"mutation allowlist")


def validate_actions(actions, allowed_targets):
    """Reject a complete preview before the first mutation is reachable.

    ``allowed_targets`` is an exact ``(step, verb) -> names`` matrix derived
    from the fleet declaration.  Substring ownership is deliberately not good
    enough here: ``api`` occurring inside an arbitrary resource name must not
    make that resource eligible for mutation.
    """
    problems = []
    for action in actions:
        step = getattr(action, "step", None)
        verb = getattr(action, "verb", None)
        target = str(getattr(action, "target", "") or "")
        if verb not in ALLOWED_ACTIONS.get(step, ()):
            problems.append(
                f"{step}.{verb} {target!r} is outside the brownfield action "
                f"allowlist")
            continue
        lowered = target.lower()
        if any(marker in lowered for marker in PROHIBITED_TARGET_MARKERS):
            problems.append(
                f"{step}.{verb} targets prohibited dependency {target!r}")
        exact = set((allowed_targets or {}).get((step, verb), ()))
        if target not in exact:
            problems.append(
                f"{step}.{verb} target {target!r} is not an exact declared "
                f"migration-owned target")
    if problems:
        raise BrownfieldCallBlocked("; ".join(problems))
    return True
