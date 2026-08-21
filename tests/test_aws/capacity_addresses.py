"""Stable outbound IPs: the request shapes, the report, and the validator.

Same testing stance as capacity.py's own module: ``botocore.stub.Stubber``
proves every request is legal against the real service model (a Mock would
accept ``AllowReassociation`` spelled wrong), captured Mocks prove the values,
and the report is exercised with locally-constructed mock AWS clients only —
no test here patches a shared module attribute.

Deliberately NO test here reads or writes a real ``AWS_STABLE_OUTBOUND_IPS``
Setting row: ``tests/test_account/test_system_setup.py`` bulk-deletes every
registered protected key's row while modules run concurrently on one database.
The validator is tested pure.

The apply, runner, and REST tests that patch capacity's module attributes
(``_write_policy``, ``_egress_enabled``, ``_dispatch``, the aws helper
modules, …) live in ``tests/test_aws_extended_serial/capacity_addresses.py``
— the opt-in serial tier — because those patches are not parallel-safe
(maestro #2558).
"""

from types import SimpleNamespace
from unittest import mock

from testit import helpers as th


REGION = "us-east-1"
BALANCER_ARN = ("arn:aws:elasticloadbalancing:us-east-1:123456789012:"
                "loadbalancer/net/mojo-api-nlb/0123456789abcdef")
GROUP_ARN = ("arn:aws:elasticloadbalancing:us-east-1:123456789012:"
             "targetgroup/mojo-api/abcdef0123456789")
NODE_A = "i-0a1b2c3d4e5f60011"
NODE_B = "i-0a1b2c3d4e5f60022"
PROFILE_ARN = "arn:aws:iam::123456789012:instance-profile/mojo-api"

IP_A = "203.0.113.10"
IP_FREE = "203.0.113.20"
IP_FOREIGN = "198.51.100.9"
ALLOC_A = "eipalloc-000000000000000a1"
ALLOC_FREE = "eipalloc-0000000000000free"
ALLOC_FOREIGN = "eipalloc-00000000000stran"


# ── fixtures ────────────────────────────────────────────────────────────────

def _stub(service):
    import boto3
    from botocore.stub import Stubber

    client = boto3.client(
        service, region_name=REGION,
        aws_access_key_id="testing", aws_secret_access_key="testing")
    return client, Stubber(client)


def _instance(instance_id, name, public_ip=None, state="running"):
    row = {
        "InstanceId": instance_id, "InstanceType": "m6i.large",
        "ImageId": "ami-0source", "SubnetId": "subnet-0aaa", "VpcId": "vpc-0aaa",
        "State": {"Name": state}, "Placement": {"AvailabilityZone": "us-east-1a"},
        "PrivateIpAddress": "10.0.1.11",
        "PrivateDnsName": "ip-10-0-1-11.ec2.internal",
        "SecurityGroups": [{"GroupId": "sg-0aaa"}],
        "IamInstanceProfile": {"Arn": PROFILE_ARN},
        "Tags": [{"Key": "Name", "Value": name}],
    }
    if public_ip:
        row["PublicIpAddress"] = public_ip
    return row


def _ec2_client(instances=(), addresses=()):
    client = mock.Mock()
    client.describe_instances.return_value = {
        "Reservations": [{"Instances": list(instances)}]}
    client.describe_addresses.return_value = {"Addresses": list(addresses)}
    return client


def _target(instance_id, state="healthy", port=443):
    return {"Target": {"Id": instance_id, "Port": port},
            "TargetHealth": {"State": state}}


def _elbv2_client(targets=None):
    targets = targets if targets is not None else [
        _target(NODE_A), _target(NODE_B)]
    client = mock.Mock()
    client.describe_load_balancers.return_value = {"LoadBalancers": [{
        "LoadBalancerArn": BALANCER_ARN, "LoadBalancerName": "mojo-api-nlb",
        "Type": "network", "Scheme": "internet-facing",
        "State": {"Code": "active"}, "DNSName": "nlb.example.com",
        "AvailabilityZones": [{"ZoneName": "us-east-1a",
                               "LoadBalancerAddresses": []}]}]}
    client.describe_target_groups.return_value = {"TargetGroups": [
        {"TargetGroupArn": GROUP_ARN, "TargetGroupName": "mojo-api",
         "TargetType": "instance", "Protocol": "TCP", "Port": 443,
         "LoadBalancerArns": [BALANCER_ARN]}]}
    client.describe_target_health.side_effect = lambda TargetGroupArn: {
        "TargetHealthDescriptions": list(targets)}
    client.describe_target_group_attributes.return_value = {
        "Attributes": [{"Key": "deregistration_delay.timeout_seconds",
                        "Value": "30"}]}
    return client


