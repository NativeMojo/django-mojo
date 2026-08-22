"""The topology, as data. Zero AWS calls — importing this costs nothing.

Everything the rest of the package creates gets its size, its name and its tags
from here, which makes this the one file to read to answer "what will this
actually build, and what will it cost me?".

Names are derived, never asked for. An operator supplies a project slug, an
environment slug and a region; every VPC, bucket, cluster, target group and log
group name falls out of those three deterministically. That is what makes a
second `apply` find its own work instead of building a parallel copy of it —
and it is why `validate_names()` runs as step 0 of `apply`, before any AWS call:
a name AWS will reject is worth discovering in a millisecond, not eleven
resources into a converge that cannot be rolled back.
"""

import re


# Aurora requires a DB subnet group spanning at least two availability zones,
# and an NLB is only meaningfully highly-available across two. Two is therefore
# the floor, not a preference, and nothing in this package is written to work
# with one.
AZ_COUNT = 2

# Pinned deliberately. "latest" is not a version an operator can reason about
# six months later, and an engine upgrade is a decision, not a side effect of
# re-running the bootstrap.
ENGINE_VERSIONS = {
    "aurora-postgresql": "17.7",
    "valkey": "8.2",
}

DB_ENGINE = "aurora-postgresql"
CACHE_ENGINE = "valkey"
DB_PORT = 5432
CACHE_PORT = 6379

# The ownership tag django-mojo already writes elsewhere
# (mojo/apps/aws/services/aws_check.py::OWNERSHIP_TAGS). Matched byte for byte
# on purpose: the two layers must agree on what "ours" means, or a resource
# created by one is invisible to the other.
MANAGED_BY = {"managed-by": "django-mojo"}

# Sizes. Each preset is one coherent shape, not a menu — mixing a large node
# count with a micro database is a way to build something that falls over under
# the load the node count implies.
#
# `medium` interpolates between `small` and `large`: four nodes is where a
# burstable instance family stops being the right answer, so it is the first
# preset on m6i, and the data tier moves to a memory-optimised class to match.
PRESETS = {
    "micro": {
        "node_count": 1,
        "node_type": "t3.small",
        "node_volume_gb": 30,
        "db_type": "db.t4g.medium",
        "db_readers": 0,
        "cache_type": "cache.t4g.micro",
        "cache_replicas": 0,
        # One node has nothing to balance across. An NLB in front of a single
        # instance buys an extra hop, an extra 20 dollars a month and no
        # availability at all, so micro does not get one — and growing to
        # `small` later is a re-run of `apply`, which adds it.
        "balancer": False,
    },
    "small": {
        "node_count": 2,
        "node_type": "t3.medium",
        "node_volume_gb": 40,
        "db_type": "db.t4g.medium",
        "db_readers": 1,
        "cache_type": "cache.t4g.micro",
        "cache_replicas": 1,
        "balancer": True,
    },
    "medium": {
        "node_count": 4,
        "node_type": "m6i.large",
        "node_volume_gb": 60,
        "db_type": "db.r6g.large",
        "db_readers": 1,
        "cache_type": "cache.t4g.medium",
        "cache_replicas": 1,
        "balancer": True,
    },
    "large": {
        "node_count": 6,
        "node_type": "m6i.large",
        "node_volume_gb": 80,
        "db_type": "db.r6g.xlarge",
        "db_readers": 2,
        "cache_type": "cache.r7g.large",
        "cache_replicas": 2,
        "balancer": True,
    },
}

PRESET_NAMES = ("micro", "small", "medium", "large")

