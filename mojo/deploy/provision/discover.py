"""Read the account's shape, and hand out the clients that read it.

THREE THINGS IN THIS REPOSITORY LOOK AT AN AWS ACCOUNT. They are not
interchangeable, and picking the wrong one wastes an afternoon:

    mojo/deploy/check_setup.py          pre-Django, read-only, JUDGES. It scores
                                        an account against the django-mojo
                                        reference topology and universal
                                        security expectations, and exits
                                        non-zero so it can gate a deploy. Use it
                                        to answer "is this account set up
                                        correctly?".
    mojo/apps/aws/services/aws_check.py in-Django, reads settings, and CREATES
                                        MISSING integration surfaces — the cron
                                        rule, the S3 file manager, SES, dnsman.
                                        It is about a running deployment's
                                        readiness, not about infrastructure.
                                        Use it to answer "can this deployment
                                        talk to AWS?".
    this module                          pre-Django, read-only, and JUDGES
                                        NOTHING. It returns the raw shape of
                                        the resources this package manages so
                                        `ensure_*` can decide what to create.
                                        Use it to answer "what is already
                                        there?".

TAG-FIRST, AND NEVER ADOPT-BY-SINGLETON. Every lookup filters on the `mojo:*`
tags from `spec.TAGS`, and falls back to an exact name match only where AWS has
no tag filter for that resource type. It never concludes "this is the only VPC
in the account, so it must be ours" — an account can already contain someone
else's `wmx-prod-vpc`, and adopting it because the name matched is how a
provisioning tool ends up modifying a stranger's production.
"""

from objict import objict

from mojo.deploy.check_setup import list_paginated
from mojo.deploy.provision import report, spec as spec_module


STEP = "discover"

# boto3 method prefixes this package must never call. Enforced at the seam
# rather than by reading the source, because `getattr(client, verb)()` defeats
# any source scan and is exactly what a well-meaning refactor reaches for.
DESTRUCTIVE_PREFIXES = ("delete_", "terminate_", "destroy_", "deregister_",
                        "revoke_", "remove_")

# "Not there" is an answer, not a failure. Every one of these is a normal
# response to asking about a resource on an empty account.
NOT_FOUND_CODES = (
    "404", "NotFound", "NoSuchEntity", "NoSuchBucket", "NoSuchTagSet",
    "NoSuchBucketPolicy", "NoSuchPublicAccessBlockConfiguration",
    "NoSuchConfiguration", "ServerSideEncryptionConfigurationNotFoundError",
    "DBClusterNotFoundFault", "DBInstanceNotFound",
    "DBSubnetGroupNotFoundFault", "ReplicationGroupNotFoundFault",
    "CacheSubnetGroupNotFoundFault", "CacheClusterNotFound",
    "LoadBalancerNotFound", "TargetGroupNotFound",
    "ResourceNotFoundException", "ParameterNotFound", "TrailNotFoundException",
    "InvalidKeyPair.NotFound", "InvalidVpcID.NotFound",
    "InvalidGroup.NotFound", "InvalidSubnetID.NotFound",
    "InvalidAllocationID.NotFound", "InvalidInstanceID.NotFound",
)


class DestructiveCallBlocked(RuntimeError):
    """Raised when something reaches for a delete/terminate verb through a
    provisioning client. This package converges accounts; it does not tear them
    down, and a caller that needs to remove a resource does it deliberately,
    elsewhere, with a human watching."""


class GuardedClient:
    """A boto3 client with the destructive half of its API removed.

    The guard lives here rather than in a lint rule because dynamic dispatch is
    the case that matters: a source scan can see `client.delete_bucket(...)` and
    cannot see `getattr(client, "delete_" + kind)(...)`. Both fail here.

    RESIDUAL RISK, stated plainly: this protects the seam, not the process.
    Nothing stops a caller from building its own `boto3.client("ec2")` and
    deleting whatever it likes. The real production control for that is an IAM
    deny policy on the provisioning principal — out of scope for this package,
    and worth having anyway.
    """

    def __init__(self, client, service):
        self._client = client
        self._service = service

    def __getattr__(self, name):
        if name.startswith(DESTRUCTIVE_PREFIXES):
            raise DestructiveCallBlocked(
                f"{self._service}.{name} is a destructive call — "
                f"mojo.deploy.provision creates and modifies, never removes")
        return getattr(self._client, name)

    def __repr__(self):
        return f"<GuardedClient {self._service}>"