def _rds_client():
    client = mock.Mock()
    client.describe_db_clusters.return_value = {"DBClusters": []}
    client.describe_db_instances.return_value = {"DBInstances": []}
    return client


def _cache_client():
    client = mock.Mock()
    client.describe_replication_groups.return_value = {"ReplicationGroups": []}
    return client


def _eip(allocation_id, public_ip, instance_id=None, association_id=None,
         network_interface_id=None, tags=None):
    row = {"AllocationId": allocation_id, "PublicIp": public_ip, "Domain": "vpc"}
    if instance_id:
        row["InstanceId"] = instance_id
        row["AssociationId"] = association_id or "eipassoc-1"
        row["NetworkInterfaceId"] = network_interface_id or "eni-1"
    elif association_id or network_interface_id:
        row["AssociationId"] = association_id or "eipassoc-nlb"
        row["NetworkInterfaceId"] = network_interface_id or "eni-nlb"
    if tags is not None:
        row["Tags"] = [{"Key": key, "Value": value}
                       for key, value in sorted(tags.items())]
    return row


def _stable_tags(name="mojo-api-a"):
    from mojo.apps.aws.services import capacity
    return {capacity.STABLE_EIP_TAG: capacity.STABLE_EIP_TAG_VALUE,
            "Name": name}


def _client_error(code, operation, status=400):
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": code, "Message": "refused"},
                        "ResponseMetadata": {"HTTPStatusCode": status}},
                       operation)


def _clear_stable_claims():
    from mojo.apps.aws.services import capacity
    for action in (capacity.ACTION_ENABLE_STABLE_IPS,
                   capacity.ACTION_DISABLE_STABLE_IPS):
        capacity._release(capacity._claim_key(action, "fleet"))
    capacity.invalidate()


@th.django_unit_setup()
def setup_capacity_addresses(opts):
    _clear_stable_claims()


# ── helpers: EC2 request shapes ─────────────────────────────────────────────

@th.django_unit_test("an address allocation is vpc-scoped and tagged at creation")
def test_allocate_address_shape(opts):
    from mojo.helpers.aws import ec2

    client, stubber = _stub("ec2")
    stubber.add_response(
        "allocate_address",
        {"AllocationId": ALLOC_A, "PublicIp": IP_A},
        {"Domain": "vpc",
         "TagSpecifications": [{"ResourceType": "elastic-ip", "Tags": [
             {"Key": "Name", "Value": "mojo-api-a"},
             {"Key": "mojo:eip", "Value": "stable-egress"}]}]})
    with stubber:
        created = ec2.allocate_address(
            {"Name": "mojo-api-a", "mojo:eip": "stable-egress"}, client=client)
    assert created == {"allocation_id": ALLOC_A, "public_ip": IP_A}, \
        f"the allocation ids were not returned: {created!r}"
    stubber.assert_no_pending_responses()


@th.django_unit_test("an association NEVER steals an address from another instance")
def test_associate_address_never_steals(opts):
    from mojo.helpers.aws import ec2

    client, stubber = _stub("ec2")
    stubber.add_response(
        "associate_address", {"AssociationId": "eipassoc-9"},
        {"AllocationId": ALLOC_A, "InstanceId": NODE_A,
         "AllowReassociation": False})
    with stubber:
        association = ec2.associate_address(ALLOC_A, NODE_A, client=client)
    assert association == "eipassoc-9", \
        f"the association id was not returned: {association!r}"
    stubber.assert_no_pending_responses()


@th.django_unit_test("a disassociation names exactly one association, and never releases")
def test_disassociate_address_shape(opts):
    from mojo.helpers.aws import ec2

    client, stubber = _stub("ec2")
    stubber.add_response("disassociate_address", {},
                         {"AssociationId": "eipassoc-9"})
    with stubber:
        done = ec2.disassociate_address("eipassoc-9", client=client)
    assert done is True, "the disassociation did not report success"
    stubber.assert_no_pending_responses()
    assert not hasattr(ec2, "release_address"), \
        "a release primitive exists — releasing reserved addresses is " \
        "deliberately NOT part of this feature"