# Approximate on-demand list prices, US regions, dollars per month. These exist
# so an operator sees a number before they press the button — they are not a
# billing integration and are not meant to be one. Reality varies by region,
# by commitment and by how much data actually moves.
COST_TABLE = {
    "t3.small": 15.0,
    "t3.medium": 30.0,
    "t3.large": 60.0,
    "m6i.large": 70.0,
    "db.t4g.medium": 50.0,
    "db.r6g.large": 175.0,
    "db.r6g.xlarge": 350.0,
    "db.r6g.2xlarge": 700.0,
    "cache.t4g.micro": 12.0,
    "cache.t4g.medium": 50.0,
    "cache.r7g.large": 120.0,
    "cache.r7g.xlarge": 240.0,
    "nlb": 20.0,
    "eip": 3.6,
    "ebs_gb_month": 0.08,
    "aurora_storage_gb_month": 0.10,
    "cloudtrail": 2.0,
    "guardduty": 5.0,
    "cloudwatch_logs_gb_month": 0.50,
}

# The Admin capacity resize allowlist: the ONLY sizes the resize_cache /
# resize_database actions accept, exposed by the capacity report with the
# price beside each rung. `(key, label, instance_type)`, ordered small to
# extra large. Every type named here must stay priced in COST_TABLE, or the
# panel would offer a size with no number on it — tests/test_deploy/
# provision_spec.py enforces both the pricing and the ordering.
CACHE_SIZES = (
    ("small", "Small", "cache.t4g.micro"),
    ("medium", "Medium", "cache.t4g.medium"),
    ("large", "Large", "cache.r7g.large"),
    ("xlarge", "Extra large", "cache.r7g.xlarge"),
)
DB_SIZES = (
    ("small", "Small", "db.t4g.medium"),
    ("medium", "Medium", "db.r6g.large"),
    ("large", "Large", "db.r6g.xlarge"),
    ("xlarge", "Extra large", "db.r6g.2xlarge"),
)

# Rough starting points for the storage lines, so the estimate is not silently
# missing the two things that grow.
ASSUMED_DB_STORAGE_GB = 20
ASSUMED_LOG_GB_MONTH = 5

# Slugs become DNS labels, tag values, bucket names and resource-name prefixes,
# so the intersection of every one of those rule sets is what is allowed:
# lowercase, starts with a letter, no leading/trailing/doubled hyphen.
SLUG_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
SLUG_MAX = 24

# RDS DBName for the PostgreSQL engine: begins with a letter, letters and digits
# only, no hyphens or underscores at all.
DBNAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9]*$")
DBNAME_MAX = 63

# RDS cluster identifiers and ElastiCache replication-group ids share almost the
# same rule — letter first, alphanumerics and single hyphens, no trailing hyphen
# — but not the same length cap, so the caps live with the names that use them.
IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
DB_CLUSTER_ID_MAX = 63
CACHE_GROUP_ID_MAX = 40

# Elastic Load Balancing v2 caps load balancer and target group names at 32
# characters. This is the cap that actually bites, because it is short and it is
# derived from the two slugs an operator chose.
ELB_NAME_MAX = 32

# CloudWatch Logs. One prefix per environment, three groups under it, and the
# retention is stated here rather than left at "never expire" — an unbounded log
# group is a bill that only ever goes up.
LOG_GROUP_ROOT = "/mojo"
LOG_GROUP_KINDS = ("nginx", "app", "cloud-init")
LOG_RETENTION_DAYS = 90

# Everything a booting node reads lives under this one prefix in the config
# bucket: the endpoints document, the stage-1 script, the application tarball
# and the CloudWatch agent's configuration. One prefix rather than four means
# stage 0 carries one string it can build every URL from.
BOOTSTRAP_PREFIX = "bootstrap"

# The published application config, on the other hand, is per-environment and
# read by `config_sync` on a timer forever — so it is namespaced by project and
# environment rather than sharing the boot prefix. This is the value that goes
# into a node's bootstrap.conf as AWS_CONFIG_PREFIX.
CONFIG_PREFIX_ROOT = "config"

# Network shape. Fixed rather than configurable: a /16 with four /24s leaves
# room for a decade of growth, and every extra knob here is another way for two
# environments to end up unable to peer.
VPC_CIDR = "10.0.0.0/16"
PUBLIC_SUBNET_CIDRS = ("10.0.1.0/24", "10.0.2.0/24")
PRIVATE_SUBNET_CIDRS = ("10.0.11.0/24", "10.0.12.0/24")