class Clients:
    """The seam every AWS call in this package goes through.

    Two reasons it exists instead of `session.client(...)` at the call site:

    1. Tests. `Clients(ec2=stubbed_client)` hands an ensure function a
       `botocore.stub.Stubber`-wrapped client, which validates every request
       against the real service model — so a member spelled wrong, or one that
       does not exist on that operation, fails locally instead of in an account.
       `mojo/deploy/check_setup.py` passes a raw `boto3.Session` around and
       cannot be tested this way; that is the mistake being avoided here.
    2. The destructive-call guard above, which needs one place to wrap.
    """

    def __init__(self, session=None, **overrides):
        self._session = session
        self._overrides = dict(overrides)
        self._cache = {}

    def get(self, service):
        if service in self._cache:
            return self._cache[service]
        client = self._overrides.get(service)
        if client is None:
            client = self._build(service)
        guarded = GuardedClient(client, service)
        self._cache[service] = guarded
        return guarded

    def _build(self, service):
        if self._session is None:
            import boto3
            self._session = boto3.Session()
        return self._session.client(service)


def optional(findings, step, name, func, default=None):
    """A read whose "it does not exist" answer is data rather than a failure.

    Everything else — denied, throttled, a rejected credential, an error nobody
    classified — still goes through `report.safe` and still becomes BLIND, so a
    section that could not be read never masquerades as an empty one.
    """
    def attempt():
        from botocore.exceptions import ClientError
        try:
            return func()
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", "")
            if code in NOT_FOUND_CODES:
                return default
            raise
    return report.safe(findings, step, name, attempt, default)


def wrap(value):
    """Boto3 responses are deep, read-only and awkward to index. objict is the
    right tool for exactly that and the wrong tool for anything that crosses a
    serialization boundary — see report.py for the other half of that split."""
    if value is None:
        return None
    if isinstance(value, dict):
        return objict.fromdict(value)
    if isinstance(value, (list, tuple)):
        return [wrap(item) for item in value]
    return value


def tag_filters(spec, role=None):
    filters = [
        {"Name": "tag:mojo:project", "Values": [spec.project]},
        {"Name": "tag:mojo:env", "Values": [spec.env]},
    ]
    if role:
        filters.append({"Name": "tag:mojo:role", "Values": [role]})
    return filters


def tags_of(resource):
    rows = (resource or {}).get("Tags") or (resource or {}).get("TagList") or []
    return {row.get("Key"): row.get("Value") for row in rows}


def blank():
    """The full observed shape with everything absent.

    Written out rather than grown by accident so a reader can see the whole
    contract in one place, and so an ensure function reading a key that no
    observer ever fills is obvious at review.
    """
    return objict(
        account_id=None, caller_arn=None, region=None,
        azs=[], offered_zone_names=[],
        vpc=None, subnets=[], internet_gateway=None, route_tables=[],
        vpc_endpoints=[], security_groups={},
        key_pair=None, node_role=None, node_role_policy=None,
        instance_profile=None,
        config_bucket=None, config_bucket_state=None, secrets=None,
        stage1=None,
        db_subnet_group=None, db_cluster=None, db_instances=[],
        cache_subnet_group=None, cache_group=None,
        ami_id=None, instances=[], addresses=[],
        balancer=None, target_groups={}, listeners=[], targets={},
        balancer_attributes={},
        cloudtrail_bucket=None, trails=[], detector_ids=[], log_groups={},
        hosted_zone=None, record_sets=[],
    )


