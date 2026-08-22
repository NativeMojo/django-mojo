"""Strict, secret-free declarations for exact-resource brownfield fleets.

This is intentionally a second input language.  A managed environment derives
and owns its data plane; a brownfield fleet names dependencies it must only
observe.  Mixing the two declarations would make that ownership boundary a
convention instead of a property of the parser.
"""

import hashlib
import json
import os
import re

from mojo.deploy.provision import inputs, spec as spec_module


FLEET_DIR = os.path.join("aws", "fleets")
SCHEMA_VERSION = 1
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ARN_RE = re.compile(
    r"^arn:(?P<partition>aws(?:-[a-z]+)*):(?P<service>[a-z0-9-]+):"
    r"(?P<region>[^:]*):(?P<account>[^:]*):(?P<resource>.+)$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+=,@-]*$")
ROLE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
NODE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
BUCKET_RE = re.compile(
    r"^(?!\d+\.\d+\.\d+\.\d+$)(?!-)(?!.*\.\.)(?!.*\.-)(?!.*-\.)"
    r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
VERSION_RE = re.compile(r"^[A-Za-z0-9._+/=]+$")
IAM_NAME_RE = re.compile(r"^[A-Za-z0-9+=,.@_-]{1,64}$")
DB_USER_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

TOP_KEYS = frozenset((
    "schema_version", "account_id", "region", "project", "environment",
    "fleet", "network", "database", "cache", "storage", "bootstrap",
    "nodes", "load_balancer", "kms_key_arn", "alarm_topic_arn",
    "compatibility_instance_ids", "manage_dns", "nlb_eip_allocations",
    "eip_handoff_role_arn", "eip_handoff_canaries",
))
NETWORK_KEYS = frozenset((
    "vpc_id", "node_security_group_id", "public_subnets",
))
SUBNET_KEYS = frozenset((
    "id", "availability_zone", "network_border_group",
))
DATABASE_KEYS = frozenset((
    "cluster_arn", "identifier", "writer_endpoint", "reader_endpoint",
    "port", "database_name", "master_user", "application_user",
    "credential", "subnet_group_name", "security_group_ids",
))
CACHE_KEYS = frozenset((
    "replication_group_arn", "identifier", "endpoint", "port",
    "transit_encryption", "auth_enabled", "credential",
    "subnet_group_name", "security_group_ids",
))
STORAGE_KEYS = frozenset((
    "config", "releases", "sites", "revisions", "fleet_config",
))
PREFIX_KEYS = frozenset(("bucket", "prefix"))
OBJECT_KEYS = frozenset(("bucket", "key", "version_id", "sha256"))
BOOTSTRAP_KEYS = frozenset(("stage1", "live_config", "role_document"))
NODES_KEYS = frozenset((
    "instance_type", "volume_gb", "ami_id", "key_pair_name", "items",
    "profiles", "session_manager",
))
NODE_KEYS = frozenset((
    "name", "role", "serving_target", "request_service", "subnet_id",
    "availability_zone", "instance_profile_arn",
))
PROFILE_KEYS = frozenset(("profile_arn", "role_arn", "managed"))
MANAGED_PROFILE_KEYS = frozenset(("profile_name", "role_name"))
BALANCER_KEYS = frozenset((
    "name", "api_target_group", "certbot_target_group", "subnet_ids",
    "api_health_path", "certbot_health_path", "api_preserve_client_ip",
    "certbot_preserve_client_ip", "security_group_id",
))
CREDENTIAL_KEYS = frozenset((
    "object", "provider", "metadata_key",
))
CANARY_KEYS = frozenset((
    "name", "protocol", "port", "tls_sni", "host", "path", "request",
    "expected_status", "expected_marker", "timeout", "target", "addresses",
))
ALLOCATION_RE = re.compile(r"^eipalloc-[0-9a-f]+$")
SECURITY_GROUP_RE = re.compile(r"^sg-[0-9a-f]{8,17}$")

# Fields whose presence would make the committed declaration a credential,
# even if a caller tries to hide one in an otherwise unknown nested object.
SECRET_KEYS = frozenset((
    "password", "secret", "secret_value", "token", "access_key",
    "secret_access_key", "private_key", "connection_string", "dsn",
))


def fleet_path(project_root, fleet):
    return os.path.join(project_root or ".", FLEET_DIR, f"{fleet}.json")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def parse_arn(value, label, account_id=None, region=None, services=None):
    match = ARN_RE.match(value or "")
    if not match:
        raise inputs.EnvFileError(f"{label} must be a complete AWS ARN")
    parsed = match.groupdict()
    if account_id and parsed["account"] and parsed["account"] != account_id:
        raise inputs.EnvFileError(
            f"{label} belongs to account {parsed['account']}, not "
            f"{account_id}")
    if region and parsed["region"] and parsed["region"] != region:
        raise inputs.EnvFileError(
            f"{label} belongs to region {parsed['region']}, not {region}")
    if services and parsed["service"] not in services:
        raise inputs.EnvFileError(
            f"{label} is a {parsed['service']} ARN, expected "
            f"{', '.join(services)}")
    return parsed


def load(path):
    try:
        with open(path) as handle:
            body = handle.read()
    except FileNotFoundError:
        raise inputs.EnvFileError(
            f"no fleet file at {path} — brownfield commands only read "
            f"aws/fleets/<fleet>.json")
    except OSError as err:
        raise inputs.EnvFileError(f"cannot read {path}: {err}")
    try:
        raw = json.loads(body)
    except ValueError as err:
        raise inputs.EnvFileError(f"{path} is not valid JSON: {err}")
    if not isinstance(raw, dict):
        raise inputs.EnvFileError(f"{path} must contain one JSON object")
    return validate(raw, path=path)


def validate(raw, path="fleet manifest"):
    _object(raw, TOP_KEYS, path)
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise inputs.EnvFileError(
            f"{path} declares schema_version "
            f"{raw.get('schema_version')!r}; this django-mojo understands "
            f"{SCHEMA_VERSION}")
    _reject_secret_values(raw, path)
    required = [
        "account_id", "region", "project", "environment", "fleet",
        "network", "database", "cache", "storage", "bootstrap", "nodes",
        "load_balancer", "kms_key_arn", "manage_dns",
    ]
    _required(raw, required, path)
    if not str(raw["account_id"]).isdigit() or len(str(raw["account_id"])) != 12:
        raise inputs.EnvFileError(f"{path}.account_id must be 12 digits")
    for label in ("project", "environment", "fleet"):
        problem = spec_module.check_slug(label, raw[label]) \
            if hasattr(spec_module, "check_slug") else None
        if not spec_module.SLUG_RE.match(raw[label]) or len(raw[label]) > 24:
            raise inputs.EnvFileError(
                f"{path}.{label} must be a short lowercase AWS slug")
    if inputs.check_region(raw["region"]):
        raise inputs.EnvFileError(inputs.check_region(raw["region"]))

    account_id, region = str(raw["account_id"]), raw["region"]
    _network(raw["network"], path)
    _database(raw["database"], path, account_id, region)
    _cache(raw["cache"], path, account_id, region)
    _storage(raw["storage"], path)
    _bootstrap(raw["bootstrap"], path)
    _nodes(raw["nodes"], raw["network"], path, account_id)
    _balancer(raw["load_balancer"], raw["network"], path)
    _boolean(raw["manage_dns"], f"{path}.manage_dns")
    if raw["manage_dns"] is not False:
        raise inputs.EnvFileError(
            f"{path}.manage_dns must be explicitly false; brownfield fleet "
            f"and preserved-address commands never manage DNS")
    _handoff(raw, path, account_id, region)

    parse_arn(raw["kms_key_arn"], f"{path}.kms_key_arn", account_id, region,
              ("kms",))
    if raw.get("alarm_topic_arn"):
        parse_arn(raw["alarm_topic_arn"], f"{path}.alarm_topic_arn",
                  account_id, region, ("sns",))
    compatibility = raw.get("compatibility_instance_ids") or []
    if not isinstance(compatibility, list) or len(set(compatibility)) != len(
            compatibility):
        raise inputs.EnvFileError(
            f"{path}.compatibility_instance_ids must be a unique list")
    for instance_id in compatibility:
        if not re.match(r"^i-[0-9a-f]+$", instance_id or ""):
            raise inputs.EnvFileError(
                f"{path} has invalid compatibility instance {instance_id!r}")

    # Stable round trips and receipts use only this normalized JSON shape.
    normalized = json.loads(canonical(raw))
    normalized["manifest_digest"] = digest(raw)
    return normalized


def to_spec(manifest, project_root=None):
    nodes = manifest["nodes"]
    built = spec_module.Spec(
        manifest["project"], manifest["environment"], manifest["region"],
        preset="brownfield")
    built.fleet = manifest["fleet"]
    built.account_id = str(manifest["account_id"])
    built.project_root = project_root
    built.node_count = len(nodes["items"])
    built.node_type = nodes["instance_type"]
    built.node_volume_gb = nodes["volume_gb"]
    built.ami_override = nodes["ami_id"]
    built.node_declarations = list(nodes["items"])
    built.role_document = dict(manifest["bootstrap"]["role_document"])
    built.bootstrap_objects = {
        key: dict(value) for key, value in manifest["bootstrap"].items()}
    built.bootstrap_prefix = _common_prefix(
        manifest["bootstrap"]["stage1"]["key"])
    built.config_prefix = manifest["storage"]["fleet_config"]["prefix"]
    built.config_bucket = manifest["storage"]["fleet_config"]["bucket"]
    built.metrics_namespace = f"mojo/{manifest['project']}-{manifest['fleet']}"
    built.compatibility_instance_ids = list(
        manifest.get("compatibility_instance_ids") or ())
    built.nlb_subnet_ids = list(manifest["load_balancer"]["subnet_ids"])
    built.nlb_name = manifest["load_balancer"]["name"]
    built.api_target_group_name = manifest["load_balancer"]["api_target_group"]
    built.certbot_target_group_name = manifest[
        "load_balancer"]["certbot_target_group"]
    built.api_health_path = manifest["load_balancer"].get(
        "api_health_path", spec_module.HEALTH_PATH_DEFAULT)
    built.certbot_health_path = manifest["load_balancer"].get(
        "certbot_health_path", spec_module.HEALTH_PATH_DEFAULT)
    built.api_preserve_client_ip = manifest["load_balancer"].get(
        "api_preserve_client_ip")
    built.certbot_preserve_client_ip = manifest["load_balancer"].get(
        "certbot_preserve_client_ip")
    built.nlb_security_group_id = manifest["load_balancer"].get(
        "security_group_id")
    built.want_balancer = True
    built.stable_node_ips = False
    built.manage_dns = manifest["manage_dns"]
    built.nlb_eip_allocations = dict(
        manifest.get("nlb_eip_allocations") or {})
    built.eip_handoff_role_arn = manifest.get("eip_handoff_role_arn")
    built.eip_handoff_canaries = list(
        manifest.get("eip_handoff_canaries") or ())
    journal_name = (f"{manifest['project']}-{manifest['environment']}-"
                    f"{manifest['fleet']}")
    built.eip_handoff_local_journal = os.path.join(
        project_root or ".", "var", "provision", "eip-handoffs",
        f"{journal_name}.json")
    built.eip_handoff_bucket = manifest["storage"]["fleet_config"]["bucket"]
    prefix = manifest["storage"]["fleet_config"]["prefix"].strip("/")
    built.eip_handoff_prefix = (
        f"{prefix}/eip-handoffs/{journal_name}" if prefix
        else f"eip-handoffs/{journal_name}")
    built.brownfield_manifest = manifest
    built.manifest_digest = manifest["manifest_digest"]
    return built


def _network(value, path):
    _object(value, NETWORK_KEYS, f"{path}.network")
    _required(value, NETWORK_KEYS, f"{path}.network")
    _aws_id(value["vpc_id"], "vpc-", f"{path}.network.vpc_id")
    _aws_id(value["node_security_group_id"], "sg-",
            f"{path}.network.node_security_group_id")
    subnets = value["public_subnets"]
    if not isinstance(subnets, list) or len(subnets) < 2:
        raise inputs.EnvFileError(
            f"{path}.network.public_subnets needs at least two exact subnets")
    azs = []
    for index, subnet in enumerate(subnets):
        _object(subnet, SUBNET_KEYS,
                f"{path}.network.public_subnets[{index}]")
        _required(subnet, SUBNET_KEYS,
                  f"{path}.network.public_subnets[{index}]")
        _aws_id(subnet["id"], "subnet-",
                f"{path}.network.public_subnets[{index}].id")
        _safe_text(subnet["availability_zone"],
                   f"{path}.network.public_subnets[{index}].availability_zone")
        _safe_text(subnet["network_border_group"],
                   f"{path}.network.public_subnets[{index}].network_border_group")
        azs.append(subnet["availability_zone"])
    if len(set(azs)) != len(azs):
        raise inputs.EnvFileError(
            f"{path}.network.public_subnets must occupy distinct AZs")


def _database(value, path, account_id, region):
    label = f"{path}.database"
    _object(value, DATABASE_KEYS, label)
    _required(value, DATABASE_KEYS, label)
    parse_arn(value["cluster_arn"], f"{label}.cluster_arn", account_id,
              region, ("rds",))
    if int(value["port"]) != spec_module.DB_PORT:
        raise inputs.EnvFileError(
            f"{label}.port must be {spec_module.DB_PORT}")
    if not DB_USER_RE.match(value["application_user"] or ""):
        raise inputs.EnvFileError(
            f"{label}.application_user must be a PostgreSQL role identifier")
    _credential(value["credential"], f"{label}.credential")
    _security_groups(value["security_group_ids"],
                     f"{label}.security_group_ids")


def _cache(value, path, account_id, region):
    label = f"{path}.cache"
    _object(value, CACHE_KEYS, label)
    required = set(CACHE_KEYS) - {"credential"}
    _required(value, required, label)
    parse_arn(value["replication_group_arn"],
              f"{label}.replication_group_arn", account_id, region,
              ("elasticache",))
    if int(value["port"]) != spec_module.CACHE_PORT:
        raise inputs.EnvFileError(
            f"{label}.port must be {spec_module.CACHE_PORT}")
    _boolean(value["transit_encryption"], f"{label}.transit_encryption")
    _boolean(value["auth_enabled"], f"{label}.auth_enabled")
    if value["auth_enabled"]:
        if not value.get("credential"):
            raise inputs.EnvFileError(
                f"{label}.credential is required when auth_enabled is true")
        _credential(value["credential"], f"{label}.credential")
    _security_groups(value["security_group_ids"],
                     f"{label}.security_group_ids")


def _storage(value, path):
    label = f"{path}.storage"
    _object(value, STORAGE_KEYS, label)
    _required(value, STORAGE_KEYS, label)
    for key in STORAGE_KEYS:
        _object(value[key], PREFIX_KEYS, f"{label}.{key}")
        _required(value[key], PREFIX_KEYS, f"{label}.{key}")
        _bucket(value[key]["bucket"], f"{label}.{key}.bucket")
        _safe_text(value[key]["prefix"], f"{label}.{key}.prefix")
    current = [(value[key]["bucket"], value[key]["prefix"], key)
               for key in ("config", "releases", "sites", "revisions")]
    fleet_config = (value["fleet_config"]["bucket"],
                    value["fleet_config"]["prefix"])
    for bucket, prefix, key in current:
        if bucket == fleet_config[0] and _prefixes_overlap(
                prefix, fleet_config[1]):
            raise inputs.EnvFileError(
                f"{label}.fleet_config overlaps read-only {key}; it must be a "
                f"distinct migration-owned prefix")


def _bootstrap(value, path):
    label = f"{path}.bootstrap"
    _object(value, BOOTSTRAP_KEYS, label)
    _required(value, BOOTSTRAP_KEYS, label)
    for key in BOOTSTRAP_KEYS:
        _object_ref(value[key], f"{label}.{key}")


def _nodes(value, network, path, account_id):
    label = f"{path}.nodes"
    _object(value, NODES_KEYS, label)
    _required(value, NODES_KEYS, label)
    items = value["items"]
    if not isinstance(items, list) or not items:
        raise inputs.EnvFileError(f"{label}.items must be a non-empty list")
    subnet_ids = {row["id"] for row in network["public_subnets"]}
    if not re.match(r"^[a-z0-9][a-z0-9.]*$", value["instance_type"] or ""):
        raise inputs.EnvFileError(f"{label}.instance_type is invalid")
    if not isinstance(value["volume_gb"], int) or value["volume_gb"] < 8:
        raise inputs.EnvFileError(f"{label}.volume_gb must be an integer >= 8")
    _aws_id(value["ami_id"], "ami-", f"{label}.ami_id")
    _safe_text(value["key_pair_name"], f"{label}.key_pair_name")
    _boolean(value["session_manager"], f"{label}.session_manager")
    names, roles = [], set()
    for index, node in enumerate(items):
        node_label = f"{label}.items[{index}]"
        _object(node, NODE_KEYS, node_label)
        _required(node, set(NODE_KEYS) - {
            "instance_profile_arn", "request_service"}, node_label)
        if not NODE_NAME_RE.match(node["name"] or ""):
            raise inputs.EnvFileError(
                f"{node_label}.name must be a lowercase hostname label")
        if not ROLE_RE.match(node["role"] or ""):
            raise inputs.EnvFileError(
                f"{node_label}.role is not an opaque role name")
        _boolean(node["serving_target"], f"{node_label}.serving_target")
        if "request_service" in node:
            _boolean(node["request_service"],
                     f"{node_label}.request_service")
        if node["subnet_id"] not in subnet_ids:
            raise inputs.EnvFileError(
                f"{node_label}.subnet_id is not a declared public subnet")
        expected_az = next(row["availability_zone"]
                           for row in network["public_subnets"]
                           if row["id"] == node["subnet_id"])
        if node["availability_zone"] != expected_az:
            raise inputs.EnvFileError(
                f"{node_label}.availability_zone does not match its subnet")
        if node.get("instance_profile_arn"):
            parse_arn(node["instance_profile_arn"],
                      f"{node_label}.instance_profile_arn", account_id, None,
                      ("iam",))
        names.append(node["name"])
        roles.add(node["role"])
    if len(names) != len(set(names)):
        raise inputs.EnvFileError(f"{label}.items contains duplicate names")
    if not any(node["serving_target"] for node in items):
        raise inputs.EnvFileError(
            f"{label}.items needs at least one serving_target")

    profiles = value["profiles"]
    if not isinstance(profiles, dict) or set(profiles) != roles:
        raise inputs.EnvFileError(
            f"{label}.profiles must have exactly one entry per node role")
    for role, profile in profiles.items():
        profile_label = f"{label}.profiles.{role}"
        _object(profile, PROFILE_KEYS, profile_label)
        existing = profile.get("profile_arn") or profile.get("role_arn")
        managed = profile.get("managed")
        if bool(existing) == bool(managed):
            raise inputs.EnvFileError(
                f"{profile_label} must declare exact existing ARNs or one "
                f"managed profile, never both")
        if existing:
            _required(profile, ("profile_arn", "role_arn"), profile_label)
            parse_arn(profile["profile_arn"],
                      f"{profile_label}.profile_arn", account_id, None,
                      ("iam",))
            parse_arn(profile["role_arn"], f"{profile_label}.role_arn",
                      account_id, None, ("iam",))
        else:
            _object(managed, MANAGED_PROFILE_KEYS,
                    f"{profile_label}.managed")
            _required(managed, MANAGED_PROFILE_KEYS,
                      f"{profile_label}.managed")
            for key in MANAGED_PROFILE_KEYS:
                if not IAM_NAME_RE.match(managed[key] or ""):
                    raise inputs.EnvFileError(
                        f"{profile_label}.managed.{key} is not an IAM name")
    for index, node in enumerate(items):
        profile = profiles[node["role"]]
        if profile.get("profile_arn"):
            if node.get("instance_profile_arn") != profile["profile_arn"]:
                raise inputs.EnvFileError(
                    f"{label}.items[{index}].instance_profile_arn must equal "
                    f"the exact profile declared for role {node['role']!r}")
        elif node.get("instance_profile_arn"):
            raise inputs.EnvFileError(
                f"{label}.items[{index}].instance_profile_arn must be omitted "
                f"for a migration-owned profile")


def _balancer(value, network, path):
    label = f"{path}.load_balancer"
    _object(value, BALANCER_KEYS, label)
    required = set(BALANCER_KEYS) - {
        "api_health_path", "certbot_health_path", "api_preserve_client_ip",
        "certbot_preserve_client_ip", "security_group_id"}
    _required(value, required, label)
    declared = {row["id"] for row in network["public_subnets"]}
    subnets = value["subnet_ids"]
    if not isinstance(subnets, list) or len(subnets) != 2 or len(
            set(subnets)) != 2:
        raise inputs.EnvFileError(
            f"{label}.subnet_ids must name exactly two distinct subnets")
    if not set(subnets).issubset(declared):
        raise inputs.EnvFileError(
            f"{label}.subnet_ids includes an undeclared subnet")
    for key in ("name", "api_target_group", "certbot_target_group"):
        value_name = value[key]
        if len(value_name) > spec_module.ELB_NAME_MAX or not ID_RE.match(
                value_name or ""):
            raise inputs.EnvFileError(
                f"{label}.{key} is not a valid ELB name")
    for key in ("api_health_path", "certbot_health_path"):
        if key in value:
            _health_path(value[key], f"{label}.{key}")
    for key in ("api_preserve_client_ip", "certbot_preserve_client_ip"):
        if key in value and not isinstance(value[key], bool):
            raise inputs.EnvFileError(f"{label}.{key} must be a boolean")
    if "security_group_id" in value and (
            not isinstance(value["security_group_id"], str)
            or not SECURITY_GROUP_RE.match(value["security_group_id"] or "")):
        raise inputs.EnvFileError(
            f"{label}.security_group_id must be an exact security-group id")


def _health_path(value, label):
    if not isinstance(value, str):
        raise inputs.EnvFileError(f"{label} must be an absolute HTTP path")
    if not spec_module.HEALTH_PATH_MIN <= len(
            value) <= spec_module.HEALTH_PATH_MAX:
        raise inputs.EnvFileError(
            f"{label} must be {spec_module.HEALTH_PATH_MIN}.."
            f"{spec_module.HEALTH_PATH_MAX} characters")
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127
           for char in value):
        raise inputs.EnvFileError(
            f"{label} cannot contain whitespace or control characters")
    if not value.startswith("/") or value.startswith("//"):
        raise inputs.EnvFileError(
            f"{label} must be an absolute path, not a scheme or host")
    if "?" in value or "#" in value:
        raise inputs.EnvFileError(
            f"{label} cannot contain a query string or fragment")
    return value