# The AL2023 x86_64 image, resolved through the public SSM parameter rather than
# a describe_images filter. describe_images(Owners=["self"]) cannot work on an
# empty account, and a name-glob against Amazon's images is a race with whatever
# they published this morning.
SSM_AMI_PARAMETERS = {
    "x86_64": "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-x86_64",
    "arm64": "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-arm64",
}

# Graviton families carry a `g` in the suffix after the generation digit:
# t4g, m6g, r6g, c7gn. Everything else this package can be pointed at is x86_64.
_FAMILY_RE = re.compile(r"^(?P<letters>[a-z]+)(?P<generation>\d+)(?P<suffix>[a-z]*)$")


class Spec:
    """One environment's declared topology.

    Built through `build()` rather than instantiated directly, so a preset name
    is always resolved into concrete values and an override is always applied on
    top of a coherent base.
    """

    def __init__(self, project, env, region, preset="small", **overrides):
        self.project = project
        self.env = env
        self.region = region
        self.preset = preset

        sizes = dict(PRESETS[preset]) if preset in PRESETS else {}
        self.node_count = sizes.get("node_count", 1)
        self.node_type = sizes.get("node_type", "t3.small")
        self.node_volume_gb = sizes.get("node_volume_gb", 30)
        self.db_type = sizes.get("db_type", "db.t4g.medium")
        self.db_readers = sizes.get("db_readers", 0)
        self.cache_type = sizes.get("cache_type", "cache.t4g.micro")
        self.cache_replicas = sizes.get("cache_replicas", 0)

        # None means "whatever the preset says"; True/False is an operator
        # overriding it, which is why this is tri-state and not a bool.
        self.balancer = sizes.get("balancer", False)
        self.want_balancer = None

        # Per-node elastic IPs INDEPENDENT of the balancer decision. Without a
        # balancer every node gets one regardless (the node IS the DNS target);
        # behind one, nodes default to changing auto-assigned addresses — set
        # this when providers allowlist caller IPs and the fleet's outbound
        # must stay fixed. The Admin capacity panel's "Stable outbound IPs"
        # control is the runtime form of the same policy.
        self.stable_node_ips = False

        # Who may reach :22. Never defaulted to 0.0.0.0/0 — an empty list means
        # SSH is not opened at all, which is a working configuration (Session
        # Manager) and a much better accident than a world-open one.
        self.admin_cidrs = []

        self.domain = None
        self.create_zone = False

        # Set to pin an image explicitly; otherwise the SSM parameter above
        # resolves one and `discover.observe` records which.
        self.ami_override = None

        # An operator-supplied SSH public key. Strongly preferred over letting
        # AWS generate one: no private material ever leaves the operator's
        # machine, and there is nothing that can only be read once.
        self.public_key = None

        self.db_retention_days = 7
        self.db_storage_gb = ASSUMED_DB_STORAGE_GB

        # Filled in by `discover.observe`. Present here so the identity policy
        # can be built for a specific account without a second lookup.
        self.account_id = None

        # Where `git archive HEAD` is taken from when the boot payload is
        # published. Set by the CLI from `--project-root`; None means the
        # process's working directory.
        self.project_root = None

        # Escape hatches for the two globally-unique names. S3 bucket names are
        # a single worldwide namespace, so a derived name can genuinely already
        # belong to a stranger.
        self.config_bucket = None
        self.cloudtrail_bucket = None

        # Brownfield fleet fields are populated only by brownfield_inputs.
        # Keeping the seams on Spec lets the existing node and balancer
        # ensure-functions remain the single implementation of those resource
        # types while every managed default above stays unchanged.
        self.fleet = None
        self.node_declarations = []
        self.role_document = None
        self.bootstrap_objects = {}
        self.bootstrap_prefix = None
        self.config_prefix = None
        self.metrics_namespace = None
        self.compatibility_instance_ids = []
        self.nlb_subnet_ids = []
        self.nlb_name = None
        self.api_target_group_name = None
        self.certbot_target_group_name = None

        for key, value in overrides.items():
            if not hasattr(self, key):
                raise AttributeError(
                    f"unknown spec field {key!r} — a typo here would otherwise "
                    f"be silently ignored and change nothing")
            setattr(self, key, value)

    def as_dict(self):
        return {k: v for k, v in vars(self).items() if not k.startswith("_")}

    def __repr__(self):
        return f"<Spec {self.project}-{self.env} {self.preset} {self.region}>"