def observe(clients, spec):
    """Read everything this package manages, and judge none of it.

    Returns `(findings, observed)`. Findings here are only ever BLIND — the
    places the credential could not see. Whether what was found is right is the
    ensure functions' business, not this one's.

    Every read lives here rather than in the ensure functions, and that is load
    bearing: it is what lets `ensure_*(…, apply=False)` make ZERO AWS calls, so
    a dry run is genuinely free and a Stubber test only has to stub mutations.
    """
    findings = []
    observed = blank()
    observed.region = spec.region

    _observe_account(clients, spec, observed, findings)
    _observe_network(clients, spec, observed, findings)
    _observe_identity(clients, spec, observed, findings)
    _observe_storage(clients, spec, observed, findings)
    _observe_data(clients, spec, observed, findings)
    _observe_nodes(clients, spec, observed, findings)
    _observe_balancer(clients, spec, observed, findings)
    _observe_observability(clients, spec, observed, findings)
    _observe_dns(clients, spec, observed, findings)

    return findings, observed


# ── account ─────────────────────────────────────────────────────────────────

def _observe_account(clients, spec, observed, findings):
    sts = clients.get("sts")
    identity = optional(findings, STEP, "sts.get_caller_identity",
                        lambda: sts.get_caller_identity(), {})
    observed.account_id = (identity or {}).get("Account")
    observed.caller_arn = (identity or {}).get("Arn")
    # The identity policy in identity.py needs the account id to scope the log
    # group ARN. Carrying it on the spec means the policy can be built and
    # compared without threading a second argument through every signature.
    if observed.account_id and not spec.account_id:
        spec.account_id = observed.account_id


# ── network ─────────────────────────────────────────────────────────────────

def _observe_network(clients, spec, observed, findings):
    ec2 = clients.get("ec2")

    zones = optional(findings, STEP, "ec2.describe_availability_zones",
                     lambda: ec2.describe_availability_zones(
                         Filters=[{"Name": "state", "Values": ["available"]}]),
                     {})
    observed.azs = wrap((zones or {}).get("AvailabilityZones") or [])

    # Which zones actually offer the instance type this preset asked for. An
    # account's AZ list is not the same question: `us-east-1e` exists and offers
    # almost nothing. Picking a subnet in one and discovering it at
    # `run_instances` would leave a subnet this tool cannot delete.
    offerings = optional(
        findings, STEP, "ec2.describe_instance_type_offerings",
        lambda: list_paginated(
            ec2, "describe_instance_type_offerings", "InstanceTypeOfferings",
            LocationType="availability-zone",
            Filters=[{"Name": "instance-type", "Values": [spec.node_type]}]),
        [])
    observed.offered_zone_names = sorted(
        {row.get("Location") for row in (offerings or []) if row.get("Location")})

    vpcs = optional(findings, STEP, "ec2.describe_vpcs",
                    lambda: list_paginated(ec2, "describe_vpcs", "Vpcs",
                                           Filters=tag_filters(spec)),
                    [])
    observed.vpc = wrap(_one(vpcs))
    if not observed.vpc:
        return

    vpc_id = observed.vpc.VpcId
    in_vpc = [{"Name": "vpc-id", "Values": [vpc_id]}]

    observed.subnets = wrap(optional(
        findings, STEP, "ec2.describe_subnets",
        lambda: list_paginated(ec2, "describe_subnets", "Subnets",
                               Filters=in_vpc + tag_filters(spec)), []))
    gateways = optional(
        findings, STEP, "ec2.describe_internet_gateways",
        lambda: list_paginated(
            ec2, "describe_internet_gateways", "InternetGateways",
            Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}]), [])
    observed.internet_gateway = wrap(_one(gateways))
    observed.route_tables = wrap(optional(
        findings, STEP, "ec2.describe_route_tables",
        lambda: list_paginated(ec2, "describe_route_tables", "RouteTables",
                               Filters=in_vpc), []))
    observed.vpc_endpoints = wrap(optional(
        findings, STEP, "ec2.describe_vpc_endpoints",
        lambda: list_paginated(ec2, "describe_vpc_endpoints", "VpcEndpoints",
                               Filters=in_vpc), []))

    groups = optional(
        findings, STEP, "ec2.describe_security_groups",
        lambda: list_paginated(ec2, "describe_security_groups",
                               "SecurityGroups",
                               Filters=in_vpc + tag_filters(spec)), []) or []
    by_role = {}
    for group in groups:
        role = tags_of(group).get("mojo:role")
        if role:
            by_role[role] = wrap(group)
    observed.security_groups = by_role


