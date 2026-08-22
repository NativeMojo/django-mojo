"""Exact-reference, metadata-only observation for a brownfield fleet."""

import hashlib
import json

from objict import objict

from mojo.deploy.provision import discover, report


STEP = "dependencies"
EC2_TRUST = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "ec2.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
}
SSM_CORE_POLICY = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"


def observe(clients, topology):
    manifest = topology.brownfield_manifest
    findings = []
    observed = discover.blank()
    inventory = {}

    identity = _read(findings, "sts.get_caller_identity",
                     lambda: clients.get("sts").get_caller_identity(), {})
    account_id = identity.get("Account")
    caller_arn = identity.get("Arn")
    client_region = getattr(clients, "region_name", None)
    observed.account_id = account_id
    observed.caller_arn = caller_arn
    observed.region = topology.region
    if account_id != str(manifest["account_id"]):
        _mismatch(findings, "account", account_id, manifest["account_id"])
    if client_region and client_region != manifest["region"]:
        _mismatch(findings, "AWS client region", client_region,
                  manifest["region"])
    caller_partition = (caller_arn or "").split(":", 2)[1] \
        if str(caller_arn or "").startswith("arn:") else None
    for label, arn in _arn_values(manifest):
        partition = arn.split(":", 2)[1]
        if caller_partition and partition != caller_partition:
            _mismatch(findings, f"{label} ARN partition", partition,
                      caller_partition)
    inventory["account"] = {"id": account_id, "caller_arn": caller_arn,
                            "region": client_region or topology.region,
                            "partition": caller_partition}

    network = manifest["network"]
    ec2 = clients.get("ec2")
    vpcs = _read(findings, "ec2.describe_vpcs",
                 lambda: ec2.describe_vpcs(VpcIds=[network["vpc_id"]]), {})
    vpc = _one(vpcs.get("Vpcs"))
    if not vpc:
        _missing(findings, "vpc", network["vpc_id"])
    observed.vpc = discover.wrap(vpc)
    observed.vpc_id = (vpc or {}).get("VpcId")

    subnet_ids = [row["id"] for row in network["public_subnets"]]
    answer = _read(findings, "ec2.describe_subnets",
                   lambda: ec2.describe_subnets(SubnetIds=subnet_ids), {})
    subnets = answer.get("Subnets") or []
    by_id = {row.get("SubnetId"): row for row in subnets}
    for declaration in network["public_subnets"]:
        subnet = by_id.get(declaration["id"])
        if not subnet:
            _missing(findings, "subnet", declaration["id"])
            continue
        _same(findings, f"subnet {declaration['id']} VPC",
              subnet.get("VpcId"), network["vpc_id"])
        _same(findings, f"subnet {declaration['id']} AZ",
              subnet.get("AvailabilityZone"), declaration["availability_zone"])
        _same(findings, f"subnet {declaration['id']} public-IP launch posture",
              bool(subnet.get("MapPublicIpOnLaunch")), True)
        if subnet.get("State") != "available":
            _mismatch(findings, f"subnet {declaration['id']} state",
                      subnet.get("State"), "available")
    observed.subnets = discover.wrap(subnets)
    observed.public_subnet_ids = subnet_ids
    observed.azs = [row["availability_zone"]
                    for row in network["public_subnets"]]
    zone_answer = _read(
        findings, "ec2.describe_availability_zones",
        lambda: ec2.describe_availability_zones(
            ZoneNames=observed.azs), {})
    zones = {row.get("ZoneName"): row
             for row in zone_answer.get("AvailabilityZones") or []}
    for declaration in network["public_subnets"]:
        _same(findings,
              f"AZ {declaration['availability_zone']} network border group",
              (zones.get(declaration["availability_zone"]) or {}).get(
                  "NetworkBorderGroup"),
              declaration["network_border_group"])

    routes = _read(findings, "ec2.describe_route_tables",
                   lambda: ec2.describe_route_tables(Filters=[{
                       "Name": "association.subnet-id", "Values": subnet_ids,
                   }]), {})
    _validate_public_routes(findings, routes.get("RouteTables") or [], subnet_ids)
    observed.route_tables = discover.wrap(routes.get("RouteTables") or [])

    tier_group_ids = list(dict.fromkeys(
        [network["node_security_group_id"]]
        + manifest["database"]["security_group_ids"]
        + manifest["cache"]["security_group_ids"]))
    groups = _read(findings, "ec2.describe_security_groups",
                   lambda: ec2.describe_security_groups(
                       GroupIds=tier_group_ids), {})
    group_rows = groups.get("SecurityGroups") or []
    group_by_id = {row.get("GroupId"): row for row in group_rows}
    node_group = group_by_id.get(network["node_security_group_id"])
    if not node_group:
        _missing(findings, "security group", network["node_security_group_id"])
    else:
        _same(findings, "node security group VPC", node_group.get("VpcId"),
              network["vpc_id"])
    for group_id in tier_group_ids:
        row = group_by_id.get(group_id)
        if not row:
            _missing(findings, "security group", group_id)
            continue
        _same(findings, f"security group {group_id} VPC", row.get("VpcId"),
              network["vpc_id"])
    for port in (80, 443):
        if node_group and not _allows_world_port(node_group, port):
            _mismatch(findings, f"node security group ingress TCP:{port}",
                      False, True)
    node_group_id = network["node_security_group_id"]
    for plane, port in (("database", manifest["database"]["port"]),
                        ("cache", manifest["cache"]["port"])):
        if not any(_allows_group_port(group_by_id.get(group_id), port,
                                      node_group_id)
                   for group_id in manifest[plane]["security_group_ids"]):
            _mismatch(findings,
                      f"{plane} security-group ingress from node group",
                      False, True)
    observed.security_groups = objict(
        node=discover.wrap(node_group),
        database=[discover.wrap(group_by_id.get(group_id))
                  for group_id in manifest["database"]["security_group_ids"]],
        cache=[discover.wrap(group_by_id.get(group_id))
               for group_id in manifest["cache"]["security_group_ids"]])
    observed.node_sg_id = (node_group or {}).get("GroupId")
    key_answer = _read(findings, "ec2.describe_key_pairs",
                       lambda: ec2.describe_key_pairs(
                           KeyNames=[manifest["nodes"]["key_pair_name"]]), {})
    key_pair = _one(key_answer.get("KeyPairs"))
    if not key_pair:
        _missing(findings, "key pair", manifest["nodes"]["key_pair_name"])
    observed.key_pair = discover.wrap(key_pair)
    observed.key_pair_name = (key_pair or {}).get("KeyName")
    image_answer = _read(findings, "ec2.describe_images",
                         lambda: ec2.describe_images(
                             ImageIds=[manifest["nodes"]["ami_id"]]), {})
    image = _one(image_answer.get("Images"))
    if not image:
        _missing(findings, "AMI", manifest["nodes"]["ami_id"])
    observed.ami_id = (image or {}).get("ImageId")
    inventory["network"] = {
        "vpc_id": observed.vpc_id,
        "subnets": [{"id": row.get("SubnetId"),
                     "az": row.get("AvailabilityZone"),
                     "network_border_group": (
                         zones.get(row.get("AvailabilityZone")) or {}).get(
                             "NetworkBorderGroup"),
                     "state": row.get("State")} for row in subnets],
        "node_security_group_id": observed.node_sg_id,
        "tier_security_group_ids": sorted(group_by_id),
        "security_group_ingress": {
            group_id: _permission_inventory(
                (group_by_id.get(group_id) or {}).get("IpPermissions"))
            for group_id in sorted(group_by_id)},
        "key_pair_name": observed.key_pair_name,
        "ami_id": observed.ami_id,
    }

    _database(clients, manifest, network, findings, observed, inventory)
    _cache(clients, manifest, network, findings, observed, inventory)
    _storage(clients, manifest, findings, observed, inventory)
    _key_and_topic(clients, manifest, findings, observed, inventory)
    _profiles(clients, manifest, findings, observed, inventory)
    _instances(clients, topology, manifest, findings, observed, inventory)
    _owned_edge(clients, topology, manifest, findings, observed, inventory)
    _telemetry(clients, topology, findings, observed, inventory)

    redacted = json.loads(json.dumps(inventory, sort_keys=True, default=str))
    dependency_digest = hashlib.sha256(
        json.dumps(redacted, sort_keys=True, separators=(",", ":")).encode(
            "utf-8")).hexdigest()
    observed.dependency_inventory = redacted
    observed.dependency_digest = dependency_digest
    if not report.Report(findings).is_blocking():
        findings.append(report.existing(
            STEP, "dependencies.exact",
            f"{len(_flatten(redacted))} exact dependency fields validated; "
            f"digest {dependency_digest}"))
    return findings, observed