def build(project, env, region, preset="small", **overrides):
    if preset not in PRESETS:
        raise ValueError(
            f"unknown preset {preset!r} — one of {', '.join(PRESET_NAMES)}")
    return Spec(project, env, region, preset=preset, **overrides)


def wants_balancer(spec):
    """An NLB is built when the preset calls for one, or when an operator asked
    for one explicitly, or whenever there is more than one node to balance
    across — two nodes with no balancer is a topology with no way to reach the
    second one."""
    if spec.want_balancer is not None:
        return bool(spec.want_balancer)
    return bool(spec.balancer) or spec.node_count >= 2


def architecture_for(instance_type):
    """x86_64 or arm64, from the instance type alone.

    x86_64 is the tested path: it is what the AMI parameter, the packages and
    the node scripts have actually been run against. arm64 resolves correctly
    here so a Graviton preset picks the right image rather than a silently
    unbootable one, but it is not a shape this package claims to have proven.
    """
    parts = instance_type.split(".")
    if len(parts) < 2:
        return "x86_64"
    match = _FAMILY_RE.match(parts[-2])
    if not match:
        return "x86_64"
    return "arm64" if "g" in match.group("suffix") else "x86_64"


def names(spec):
    """Every name this package will create or look for, derived from the spec.

    This is the single source. The node's CloudWatch agent configuration, the
    portal's readiness panel and the tests all read log-group names from here
    rather than rebuilding the string, so there is exactly one place a rename
    has to happen.
    """
    base = f"{spec.project}-{spec.env}"
    db_name = re.sub(r"[^a-z0-9]", "", f"{spec.project}{spec.env}")
    log_prefix = f"{LOG_GROUP_ROOT}/{base}"
    resolved = {
        "base": base,
        "vpc": f"{base}-vpc",
        "internet_gateway": f"{base}-igw",
        "public_route_table": f"{base}-public",
        "private_route_table": f"{base}-private",
        "public_subnets": [f"{base}-public-{i + 1}" for i in range(AZ_COUNT)],
        "private_subnets": [f"{base}-private-{i + 1}" for i in range(AZ_COUNT)],
        "node_sg": f"{base}-node",
        "rds_sg": f"{base}-rds",
        "cache_sg": f"{base}-cache",
        "key_pair": f"{base}-node",
        "node_role": f"{base}-node",
        # Named to match the terraform module this replaces, so an account that
        # was built by tofu and one built by this tool look identical to an
        # auditor.
        "node_policy": "django-mojo-setup",
        "instance_profile": f"{base}-node",
        "config_bucket": spec.config_bucket or f"{base}-config",
        # Where a WebApp's built artifacts are uploaded and where every node
        # fetches them from. Separate from the config bucket on purpose: this
        # one receives writes from a GitHub Actions credential, and the config
        # bucket holds the environment's secrets.
        "releases_bucket": f"{base}-releases",
        # KMS_KEY_ID. `KSMSecrets` models — dnsman certificates above all —
        # refuse to load without one, so the edge plane cannot serve TLS in an
        # environment that has no key.
        "kms_alias": f"alias/{base}",
        "secrets_object": "bootstrap-secrets.json",
        "bootstrap_prefix": BOOTSTRAP_PREFIX,
        "stage1_object": f"{BOOTSTRAP_PREFIX}/stage1.json",
        "stage1_script_object": f"{BOOTSTRAP_PREFIX}/stage1.sh",
        "app_archive_object": f"{BOOTSTRAP_PREFIX}/app.tar.gz",
        # The commit app.tar.gz was archived from. The tarball carries no
        # history, so without this the node cannot say which commit it runs
        # and `configure` cannot wire it to origin — see storage.app_archive.
        "app_sha_object": f"{BOOTSTRAP_PREFIX}/app.sha",
        "cloudwatch_object": f"{BOOTSTRAP_PREFIX}/cloudwatch-agent.json",
        "config_prefix": f"{CONFIG_PREFIX_ROOT}/{spec.project}/{spec.env}",
        "django_conf_object":
            f"{CONFIG_PREFIX_ROOT}/{spec.project}/{spec.env}/django.conf",
        "metrics_namespace": f"mojo/{base}",
        "db_subnet_group": f"{base}-aurora",
        "db_cluster": f"{base}-aurora",
        "db_writer": f"{base}-aurora-writer",
        "db_readers": [f"{base}-aurora-reader-{i + 1}"
                       for i in range(spec.db_readers)],
        "db_name": db_name,
        "cache_subnet_group": f"{base}-cache",
        "cache_group": f"{base}-cache",
        # wmx1, wmx2, … — the existing fleet convention, and what
        # PRIMARY_BALANCER_HOST in django.conf is compared against.
        "nodes": [f"{spec.project}{i + 1}" for i in range(spec.node_count)],
        "balancer": f"{base}-nlb",
        "api_target_group": f"{base}-api",
        "certbot_target_group": f"{base}-certbot",
        "cloudtrail_bucket": spec.cloudtrail_bucket or f"{base}-cloudtrail",
        "cloudtrail": f"{base}-trail",
        "log_group_prefix": log_prefix,
        "log_groups": {kind: f"{log_prefix}/{kind}" for kind in LOG_GROUP_KINDS},
    }
    if spec.fleet:
        resolved["nodes"] = [row["name"] for row in spec.node_declarations]
        resolved["bootstrap_prefix"] = spec.bootstrap_prefix
        resolved["stage1_script_object"] = (
            spec.bootstrap_objects.get("stage1") or {}).get("key")
        resolved["config_prefix"] = spec.config_prefix
        resolved["metrics_namespace"] = spec.metrics_namespace
        resolved["balancer"] = spec.nlb_name
        resolved["api_target_group"] = spec.api_target_group_name
        resolved["certbot_target_group"] = spec.certbot_target_group_name
    return resolved