# ── identity ────────────────────────────────────────────────────────────────

def _observe_identity(clients, spec, observed, findings):
    ec2 = clients.get("ec2")
    iam = clients.get("iam")
    names = spec_module.names(spec)

    pairs = optional(findings, STEP, "ec2.describe_key_pairs",
                     lambda: ec2.describe_key_pairs(
                         Filters=tag_filters(spec)), {})
    observed.key_pair = wrap(_one((pairs or {}).get("KeyPairs") or []))

    role = optional(findings, STEP, "iam.get_role",
                    lambda: iam.get_role(RoleName=names["node_role"]), {})
    observed.node_role = wrap((role or {}).get("Role"))

    if observed.node_role:
        policy = optional(
            findings, STEP, "iam.get_role_policy",
            lambda: iam.get_role_policy(RoleName=names["node_role"],
                                        PolicyName=names["node_policy"]), {})
        observed.node_role_policy = _decode_policy((policy or {}).get(
            "PolicyDocument"))

    profile = optional(
        findings, STEP, "iam.get_instance_profile",
        lambda: iam.get_instance_profile(
            InstanceProfileName=names["instance_profile"]), {})
    observed.instance_profile = wrap((profile or {}).get("InstanceProfile"))


def _decode_policy(document):
    """IAM returns an inline policy document as a URL-encoded JSON string, or
    already parsed depending on the client's config. Handle both."""
    if not document:
        return None
    if isinstance(document, dict):
        return document
    import json
    from urllib.parse import unquote
    try:
        return json.loads(unquote(document))
    except (ValueError, TypeError):
        return None


# ── storage ─────────────────────────────────────────────────────────────────

def _observe_storage(clients, spec, observed, findings):
    import json

    s3 = clients.get("s3")
    names = spec_module.names(spec)
    bucket = names["config_bucket"]

    head = optional(findings, STEP, "s3.head_bucket",
                    lambda: s3.head_bucket(Bucket=bucket), None)
    if head is None:
        return
    observed.config_bucket = bucket

    state = objict()
    state.tags = tags_of({"Tags": (optional(
        findings, STEP, "s3.get_bucket_tagging",
        lambda: s3.get_bucket_tagging(Bucket=bucket), {}) or {}).get("TagSet")
        or []})
    state.versioning = (optional(
        findings, STEP, "s3.get_bucket_versioning",
        lambda: s3.get_bucket_versioning(Bucket=bucket), {}) or {}).get("Status")
    state.encryption = optional(
        findings, STEP, "s3.get_bucket_encryption",
        lambda: s3.get_bucket_encryption(Bucket=bucket), None)
    state.public_access_block = optional(
        findings, STEP, "s3.get_public_access_block",
        lambda: s3.get_public_access_block(Bucket=bucket), None)
    policy = optional(findings, STEP, "s3.get_bucket_policy",
                      lambda: s3.get_bucket_policy(Bucket=bucket), None)
    state.policy = None
    if policy and policy.get("Policy"):
        try:
            state.policy = json.loads(policy["Policy"])
        except (ValueError, TypeError):
            state.policy = None
    observed.config_bucket_state = state

    body = optional(
        findings, STEP, "s3.get_object",
        lambda: s3.get_object(Bucket=bucket, Key=names["secrets_object"]), None)
    observed.secrets = _read_json_body(body)

    stage1 = optional(
        findings, STEP, "s3.get_object",
        lambda: s3.get_object(Bucket=bucket, Key=names["stage1_object"]), None)
    observed.stage1 = _read_json_body(stage1)


def _read_json_body(response):
    if not response:
        return None
    import json
    try:
        raw = response["Body"].read()
    except (KeyError, AttributeError, OSError):
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


# ── data ────────────────────────────────────────────────────────────────────