def _database(clients, manifest, network, findings, observed, inventory):
    wanted = manifest["database"]
    answer = _read(findings, "rds.describe_db_clusters",
                   lambda: clients.get("rds").describe_db_clusters(
                       DBClusterIdentifier=wanted["identifier"]), {})
    cluster = _one(answer.get("DBClusters"))
    if not cluster:
        _missing(findings, "Aurora cluster", wanted["identifier"])
        return
    checks = (
        ("ARN", cluster.get("DBClusterArn"), wanted["cluster_arn"]),
        ("identifier", cluster.get("DBClusterIdentifier"), wanted["identifier"]),
        ("engine", cluster.get("Engine"), "aurora-postgresql"),
        ("writer endpoint", cluster.get("Endpoint"), wanted["writer_endpoint"]),
        ("reader endpoint", cluster.get("ReaderEndpoint"), wanted["reader_endpoint"]),
        ("port", cluster.get("Port"), wanted["port"]),
        ("database", cluster.get("DatabaseName"), wanted["database_name"]),
        ("master user", cluster.get("MasterUsername"), wanted["master_user"]),
        ("status", cluster.get("Status"), "available"),
    )
    for label, current, expected in checks:
        _same(findings, f"Aurora {label}", current, expected)
    current_groups = sorted(row.get("VpcSecurityGroupId")
                            for row in cluster.get("VpcSecurityGroups") or [])
    _same(findings, "Aurora security groups", current_groups,
          sorted(wanted["security_group_ids"]))
    _same(findings, "Aurora subnet group", cluster.get("DBSubnetGroup"),
          wanted["subnet_group_name"])
    subnet_answer = _read(
        findings, "rds.describe_db_subnet_groups",
        lambda: clients.get("rds").describe_db_subnet_groups(
            DBSubnetGroupName=wanted["subnet_group_name"]), {})
    subnet_group = _one(subnet_answer.get("DBSubnetGroups"))
    _same(findings, "Aurora subnet-group VPC",
          (subnet_group or {}).get("VpcId"), network["vpc_id"])
    observed.db_cluster = discover.wrap(cluster)
    observed.db_endpoint = cluster.get("Endpoint")
    observed.db_reader_endpoint = cluster.get("ReaderEndpoint")
    observed.db_port = cluster.get("Port")
    inventory["database"] = {
        "arn": cluster.get("DBClusterArn"),
        "identifier": cluster.get("DBClusterIdentifier"),
        "engine": cluster.get("Engine"),
        "engine_version": cluster.get("EngineVersion"),
        "status": cluster.get("Status"),
        "endpoint": cluster.get("Endpoint"),
        "reader_endpoint": cluster.get("ReaderEndpoint"),
        "port": cluster.get("Port"),
        "vpc_security_group_ids": current_groups,
        "subnet_group": cluster.get("DBSubnetGroup"),
        "vpc_id": (subnet_group or {}).get("VpcId"),
    }
    _credential_metadata(clients, wanted["credential"], findings,
                         "database credential", inventory,
                         manifest["account_id"],
                         expected_metadata_value=wanted["application_user"])


