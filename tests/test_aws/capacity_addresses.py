"""Stable outbound IPs: the request shapes, the report, and the two runners.

Same testing stance as capacity.py's own module: ``botocore.stub.Stubber``
proves every request is legal against the real service model (a Mock would
accept ``AllowReassociation`` spelled wrong), captured Mocks prove the values,
and the runners are exercised through capacity's own seams
(``mock.patch.object(capacity, ...)``), the way the module already patches
``_local_hostname`` and ``_dispatch``.

Deliberately NO test here reads or writes a real ``AWS_STABLE_OUTBOUND_IPS``
Setting row: ``tests/test_account/test_system_setup.py`` bulk-deletes every
registered protected key's row while modules run concurrently on one database,
and the house ``_actor()`` is a SimpleNamespace the protected writer rejects.
The policy read and the policy writer are patched at capacity's boundary; the
validator is tested pure.
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
NEW_NODE = "i-0a1b2c3d4e5f60044"
PROFILE_ARN = "arn:aws:iam::123456789012:instance-profile/mojo-api"

IP_A = "203.0.113.10"
IP_B = "203.0.113.11"
IP_FREE = "203.0.113.20"
IP_FOREIGN = "198.51.100.9"
ALLOC_A = "eipalloc-000000000000000a1"
ALLOC_B = "eipalloc-000000000000000b2"
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


def _actor(pk=1):
    return SimpleNamespace(pk=pk, username="capacity-actor", is_superuser=True)


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


def _provider_error(code, operation="ec2.allocate_address",
                    iam="ec2:AllocateAddress"):
    from mojo.helpers.aws.provider_call import ProviderCallError
    return ProviderCallError(operation, code, iam, "", False, "attempted", False)


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


@th.django_unit_test("a converged, enabled fleet blocks enable and offers disable")
def test_report_offers_when_enabled(opts):
    from mojo.apps.aws.services import capacity

    ec2 = _ec2_client(
        instances=[_instance(NODE_A, "mojo-api-a", public_ip=IP_A),
                   _instance(NODE_B, "mojo-api-b", public_ip=IP_B)],
        addresses=[
            _eip(ALLOC_A, IP_A, instance_id=NODE_A, tags=_stable_tags()),
            _eip(ALLOC_B, IP_B, instance_id=NODE_B,
                 tags=_stable_tags("mojo-api-b"))])
    with mock.patch.object(capacity, "_egress_enabled", return_value=True):
        envelope = _report(ec2=ec2)
    actions = envelope["actions"]
    assert actions["enable_stable_ips"] == {
        "offered": False, "blocked_reason": "already_enabled"}, \
        f"a converged fleet still offered enable: {actions['enable_stable_ips']!r}"
    assert actions["disable_stable_ips"]["offered"], \
        f"an enabled fleet did not offer disable: {actions['disable_stable_ips']!r}"
    assert sorted(envelope["egress"]["addresses"]) == [IP_A, IP_B], \
        f"the allowlist is wrong: {envelope['egress']['addresses']!r}"

    # Enabled but UNCONVERGED: re-running enable IS the retry path.
    ec2 = _ec2_client(
        instances=[_instance(NODE_A, "mojo-api-a", public_ip=IP_A),
                   _instance(NODE_B, "mojo-api-b", public_ip="52.0.0.9")],
        addresses=[_eip(ALLOC_A, IP_A, instance_id=NODE_A,
                        tags=_stable_tags())])
    with mock.patch.object(capacity, "_egress_enabled", return_value=True):
        envelope = _report(ec2=ec2)
    assert envelope["actions"]["enable_stable_ips"]["offered"], \
        "an unconverged enable was not re-offered as the retry path"


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

@th.django_unit_test("an enable is planned, policy-written, claimed once, and dispatched")
def test_apply_enable_writes_policy_and_dispatches(opts):
    from mojo.apps.aws.services import capacity

    _clear_stable_claims()
    ec2 = _ec2_client(addresses=[_eip(ALLOC_FREE, IP_FREE,
                                      tags=_stable_tags("spare"))])
    with mock.patch.object(capacity, "_write_policy") as policy, \
            mock.patch.object(capacity, "_dispatch") as dispatched:
        record = capacity.apply(_actor(), capacity.ACTION_ENABLE_STABLE_IPS,
                                elbv2_client=_elbv2_client(), ec2_client=ec2)
    assert policy.call_args[0][1] is True, \
        f"the durable policy was not written as enabled: {policy.call_args!r}"
    assert dispatched.call_count == 1, "the operation was never dispatched"
    detail = record["detail"]
    assert detail["pending"] == [NODE_A, NODE_B], \
        f"the plan did not name the unaddressed nodes: {detail!r}"
    assert detail["reserved"] == [ALLOC_FREE] and detail["to_allocate"] == 1, \
        f"the reuse/allocate split is wrong: {detail!r}"
    assert record["phases"] == ["planning", "associating", "verifying"], \
        f"the enable phases are wrong: {record['phases']!r}"
    _clear_stable_claims()

    # The policy writer failing must RELEASE the claim and start nothing.
    with mock.patch.object(capacity, "_write_policy",
                           side_effect=RuntimeError("revoked")), \
            mock.patch.object(capacity, "_dispatch") as dispatched:
        with th.assert_raises(RuntimeError):
            capacity.apply(_actor(), capacity.ACTION_ENABLE_STABLE_IPS,
                           elbv2_client=_elbv2_client(), ec2_client=_ec2_client())
        assert dispatched.call_count == 0, \
            "a failed policy write still dispatched an operation"
    key = capacity._claim(capacity.ACTION_ENABLE_STABLE_IPS, "fleet", 9)
    assert key, "the claim was still held after a failed policy write"
    _clear_stable_claims()


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


@th.django_unit_test("an empty fleet refuses enable before any claim or write")
def test_apply_enable_refuses_empty_fleet(opts):
    from mojo.apps.aws.services import capacity

    _clear_stable_claims()
    with mock.patch.object(capacity, "_write_policy") as policy, \
            mock.patch.object(capacity, "_dispatch") as dispatched:
        with th.assert_raises(capacity.CapacityError) as caught:
            capacity.apply(_actor(), capacity.ACTION_ENABLE_STABLE_IPS,
                           elbv2_client=_elbv2_client(targets=[]),
                           ec2_client=_ec2_client())
    assert caught.exception.error_code == "no_fleet_nodes" \
        and caught.exception.status == 409, \
        f"an empty fleet was not refused: {caught.exception.error_code}"
    assert policy.call_count == 0 and dispatched.call_count == 0, \
        "an empty-fleet enable still wrote policy or dispatched"


@th.django_unit_test("an explicit assignment is refused unless provably safe")
def test_apply_assign_validation(opts):
    from mojo.apps.aws.services import capacity

    _clear_stable_claims()
    ec2 = _ec2_client(addresses=[
        _eip(ALLOC_A, IP_A, instance_id=NODE_A, tags=_stable_tags()),
        _eip(ALLOC_FOREIGN, IP_FOREIGN, tags={}),
        _eip(ALLOC_FREE, IP_FREE, tags={"managed-by": "django-mojo"}),
    ])

    def attempt(assign):
        with mock.patch.object(capacity, "_write_policy"), \
                mock.patch.object(capacity, "_dispatch"):
            return capacity.apply(
                _actor(), capacity.ACTION_ENABLE_STABLE_IPS, assign=assign,
                elbv2_client=_elbv2_client(), ec2_client=ec2)

    # A foreign, untagged reservation is never consumable — even explicitly.
    with th.assert_raises(capacity.CapacityError) as caught:
        attempt({NODE_B: ALLOC_FOREIGN})
    assert caught.exception.error_code == "address_not_eligible", \
        f"a foreign reservation was consumable: {caught.exception.error_code}"

    # A node that already holds an address takes no assignment.
    with th.assert_raises(capacity.CapacityError) as caught:
        attempt({NODE_A: ALLOC_FREE})
    assert caught.exception.error_code == "invalid_request", \
        f"an already-addressed node accepted an assignment: " \
        f"{caught.exception.error_code}"

    # A stranger to the fleet takes no assignment.
    with th.assert_raises(capacity.CapacityError) as caught:
        attempt({NEW_NODE: ALLOC_FREE})
    assert caught.exception.error_code == "invalid_request", \
        f"a non-fleet instance accepted an assignment: " \
        f"{caught.exception.error_code}"

    # A django-mojo-tagged reservation IS eligible.
    record = attempt({NODE_B: ALLOC_FREE})
    assert record["detail"]["assign"] == {NODE_B: ALLOC_FREE}, \
        f"a valid assignment was not carried: {record['detail']!r}"
    _clear_stable_claims()


@th.django_unit_test("a disable with nothing to detach is a named refusal")
def test_apply_disable_no_change(opts):
    from mojo.apps.aws.services import capacity

    _clear_stable_claims()
    with mock.patch.object(capacity, "_egress_enabled", return_value=False), \
            mock.patch.object(capacity, "_write_policy") as policy, \
            mock.patch.object(capacity, "_dispatch") as dispatched:
        with th.assert_raises(capacity.CapacityError) as caught:
            capacity.apply(_actor(), capacity.ACTION_DISABLE_STABLE_IPS,
                           elbv2_client=_elbv2_client(),
                           ec2_client=_ec2_client())
    assert caught.exception.error_code == "no_change" \
        and caught.exception.status == 409, \
        f"an off, detached fleet accepted a disable: {caught.exception.error_code}"
    assert policy.call_count == 0 and dispatched.call_count == 0, \
        "a no-change disable still wrote policy or dispatched"


# ── the enable runner ───────────────────────────────────────────────────────

def _stable_record(capacity, action, detail=None):
    return {
        "schema_version": 1, "id": "op-egress", "action": action,
        "resource": "fleet", "actor": None, "started": "", "state": "running",
        "phase": capacity.PHASES[action][0],
        "phases": list(capacity.PHASES[action]),
        "message": "requested", "error_code": None, "warnings": [],
        "detail": dict(detail or {}), "claim": "test-claim",
    }


def _facts_map():
    return {NODE_A: {"instance_id": NODE_A, "state": "running",
                     "name": "mojo-api-a", "public_ip": IP_A},
            NODE_B: {"instance_id": NODE_B, "state": "running",
                     "name": "mojo-api-b", "public_ip": "52.0.0.9"}}


def _serving_two():
    return {"balancers": [], "groups": [{
        "arn": GROUP_ARN, "name": "mojo-api", "target_type": "instance",
        "port": 443, "protocol": "TCP",
        "targets": [{"id": NODE_A, "state": "healthy", "port": 443},
                    {"id": NODE_B, "state": "healthy", "port": 443}]}]}


def _row(allocation_id, public_ip, instance_id=None, tags=None):
    return {"allocation_id": allocation_id, "public_ip": public_ip,
            "instance_id": instance_id,
            "association_id": "eipassoc-x" if instance_id else None,
            "network_interface_id": "eni-x" if instance_id else None,
            "tags": dict(tags or {}), "name": (tags or {}).get("Name")}


@th.django_unit_test("enable adopts by tag, reuses the reservation, and proves the result")
def test_enable_adopts_reuses_and_verifies(opts):
    from mojo.apps.aws.services import capacity
    from mojo.helpers.aws import ec2 as ec2_helper

    record = _stable_record(capacity, capacity.ACTION_ENABLE_STABLE_IPS)
    # NODE_A holds a provision-era EIP (django-mojo tags, no stable tag) —
    # adopted by TAG. NODE_B is pending; one stable reservation exists — reused
    # before anything is allocated.
    provision_tags = {"managed-by": "django-mojo", "mojo:project": "mojo",
                      "Name": "mojo-api-a"}
    before = [_row(ALLOC_A, IP_A, instance_id=NODE_A, tags=provision_tags),
              _row(ALLOC_FREE, IP_FREE, tags=_stable_tags("spare"))]
    after = [_row(ALLOC_A, IP_A, instance_id=NODE_A, tags=provision_tags),
             _row(ALLOC_FREE, IP_FREE, instance_id=NODE_B,
                  tags=_stable_tags("mojo-api-b"))]
    reads = [before, after]
    with mock.patch.object(capacity, "_serving", return_value=_serving_two()), \
            mock.patch.object(capacity, "_write_operation", side_effect=lambda r: r), \
            mock.patch.object(capacity, "invalidate"), \
            mock.patch.object(capacity, "_release") as released, \
            mock.patch.object(ec2_helper, "instance_map",
                              return_value=_facts_map()), \
            mock.patch.object(ec2_helper, "address_map",
                              side_effect=lambda **kw: reads.pop(0)), \
            mock.patch.object(ec2_helper, "tag_resources") as tagged, \
            mock.patch.object(ec2_helper, "allocate_address") as allocated, \
            mock.patch.object(ec2_helper, "associate_address") as associated:
        capacity._run_enable_stable_ips(record)

    assert record["state"] == "done", f"the enable did not finish: {record!r}"
    assert allocated.call_count == 0, \
        "a reservation existed and something was still allocated"
    assert associated.call_args[0] == (ALLOC_FREE, NODE_B), \
        f"the reservation was not associated to the pending node: " \
        f"{associated.call_args!r}"
    tagged_ids = [call[0][0][0] for call in tagged.call_args_list]
    assert ALLOC_A in tagged_ids, \
        "the provision-era attached address was not adopted by tag"
    assert ALLOC_FREE in tagged_ids, \
        "the reused reservation was not renamed to its node"
    assert record["detail"]["addresses"] == [IP_A, IP_FREE], \
        f"the finish did not carry the allowlist: {record['detail']!r}"
    assert released.call_count == 1, "the claim was not released on finish"


@th.django_unit_test("an untagged attached address satisfies its node and is never touched")
def test_enable_leaves_foreign_attached_alone(opts):
    from mojo.apps.aws.services import capacity
    from mojo.helpers.aws import ec2 as ec2_helper

    record = _stable_record(capacity, capacity.ACTION_ENABLE_STABLE_IPS)
    foreign = {"Name": "somebody-elses"}
    rows = [_row(ALLOC_A, IP_A, instance_id=NODE_A, tags=foreign),
            _row(ALLOC_B, IP_B, instance_id=NODE_B,
                 tags=_stable_tags("mojo-api-b"))]
    with mock.patch.object(capacity, "_serving", return_value=_serving_two()), \
            mock.patch.object(capacity, "_write_operation", side_effect=lambda r: r), \
            mock.patch.object(capacity, "invalidate"), \
            mock.patch.object(capacity, "_release"), \
            mock.patch.object(ec2_helper, "instance_map",
                              return_value=_facts_map()), \
            mock.patch.object(ec2_helper, "address_map",
                              side_effect=lambda **kw: list(rows)), \
            mock.patch.object(ec2_helper, "tag_resources") as tagged, \
            mock.patch.object(ec2_helper, "allocate_address") as allocated, \
            mock.patch.object(ec2_helper, "associate_address") as associated:
        capacity._run_enable_stable_ips(record)

    assert record["state"] == "done", f"the enable did not finish: {record!r}"
    assert tagged.call_count == 0 and allocated.call_count == 0 \
        and associated.call_count == 0, \
        ("a foreign attached address was tagged, or something was mutated on "
         "an already-satisfied fleet")
    assert record["detail"]["addresses"] == sorted([IP_A, IP_B]), \
        f"the allowlist must still REPORT the foreign address: {record['detail']!r}"


@th.django_unit_test("quota exhaustion fails NAMED and releases the claim for the retry")
def test_enable_quota_failure_releases_claim(opts):
    from mojo.apps.aws.services import capacity
    from mojo.helpers.aws import ec2 as ec2_helper

    record = _stable_record(capacity, capacity.ACTION_ENABLE_STABLE_IPS)
    rows = [_row(ALLOC_A, IP_A, instance_id=NODE_A, tags=_stable_tags())]
    with mock.patch.object(capacity, "_serving", return_value=_serving_two()), \
            mock.patch.object(capacity, "_write_operation", side_effect=lambda r: r), \
            mock.patch.object(capacity, "invalidate"), \
            mock.patch.object(capacity, "_release") as released, \
            mock.patch.object(ec2_helper, "instance_map",
                              return_value=_facts_map()), \
            mock.patch.object(ec2_helper, "address_map",
                              side_effect=lambda **kw: list(rows)), \
            mock.patch.object(ec2_helper, "tag_resources"), \
            mock.patch.object(ec2_helper, "allocate_address",
                              side_effect=_provider_error("AddressLimitExceeded")), \
            mock.patch.object(ec2_helper, "associate_address") as associated:
        capacity._run_enable_stable_ips(record)

    assert record["state"] == "failed" \
        and record["error_code"] == "address_quota", \
        f"quota exhaustion was not a named failure: {record!r}"
    assert "quota" in record["message"].lower(), \
        f"the failure does not explain the quota: {record['message']!r}"
    assert associated.call_count == 0, \
        "an association was attempted after the allocate was refused"
    assert released.call_count == 1, \
        ("the claim was HELD after a quota failure — that 409s the exact "
         "re-run the panel offers as the retry")


@th.django_unit_test("a generic provider failure also releases the claim — the op is idempotent")
def test_enable_provider_failure_releases_claim(opts):
    from mojo.apps.aws.services import capacity
    from mojo.helpers.aws import ec2 as ec2_helper

    record = _stable_record(capacity, capacity.ACTION_ENABLE_STABLE_IPS)
    denied = _provider_error("UnauthorizedOperation",
                             "ec2.associate_address", "ec2:AssociateAddress")
    denied.denied = True
    rows = [_row(ALLOC_FREE, IP_FREE, tags=_stable_tags("spare"))]
    with mock.patch.object(capacity, "_serving", return_value=_serving_two()), \
            mock.patch.object(capacity, "_write_operation", side_effect=lambda r: r), \
            mock.patch.object(capacity, "invalidate"), \
            mock.patch.object(capacity, "_release") as released, \
            mock.patch.object(ec2_helper, "instance_map",
                              return_value=_facts_map()), \
            mock.patch.object(ec2_helper, "address_map",
                              side_effect=lambda **kw: list(rows)), \
            mock.patch.object(ec2_helper, "tag_resources"), \
            mock.patch.object(ec2_helper, "associate_address",
                              side_effect=denied):
        capacity._run_enable_stable_ips(record)

    assert record["state"] == "failed" \
        and record["error_code"] == "provider_denied", \
        f"a denied associate was not a named failure: {record!r}"
    assert released.call_count == 1, \
        "the claim was held after a denied associate — locking out the retry"


@th.django_unit_test("the verify leg gates the finish on a fresh AWS read")
def test_enable_verify_gates_finish(opts):
    from mojo.apps.aws.services import capacity
    from mojo.helpers.aws import ec2 as ec2_helper

    record = _stable_record(capacity, capacity.ACTION_ENABLE_STABLE_IPS)
    before = [_row(ALLOC_FREE, IP_FREE, tags=_stable_tags("spare"))]
    # The verify re-read still shows NODE_B without an address.
    after = [_row(ALLOC_FREE, IP_FREE, instance_id=NODE_A,
                  tags=_stable_tags("mojo-api-a"))]
    reads = [before, after]
    with mock.patch.object(capacity, "_serving", return_value=_serving_two()), \
            mock.patch.object(capacity, "_write_operation", side_effect=lambda r: r), \
            mock.patch.object(capacity, "invalidate"), \
            mock.patch.object(capacity, "_release"), \
            mock.patch.object(ec2_helper, "instance_map",
                              return_value=_facts_map()), \
            mock.patch.object(ec2_helper, "address_map",
                              side_effect=lambda **kw: reads.pop(0)), \
            mock.patch.object(ec2_helper, "tag_resources"), \
            mock.patch.object(ec2_helper, "allocate_address",
                              return_value={"allocation_id": ALLOC_B,
                                            "public_ip": IP_B}), \
            mock.patch.object(ec2_helper, "associate_address"):
        capacity._run_enable_stable_ips(record)

    assert record["state"] == "failed" \
        and record["error_code"] == "address_unverified", \
        f"an unproven association still finished: {record!r}"
    assert NODE_B in record["message"], \
        f"the failure does not name the unaddressed node: {record['message']!r}"


# ── the disable runner ──────────────────────────────────────────────────────

@th.django_unit_test("disable detaches ONLY managed addresses and keeps every allocation")
def test_disable_detaches_only_managed(opts):
    from mojo.apps.aws.services import capacity
    from mojo.helpers.aws import ec2 as ec2_helper

    record = _stable_record(capacity, capacity.ACTION_DISABLE_STABLE_IPS)
    managed = _row(ALLOC_A, IP_A, instance_id=NODE_A,
                   tags=_stable_tags("mojo-api-a"))
    foreign = _row(ALLOC_B, IP_B, instance_id=NODE_B, tags={"Name": "theirs"})
    before = [managed, foreign]
    after = [_row(ALLOC_A, IP_A, tags=_stable_tags("mojo-api-a")), foreign]
    reads = [before, after]
    with mock.patch.object(capacity, "_serving", return_value=_serving_two()), \
            mock.patch.object(capacity, "_write_operation", side_effect=lambda r: r), \
            mock.patch.object(capacity, "invalidate"), \
            mock.patch.object(capacity, "_release"), \
            mock.patch.object(ec2_helper, "instance_map",
                              return_value=_facts_map()), \
            mock.patch.object(ec2_helper, "address_map",
                              side_effect=lambda **kw: reads.pop(0)), \
            mock.patch.object(ec2_helper, "disassociate_address") as detached:
        capacity._run_disable_stable_ips(record)

    assert record["state"] == "done", f"the disable did not finish: {record!r}"
    assert detached.call_count == 1 \
        and detached.call_args[0][0] == managed["association_id"], \
        (f"disable must detach exactly the managed association, never the "
         f"foreign one: {detached.call_args_list!r}")
    assert ALLOC_A in record["message"] or IP_A in record["message"], \
        f"the kept reservation is not named: {record['message']!r}"
    assert IP_B in record["message"], \
        f"the foreign attached address was not called out: {record['message']!r}"
    assert record["detail"]["reserved"] == [ALLOC_A], \
        f"the kept allocations were not recorded: {record['detail']!r}"


@th.django_unit_test("a detach that does not survive the re-read is not reported off")
def test_disable_verify_gates_finish(opts):
    from mojo.apps.aws.services import capacity
    from mojo.helpers.aws import ec2 as ec2_helper

    record = _stable_record(capacity, capacity.ACTION_DISABLE_STABLE_IPS)
    managed = _row(ALLOC_A, IP_A, instance_id=NODE_A,
                   tags=_stable_tags("mojo-api-a"))
    with mock.patch.object(capacity, "_serving", return_value=_serving_two()), \
            mock.patch.object(capacity, "_write_operation", side_effect=lambda r: r), \
            mock.patch.object(capacity, "invalidate"), \
            mock.patch.object(capacity, "_release"), \
            mock.patch.object(ec2_helper, "instance_map",
                              return_value=_facts_map()), \
            mock.patch.object(ec2_helper, "address_map",
                              side_effect=lambda **kw: [dict(managed)]), \
            mock.patch.object(ec2_helper, "disassociate_address"):
        capacity._run_disable_stable_ips(record)

    assert record["state"] == "failed" \
        and record["error_code"] == "address_unverified", \
        f"a detach nobody re-proved was reported done: {record!r}"


# ── the add_node addressing leg ─────────────────────────────────────────────

def _add_record(capacity):
    return {
        "schema_version": 1, "id": "op-test", "action": capacity.ACTION_ADD_NODE,
        "resource": NODE_A, "actor": None, "started": "", "state": "running",
        "phase": "capturing", "phases": list(capacity.PHASES[capacity.ACTION_ADD_NODE]),
        "message": "requested", "error_code": None, "warnings": [],
        "detail": {"source_instance": NODE_A, "source_name": "mojo-api-a",
                   "target_groups": [{"arn": GROUP_ARN, "name": "mojo-api",
                                      "port": 443}]},
        "claim": "test-claim",
    }


def _add_node_harness(capacity, ec2_helper, elbv2_helper, platform_deploy,
                      system_settings, extra):
    """The proven-add mock stack, with the addressing seams appended."""
    row = SimpleNamespace(pk="18180000-0000-4000-8000-000000000001",
                          sha="a" * 40, framework_version="1.13.0")
    source = {"instance_id": NODE_A, "name": "mojo-api-a", "state": "running",
              "instance_type": "m6i.large", "subnet_id": "subnet-0aaa",
              "security_group_ids": ["sg-0aaa"],
              "iam_instance_profile_arn": PROFILE_ARN}
    running = dict(source, instance_id=NEW_NODE)
    stack = [
        mock.patch.object(capacity, "_sleep"),
        mock.patch.object(capacity, "_write_operation", side_effect=lambda r: r),
        mock.patch.object(capacity, "invalidate"),
        mock.patch.object(capacity, "_release"),
        mock.patch.object(capacity, "_await_runner", return_value=True),
        mock.patch.object(capacity, "_await_proof", return_value=True),
        mock.patch.object(capacity, "_await_healthy", return_value=True),
        mock.patch.object(capacity, "_converge_pools"),
        mock.patch.object(ec2_helper, "instance_facts",
                          side_effect=lambda value, **kw:
                          source if value == NODE_A else running),
        mock.patch.object(ec2_helper, "find_reusable_image",
                          return_value={"image_id": "ami-0new", "age_days": 2}),
        mock.patch.object(ec2_helper, "launch_clone", return_value=NEW_NODE),
        mock.patch.object(platform_deploy, "last_converged_deployment",
                          return_value=row),
        mock.patch.object(system_settings, "get_value", return_value=None),
        mock.patch("mojo.apps.edge.asyncjobs._publish_deploy_node"),
    ] + list(extra)
    return stack


@th.django_unit_test("with the policy on, the clone is addressed BEFORE it is registered")
def test_add_node_addresses_before_registering(opts):
    from mojo.apps.account.services import system_settings
    from mojo.apps.aws.services import capacity
    from mojo.helpers.aws import ec2 as ec2_helper
    from mojo.helpers.aws import elbv2 as elbv2_helper
    from mojo.apps.edge.services import platform_deploy

    record = _add_record(capacity)
    order = []
    stack = _add_node_harness(
        capacity, ec2_helper, elbv2_helper, platform_deploy, system_settings, [
            mock.patch.object(capacity, "_egress_enabled", return_value=True),
            mock.patch.object(ec2_helper, "address_map", return_value=[]),
            mock.patch.object(
                capacity, "_attach_stable_ip",
                side_effect=lambda *a, **k: (order.append("address"),
                                             (ALLOC_A, IP_A))[1]),
            mock.patch.object(
                elbv2_helper, "register_target",
                side_effect=lambda *a, **k: order.append("register")),
        ])
    from contextlib import ExitStack
    with ExitStack() as stacked:
        for patch in stack:
            stacked.enter_context(patch)
        capacity._run_add_node(record)

    assert record["state"] == "done", f"the add did not finish: {record!r}"
    assert order == ["address", "register"], \
        (f"the stable address must attach BEFORE registration admits the node "
         f"to serving: {order!r}")
    assert record["detail"].get("stable_ip") == IP_A, \
        f"the attached address was not recorded: {record['detail']!r}"


@th.django_unit_test("an unaddressable clone is NEVER registered — and policy off skips the leg")
def test_add_node_addressing_fails_closed(opts):
    from mojo.apps.account.services import system_settings
    from mojo.apps.aws.services import capacity
    from mojo.helpers.aws import ec2 as ec2_helper
    from mojo.helpers.aws import elbv2 as elbv2_helper
    from mojo.apps.edge.services import platform_deploy
    from contextlib import ExitStack

    # Policy on, quota gone: failed as address_quota, never registered.
    record = _add_record(capacity)
    stack = _add_node_harness(
        capacity, ec2_helper, elbv2_helper, platform_deploy, system_settings, [
            mock.patch.object(capacity, "_egress_enabled", return_value=True),
            mock.patch.object(ec2_helper, "address_map", return_value=[]),
            mock.patch.object(capacity, "_attach_stable_ip",
                              side_effect=_provider_error("AddressLimitExceeded")),
            mock.patch.object(elbv2_helper, "register_target"),
        ])
    with ExitStack() as stacked:
        registered = None
        for patch in stack:
            entered = stacked.enter_context(patch)
            if getattr(patch, "attribute", "") == "register_target":
                registered = entered
        capacity._run_add_node(record)
    assert record["state"] == "failed" \
        and record["error_code"] == "address_quota", \
        f"an unaddressable clone did not fail named: {record!r}"
    assert registered.call_count == 0, \
        "a node whose egress nobody allowlisted was registered anyway"

    # Policy UNREADABLE: the leg must fail the add, never silently skip.
    record = _add_record(capacity)
    stack = _add_node_harness(
        capacity, ec2_helper, elbv2_helper, platform_deploy, system_settings, [
            mock.patch.object(capacity, "_egress_enabled",
                              side_effect=RuntimeError("db down")),
            mock.patch.object(elbv2_helper, "register_target"),
        ])
    with ExitStack() as stacked:
        registered = None
        for patch in stack:
            entered = stacked.enter_context(patch)
            if getattr(patch, "attribute", "") == "register_target":
                registered = entered
        capacity._run_add_node(record)
    assert record["state"] == "failed" \
        and record["error_code"] == "policy_unreadable", \
        f"an unreadable policy did not fail the add: {record!r}"
    assert registered.call_count == 0, \
        "an unreadable policy still registered the node — a silent fail-open"

    # Policy OFF: no address call at all, and the add completes as before.
    record = _add_record(capacity)
    stack = _add_node_harness(
        capacity, ec2_helper, elbv2_helper, platform_deploy, system_settings, [
            mock.patch.object(capacity, "_egress_enabled", return_value=False),
            mock.patch.object(capacity, "_attach_stable_ip"),
            mock.patch.object(elbv2_helper, "register_target"),
        ])
    with ExitStack() as stacked:
        attached = None
        for patch in stack:
            entered = stacked.enter_context(patch)
            if getattr(patch, "attribute", "") == "_attach_stable_ip":
                attached = entered
        capacity._run_add_node(record)
    assert record["state"] == "done", f"a policy-off add did not finish: {record!r}"
    assert attached.call_count == 0, \
        "the addressing leg ran with the policy off"


# ── REST ────────────────────────────────────────────────────────────────────

def _view(name):
    import inspect
    from mojo.apps.aws.rest import capacity as views
    return inspect.unwrap(getattr(views, name))


def _request(user, **data):
    from objict import objict
    return SimpleNamespace(user=user, DATA=objict(**data), META={})


def _superuser(pk=1):
    user = mock.Mock(is_superuser=True, pk=pk, username=f"user-{pk}")
    user.has_permission.side_effect = lambda wanted: True
    return user


@th.django_unit_test("the new actions echo their action word and carry assign through")
def test_rest_stable_ips_echo_and_assign(opts):
    from mojo import errors as me
    from mojo.apps.aws.services import capacity

    view = _view("on_capacity_apply")
    actor = _superuser()
    with mock.patch.object(capacity, "apply") as applied:
        # The echo is the action word, exactly.
        with th.assert_raises(me.ValueException):
            view(_request(actor, action="enable_stable_ips",
                          confirm_resource="yes"))
        # assign must be a nonempty str->str object when present.
        for bad in ("x", [], {}, {"i-1": ""}, {"": "eipalloc-1"}, {"i-1": 3}):
            with th.assert_raises(me.ValueException):
                view(_request(actor, action="enable_stable_ips",
                              confirm_resource="enable_stable_ips", assign=bad))
        assert applied.call_count == 0, \
            "a refused request still reached the apply service"

        applied.return_value = {"id": "op-1"}
        with mock.patch("mojo.apps.account.services.admin_platform.audit_after_commit") as audited:
            view(_request(actor, action="enable_stable_ips",
                          confirm_resource="enable_stable_ips",
                          assign={NODE_B: ALLOC_FREE}))
            view(_request(actor, action="disable_stable_ips",
                          confirm_resource="disable_stable_ips"))
        assert applied.call_count == 2, "the panel's echo words were refused"
        assert applied.call_args_list[0][1] == {"assign": {NODE_B: ALLOC_FREE}} \
            or applied.call_args_list[0][0][3:] == ({"assign": {NODE_B: ALLOC_FREE}},), \
            f"assign was not carried through: {applied.call_args_list[0]!r}"
        audit_actions = [call[0][1] for call in audited.call_args_list]
        assert audit_actions == ["aws_capacity_enable_stable_ips",
                                 "aws_capacity_disable_stable_ips"], \
            f"the audit trail actions are wrong: {audit_actions!r}"


# ── security-review fold-ins ────────────────────────────────────────────────

@th.django_unit_test("an unreadable policy renders Unknown, never a canonical 'off'")
def test_report_policy_read_degrades(opts):
    from mojo.apps.aws.services import capacity

    ec2 = _ec2_client(
        instances=[_instance(NODE_A, "mojo-api-a", public_ip=IP_A)],
        addresses=[_eip(ALLOC_A, IP_A, instance_id=NODE_A,
                        tags=_stable_tags())])
    with mock.patch.object(capacity, "_egress_enabled",
                           side_effect=RuntimeError("db down")):
        envelope = _report(ec2=ec2)
    egress = envelope["egress"]
    assert egress["policy_available"] is False and egress["enabled"] is False, \
        f"a failed policy read did not degrade: {egress!r}"
    assert egress["addresses"] == [IP_A], \
        "the AWS-side facts must still render — only the policy is unknown"
    for name in ("enable_stable_ips", "disable_stable_ips"):
        assert envelope["actions"][name] == {
            "offered": False, "blocked_reason": "policy_unavailable"}, \
            f"{name} was not blocked on the unreadable policy: " \
            f"{envelope['actions'][name]!r}"


@th.django_unit_test("a stale explicit assignment is dropped, never tagged")
def test_attach_stale_assignment_never_tags(opts):
    from mojo.apps.aws.services import capacity
    from mojo.helpers.aws import ec2 as ec2_helper

    # The assignment names an allocation that is no longer unassociated at
    # job time. It must not be tagged — it may be somebody else's now — and
    # the node falls through to a fresh allocation.
    view = {"by_instance": {}, "attached": [], "unassociated": [],
            "reserved": [], "pending": [NODE_B]}
    with mock.patch.object(ec2_helper, "tag_resources") as tagged, \
            mock.patch.object(ec2_helper, "allocate_address",
                              return_value={"allocation_id": ALLOC_B,
                                            "public_ip": IP_B}) as allocated, \
            mock.patch.object(ec2_helper, "associate_address") as associated:
        allocation, public_ip = capacity._attach_stable_ip(
            NODE_B, "mojo-api-b", view, {NODE_B: ALLOC_FOREIGN})
    assert tagged.call_count == 0, \
        "a reservation that stopped being free was still tagged as ours"
    assert allocated.call_count == 1 and allocation == ALLOC_B, \
        f"the stale assignment did not fall through to allocation: " \
        f"{allocation!r}"
    assert associated.call_args[0] == (ALLOC_B, NODE_B), \
        f"the fresh allocation was not the one associated: " \
        f"{associated.call_args!r}"


@th.django_unit_test("disable refuses an empty serving read when addresses were attached")
def test_disable_refuses_empty_fleet_with_expected_attachments(opts):
    from mojo.apps.aws.services import capacity
    from mojo.helpers.aws import ec2 as ec2_helper

    record = _stable_record(capacity, capacity.ACTION_DISABLE_STABLE_IPS,
                            detail={"fleet": [NODE_A], "attached": [ALLOC_A]})
    empty = {"balancers": [], "groups": []}
    with mock.patch.object(capacity, "_serving", return_value=empty), \
            mock.patch.object(capacity, "_write_operation", side_effect=lambda r: r), \
            mock.patch.object(capacity, "invalidate"), \
            mock.patch.object(capacity, "_release"), \
            mock.patch.object(ec2_helper, "address_map") as read, \
            mock.patch.object(ec2_helper, "disassociate_address") as detached:
        capacity._run_disable_stable_ips(record)
    assert record["state"] == "failed" \
        and record["error_code"] == "address_unverified", \
        f"an empty serving read laundered the detach: {record!r}"
    assert read.call_count == 0 and detached.call_count == 0, \
        "the guard must fire before any address read or mutation"


@th.django_unit_test("a caller-supplied resource cannot steer a fleet action's echo or audit")
def test_rest_fleet_resource_is_ignored(opts):
    from mojo import errors as me
    from mojo.apps.aws.services import capacity

    view = _view("on_capacity_apply")
    actor = _superuser()
    with mock.patch.object(capacity, "apply") as applied:
        # The old shape — echo whatever `resource` says — must be dead: the
        # typed echo is the action word, full stop.
        with th.assert_raises(me.ValueException):
            view(_request(actor, action="disable_stable_ips", resource="x",
                          confirm_resource="x"))
        assert applied.call_count == 0, \
            "a steered echo still reached the apply service"

        applied.return_value = {"id": "op-9"}
        with mock.patch("mojo.apps.account.services.admin_platform.audit_after_commit") as audited:
            view(_request(actor, action="disable_stable_ips", resource="x",
                          confirm_resource="disable_stable_ips"))
        assert applied.call_args[0][2] == "", \
            f"the ignored resource leaked into the service call: " \
            f"{applied.call_args!r}"
        assert audited.call_args[0][2] == "fleet:op-9", \
            f"the audit subject was steerable: {audited.call_args!r}"


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