@th.django_unit_test("address_map projects one model-valid describe")
def test_address_map_projection(opts):
    from mojo.helpers.aws import ec2

    client, stubber = _stub("ec2")
    stubber.add_response("describe_addresses", {"Addresses": [
        _eip(ALLOC_A, IP_A, instance_id=NODE_A, tags=_stable_tags()),
        _eip(ALLOC_FREE, IP_FREE, tags=_stable_tags("spare")),
    ]}, {})
    with stubber:
        rows = ec2.address_map(client=client)
    assert [row["allocation_id"] for row in rows] == [ALLOC_A, ALLOC_FREE], \
        f"the describe rows were not projected: {rows!r}"
    assert rows[0]["instance_id"] == NODE_A and rows[0]["association_id"], \
        f"the attached row lost its association facts: {rows[0]!r}"
    assert rows[1]["instance_id"] is None and rows[1]["association_id"] is None, \
        f"the free row grew association facts: {rows[1]!r}"
    assert rows[0]["tags"]["mojo:eip"] == "stable-egress", \
        f"tags were not mapped: {rows[0]['tags']!r}"
    stubber.assert_no_pending_responses()


# ── the address view ────────────────────────────────────────────────────────

@th.django_unit_test("an NLB-held address is never treated as a reusable reservation")
def test_address_view_classification(opts):
    from mojo.apps.aws.services import capacity

    addresses = [
        {"allocation_id": ALLOC_A, "public_ip": IP_A, "instance_id": NODE_A,
         "association_id": "eipassoc-1", "network_interface_id": "eni-1",
         "tags": _stable_tags(), "name": "mojo-api-a"},
        # An NLB's address: no instance, but very much associated.
        {"allocation_id": "eipalloc-nlb", "public_ip": "203.0.113.99",
         "instance_id": None, "association_id": None,
         "network_interface_id": "eni-nlb",
         "tags": _stable_tags("nlb"), "name": "nlb"},
        # A genuinely free managed reservation.
        {"allocation_id": ALLOC_FREE, "public_ip": IP_FREE, "instance_id": None,
         "association_id": None, "network_interface_id": None,
         "tags": _stable_tags("spare"), "name": "spare"},
        # A free FOREIGN address: untagged, never reusable.
        {"allocation_id": ALLOC_FOREIGN, "public_ip": IP_FOREIGN,
         "instance_id": None, "association_id": None,
         "network_interface_id": None, "tags": {}, "name": None},
    ]
    view = capacity._address_view([NODE_A, NODE_B], addresses)
    assert [row["instance"] for row in view["attached"]] == [NODE_A], \
        f"attached mis-classified: {view['attached']!r}"
    assert view["attached"][0]["managed"] is True, \
        "a stable-tagged attached address was not marked managed"
    assert view["pending"] == [NODE_B], \
        f"the node without an address was not pending: {view['pending']!r}"
    assert [row["allocation_id"] for row in view["reserved"]] == [ALLOC_FREE], \
        (f"reserved must be exactly the stable-tagged, fully unassociated "
         f"addresses — never the NLB's, never a stranger's: {view['reserved']!r}")


# ── the report ──────────────────────────────────────────────────────────────

def _report(elbv2=None, ec2=None):
    from mojo.apps.aws.services import capacity
    return capacity.report(
        elbv2_client=elbv2 if elbv2 is not None else _elbv2_client(),
        ec2_client=ec2 if ec2 is not None else _ec2_client(),
        rds_client=_rds_client(), cache_client=_cache_client())