def _cache(clients, manifest, network, findings, observed, inventory):
    wanted = manifest["cache"]
    answer = _read(findings, "elasticache.describe_replication_groups",
                   lambda: clients.get("elasticache").describe_replication_groups(
                       ReplicationGroupId=wanted["identifier"]), {})
    group = _one(answer.get("ReplicationGroups"))
    if not group:
        _missing(findings, "Valkey replication group", wanted["identifier"])
        return
    endpoint = ((group.get("NodeGroups") or [{}])[0].get(
        "PrimaryEndpoint") or {})
    checks = (
        ("ARN", group.get("ARN"), wanted["replication_group_arn"]),
        ("identifier", group.get("ReplicationGroupId"), wanted["identifier"]),
        ("engine", str(group.get("Engine") or "").strip().lower(), "valkey"),
        ("status", group.get("Status"), "available"),
        ("endpoint", endpoint.get("Address"), wanted["endpoint"]),
        ("port", endpoint.get("Port"), wanted["port"]),
        ("TLS", bool(group.get("TransitEncryptionEnabled")),
         bool(wanted["transit_encryption"])),
        ("auth", bool(group.get("AuthTokenEnabled")),
         bool(wanted["auth_enabled"])),
    )
    for label, current, expected in checks:
        _same(findings, f"Valkey {label}", current, expected)
    current_groups = sorted(row.get("SecurityGroupId")
                            for row in group.get("SecurityGroups") or [])
    _same(findings, "Valkey security groups", current_groups,
          sorted(wanted["security_group_ids"]))
    _same(findings, "Valkey subnet group", group.get("CacheSubnetGroupName"),
          wanted["subnet_group_name"])
    subnet_answer = _read(
        findings, "elasticache.describe_cache_subnet_groups",
        lambda: clients.get("elasticache").describe_cache_subnet_groups(
            CacheSubnetGroupName=wanted["subnet_group_name"]), {})
    subnet_group = _one(subnet_answer.get("CacheSubnetGroups"))
    _same(findings, "Valkey subnet-group VPC",
          (subnet_group or {}).get("VpcId"), network["vpc_id"])
    observed.cache_group = discover.wrap(group)
    observed.cache_endpoint = endpoint.get("Address")
    observed.cache_port = endpoint.get("Port")
    inventory["cache"] = {
        "arn": group.get("ARN"),
        "identifier": group.get("ReplicationGroupId"),
        "engine": group.get("Engine"),
        "engine_version": group.get("EngineVersion"),
        "status": group.get("Status"),
        "endpoint": endpoint.get("Address"), "port": endpoint.get("Port"),
        "transit_encryption": bool(group.get("TransitEncryptionEnabled")),
        "auth_enabled": bool(group.get("AuthTokenEnabled")),
        "security_group_ids": current_groups,
        "subnet_group": group.get("CacheSubnetGroupName"),
        "vpc_id": (subnet_group or {}).get("VpcId"),
    }
    if wanted.get("credential"):
        _credential_metadata(clients, wanted["credential"], findings,
                             "cache credential", inventory,
                             manifest["account_id"])


def _storage(clients, manifest, findings, observed, inventory):
    s3 = clients.get("s3")
    buckets = {}
    for label, reference in manifest["storage"].items():
        location = _read(
            findings, "s3.get_bucket_location",
            lambda bucket=reference["bucket"]: s3.get_bucket_location(
                Bucket=bucket, ExpectedBucketOwner=manifest["account_id"]), {})
        region = location.get("LocationConstraint") or "us-east-1"
        _same(findings, f"{label} bucket region", region, manifest["region"])
        prefix = _read(
            findings, "s3.list_objects_v2",
            lambda ref=reference: s3.list_objects_v2(
                Bucket=ref["bucket"], Prefix=ref["prefix"], MaxKeys=1,
                ExpectedBucketOwner=manifest["account_id"]), {})
        buckets[label] = {"bucket": reference["bucket"],
                          "prefix": reference["prefix"],
                          "region": region,
                          "has_objects": bool(prefix.get("Contents"))}
    objects = {}
    for label, reference in manifest["bootstrap"].items():
        metadata = _head_object(s3, reference, findings, label,
                                manifest["account_id"])
        objects[label] = metadata
    inventory["storage"] = buckets
    inventory["bootstrap"] = objects
    observed.config_bucket = manifest["storage"]["fleet_config"]["bucket"]
    observed.bootstrap_payload = all(
        row.get("version_id") and row.get("sha256_matches")
        for row in objects.values())
    observed.stage1 = discover.wrap(objects.get("stage1"))


def _head_object(s3, reference, findings, label, expected_owner,
                 metadata_key=None, expected_metadata_value=None):
    answer = _read(findings, "s3.head_object",
                   lambda: s3.head_object(
                       Bucket=reference["bucket"], Key=reference["key"],
                       VersionId=reference["version_id"],
                       ExpectedBucketOwner=expected_owner), {})
    version = answer.get("VersionId")
    _same(findings, f"{label} object version", version,
          reference["version_id"])
    metadata = answer.get("Metadata") or {}
    checksum = (metadata.get("sha256") or answer.get("ChecksumSHA256") or "")
    checksum_matches = checksum.lower() == reference["sha256"].lower()
    if not checksum_matches:
        _mismatch(findings, f"{label} object sha256 metadata", checksum or None,
                  reference["sha256"])
    result = {"bucket": reference["bucket"], "key": reference["key"],
              "version_id": version, "etag": answer.get("ETag"),
              "content_length": answer.get("ContentLength"),
              "sha256_matches": checksum_matches}
    if metadata_key:
        normalized_key = metadata_key.lower()
        proof = metadata.get(normalized_key)
        if proof in (None, ""):
            _mismatch(findings, f"{label} metadata key {normalized_key}",
                      proof, "a non-empty metadata proof")
        if (expected_metadata_value is not None
                and proof != expected_metadata_value):
            _mismatch(findings, f"{label} application user metadata",
                      proof, expected_metadata_value)
        result["metadata_key"] = normalized_key
        result["metadata_proven"] = bool(
            proof not in (None, "")
            and (expected_metadata_value is None
                 or proof == expected_metadata_value))
    return result


def _credential_metadata(clients, reference, findings, label, inventory,
                         expected_owner, expected_metadata_value=None):
    provider = reference["provider"]
    object_ref = reference["object"]
    if provider == "s3":
        metadata = _head_object(
            clients.get("s3"), object_ref, findings, label, expected_owner,
            metadata_key=reference["metadata_key"],
            expected_metadata_value=expected_metadata_value)
    else:
        secret_id = object_ref["bucket"]
        answer = _read(findings, "secretsmanager.describe_secret",
                       lambda: clients.get("secretsmanager").describe_secret(
                           SecretId=secret_id), {})
        versions = answer.get("VersionIdsToStages") or {}
        _same(findings, f"{label} version", object_ref["version_id"]
              in versions, True)
        metadata = {"secret_arn": answer.get("ARN"), "name": answer.get("Name"),
                    "version_present": object_ref["version_id"] in versions}
    inventory.setdefault("credential_metadata", {})[label] = metadata


