"""Botocore-model validation for exact brownfield discovery requests."""

from objict import objict
from testit import helpers as th

from .brownfield_fixture import topology


class _Clients:
    def __init__(self, **clients):
        self.clients = clients

    def get(self, name):
        return self.clients[name]


def _client(service):
    import boto3

    session = boto3.Session(
        aws_access_key_id="testing", aws_secret_access_key="testing",
        aws_session_token="testing", region_name="us-west-2")
    return session.client(service)


@th.django_unit_test()
def test_stubber_validates_sts_ec2_and_owned_tag_request_shapes(opts):
    from botocore.stub import Stubber
    from mojo.deploy.provision import brownfield_discover

    spec = topology()
    manifest = spec.brownfield_manifest
    manifest["compatibility_instance_ids"] = []
    declaration = manifest["nodes"]["items"][0]
    tags = [
        {"Key": "Name", "Value": declaration["name"]},
        {"Key": "managed-by", "Value": "django-mojo"},
        {"Key": "mojo:project", "Value": spec.project},
        {"Key": "mojo:env", "Value": spec.env},
        {"Key": "mojo:fleet", "Value": spec.fleet},
        {"Key": "mojo:role", "Value": "node"},
        {"Key": "mojo:application-role", "Value": declaration["role"]},
    ]
    sts, ec2, elbv2 = _client("sts"), _client("ec2"), _client("elbv2")
    sts_stub, ec2_stub, elb_stub = Stubber(sts), Stubber(ec2), Stubber(elbv2)
    sts_stub.add_response("get_caller_identity", {
        "UserId": "AIDATESTING", "Account": manifest["account_id"],
        "Arn": f"arn:aws:iam::{manifest['account_id']}:user/testing"}, {})
    filters = [
        {"Name": "tag:Name", "Values": [row["name"] for row in
                                         manifest["nodes"]["items"]]},
        {"Name": "instance-state-name",
         "Values": ["pending", "running", "stopping", "stopped"]},
    ]
    ec2_stub.add_response("describe_instances", {"Reservations": [{
        "Instances": [{
            "InstanceId": "i-aaaaaaaaaaaaaaaaa",
            "InstanceType": manifest["nodes"]["instance_type"],
            "ImageId": manifest["nodes"]["ami_id"],
            "VpcId": manifest["network"]["vpc_id"],
            "SubnetId": declaration["subnet_id"],
            "Placement": {"AvailabilityZone": declaration["availability_zone"]},
            "State": {"Name": "running"},
            "IamInstanceProfile": {"Arn": declaration["instance_profile_arn"]},
            "SecurityGroups": [{"GroupId":
                manifest["network"]["node_security_group_id"]}],
            "RootDeviceName": "/dev/xvda",
            "BlockDeviceMappings": [{"DeviceName": "/dev/xvda",
                                     "Ebs": {"VolumeId": "vol-aaaaaaaa"}}],
            "Tags": tags,
        }],
    }]}, {"Filters": filters})
    ec2_stub.add_response("describe_volumes", {"Volumes": [{
        "VolumeId": "vol-aaaaaaaa", "Size": manifest["nodes"]["volume_gb"],
        "Encrypted": True,
    }]}, {"VolumeIds": ["vol-aaaaaaaa"]})
    balancer_arn = ("arn:aws:elasticloadbalancing:us-west-2:123456789012:"
                    "loadbalancer/net/maestro-shadow-nlb/abc")
    ownership = [
        {"Key": "managed-by", "Value": "django-mojo"},
        {"Key": "mojo:project", "Value": spec.project},
        {"Key": "mojo:env", "Value": spec.env},
        {"Key": "mojo:fleet", "Value": spec.fleet},
        {"Key": "mojo:role", "Value": "balancer"},
    ]
    elb_stub.add_response("describe_tags", {"TagDescriptions": [{
        "ResourceArn": balancer_arn, "Tags": ownership,
    }]}, {"ResourceArns": [balancer_arn]})

    with sts_stub, ec2_stub, elb_stub:
        answer = sts.get_caller_identity()
        th.assert_eq(answer["Account"], manifest["account_id"],
                     f"STS exact request must be model-valid: {answer}")
        findings, observed, inventory = [], objict(), {}
        observed.brownfield_profiles = {declaration["role"]: {
            "profile_arn": declaration["instance_profile_arn"]}}
        brownfield_discover._instances(
            _Clients(ec2=ec2), spec, manifest, findings, observed, inventory)
        th.assert_eq(len(observed.instances), 1,
                     f"exact EC2 instance/volume requests must converge: {findings}")
        accepted = brownfield_discover._owned_elbv2(
            elbv2, spec, balancer_arn, findings, "load balancer")
        th.assert_true(accepted,
                       f"exact ELB ownership lookup must pass: {findings}")