def _observe_data(clients, spec, observed, findings):
    rds = clients.get("rds")
    cache = clients.get("elasticache")
    names = spec_module.names(spec)

    groups = optional(
        findings, STEP, "rds.describe_db_subnet_groups",
        lambda: rds.describe_db_subnet_groups(
            DBSubnetGroupName=names["db_subnet_group"]), {})
    observed.db_subnet_group = wrap(_one(
        (groups or {}).get("DBSubnetGroups") or []))

    clusters = optional(
        findings, STEP, "rds.describe_db_clusters",
        lambda: rds.describe_db_clusters(
            DBClusterIdentifier=names["db_cluster"]), {})
    observed.db_cluster = wrap(_one((clusters or {}).get("DBClusters") or []))

    if observed.db_cluster:
        instances = optional(
            findings, STEP, "rds.describe_db_instances",
            lambda: rds.describe_db_instances(Filters=[{
                "Name": "db-cluster-id",
                "Values": [names["db_cluster"]]}]), {})
        observed.db_instances = wrap(
            (instances or {}).get("DBInstances") or [])

    cache_subnets = optional(
        findings, STEP, "elasticache.describe_cache_subnet_groups",
        lambda: cache.describe_cache_subnet_groups(
            CacheSubnetGroupName=names["cache_subnet_group"]), {})
    observed.cache_subnet_group = wrap(_one(
        (cache_subnets or {}).get("CacheSubnetGroups") or []))

    replication = optional(
        findings, STEP, "elasticache.describe_replication_groups",
        lambda: cache.describe_replication_groups(
            ReplicationGroupId=names["cache_group"]), {})
    observed.cache_group = wrap(_one(
        (replication or {}).get("ReplicationGroups") or []))


# ── nodes ───────────────────────────────────────────────────────────────────

def _observe_nodes(clients, spec, observed, findings):
    ec2 = clients.get("ec2")

    reservations = optional(
        findings, STEP, "ec2.describe_instances",
        lambda: list_paginated(
            ec2, "describe_instances", "Reservations",
            Filters=tag_filters(spec, role="node") + [{
                "Name": "instance-state-name",
                "Values": ["pending", "running", "stopping", "stopped"]}]), [])
    instances = []
    for reservation in reservations or []:
        instances.extend(reservation.get("Instances") or [])
    observed.instances = wrap(instances)

    addresses = optional(
        findings, STEP, "ec2.describe_addresses",
        lambda: ec2.describe_addresses(Filters=tag_filters(spec)), {})
    observed.addresses = wrap((addresses or {}).get("Addresses") or [])

    observed.ami_id = spec.ami_override or _resolve_ami(clients, spec, findings)


def _resolve_ami(clients, spec, findings):
    """The base image, from the public SSM parameter Amazon maintains.

    Recorded on the observation (and reported by `nodes.ensure_nodes`) so an
    operator can see exactly which image a fleet was built from, months later,
    without an AMI id having been the thing they had to remember to write down.
    """
    architecture = spec_module.architecture_for(spec.node_type)
    parameter = spec_module.SSM_AMI_PARAMETERS.get(architecture)
    if not parameter:
        return None
    ssm = clients.get("ssm")
    result = optional(findings, STEP, "ssm.get_parameter",
                      lambda: ssm.get_parameter(Name=parameter), {})
    return ((result or {}).get("Parameter") or {}).get("Value")


# ── balancer ────────────────────────────────────────────────────────────────