def _key_and_topic(clients, manifest, findings, observed, inventory):
    kms = clients.get("kms")
    key = _read(findings, "kms.describe_key",
                lambda: kms.describe_key(
                    KeyId=manifest["kms_key_arn"]), {})
    metadata = key.get("KeyMetadata") or {}
    _same(findings, "KMS key ARN", metadata.get("Arn"),
          manifest["kms_key_arn"])
    if metadata and (not metadata.get("Enabled") or metadata.get("KeyState")
                     != "Enabled"):
        _mismatch(findings, "KMS key state", metadata.get("KeyState"),
                  "Enabled")
    observed.kms_key_id = metadata.get("KeyId")
    policy_answer = _read(
        findings, "kms.get_key_policy",
        lambda: kms.get_key_policy(KeyId=manifest["kms_key_arn"],
                                   PolicyName="default"), {})
    key_policy = _decode_policy(policy_answer.get("Policy")) or {}
    grants_answer = _read(
        findings, "kms.list_grants",
        lambda: kms.list_grants(KeyId=manifest["kms_key_arn"]), {})
    grants = grants_answer.get("Grants") or []
    partition = manifest["kms_key_arn"].split(":", 2)[1]
    account_root = f"arn:{partition}:iam::{manifest['account_id']}:root"
    roles = []
    for declaration in manifest["nodes"]["profiles"].values():
        if declaration.get("role_arn"):
            roles.append(declaration["role_arn"])
        else:
            roles.append(
                f"arn:{partition}:iam::{manifest['account_id']}:role/"
                f"{declaration['managed']['role_name']}")
    root_delegates = _policy_allows(key_policy, account_root)
    for role_arn in roles:
        grant_allows = any(
            row.get("GranteePrincipal") == role_arn
            and "Decrypt" in (row.get("Operations") or []) for row in grants)
        if not root_delegates and not _policy_allows(
                key_policy, role_arn) and not grant_allows:
            _mismatch(findings, f"KMS decrypt reachability for {role_arn}",
                      False, True)
    inventory["kms"] = {"arn": metadata.get("Arn"),
                        "key_id": metadata.get("KeyId"),
                        "state": metadata.get("KeyState"),
                        "account_iam_enabled": root_delegates,
                        "decrypt_grants": sorted(
                            {row.get("GranteePrincipal") for row in grants
                             if "Decrypt" in (row.get("Operations") or [])
                             and row.get("GranteePrincipal")})}
    if manifest.get("alarm_topic_arn"):
        topic = _read(findings, "sns.get_topic_attributes",
                      lambda: clients.get("sns").get_topic_attributes(
                          TopicArn=manifest["alarm_topic_arn"]), {})
        arn = (topic.get("Attributes") or {}).get("TopicArn")
        _same(findings, "alarm topic ARN", arn, manifest["alarm_topic_arn"])
        inventory["alarm_topic"] = {"arn": arn}


def _profiles(clients, manifest, findings, observed, inventory):
    iam = clients.get("iam")
    profiles = {}
    for role, declaration in manifest["nodes"]["profiles"].items():
        if declaration.get("managed"):
            managed = declaration["managed"]
            profile_answer = discover.optional(
                findings, STEP, "iam.get_instance_profile",
                lambda name=managed["profile_name"]: iam.get_instance_profile(
                    InstanceProfileName=name), {}) or {}
            role_answer = discover.optional(
                findings, STEP, "iam.get_role",
                lambda name=managed["role_name"]: iam.get_role(RoleName=name),
                {}) or {}
            profile = profile_answer.get("InstanceProfile") or {}
            role_row = role_answer.get("Role") or {}
            expected_tags = _owned_tags(manifest, role, "identity")
            role_collision = bool(role_row) and not _tags_match(
                role_row.get("Tags"), expected_tags)
            profile_collision = bool(profile) and not _tags_match(
                profile.get("Tags"), expected_tags)
            if role_collision:
                _mismatch(findings, f"IAM role {managed['role_name']} tags",
                          _tag_dict(role_row.get("Tags")), expected_tags)
            if profile_collision:
                _mismatch(
                    findings,
                    f"instance profile {managed['profile_name']} tags",
                    _tag_dict(profile.get("Tags")), expected_tags)
            role_owned = bool(role_row) and not role_collision
            profile_owned = bool(profile) and not profile_collision
            policy_answer, attached = {}, {}
            if role_owned:
                trust = _decode_policy(role_row.get(
                    "AssumeRolePolicyDocument"))
                if trust != EC2_TRUST:
                    _mismatch(findings, f"IAM role {managed['role_name']} trust",
                              trust, EC2_TRUST)
                    role_collision = True
                    role_owned = False
            if role_owned:
                inline = _read(
                    findings, "iam.list_role_policies",
                    lambda name=managed["role_name"]:
                    iam.list_role_policies(RoleName=name), {})
                attached = _read(
                    findings, "iam.list_attached_role_policies",
                    lambda name=managed["role_name"]:
                    iam.list_attached_role_policies(RoleName=name), {})
                expected_inline = f"{managed['role_name']}-runtime"
                extra_inline = sorted(
                    set(inline.get("PolicyNames") or ()) - {expected_inline})
                allowed_attached = ({SSM_CORE_POLICY}
                                    if manifest["nodes"]["session_manager"]
                                    else set())
                attached_arns = {row.get("PolicyArn") for row in
                                 attached.get("AttachedPolicies") or []}
                extra_attached = sorted(attached_arns - allowed_attached)
                if extra_inline or extra_attached:
                    _mismatch(
                        findings, f"IAM role {managed['role_name']} policies",
                        {"extra_inline": extra_inline,
                         "extra_attached": extra_attached},
                        {"inline": [expected_inline],
                         "attached": sorted(allowed_attached)})
                    role_collision = True
                    role_owned = False
            if profile_owned and not role_owned:
                _mismatch(
                    findings,
                    f"instance profile {managed['profile_name']} ownership",
                    "profile exists without its exact owned role",
                    "both absent or both exactly owned")
                profile_collision = True
                profile_owned = False
            if role_owned and profile_owned:
                attached_roles = profile.get("Roles") or []
                if [row.get("Arn") for row in attached_roles] != [
                        role_row.get("Arn")]:
                    _mismatch(
                        findings,
                        f"instance profile {managed['profile_name']} role",
                        [row.get("Arn") for row in attached_roles],
                        role_row.get("Arn"))
                    profile_collision = True
                    profile_owned = False
            if role_owned:
                policy_answer = discover.optional(
                    findings, STEP, "iam.get_role_policy",
                    lambda name=managed["role_name"]: iam.get_role_policy(
                        RoleName=name, PolicyName=f"{name}-runtime"), {}) or {}
            profiles[role] = {
                "managed": dict(managed),
                "profile_name": managed["profile_name"],
                "profile_arn": profile.get("Arn") if profile_owned else None,
                "role_name": managed["role_name"],
                "role_arn": role_row.get("Arn") if role_owned else None,
                "role_collision": role_collision,
                "profile_collision": profile_collision,
                "policy_document": _decode_policy(
                    policy_answer.get("PolicyDocument")),
                "ssm_core_attached": any(
                    row.get("PolicyArn") == SSM_CORE_POLICY
                    for row in attached.get("AttachedPolicies") or []),
            }
            continue
        profile_name = declaration["profile_arn"].split("instance-profile/", 1)[-1]
        role_name = declaration["role_arn"].split("role/", 1)[-1]
        answer = _read(findings, "iam.get_instance_profile",
                       lambda name=profile_name: iam.get_instance_profile(
                           InstanceProfileName=name), {})
        profile = answer.get("InstanceProfile") or {}
        _same(findings, f"{role} profile ARN", profile.get("Arn"),
              declaration["profile_arn"])
        roles = profile.get("Roles") or []
        if not any(row.get("Arn") == declaration["role_arn"] for row in roles):
            _mismatch(findings, f"{role} profile role", [row.get("Arn")
                      for row in roles], declaration["role_arn"])
        role_answer = _read(findings, "iam.get_role",
                            lambda name=role_name: iam.get_role(RoleName=name), {})
        _same(findings, f"{role} role ARN",
              (role_answer.get("Role") or {}).get("Arn"),
              declaration["role_arn"])
        profiles[role] = {"profile_name": profile_name,
                          "profile_arn": profile.get("Arn"),
                          "role_arn": declaration["role_arn"]}
    observed.brownfield_profiles = discover.wrap(profiles)
    inventory["profiles"] = profiles