def _handoff(raw, path, account_id, region):
    """Validate the optional destructive capability as one all-or-none set.

    The ordinary brownfield language remains useful without a handoff.  Once
    one preserved allocation is declared, however, omission is ambiguity and
    every field needed to prove and invert the operation becomes mandatory.
    """
    allocations = raw.get("nlb_eip_allocations")
    role_arn = raw.get("eip_handoff_role_arn")
    canaries = raw.get("eip_handoff_canaries")
    enabled = any(value not in (None, {}, [])
                  for value in (allocations, role_arn, canaries))
    if not enabled:
        if any(key in raw for key in (
                "nlb_eip_allocations", "eip_handoff_role_arn",
                "eip_handoff_canaries")):
            raise inputs.EnvFileError(
                f"{path} has an empty preserved-address field; omit all three "
                f"handoff fields or declare a complete handoff")
        return
    if not isinstance(allocations, dict) or not allocations:
        raise inputs.EnvFileError(
            f"{path}.nlb_eip_allocations must be a non-empty AZ-to-allocation "
            f"object")
    if len(allocations) > spec_module.AZ_COUNT:
        raise inputs.EnvFileError(
            f"{path}.nlb_eip_allocations may contain at most "
            f"{spec_module.AZ_COUNT} mappings")
    selected = {
        row["availability_zone"]: row
        for row in raw["network"]["public_subnets"]
        if row["id"] in raw["load_balancer"]["subnet_ids"]}
    if not set(allocations).issubset(selected):
        unknown = sorted(set(allocations) - set(selected))
        raise inputs.EnvFileError(
            f"{path}.nlb_eip_allocations names AZ(s) outside the selected "
            f"NLB subnets: {', '.join(unknown)}")
    if len(set(allocations.values())) != len(allocations):
        raise inputs.EnvFileError(
            f"{path}.nlb_eip_allocations must use unique allocation ids")
    for az, allocation_id in allocations.items():
        if not ID_RE.match(az or ""):
            raise inputs.EnvFileError(
                f"{path}.nlb_eip_allocations has invalid AZ {az!r}")
        if not ALLOCATION_RE.match(allocation_id or ""):
            raise inputs.EnvFileError(
                f"{path}.nlb_eip_allocations.{az} is not an eipalloc id")
    if not role_arn:
        raise inputs.EnvFileError(
            f"{path}.eip_handoff_role_arn is required with preserved EIPs")
    parsed = parse_arn(role_arn, f"{path}.eip_handoff_role_arn", account_id,
                       None, ("iam",))
    if not parsed["resource"].startswith("role/"):
        raise inputs.EnvFileError(
            f"{path}.eip_handoff_role_arn must name an IAM role")
    if not isinstance(canaries, list) or not canaries:
        raise inputs.EnvFileError(
            f"{path}.eip_handoff_canaries must be a non-empty list")
    names = []
    nlb_canaries = 0
    for index, canary in enumerate(canaries):
        label = f"{path}.eip_handoff_canaries[{index}]"
        _object(canary, CANARY_KEYS, label)
        _required(canary, ("name", "protocol", "port", "host", "timeout"),
                  label)
        _safe_text(canary["name"], f"{label}.name")
        protocol = str(canary["protocol"]).lower()
        if protocol not in ("http", "https", "tcp"):
            raise inputs.EnvFileError(
                f"{label}.protocol must be http, https, or tcp")
        if not isinstance(canary["port"], int) or not 1 <= canary["port"] <= 65535:
            raise inputs.EnvFileError(f"{label}.port must be 1..65535")
        if not isinstance(canary["timeout"], (int, float)) or isinstance(
                canary["timeout"], bool) or not 0 < canary["timeout"] <= 30:
            raise inputs.EnvFileError(f"{label}.timeout must be > 0 and <= 30")
        _safe_text(canary["host"], f"{label}.host")
        request = canary.get("request")
        if request is not None:
            if not isinstance(request, str) or not request or len(request) > 8192:
                raise inputs.EnvFileError(
                    f"{label}.request must be a non-empty string no longer "
                    f"than 8192 characters")
            lowered = request.casefold()
            credential_markers = (
                "authorization", "proxy-authorization", "cookie",
                "bearer ", "basic ", "token", "secret", "password",
                "api-key", "api_key", "x-api-key",
            )
            if any(marker in lowered for marker in credential_markers):
                raise inputs.EnvFileError(
                    f"{label}.request cannot contain credential-bearing "
                    f"material; use a public canary or out-of-band secret "
                    f"resolution")
        if protocol == "https" and not canary.get("tls_sni"):
            raise inputs.EnvFileError(
                f"{label}.tls_sni is required for an HTTPS canary")
        target = canary.get("target", "nlb")
        if target not in ("nlb", "node"):
            raise inputs.EnvFileError(f"{label}.target must be nlb or node")
        if target == "nlb":
            nlb_canaries += 1
        addresses = canary.get("addresses") or []
        if target == "node" and not addresses:
            raise inputs.EnvFileError(
                f"{label}.addresses is required for a node canary")
        if addresses and (not isinstance(addresses, list)
                          or not all(isinstance(value, str) and value
                                     for value in addresses)):
            raise inputs.EnvFileError(
                f"{label}.addresses must be a non-empty string list")
        status = canary.get("expected_status")
        if protocol in ("http", "https") and (
                not isinstance(status, int) or not 100 <= status <= 599):
            raise inputs.EnvFileError(
                f"{label}.expected_status must be an HTTP status")
        names.append(canary["name"])
    if len(names) != len(set(names)):
        raise inputs.EnvFileError(
            f"{path}.eip_handoff_canaries has duplicate names")
    if not nlb_canaries:
        raise inputs.EnvFileError(
            f"{path}.eip_handoff_canaries needs at least one address-specific "
            f"NLB shadow/application canary")