@th.django_unit_test("node rows carry the public address, and egress the allowlist")
def test_report_egress_and_node_rows(opts):
    ec2 = _ec2_client(
        instances=[_instance(NODE_A, "mojo-api-a", public_ip=IP_A),
                   _instance(NODE_B, "mojo-api-b", public_ip="52.0.0.9")],
        addresses=[_eip(ALLOC_A, IP_A, instance_id=NODE_A,
                        tags=_stable_tags())])
    envelope = _report(ec2=ec2)

    rows = {row["id"]: row for row in envelope["nodes"]["instances"]}
    assert rows[NODE_A]["public_ip"] == IP_A and rows[NODE_A]["stable_ip"], \
        f"the addressed node row is wrong: {rows[NODE_A]!r}"
    assert rows[NODE_B]["public_ip"] == "52.0.0.9" and not rows[NODE_B]["stable_ip"], \
        f"the auto-assigned node row is wrong: {rows[NODE_B]!r}"

    egress = envelope["egress"]
    assert egress["available"] and egress["fleet_available"] \
        and egress["addresses_available"], f"egress degraded: {egress!r}"
    assert egress["addresses"] == [IP_A], \
        f"the allowlist is not the attached EIPs: {egress['addresses']!r}"
    assert egress["pending_nodes"] == [NODE_B], \
        f"the unaddressed node is not pending: {egress['pending_nodes']!r}"
    assert egress["to_allocate"] == 1 and egress["monthly_usd_per_address"] == 3.6, \
        f"the allocation plan math is wrong: {egress!r}"
    assert egress["enabled"] is False, \
        "policy read True with no Setting row present"

    actions = envelope["actions"]
    assert actions["enable_stable_ips"]["offered"], \
        f"enable was not offered on a healthy read: {actions['enable_stable_ips']!r}"
    # Policy off and one MANAGED address still attached: a half-done detach —
    # disable stays offered to finish it.
    assert actions["disable_stable_ips"]["offered"], \
        f"disable was not offered with a managed address attached: " \
        f"{actions['disable_stable_ips']!r}"


@th.django_unit_test("disable is blocked only when there is truly nothing to detach")
def test_report_disable_not_enabled(opts):
    ec2 = _ec2_client(
        instances=[_instance(NODE_A, "mojo-api-a", public_ip="52.0.0.8"),
                   _instance(NODE_B, "mojo-api-b", public_ip="52.0.0.9")],
        addresses=[])
    envelope = _report(ec2=ec2)
    offer = envelope["actions"]["disable_stable_ips"]
    assert offer == {"offered": False, "blocked_reason": "not_enabled"}, \
        f"an off, detached fleet still offered disable: {offer!r}"


@th.django_unit_test("a failed read degrades visibly — unknown is never an empty allowlist")
def test_report_degrades_visibly(opts):
    # The ADDRESSES read fails: egress unavailable, both actions blocked on it.
    ec2 = _ec2_client(instances=[_instance(NODE_A, "mojo-api-a", public_ip=IP_A)])
    ec2.describe_addresses.side_effect = _client_error(
        "UnauthorizedOperation", "DescribeAddresses", 403)
    envelope = _report(ec2=ec2)
    egress = envelope["egress"]
    assert egress["available"] is False and egress["addresses"] == [], \
        f"a failed addresses read did not degrade: {egress!r}"
    assert any(warning["code"] == "addresses"
               for warning in envelope["warnings"]), \
        f"the failed read produced no warning: {envelope['warnings']!r}"
    for name in ("enable_stable_ips", "disable_stable_ips"):
        assert envelope["actions"][name] == {
            "offered": False, "blocked_reason": "addresses_unavailable"}, \
            f"{name} was not blocked on the failed read: {envelope['actions'][name]!r}"

    # The SERVING read fails: the fleet is UNKNOWN, which must never be
    # reported as an empty fleet (`no_fleet_nodes`) or an empty canonical list.
    elbv2 = _elbv2_client()
    elbv2.describe_load_balancers.side_effect = _client_error(
        "UnauthorizedOperation", "DescribeLoadBalancers", 403)
    envelope = _report(elbv2=elbv2)
    assert envelope["egress"]["available"] is False, \
        "a failed serving read still claimed an available egress picture"
    for name in ("enable_stable_ips", "disable_stable_ips"):
        assert envelope["actions"][name] == {
            "offered": False, "blocked_reason": "fleet_unavailable"}, \
            f"{name} did not distinguish unknown from empty: " \
            f"{envelope['actions'][name]!r}"


# ── the validator ───────────────────────────────────────────────────────────