def _instances(clients, topology, manifest, findings, observed, inventory):
    ec2 = clients.get("ec2")
    declared_ids = list(manifest.get("compatibility_instance_ids") or ())
    declarations = list(manifest["nodes"]["items"])
    filters = [
        {"Name": "tag:Name", "Values": [row["name"] for row in declarations]},
        {"Name": "instance-state-name",
         "Values": ["pending", "running", "stopping", "stopped"]},
    ]
    owned_answer = _read(findings, "ec2.describe_instances",
                         lambda: ec2.describe_instances(Filters=filters), {})
    candidates = _reservation_instances(owned_answer)
    volume_ids = []
    for row in candidates:
        root_name = row.get("RootDeviceName") or "/dev/xvda"
        for mapping in row.get("BlockDeviceMappings") or []:
            volume_id = (mapping.get("Ebs") or {}).get("VolumeId")
            if mapping.get("DeviceName") == root_name and volume_id:
                volume_ids.append(volume_id)
    volume_rows = []
    if volume_ids:
        volume_answer = _read(
            findings, "ec2.describe_volumes",
            lambda: ec2.describe_volumes(VolumeIds=sorted(set(volume_ids))), {})
        volume_rows = volume_answer.get("Volumes") or []
    volumes = {row.get("VolumeId"): row for row in volume_rows}
    by_name = {}
    for row in candidates:
        by_name.setdefault(discover.tags_of(row).get("Name"), []).append(row)
    instances = []
    profiles = observed.get("brownfield_profiles") or {}
    for declaration in declarations:
        matches = by_name.get(declaration["name"]) or []
        if len(matches) > 1:
            _mismatch(findings, f"node {declaration['name']} match count",
                      len(matches), 1)
            continue
        if not matches:
            continue
        row = matches[0]
        tags = discover.tags_of(row)
        expected_tags = {
            "managed-by": "django-mojo",
            "mojo:project": topology.project,
            "mojo:env": topology.env,
            "mojo:fleet": topology.fleet,
            "mojo:role": "node",
            "mojo:application-role": declaration["role"],
        }
        bad_tags = {key: (tags.get(key), value)
                    for key, value in expected_tags.items()
                    if tags.get(key) != value}
        if bad_tags:
            _mismatch(findings, f"node {declaration['name']} ownership tags",
                      bad_tags, expected_tags)
            continue
        expected_profile = declaration.get("instance_profile_arn") or (
            profiles.get(declaration["role"]) or {}).get("profile_arn")
        checks = (
            ("instance type", row.get("InstanceType"),
             manifest["nodes"]["instance_type"]),
            ("AMI", row.get("ImageId"), manifest["nodes"]["ami_id"]),
            ("VPC", row.get("VpcId"), manifest["network"]["vpc_id"]),
            ("subnet", row.get("SubnetId"), declaration["subnet_id"]),
            ("AZ", (row.get("Placement") or {}).get("AvailabilityZone"),
             declaration["availability_zone"]),
            ("instance profile", (row.get("IamInstanceProfile") or {}).get("Arn"),
             expected_profile),
        )
        valid = True
        if (row.get("State") or {}).get("Name") not in ("pending", "running"):
            _mismatch(findings, f"node {declaration['name']} state",
                      (row.get("State") or {}).get("Name"),
                      "pending or running")
            valid = False
        for label, current, expected in checks:
            if str(current) != str(expected):
                _mismatch(findings, f"node {declaration['name']} {label}",
                          current, expected)
                valid = False
        group_ids = sorted(group.get("GroupId")
                           for group in row.get("SecurityGroups") or [])
        expected_groups = [manifest["network"]["node_security_group_id"]]
        if group_ids != expected_groups:
            _mismatch(findings,
                      f"node {declaration['name']} security groups",
                      group_ids, expected_groups)
            valid = False
        root_name = row.get("RootDeviceName") or "/dev/xvda"
        root_mapping = next((mapping for mapping in
                             row.get("BlockDeviceMappings") or []
                             if mapping.get("DeviceName") == root_name), None)
        root_volume_id = ((root_mapping or {}).get("Ebs") or {}).get("VolumeId")
        root_volume = volumes.get(root_volume_id) or {}
        for label, current, expected in (
                ("root volume size", root_volume.get("Size"),
                 manifest["nodes"]["volume_gb"]),
                ("root volume encryption", root_volume.get("Encrypted"), True)):
            if current != expected:
                _mismatch(findings, f"node {declaration['name']} {label}",
                          current, expected)
                valid = False
        if valid:
            instances.append(row)
    compatibility = []
    if declared_ids:
        compat_answer = _read(findings, "ec2.describe_instances",
                              lambda: ec2.describe_instances(
                                  InstanceIds=declared_ids), {})
        compatibility = _reservation_instances(compat_answer)
        by_id = {row.get("InstanceId"): row for row in compatibility}
        for instance_id in declared_ids:
            row = by_id.get(instance_id)
            if not row:
                _missing(findings, "compatibility instance", instance_id)
                continue
            _same(findings, f"compatibility instance {instance_id} VPC",
                  row.get("VpcId"), manifest["network"]["vpc_id"])
            if (row.get("State") or {}).get("Name") not in ("pending", "running"):
                _mismatch(findings, f"compatibility instance {instance_id} state",
                          (row.get("State") or {}).get("Name"),
                          "pending or running")
    observed.instances = discover.wrap(instances)
    observed.compatibility_instances = discover.wrap(compatibility)
    inventory["instances"] = {
        "owned": [_instance_inventory(
            row, volumes.get(_root_volume_id(row))) for row in instances],
        "compatibility": [_instance_inventory(row) for row in compatibility],
    }