@th.django_unit_test()
def test_stubber_validates_target_group_attribute_request_shapes(opts):
    from botocore.stub import Stubber

    elbv2 = _client("elbv2")
    arn = ("arn:aws:elasticloadbalancing:us-west-2:123456789012:"
           "targetgroup/maestro-shadow-api/1234567890123456")
    stubber = Stubber(elbv2)
    load_balancer_arn = (
        "arn:aws:elasticloadbalancing:us-west-2:123456789012:"
        "loadbalancer/net/maestro-shadow-nlb/1234567890123456")
    create_request = {
        "Name": "maestro-shadow-nlb", "Type": "network",
        "Scheme": "internet-facing",
        "Subnets": ["subnet-0123456789abcdef0",
                    "subnet-1123456789abcdef0"],
        "SecurityGroups": ["sg-3123456789abcdef0"],
        "Tags": [{"Key": "managed-by", "Value": "django-mojo"}],
    }
    stubber.add_response("create_load_balancer", {"LoadBalancers": [{
        "LoadBalancerArn": load_balancer_arn,
    }]}, create_request)
    stubber.add_response("describe_target_group_attributes", {
        "Attributes": [{"Key": "preserve_client_ip.enabled",
                        "Value": "false"}],
    }, {"TargetGroupArn": arn})
    stubber.add_response("modify_target_group_attributes", {
        "Attributes": [{"Key": "preserve_client_ip.enabled",
                        "Value": "false"}],
    }, {"TargetGroupArn": arn,
        "Attributes": [{"Key": "preserve_client_ip.enabled",
                        "Value": "false"}]})
    with stubber:
        created = elbv2.create_load_balancer(**create_request)
        observed = elbv2.describe_target_group_attributes(
            TargetGroupArn=arn)
        changed = elbv2.modify_target_group_attributes(
            TargetGroupArn=arn, Attributes=[{
                "Key": "preserve_client_ip.enabled", "Value": "false"}])
    stubber.assert_no_pending_responses()
    th.assert_eq(created["LoadBalancers"][0]["LoadBalancerArn"],
                 load_balancer_arn,
                 "the provider model must accept an NLB SG at creation")
    th.assert_eq(observed, changed,
                 "exact describe/modify attribute request shapes must match")


@th.django_unit_test()
def test_stubber_validates_rds_cache_and_s3_metadata_request_shapes(opts):
    from botocore.stub import Stubber
    from mojo.deploy.provision import brownfield_discover

    spec = topology()
    manifest = spec.brownfield_manifest
    network = manifest["network"]
    database, cache = manifest["database"], manifest["cache"]
    rds, elasticache, s3 = _client("rds"), _client("elasticache"), _client("s3")
    rds_stub = Stubber(rds)
    cache_stub = Stubber(elasticache)
    s3_stub = Stubber(s3)
    rds_stub.add_response("describe_db_clusters", {"DBClusters": [{
        "DBClusterArn": database["cluster_arn"],
        "DBClusterIdentifier": database["identifier"],
        "Engine": "aurora-postgresql", "Status": "available",
        "Endpoint": database["writer_endpoint"],
        "ReaderEndpoint": database["reader_endpoint"],
        "Port": database["port"], "DatabaseName": database["database_name"],
        "MasterUsername": database["master_user"],
        "DBSubnetGroup": database["subnet_group_name"],
        "VpcSecurityGroups": [{"VpcSecurityGroupId":
            database["security_group_ids"][0]}],
    }]}, {"DBClusterIdentifier": database["identifier"]})
    rds_stub.add_response("describe_db_subnet_groups", {"DBSubnetGroups": [{
        "DBSubnetGroupName": database["subnet_group_name"],
        "VpcId": network["vpc_id"],
    }]}, {"DBSubnetGroupName": database["subnet_group_name"]})
    credential = database["credential"]
    s3_stub.add_response("head_object", {
        "VersionId": credential["object"]["version_id"], "ETag": "etag",
        "ContentLength": 20,
        "Metadata": {"sha256": credential["object"]["sha256"],
                     credential["metadata_key"]: database["application_user"]},
    }, {"Bucket": credential["object"]["bucket"],
        "Key": credential["object"]["key"],
        "VersionId": credential["object"]["version_id"],
        "ExpectedBucketOwner": manifest["account_id"]})
    cache_stub.add_response("describe_replication_groups", {
        "ReplicationGroups": [{
            "ARN": cache["replication_group_arn"],
            "ReplicationGroupId": cache["identifier"], "Engine": "valkey",
            "Status": "available", "TransitEncryptionEnabled": True,
            "AuthTokenEnabled": False,
            "NodeGroups": [{"PrimaryEndpoint": {
                "Address": cache["endpoint"], "Port": cache["port"]}}],
            "MemberClusters": [f"{cache['identifier']}-001"],
        }]}, {"ReplicationGroupId": cache["identifier"]})
    cache_stub.add_response("describe_cache_clusters", {
        "CacheClusters": [{
            "CacheClusterId": f"{cache['identifier']}-001",
            "ReplicationGroupId": cache["identifier"], "Engine": "valkey",
            "CacheClusterStatus": "available",
            "SecurityGroups": [{"SecurityGroupId":
                cache["security_group_ids"][0], "Status": "active"}],
            "CacheSubnetGroupName": cache["subnet_group_name"],
        }]}, {"CacheClusterId": f"{cache['identifier']}-001"})
    cache_stub.add_response("describe_cache_subnet_groups", {
        "CacheSubnetGroups": [{
            "CacheSubnetGroupName": cache["subnet_group_name"],
            "VpcId": network["vpc_id"],
        }]}, {"CacheSubnetGroupName": cache["subnet_group_name"]})

    with rds_stub, cache_stub, s3_stub:
        findings, observed, inventory = [], objict(), {}
        clients = _Clients(rds=rds, elasticache=elasticache, s3=s3)
        brownfield_discover._database(
            clients, manifest, network, findings, observed, inventory)
        brownfield_discover._cache(
            clients, manifest, network, findings, observed, inventory)
        th.assert_eq(findings, [],
                     f"exact RDS/ElastiCache/S3 metadata requests must pass: "
                     f"{findings}")
        th.assert_true(inventory["credential_metadata"][
            "database credential"]["metadata_proven"],
            f"the S3 metadata proof must be retained: {inventory}")