def validate_names(spec):
    """Every name AWS would reject, checked before AWS is called at all.

    Returns a list of human-readable problems; empty means the topology is
    nameable. `plan.apply` runs this as step 0 and refuses to proceed on a
    non-empty result, because the alternative is discovering a 33-character
    target group name after the VPC, the subnets and the database already exist
    and cannot be deleted by this tool.
    """
    problems = []
    for label, slug in (("project", spec.project), ("env", spec.env)):
        if not slug:
            problems.append(f"{label} is required")
            continue
        if not SLUG_RE.match(slug):
            problems.append(
                f"{label} {slug!r} must be lowercase, start with a letter, and "
                f"use single hyphens between alphanumeric parts")
        if len(slug) > SLUG_MAX:
            problems.append(
                f"{label} {slug!r} is {len(slug)} characters — the cap is "
                f"{SLUG_MAX}, because every derived name is built from it")

    if spec.fleet:
        return _validate_brownfield_names(spec, problems)

    if spec.preset not in PRESETS:
        problems.append(
            f"unknown preset {spec.preset!r} — one of {', '.join(PRESET_NAMES)}")
    if not spec.region:
        problems.append("region is required")

    if problems:
        # Deriving names from a slug that is already invalid produces a second
        # wave of noise about names nobody would ever have used.
        return problems

    resolved = names(spec)

    for key in ("balancer", "api_target_group", "certbot_target_group"):
        value = resolved[key]
        if len(value) > ELB_NAME_MAX:
            problems.append(
                f"{key} name {value!r} is {len(value)} characters — Elastic "
                f"Load Balancing caps names at {ELB_NAME_MAX}; shorten the "
                f"project or env slug")

    db_name = resolved["db_name"]
    if not DBNAME_RE.match(db_name or ""):
        problems.append(
            f"database name {db_name!r} must start with a letter and contain "
            f"only letters and digits")
    elif len(db_name) > DBNAME_MAX:
        problems.append(
            f"database name {db_name!r} is longer than {DBNAME_MAX} characters")

    problems.extend(_identifier_problems(
        "RDS cluster identifier", resolved["db_cluster"], DB_CLUSTER_ID_MAX))
    problems.extend(_identifier_problems(
        "cache replication group id", resolved["cache_group"],
        CACHE_GROUP_ID_MAX))

    if spec.node_count < 1:
        problems.append("node_count must be at least 1")
    if spec.db_readers < 0 or spec.cache_replicas < 0:
        problems.append("db_readers and cache_replicas cannot be negative")

    for cidr in spec.admin_cidrs or ():
        if "/" not in cidr:
            problems.append(f"admin CIDR {cidr!r} must carry a prefix length")

    return problems