def _credential(value, label):
    _object(value, CREDENTIAL_KEYS, label)
    _required(value, CREDENTIAL_KEYS, label)
    _object_ref(value["object"], f"{label}.object")
    if value["provider"] != "s3":
        raise inputs.EnvFileError(
            f"{label}.provider must be s3; brownfield discovery validates a "
            f"versioned object without secret-value permission")
    if not ID_RE.match(value["metadata_key"] or ""):
        raise inputs.EnvFileError(
            f"{label}.metadata_key is not a safe metadata identifier")
    if value["metadata_key"].lower() == "sha256":
        raise inputs.EnvFileError(
            f"{label}.metadata_key cannot reuse the sha256 integrity field")


def _object_ref(value, label):
    _object(value, OBJECT_KEYS, label)
    _required(value, OBJECT_KEYS, label)
    _bucket(value["bucket"], f"{label}.bucket")
    _safe_text(value["key"], f"{label}.key")
    _safe_text(value["version_id"], f"{label}.version_id")
    if not VERSION_RE.match(value["version_id"] or ""):
        raise inputs.EnvFileError(
            f"{label}.version_id is not an AWS object version id")
    if not SHA256_RE.match(value["sha256"] or ""):
        raise inputs.EnvFileError(f"{label}.sha256 must be 64 lowercase hex")
    if not value["version_id"]:
        raise inputs.EnvFileError(f"{label}.version_id must pin one version")