def _owned_edge(clients, topology, manifest, findings, observed, inventory):
    ec2 = clients.get("ec2")
    addresses = _read(findings, "ec2.describe_addresses",
                      lambda: ec2.describe_addresses(Filters=[
                          {"Name": "tag:mojo:project", "Values": [topology.project]},
                          {"Name": "tag:mojo:env", "Values": [topology.env]},
                          {"Name": "tag:mojo:fleet", "Values": [topology.fleet]},
                      ]), {})
    address_rows = addresses.get("Addresses") or []
    elbv2 = clients.get("elbv2")
    balancer = None
    try:
        answer = elbv2.describe_load_balancers(
            Names=[manifest["load_balancer"]["name"]])
        balancer = _one(answer.get("LoadBalancers"))
    except Exception as err:
        if not _not_found(err):
            _call_error(findings, "elbv2.describe_load_balancers", err)
    target_groups = {}
    for role, name in (("api", manifest["load_balancer"]["api_target_group"]),
                       ("certbot", manifest["load_balancer"]["certbot_target_group"])):
        try:
            answer = elbv2.describe_target_groups(Names=[name])
            group = _one(answer.get("TargetGroups"))
        except Exception as err:
            group = None
            if not _not_found(err):
                _call_error(findings, "elbv2.describe_target_groups", err)
        if group:
            target_groups[role] = group
    listeners, targets, attributes = [], {}, {}
    if balancer and not _owned_elbv2(
            elbv2, topology, balancer.get("LoadBalancerArn"), findings,
            "load balancer"):
        balancer = None
    if balancer:
        expected_subnets = sorted(manifest["load_balancer"]["subnet_ids"])
        actual_subnets = sorted(
            row.get("SubnetId") for row in balancer.get("AvailabilityZones") or [])
        shape_ok = True
        for label, current, expected in (
                ("load balancer type", balancer.get("Type"), "network"),
                ("load balancer scheme", balancer.get("Scheme"),
                 "internet-facing"),
                ("load balancer subnets", actual_subnets, expected_subnets)):
            if current != expected:
                _mismatch(findings, label, current, expected)
                shape_ok = False
        if not shape_ok:
            balancer = None
    for role, group in list(target_groups.items()):
        if not _owned_elbv2(
                elbv2, topology, group.get("TargetGroupArn"), findings,
                f"{role} target group"):
            target_groups.pop(role, None)
            continue
        if group.get("VpcId") != manifest["network"]["vpc_id"]:
            _mismatch(findings, f"{role} target group VPC",
                      group.get("VpcId"), manifest["network"]["vpc_id"])
            target_groups.pop(role, None)
    valid_addresses = _owned_addresses(
        topology, manifest, address_rows, balancer, findings)
    observed.addresses = discover.wrap(valid_addresses)
    if balancer:
        arn = balancer.get("LoadBalancerArn")
        listener_answer = _read(findings, "elbv2.describe_listeners",
                                lambda: elbv2.describe_listeners(
                                    LoadBalancerArn=arn), {})
        listeners = listener_answer.get("Listeners") or []
        attr_answer = _read(
            findings, "elbv2.describe_load_balancer_attributes",
            lambda: elbv2.describe_load_balancer_attributes(
                LoadBalancerArn=arn), {})
        attributes = {row.get("Key"): row.get("Value")
                      for row in attr_answer.get("Attributes") or []}
    for role, group in target_groups.items():
        answer = _read(findings, "elbv2.describe_target_health",
                       lambda arn=group.get("TargetGroupArn"):
                       elbv2.describe_target_health(TargetGroupArn=arn), {})
        targets[role] = answer.get("TargetHealthDescriptions") or []
    observed.balancer = discover.wrap(balancer)
    observed.target_groups = discover.wrap(target_groups)
    observed.listeners = discover.wrap(listeners)
    observed.targets = discover.wrap(targets)
    observed.balancer_attributes = objict(attributes)
    inventory["owned_edge"] = {
        "balancer_arn": (balancer or {}).get("LoadBalancerArn"),
        "target_group_arns": {key: row.get("TargetGroupArn")
                              for key, row in target_groups.items()},
        "address_allocations": sorted(row.get("AllocationId")
                                      for row in observed.addresses),
    }


