"""Botocore-model checks for the narrow preserved-EIP provider boundary."""

import tempfile

from testit import helpers as th

from .brownfield_fixture import ACCOUNT, handoff_topology
from .provision_handoff import _plan, _source


def _client(service):
    import boto3

    session = boto3.Session(
        aws_access_key_id="testing", aws_secret_access_key="testing",
        aws_session_token="testing", region_name="us-west-2")
    return session.client(service)


def _valid_nlb_plan(topology):
    from mojo.deploy.provision import handoff

    plan = _plan(topology)
    old = plan["load_balancer"]["arn"]
    arn = (f"arn:aws:elasticloadbalancing:us-west-2:{ACCOUNT}:"
           "loadbalancer/net/maestro-shadow/0123456789abcdef")
    plan["load_balancer"]["arn"] = arn
    plan["ownership_tags"][arn] = plan["ownership_tags"].pop(old)
    plan.pop("plan_digest")
    plan["plan_digest"] = handoff.digest(plan)
    return plan


@th.django_unit_test()
def test_stubber_validates_exact_cutover_mutation_request_shapes(opts):
    from botocore.stub import Stubber
    from mojo.deploy.provision import handoff

    topology = handoff_topology(
        project_root=tempfile.mkdtemp(prefix="testit_handoff_stubber."))
    plan = _valid_nlb_plan(topology)
    source = _source()
    sts, ec2, elbv2 = _client("sts"), _client("ec2"), _client("elbv2")
    sts_stub, ec2_stub = Stubber(sts), Stubber(ec2)
    elb_stub = Stubber(elbv2)
    sts_stub.add_response("get_caller_identity", {
        "UserId": "AROATEST:handoff", "Account": ACCOUNT,
        "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/mojo-eip-handoff/test",
    }, {})
    ec2_stub.add_response("describe_addresses", {"Addresses": [{
        "AllocationId": source["allocation_id"],
        "PublicIp": source["public_ip"], "Domain": "vpc",
        "NetworkBorderGroup": "us-west-2",
        "AssociationId": source["association_id"],
        "NetworkInterfaceId": source["network_interface_id"],
        "PrivateIpAddress": source["private_ip"],
        "InstanceId": source["instance_id"],
    }]}, {"AllocationIds": [source["allocation_id"]]})
    ec2_stub.add_response("describe_network_interfaces", {
        "NetworkInterfaces": [{
            "NetworkInterfaceId": source["network_interface_id"],
            "VpcId": "vpc-0123456789abcdef0", "SubnetId": "subnet-source",
            "AvailabilityZone": "us-west-2c", "Status": "in-use",
            "Attachment": {"InstanceId": source["instance_id"],
                           "AttachmentId": "eni-attach-0123456789abcdef0",
                           "DeviceIndex": 0, "Status": "attached"},
            "PrivateIpAddress": source["private_ip"],
            "PrivateIpAddresses": [{"PrivateIpAddress": source["private_ip"],
                                     "Primary": True}],
        }]}, {"NetworkInterfaceIds": [source["network_interface_id"]]})
    ec2_stub.add_response("disassociate_address", {}, {
        "AssociationId": source["association_id"]})
    ec2_stub.add_response("associate_address", {
        "AssociationId": "eipassoc-restored"}, {
            "AllocationId": source["allocation_id"],
            "NetworkInterfaceId": source["network_interface_id"],
            "PrivateIpAddress": source["private_ip"],
            "AllowReassociation": False,
        })
    mappings = [{"SubnetId": "subnet-0123456789abcdef0",
                 "AllocationId": source["allocation_id"]},
                {"SubnetId": "subnet-1123456789abcdef0"}]
    elb_stub.add_response("set_subnets", {"AvailabilityZones": []}, {
        "LoadBalancerArn": plan["load_balancer"]["arn"],
        "SubnetMappings": mappings})

    connection = handoff.HandoffClients(
        topology=topology, sts=sts, ec2=ec2, elbv2=elbv2)
    handoff._bind_boundary_from_plan(connection, plan)
    with sts_stub, ec2_stub, elb_stub:
        identity = connection.get("sts").get_caller_identity()
        th.assert_eq(identity["Account"], ACCOUNT,
                     f"exact STS request must be model-valid: {identity}")
        rows = connection.get("ec2").describe_addresses(
            AllocationIds=[source["allocation_id"]])["Addresses"]
        th.assert_eq(rows[0]["NetworkInterfaceId"],
                     source["network_interface_id"],
                     f"exact address request must be model-valid: {rows}")
        connection.get("ec2").describe_network_interfaces(
            NetworkInterfaceIds=[source["network_interface_id"]])
        connection.get("ec2").disassociate_address(
            AssociationId=source["association_id"])
        connection.get("ec2").associate_address(
            AllocationId=source["allocation_id"],
            NetworkInterfaceId=source["network_interface_id"],
            PrivateIpAddress=source["private_ip"],
            AllowReassociation=False)
        connection.get("elbv2").set_subnets(
            LoadBalancerArn=plan["load_balancer"]["arn"],
            SubnetMappings=mappings)


@th.django_unit_test()
def test_stubber_validates_versioned_conditional_journal_requests(opts):
    from botocore.stub import ANY, Stubber
    from mojo.deploy.provision import handoff

    topology = handoff_topology(
        project_root=tempfile.mkdtemp(prefix="testit_handoff_s3_stubber."))
    plan = _valid_nlb_plan(topology)
    operation = "op-stubber"
    s3 = _client("s3")
    stub = Stubber(s3)
    connection = handoff.HandoffClients(topology=topology, s3=s3)
    store = handoff.JournalStore(connection, topology, operation)
    journal = handoff._new_journal(operation, plan, "rehearsal")
    stub.add_response("get_bucket_versioning", {"Status": "Enabled"}, {
        "Bucket": topology.eip_handoff_bucket})
    stub.add_response("get_bucket_encryption", {
        "ServerSideEncryptionConfiguration": {"Rules": [{
            "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
            "BucketKeyEnabled": False,
        }]},
    }, {"Bucket": topology.eip_handoff_bucket})
    stub.add_client_error(
        "get_object", service_error_code="NoSuchKey",
        service_message="missing", http_status_code=404,
        expected_params={"Bucket": topology.eip_handoff_bucket,
                         "Key": store.lock_key})
    stub.add_response("put_object", {"ETag": '"lock-etag"'}, {
        "Bucket": topology.eip_handoff_bucket, "Key": store.lock_key,
        "Body": ANY, "ContentType": "application/json", "IfNoneMatch": "*",
    })
    stub.add_response("put_object", {"ETag": '"journal-etag"'}, {
        "Bucket": topology.eip_handoff_bucket, "Key": store.journal_key,
        "Body": ANY, "ContentType": "application/json", "IfNoneMatch": "*",
    })
    stub.add_response("put_object", {"ETag": '"journal-next"'}, {
        "Bucket": topology.eip_handoff_bucket, "Key": store.journal_key,
        "Body": ANY, "ContentType": "application/json",
        "IfMatch": "journal-etag",
    })
    with stub:
        store.verify_bucket()
        store.prepare_local(journal)
        store.acquire_lock(plan["plan_digest"], journal)
        store.start(journal)
        store.advance(journal)
    th.assert_eq(store.lock_key,
                 handoff.journal_coordinates(topology, operation)["lock_key"],
                 "the printed and conditionally-created lock must be identical")