def _validate_brownfield_names(spec, problems):
    """Validate the exact fleet vocabulary without admitting a new preset.

    Brownfield is an input mode, not a managed capacity preset. Keeping this
    seam separate means ``build(..., preset="brownfield")`` remains invalid and
    the four managed topology presets retain their existing contract.
    """
    if not spec.region:
        problems.append("region is required")
    if problems:
        return problems

    resolved = names(spec)
    for key in ("balancer", "api_target_group", "certbot_target_group"):
        value = resolved[key]
        if not value or len(value) > ELB_NAME_MAX:
            problems.append(
                f"{key} name {value!r} must be present and no longer than "
                f"{ELB_NAME_MAX} characters")
    declared = list(spec.node_declarations or ())
    node_names = [row.get("name") for row in declared]
    if len(declared) != spec.node_count:
        problems.append(
            f"node_count is {spec.node_count}, but the fleet declares "
            f"{len(declared)} node(s)")
    if len(node_names) != len(set(node_names)):
        problems.append("brownfield node names must be unique")
    if wants_balancer(spec) and not any(
            row.get("serving_target") for row in declared):
        problems.append(
            "a brownfield fleet with an NLB needs at least one serving_target "
            "node")
    return problems


def _identifier_problems(label, value, cap):
    problems = []
    if not IDENTIFIER_RE.match(value or ""):
        problems.append(
            f"{label} {value!r} must start with a letter and use single "
            f"hyphens — no underscores, no doubled or trailing hyphen")
    if len(value or "") > cap:
        problems.append(
            f"{label} {value!r} is {len(value)} characters — AWS caps it at {cap}")
    return problems


def TAGS(spec, role):
    """The tag set every resource this package creates carries.

    `mojo:*` is the namespace discovery filters on. `Project`/`Env` are the
    legacy keys the existing fleet and its dashboards already read, kept so a
    newly provisioned environment shows up in them without a migration.
    `managed-by` matches what the in-Django layer writes.
    """
    tags = {
        "mojo:project": spec.project,
        "mojo:env": spec.env,
        "mojo:role": role,
        "Project": spec.project,
        "Env": spec.env,
    }
    tags.update(MANAGED_BY)
    if spec.fleet:
        tags["mojo:fleet"] = spec.fleet
    return tags


def node_tags(spec, declaration):
    """Creation-time tags for one role-bearing brownfield node."""
    tags = TAGS(spec, "node")
    if spec.fleet:
        tags["mojo:application-role"] = declaration.get("role")
    return tags


def node_tag_specifications(spec, declaration, resource_type, name=None):
    tags = node_tags(spec, declaration)
    if name:
        tags["Name"] = name
    return [{"ResourceType": resource_type,
             "Tags": [{"Key": key, "Value": tags[key]}
                      for key in sorted(tags)]}]