def _owned_addresses(topology, manifest, rows, balancer, findings):
    """Return only exact, unambiguous subnet-bound NLB addresses."""
    base_tags = {
        "managed-by": "django-mojo", "mojo:project": topology.project,
        "mojo:env": topology.env, "mojo:fleet": topology.fleet,
        "mojo:role": "balancer",
    }
    balancer_name = manifest["load_balancer"]["name"]
    subnet_rows = {row["id"]: row for row in manifest["network"][
        "public_subnets"]}
    by_name = {}
    considered = []
    for row in rows:
        tags = discover.tags_of(row)
        if tags.get("mojo:role") != "balancer":
            continue
        considered.append(row)
        name = tags.get("Name")
        by_name.setdefault(name, []).append(row)

    attached = set()
    for zone in (balancer or {}).get("AvailabilityZones") or []:
        for address in zone.get("LoadBalancerAddresses") or []:
            if address.get("AllocationId"):
                attached.add(address["AllocationId"])

    valid = []
    desired_names = set()
    for subnet_id in manifest["load_balancer"]["subnet_ids"]:
        name = f"{balancer_name}:{subnet_id}"
        desired_names.add(name)
        matches = by_name.get(name) or []
        if len(matches) > 1:
            _mismatch(findings, f"NLB address {name} count", len(matches), 1)
            continue
        if not matches:
            continue
        row = matches[0]
        tags = discover.tags_of(row)
        expected_tags = dict(base_tags, Name=name)
        bad_tags = {key: (tags.get(key), value)
                    for key, value in expected_tags.items()
                    if tags.get(key) != value}
        if bad_tags:
            _mismatch(findings, f"NLB address {name} ownership tags",
                      bad_tags, expected_tags)
            continue
        declaration = subnet_rows[subnet_id]
        valid_row = True
        for label, current, expected in (
                ("domain", row.get("Domain"), "vpc"),
                ("network border group", row.get("NetworkBorderGroup"),
                 declaration["network_border_group"])):
            if current != expected:
                _mismatch(findings, f"NLB address {name} {label}",
                          current, expected)
                valid_row = False
        if row.get("AssociationId") and row.get("AllocationId") not in attached:
            _mismatch(findings, f"NLB address {name} association",
                      row.get("AssociationId"),
                      "unassociated or attached to the exact owned NLB")
            valid_row = False
        if valid_row:
            valid.append(row)

    extras = []
    for row in considered:
        if discover.tags_of(row).get("Name") not in desired_names:
            extras.append(row.get("AllocationId") or "unknown")
    if extras:
        _mismatch(findings, "fleet balancer address set", sorted(extras),
                  sorted(desired_names))
    return valid


def _owned_elbv2(client, topology, arn, findings, label):
    answer = _read(findings, "elbv2.describe_tags",
                   lambda: client.describe_tags(ResourceArns=[arn]), {})
    rows = answer.get("TagDescriptions") or []
    tags = {row.get("Key"): row.get("Value")
            for row in ((rows[0].get("Tags") if rows else None) or [])}
    expected = {
        "managed-by": "django-mojo",
        "mojo:project": topology.project,
        "mojo:env": topology.env,
        "mojo:fleet": topology.fleet,
        "mojo:role": "balancer",
    }
    if any(tags.get(key) != value for key, value in expected.items()):
        _mismatch(findings, f"{label} ownership tags", tags, expected)
        return False
    return True


def _telemetry(clients, topology, findings, observed, inventory):
    prefix = f"/mojo/{topology.project}-{topology.fleet}"
    logs_client = clients.get("logs")
    logs = _read(findings, "logs.describe_log_groups",
                 lambda: logs_client.describe_log_groups(
                     logGroupNamePrefix=prefix), {})
    expected_tags = _owned_tags(
        topology.brownfield_manifest, None, "telemetry")
    expected_tags.pop("mojo:application-role", None)
    exact_names = {f"{prefix}/{kind}"
                   for kind in ("nginx", "app", "cloud-init")}
    groups, log_collisions = [], []
    for row in logs.get("logGroups") or []:
        name = row.get("logGroupName")
        if name not in exact_names:
            continue
        tag_answer = _read(
            findings, "logs.list_tags_log_group",
            lambda n=name: logs_client.list_tags_log_group(logGroupName=n), {})
        tags = tag_answer.get("tags") or {}
        if any(tags.get(key) != value for key, value in expected_tags.items()):
            _mismatch(findings, f"log group {name} ownership tags", tags,
                      expected_tags)
            log_collisions.append(name)
            continue
        groups.append(row)
    observed.log_groups = objict({row.get("logGroupName"): discover.wrap(row)
                                  for row in groups})
    observed.log_group_collisions = log_collisions

    cloudwatch = clients.get("cloudwatch")
    alarm_names = [f"{topology.project}-{topology.fleet}-{role}-unhealthy"
                   for role in ("api", "certbot")]
    alarms = _read(findings, "cloudwatch.describe_alarms",
                   lambda: cloudwatch.describe_alarms(
                       AlarmNames=alarm_names), {})
    owned_alarms, alarm_collisions = [], []
    for row in alarms.get("MetricAlarms") or []:
        if row.get("AlarmName") not in alarm_names:
            continue
        arn = row.get("AlarmArn")
        tag_answer = _read(
            findings, "cloudwatch.list_tags_for_resource",
            lambda a=arn: cloudwatch.list_tags_for_resource(ResourceARN=a), {})
        tags = _tag_dict(tag_answer.get("Tags"))
        if any(tags.get(key) != value for key, value in expected_tags.items()):
            _mismatch(findings,
                      f"alarm {row.get('AlarmName')} ownership tags", tags,
                      expected_tags)
            alarm_collisions.append(row.get("AlarmName"))
            continue
        owned_alarms.append(row)
    observed.brownfield_alarms = discover.wrap(owned_alarms)
    observed.alarm_collisions = alarm_collisions
    inventory["telemetry"] = {
        "log_groups": sorted(row.get("logGroupName") for row in groups),
        "alarms": sorted(row.get("AlarmName")
                         for row in owned_alarms),
        "collisions": sorted(log_collisions + alarm_collisions),
    }