@th.django_unit_test("the policy validator accepts exactly {'enabled': bool}")
def test_stable_outbound_ips_validator(opts):
    from mojo.apps.aws.settings_validators import stable_outbound_ips

    assert stable_outbound_ips("K", {"enabled": True}) == {"enabled": True}, \
        "a valid enable payload was rewritten"
    assert stable_outbound_ips("K", {"enabled": False}) == {"enabled": False}, \
        "a valid disable payload was rewritten"
    for bad in (True, "on", {"enabled": "yes"}, {"enabled": 1},
                {"enabled": True, "extra": 1}, {}):
        with th.assert_raises(ValueError):
            stable_outbound_ips("K", bad)


@th.django_unit_test("the policy key is registered as a protected setting")
def test_policy_key_is_protected(opts):
    from mojo.apps.account.services import system_settings
    from mojo.apps.aws.services import capacity

    assert system_settings.is_protected_setting(capacity.STABLE_EGRESS_SETTING), \
        ("AWS_STABLE_OUTBOUND_IPS is not registered as protected — the "
         "generic settings REST plane could write the admission gate")


# ── apply ───────────────────────────────────────────────────────────────────


@th.django_unit_test("enable and disable serialize on ONE fixed fleet key")
def test_stable_ips_single_flight(opts):
    from mojo.apps.aws.services import capacity

    _clear_stable_claims()
    capacity._claim(capacity.ACTION_ENABLE_STABLE_IPS, "fleet", 1)
    with th.assert_raises(capacity.CapacityError) as caught:
        capacity._claim(capacity.ACTION_DISABLE_STABLE_IPS, "anything", 2)
    assert caught.exception.error_code == "capacity_in_progress", \
        (f"a disable was not serialized against a running enable: "
         f"{caught.exception.error_code}")
    _clear_stable_claims()


# ── balancer-less read-only fallback ────────────────────────────────────────

@th.django_unit_test("a balancer-less install still shows who holds a stable address")
def test_report_fallback_for_balancerless_install(opts):
    # A stage-shaped estate: one node, no registered fleet, an EIP attached
    # by an external tool (opentofu tags), plus an NLB-held address and a free
    # reservation that must both stay out of the fallback.
    ec2 = _ec2_client(
        instances=[_instance(NODE_A, "wmx1", public_ip=IP_A)],
        addresses=[
            _eip(ALLOC_A, IP_A, instance_id=NODE_A,
                 tags={"Env": "stage", "ManagedBy": "opentofu"}),
            _eip("eipalloc-000000000000n1b00", "198.51.100.44",
                 association_id="eipassoc-nlb", network_interface_id="eni-nlb"),
            _eip(ALLOC_FREE, IP_FREE, tags=_stable_tags()),
        ])
    envelope = _report(elbv2=_elbv2_client(targets=[]), ec2=ec2)
    egress = envelope["egress"]

    assert egress["fallback_attached"] == [{
        "instance": NODE_A, "instance_name": "wmx1", "public_ip": IP_A,
        "allocation_id": ALLOC_A, "managed": False,
    }], f"the externally managed address was not surfaced: " \
        f"{egress['fallback_attached']!r}"
    assert egress["addresses"] == [] and egress["attached"] == [], \
        "fallback rows must never leak into the fleet-scoped allowlist"
    assert envelope["actions"]["enable_stable_ips"]["blocked_reason"] \
        == "no_fleet_nodes", \
        "the toggle must stay absent — the fallback is read-only"


@th.django_unit_test("a managed-tagged fallback address never makes disable available")
def test_report_fallback_never_reaches_offers(opts):
    # Even wearing this feature's own tag, an address on an unregistered
    # instance is outside the fleet: disable must stay blocked, because the
    # runner (fleet-scoped) would detach nothing and must not be offered.
    ec2 = _ec2_client(
        instances=[_instance(NODE_A, "wmx1", public_ip=IP_A)],
        addresses=[_eip(ALLOC_A, IP_A, instance_id=NODE_A,
                        tags=_stable_tags("wmx1"))])
    envelope = _report(elbv2=_elbv2_client(targets=[]), ec2=ec2)

    assert envelope["egress"]["fallback_attached"][0]["managed"] is True, \
        "the managed flag should still report the tag truthfully"
    assert envelope["actions"]["disable_stable_ips"] == {
        "offered": False, "blocked_reason": "not_enabled"}, \
        f"a fallback row reached the offers: " \
        f"{envelope['actions']['disable_stable_ips']!r}"