def _object(value, allowed, label):
    if not isinstance(value, dict):
        raise inputs.EnvFileError(f"{label} must be a JSON object")
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise inputs.EnvFileError(
            f"{label} has unknown key(s): {', '.join(unknown)}")


def _required(value, keys, label):
    missing = sorted(key for key in keys if value.get(key) in (None, "", []))
    if missing:
        raise inputs.EnvFileError(
            f"{label} is missing: {', '.join(missing)}")


def _reject_secret_values(value, label):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in SECRET_KEYS:
                raise inputs.EnvFileError(
                    f"{label}.{key} is a secret value; committed fleet files "
                    f"may contain only versioned metadata references")
            _reject_secret_values(item, f"{label}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_values(item, f"{label}[{index}]")


def _bucket(value, label):
    if not BUCKET_RE.match(value or ""):
        raise inputs.EnvFileError(f"{label} is not a valid S3 bucket name")


def _safe_text(value, label):
    if not isinstance(value, str) or not value:
        raise inputs.EnvFileError(f"{label} must be a non-empty string")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise inputs.EnvFileError(
            f"{label} contains a control character and cannot be rendered")


def _aws_id(value, prefix, label):
    if not re.match(rf"^{re.escape(prefix)}[0-9a-f]+$", value or ""):
        raise inputs.EnvFileError(f"{label} is not a valid {prefix} id")


def _security_groups(value, label):
    if not isinstance(value, list) or not value or len(set(value)) != len(value):
        raise inputs.EnvFileError(f"{label} must be a unique non-empty list")
    for item in value:
        _aws_id(item, "sg-", label)


def _boolean(value, label):
    if not isinstance(value, bool):
        raise inputs.EnvFileError(f"{label} must be a JSON boolean")


def _prefixes_overlap(first, second):
    first = str(first).strip("/")
    second = str(second).strip("/")
    return (first == second or first.startswith(second + "/")
            or second.startswith(first + "/"))


def _common_prefix(key):
    return key.rsplit("/", 1)[0] if "/" in key else ""