def tag_list(spec, role, name=None):
    """TAGS as the [{"Key":…,"Value":…}] shape most AWS APIs take."""
    tags = TAGS(spec, role)
    if name:
        tags["Name"] = name
    return [{"Key": key, "Value": tags[key]} for key in sorted(tags)]


def tag_specifications(spec, role, resource_type, name=None):
    """EC2's TagSpecifications, for tagging AT CREATION.

    Never tag in a second call. A create that succeeds and a tag call that does
    not leaves an untagged resource, and this package never adopts an untagged
    resource — so the next run would build a second one, forever, with no way to
    clean up the first.
    """
    return [{"ResourceType": resource_type,
             "Tags": tag_list(spec, role, name=name)}]


def owns(tags, spec, role=None):
    """Is this resource ours, by tag alone?

    Never by name, never by "it is the only VPC in the account". An account can
    contain someone else's `wmx-prod-vpc`, and adopting it because the name
    matched is how a provisioning tool ends up modifying production for a
    different team.
    """
    if isinstance(tags, (list, tuple)):
        tags = {t.get("Key"): t.get("Value") for t in tags}
    tags = tags or {}
    if tags.get("mojo:project") != spec.project:
        return False
    if tags.get("mojo:env") != spec.env:
        return False
    if spec.fleet and (
            tags.get("managed-by") != "django-mojo"
            or tags.get("mojo:fleet") != spec.fleet):
        return False
    if role is not None and tags.get("mojo:role") != role:
        return False
    return True


def estimate_cost(spec):
    """Approximate monthly cost, as ordered rows plus a total.

    The NLB line is present only when one will actually be created, because a
    cost estimate that lists a resource the run will not build is worse than no
    estimate — it is the number an operator budgets against.
    """
    resolved = names(spec)
    rows = []

    def row(item, detail, monthly):
        rows.append({"item": item, "detail": detail,
                     "monthly": round(monthly, 2)})

    node_each = COST_TABLE.get(spec.node_type, 0.0)
    row("nodes", f"{spec.node_count} x {spec.node_type}",
        node_each * spec.node_count)
    row("node storage",
        f"{spec.node_count} x {spec.node_volume_gb}GB gp3",
        COST_TABLE["ebs_gb_month"] * spec.node_volume_gb * spec.node_count)

    db_each = COST_TABLE.get(spec.db_type, 0.0)
    db_instances = 1 + spec.db_readers
    row("database", f"{db_instances} x {spec.db_type} (aurora-postgresql)",
        db_each * db_instances)
    row("database storage", f"~{spec.db_storage_gb}GB",
        COST_TABLE["aurora_storage_gb_month"] * spec.db_storage_gb)

    cache_each = COST_TABLE.get(spec.cache_type, 0.0)
    cache_nodes = 1 + spec.cache_replicas
    row("cache", f"{cache_nodes} x {spec.cache_type} (valkey)",
        cache_each * cache_nodes)

    if wants_balancer(spec):
        row("load balancer", resolved["balancer"], COST_TABLE["nlb"])
        row("balancer addresses", f"{AZ_COUNT} x elastic IP",
            COST_TABLE["eip"] * AZ_COUNT)
    if not wants_balancer(spec) or spec.stable_node_ips:
        # Node addresses exist without a balancer (the node is the DNS
        # target), or behind one when stable_node_ips pins outbound.
        row("node addresses", f"{spec.node_count} x elastic IP",
            COST_TABLE["eip"] * spec.node_count)

    row("cloudtrail", resolved["cloudtrail"], COST_TABLE["cloudtrail"])
    row("guardduty", "one detector", COST_TABLE["guardduty"])
    row("logs", f"~{ASSUMED_LOG_GB_MONTH}GB/month across "
                f"{len(LOG_GROUP_KINDS)} groups",
        COST_TABLE["cloudwatch_logs_gb_month"] * ASSUMED_LOG_GB_MONTH)

    total = round(sum(r["monthly"] for r in rows), 2)
    return {"rows": rows, "total": total, "currency": "USD",
            "note": "approximate US on-demand list prices, before data transfer"}