def _validate_public_routes(findings, route_tables, subnet_ids):
    public = set()
    for table in route_tables:
        has_public = any(
            row.get("DestinationCidrBlock") == "0.0.0.0/0"
            and str(row.get("GatewayId") or "").startswith("igw-")
            and row.get("State", "active") == "active"
            for row in table.get("Routes") or [])
        if not has_public:
            continue
        for association in table.get("Associations") or []:
            if association.get("SubnetId"):
                public.add(association["SubnetId"])
    for subnet_id in subnet_ids:
        if subnet_id not in public:
            _mismatch(findings, f"subnet {subnet_id} internet-facing route",
                      False, True)


def _allows_world_port(group, port):
    for permission in (group or {}).get("IpPermissions") or []:
        if not _permission_port(permission, port):
            continue
        if any(row.get("CidrIp") == "0.0.0.0/0"
               for row in permission.get("IpRanges") or []):
            return True
        if any(row.get("CidrIpv6") == "::/0"
               for row in permission.get("Ipv6Ranges") or []):
            return True
    return False


def _allows_group_port(group, port, source_group_id):
    for permission in (group or {}).get("IpPermissions") or []:
        if _permission_port(permission, port) and any(
                row.get("GroupId") == source_group_id
                for row in permission.get("UserIdGroupPairs") or []):
            return True
    return False


def _permission_port(permission, port):
    if permission.get("IpProtocol") == "-1":
        return True
    if permission.get("IpProtocol") not in ("tcp", "6"):
        return False
    return (permission.get("FromPort") is not None
            and permission.get("ToPort") is not None
            and permission["FromPort"] <= port <= permission["ToPort"])


def _permission_inventory(permissions):
    rows = []
    for permission in permissions or []:
        rows.append({
            "protocol": permission.get("IpProtocol"),
            "from": permission.get("FromPort"),
            "to": permission.get("ToPort"),
            "ipv4": sorted(row.get("CidrIp") for row in
                           permission.get("IpRanges") or []
                           if row.get("CidrIp")),
            "ipv6": sorted(row.get("CidrIpv6") for row in
                           permission.get("Ipv6Ranges") or []
                           if row.get("CidrIpv6")),
            "groups": sorted(row.get("GroupId") for row in
                             permission.get("UserIdGroupPairs") or []
                             if row.get("GroupId")),
            "prefix_lists": sorted(row.get("PrefixListId") for row in
                                   permission.get("PrefixListIds") or []
                                   if row.get("PrefixListId")),
        })
    return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))


def _policy_allows(policy, principal):
    for statement in policy.get("Statement") or []:
        if statement.get("Effect") != "Allow":
            continue
        principals = (statement.get("Principal") or {}).get("AWS", [])
        if isinstance(principals, str):
            principals = [principals]
        actions = statement.get("Action") or []
        if isinstance(actions, str):
            actions = [actions]
        if principal in principals and any(
                action in ("kms:*", "kms:Decrypt") for action in actions):
            return True
    return False


def _read(findings, name, func, default):
    return report.safe(findings, STEP, name, func, default)


def _same(findings, label, current, expected):
    if str(current) != str(expected):
        _mismatch(findings, label, current, expected)


def _mismatch(findings, label, current, expected):
    findings.append(report.Finding(
        STEP, report.BLIND, "dependency.mismatch",
        f"{label} is {current!r}; the manifest pins {expected!r}",
        "correct the exact reference or restore the dependency; nothing was mutated"))


def _missing(findings, kind, reference):
    findings.append(report.Finding(
        STEP, report.BLIND, "dependency.missing",
        f"exact {kind} {reference!r} was not found",
        "correct the reference or restore the dependency; nothing was mutated"))


def _one(rows):
    rows = list(rows or ())
    return rows[0] if len(rows) == 1 else None


def _reservation_instances(answer):
    rows = []
    for reservation in answer.get("Reservations") or []:
        rows.extend(reservation.get("Instances") or [])
    return rows


def _instance_inventory(row, root_volume=None):
    return {"id": row.get("InstanceId"), "vpc_id": row.get("VpcId"),
            "subnet_id": row.get("SubnetId"),
            "state": (row.get("State") or {}).get("Name"),
            "name": discover.tags_of(row).get("Name"),
            "instance_type": row.get("InstanceType"),
            "image_id": row.get("ImageId"),
            "root_volume": ({"id": (root_volume or {}).get("VolumeId"),
                             "size": (root_volume or {}).get("Size"),
                             "encrypted": (root_volume or {}).get("Encrypted")}
                            if root_volume is not None else None)}


def _root_volume_id(row):
    root_name = row.get("RootDeviceName") or "/dev/xvda"
    mapping = next((item for item in row.get("BlockDeviceMappings") or []
                    if item.get("DeviceName") == root_name), None)
    return ((mapping or {}).get("Ebs") or {}).get("VolumeId")


def _not_found(err):
    response = getattr(err, "response", {}) or {}
    code = (response.get("Error") or {}).get("Code", "")
    return code in discover.NOT_FOUND_CODES


def _call_error(findings, name, err):
    findings.append(report.Finding(
        STEP, report.BLIND, f"{name}.error", f"{name} failed: {err}",
        "fix the AWS read before applying; nothing was mutated"))


def _flatten(value):
    if isinstance(value, dict):
        rows = []
        for item in value.values():
            rows.extend(_flatten(item))
        return rows
    if isinstance(value, list):
        rows = []
        for item in value:
            rows.extend(_flatten(item))
        return rows
    return [value]


def _arn_values(value, path="manifest"):
    rows = []
    if isinstance(value, dict):
        for key, item in value.items():
            rows.extend(_arn_values(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            rows.extend(_arn_values(item, f"{path}[{index}]"))
    elif isinstance(value, str) and value.startswith("arn:"):
        rows.append((path, value))
    return rows


def _decode_policy(value):
    if isinstance(value, dict):
        return value
    if not value:
        return None
    from urllib.parse import unquote
    try:
        return json.loads(unquote(value))
    except (TypeError, ValueError):
        return None


def _owned_tags(manifest, role, resource_role):
    return {
        "managed-by": "django-mojo",
        "mojo:project": manifest["project"],
        "mojo:env": manifest["environment"],
        "mojo:fleet": manifest["fleet"],
        "mojo:role": resource_role,
        "mojo:application-role": role,
    }


def _tag_dict(rows):
    return {row.get("Key"): row.get("Value") for row in rows or []}


def _tags_match(rows, expected):
    current = _tag_dict(rows)
    return all(current.get(key) == value for key, value in expected.items())