def _observe_balancer(clients, spec, observed, findings):
    if not spec_module.wants_balancer(spec):
        return
    elbv2 = clients.get("elbv2")
    names = spec_module.names(spec)

    balancers = optional(
        findings, STEP, "elbv2.describe_load_balancers",
        lambda: elbv2.describe_load_balancers(Names=[names["balancer"]]), {})
    observed.balancer = wrap(_one((balancers or {}).get("LoadBalancers") or []))

    wanted = {"api": names["api_target_group"],
              "certbot": names["certbot_target_group"]}
    groups = {}
    for role, group_name in wanted.items():
        found = optional(
            findings, STEP, "elbv2.describe_target_groups",
            lambda name=group_name: elbv2.describe_target_groups(Names=[name]),
            {})
        row = _one((found or {}).get("TargetGroups") or [])
        if row:
            groups[role] = wrap(row)
    observed.target_groups = groups

    if observed.balancer:
        listeners = optional(
            findings, STEP, "elbv2.describe_listeners",
            lambda: list_paginated(
                elbv2, "describe_listeners", "Listeners",
                LoadBalancerArn=observed.balancer.LoadBalancerArn), [])
        observed.listeners = wrap(listeners or [])
        # Read the attributes rather than writing them blind on every run. A
        # converge that always calls modify_load_balancer_attributes is not a
        # no-op, and "a second apply creates nothing" is the property this
        # whole design rests on.
        attributes = optional(
            findings, STEP, "elbv2.describe_load_balancer_attributes",
            lambda: elbv2.describe_load_balancer_attributes(
                LoadBalancerArn=observed.balancer.LoadBalancerArn), {})
        observed.balancer_attributes = {
            row.get("Key"): row.get("Value")
            for row in (attributes or {}).get("Attributes") or []}

    health = {}
    for role, group in groups.items():
        arn = group.TargetGroupArn
        result = optional(
            findings, STEP, "elbv2.describe_target_health",
            lambda value=arn: elbv2.describe_target_health(
                TargetGroupArn=value), {})
        health[role] = wrap(
            (result or {}).get("TargetHealthDescriptions") or [])
    observed.targets = health


# ── observability ───────────────────────────────────────────────────────────

def _observe_observability(clients, spec, observed, findings):
    names = spec_module.names(spec)

    s3 = clients.get("s3")
    head = optional(findings, STEP, "s3.head_bucket",
                    lambda: s3.head_bucket(Bucket=names["cloudtrail_bucket"]),
                    None)
    observed.cloudtrail_bucket = names["cloudtrail_bucket"] if head is not None else None

    trail_client = clients.get("cloudtrail")
    trails = optional(findings, STEP, "cloudtrail.describe_trails",
                      lambda: trail_client.describe_trails(), {})
    observed.trails = wrap((trails or {}).get("trailList") or [])

    guardduty = clients.get("guardduty")
    detectors = optional(findings, STEP, "guardduty.list_detectors",
                         lambda: guardduty.list_detectors(), {})
    observed.detector_ids = list((detectors or {}).get("DetectorIds") or [])

    logs = clients.get("logs")
    groups = optional(
        findings, STEP, "logs.describe_log_groups",
        lambda: list_paginated(
            logs, "describe_log_groups", "logGroups",
            token_field="nextToken",
            logGroupNamePrefix=names["log_group_prefix"]), [])
    observed.log_groups = {
        row.get("logGroupName"): wrap(row) for row in (groups or [])
        if row.get("logGroupName")}


# ── dns ─────────────────────────────────────────────────────────────────────

def _observe_dns(clients, spec, observed, findings):
    if not spec.domain:
        return
    route53 = clients.get("route53")
    wanted = spec.domain.rstrip(".") + "."

    zones = optional(
        findings, STEP, "route53.list_hosted_zones_by_name",
        lambda: route53.list_hosted_zones_by_name(DNSName=wanted), {})
    for row in (zones or {}).get("HostedZones") or []:
        if row.get("Name") == wanted:
            observed.hosted_zone = wrap(row)
            break

    if not observed.hosted_zone:
        return
    records = optional(
        findings, STEP, "route53.list_resource_record_sets",
        lambda: route53.list_resource_record_sets(
            HostedZoneId=observed.hosted_zone.Id), {})
    observed.record_sets = wrap((records or {}).get("ResourceRecordSets") or [])


def _one(rows):
    """Exactly one, or nothing.

    Two matches means two resources carry our tags, which is a state this tool
    must not guess its way out of — it returns nothing, the ensure step reports
    the resource as missing, and a human looks at why there are two.
    """
    rows = list(rows or [])
    return rows[0] if len(rows) == 1 else None
