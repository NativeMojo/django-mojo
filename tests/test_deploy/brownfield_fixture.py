"""Secret-free brownfield declarations shared by the provision tests."""


ACCOUNT = "123456789012"
REGION = "us-west-2"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def raw_manifest():
    profile = f"arn:aws:iam::{ACCOUNT}:instance-profile/maestro-api-fleet"
    role = f"arn:aws:iam::{ACCOUNT}:role/maestro-api-fleet"
    return {
        "schema_version": 1,
        "manage_dns": False,
        "account_id": ACCOUNT,
        "region": REGION,
        "project": "maestro",
        "environment": "prod",
        "fleet": "shadow",
        "network": {
            "vpc_id": "vpc-0123456789abcdef0",
            "node_security_group_id": "sg-0123456789abcdef0",
            "public_subnets": [
                {"id": "subnet-0123456789abcdef0",
                 "availability_zone": "us-west-2a",
                 "network_border_group": "us-west-2"},
                {"id": "subnet-1123456789abcdef0",
                 "availability_zone": "us-west-2b",
                 "network_border_group": "us-west-2"},
            ],
        },
        "database": {
            "cluster_arn": (f"arn:aws:rds:{REGION}:{ACCOUNT}:cluster:"
                            "orchestra"),
            "identifier": "orchestra",
            "writer_endpoint": "orchestra.cluster.example.us-west-2.rds.amazonaws.com",
            "reader_endpoint": "orchestra.cluster-ro.example.us-west-2.rds.amazonaws.com",
            "port": 5432,
            "database_name": "orchestra",
            "master_user": "postgres",
            "application_user": "maestro_app_next",
            "subnet_group_name": "orchestra-db",
            "security_group_ids": ["sg-1123456789abcdef0"],
            "credential": {
                "provider": "s3", "metadata_key": "application-user",
                "object": {"bucket": "maestro-prod-config", "key": "secrets/db.json",
                           "version_id": "dbversion1", "sha256": SHA_A},
            },
        },
        "cache": {
            "replication_group_arn": (
                f"arn:aws:elasticache:{REGION}:{ACCOUNT}:replicationgroup:orchestra-cache"),
            "identifier": "orchestra-cache",
            "endpoint": "orchestra-cache.example.cache.amazonaws.com",
            "port": 6379,
            "transit_encryption": True,
            "auth_enabled": False,
            "subnet_group_name": "orchestra-cache",
            "security_group_ids": ["sg-2123456789abcdef0"],
        },
        "storage": {
            "config": {"bucket": "maestro-prod-config", "prefix": "config/live"},
            "releases": {"bucket": "maestro-prod-releases", "prefix": "releases"},
            "sites": {"bucket": "maestro-prod-sites", "prefix": "sites"},
            "revisions": {"bucket": "maestro-prod-sites", "prefix": "revisions"},
            "fleet_config": {"bucket": "maestro-prod-config",
                             "prefix": "fleets/shadow"},
        },
        "bootstrap": {
            "stage1": {"bucket": "maestro-prod-config", "key": "bootstrap/stage1.sh",
                       "version_id": "stageversion1", "sha256": SHA_A},
            "live_config": {"bucket": "maestro-prod-config",
                            "key": "config/live/django.conf",
                            "version_id": "configversion1", "sha256": SHA_B},
            "role_document": {"bucket": "maestro-prod-config",
                              "key": "bootstrap/node-role.json",
                              "version_id": "roleversion1", "sha256": SHA_C},
        },
        "nodes": {
            "instance_type": "t3.medium", "volume_gb": 40,
            "ami_id": "ami-0123456789abcdef0", "key_pair_name": "maestro-prod",
            "session_manager": True,
            "items": [
                {"name": "maestro-api-1", "role": "api", "serving_target": True,
                 "subnet_id": "subnet-0123456789abcdef0",
                 "availability_zone": "us-west-2a",
                 "instance_profile_arn": profile},
                {"name": "maestro-api-2", "role": "api", "serving_target": True,
                 "subnet_id": "subnet-1123456789abcdef0",
                 "availability_zone": "us-west-2b",
                 "instance_profile_arn": profile},
            ],
            "profiles": {"api": {"profile_arn": profile, "role_arn": role}},
        },
        "load_balancer": {
            "name": "maestro-shadow-nlb",
            "api_target_group": "maestro-shadow-api",
            "certbot_target_group": "maestro-shadow-http",
            "subnet_ids": ["subnet-0123456789abcdef0",
                           "subnet-1123456789abcdef0"],
        },
        "kms_key_arn": f"arn:aws:kms:{REGION}:{ACCOUNT}:key/01234567-89ab-cdef-0123-456789abcdef",
        "alarm_topic_arn": f"arn:aws:sns:{REGION}:{ACCOUNT}:maestro-alarms",
        "compatibility_instance_ids": ["i-0123456789abcdef0"],
    }


def preserved_raw(single=True):
    """A manifest declaring elastic IPs that are held OUTSIDE this fleet.

    Nothing here asks django-mojo to move an address — the provisioner has no
    such command. Declaring the allocations only tells fleet preparation that
    those addresses are already spoken for, so the shadow NLB must take AWS
    temporary addresses instead of allocating replacements.
    """
    raw = raw_manifest()
    raw["nlb_eip_allocations"] = {
        "us-west-2a": "eipalloc-0123456789abcdef0"}
    if not single:
        raw["nlb_eip_allocations"]["us-west-2b"] = (
            "eipalloc-1123456789abcdef0")
    return raw


def preserved_topology(single=True, project_root=None):
    from mojo.deploy.provision import brownfield_inputs
    return brownfield_inputs.to_spec(
        brownfield_inputs.validate(preserved_raw(single=single)),
        project_root=project_root)


def manifest():
    from mojo.deploy.provision import brownfield_inputs
    return brownfield_inputs.validate(raw_manifest())


def topology():
    from mojo.deploy.provision import brownfield_inputs
    return brownfield_inputs.to_spec(manifest())


def managed_topology():
    from mojo.deploy.provision import brownfield_inputs
    raw = raw_manifest()
    profile = {"managed": {"profile_name": "maestro-shadow-api",
                           "role_name": "maestro-shadow-api"}}
    raw["nodes"]["profiles"] = {"api": profile}
    for node in raw["nodes"]["items"]:
        node.pop("instance_profile_arn")
    return brownfield_inputs.to_spec(brownfield_inputs.validate(raw))
