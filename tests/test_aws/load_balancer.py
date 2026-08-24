"""ELBv2 serving-tier evidence: what it reads, and what it refuses to guess.

The helper is a read seam over mocked boto clients — no live AWS. The status
ladder it feeds is asserted through ``admin_platform._load_balancer`` because
that is where "some targets unhealthy" becomes amber and "no healthy target"
becomes red; the helper itself only reports counts.
"""

TESTIT_TIER = "edge"

from unittest import mock

from botocore.exceptions import ClientError

from testit import helpers as th


BALANCER_ARN = ("arn:aws:elasticloadbalancing:us-east-2:123456789012:"
                "loadbalancer/net/mojo-api-nlb/0123456789abcdef")
GROUP_ARN = ("arn:aws:elasticloadbalancing:us-east-2:123456789012:"
             "targetgroup/mojo-api/abcdef0123456789")
OTHER_GROUP_ARN = ("arn:aws:elasticloadbalancing:us-east-2:123456789012:"
                   "targetgroup/unattached/9999999999999999")

# A real denial message names the assumed-role ARN and the resource. None of it
# may reach an API response.
DENIAL = ClientError(
    {"Error": {"Code": "AccessDeniedException",
               "Message": ("User: arn:aws:sts::123456789012:assumed-role/"
                           "mojo-api/i-secret is not authorized to perform: "
                           "elasticloadbalancing:DescribeTargetHealth")},
     "ResponseMetadata": {"HTTPStatusCode": 403, "RequestId": "abcd1234efgh"}},
    "DescribeTargetHealth")


def _balancer(addresses=None):
    if addresses is None:
        addresses = [{"IpAddress": "52.14.9.201", "AllocationId": "eipalloc-0a1b"}]
    return {
        "LoadBalancerArn": BALANCER_ARN, "LoadBalancerName": "mojo-api-nlb",
        "Type": "network", "Scheme": "internet-facing",
        "DNSName": "mojo-api-nlb.elb.amazonaws.com",
        "State": {"Code": "active"},
        "AvailabilityZones": [
            {"ZoneName": "us-east-2a", "LoadBalancerAddresses": addresses}],
    }


def _targets(states):
    return [{"Target": {"Id": f"i-0{index}", "Port": 443},
             "TargetHealth": {"State": state}}
            for index, state in enumerate(states)]


def _helper(balancers=None, groups=None, health=None, health_error=None,
            instances=None):
    """A LoadBalancerHelper whose provider calls answer from fixtures."""
    from mojo.helpers.aws.elbv2 import LoadBalancerHelper

    elbv2 = mock.Mock()
    elbv2.describe_load_balancers.return_value = {
        "LoadBalancers": [_balancer()] if balancers is None else balancers}
    elbv2.describe_target_groups.return_value = {
        "TargetGroups": [{
            "TargetGroupArn": GROUP_ARN, "TargetGroupName": "mojo-api",
            "TargetType": "instance", "Protocol": "TCP", "Port": 443,
            "LoadBalancerArns": [BALANCER_ARN],
        }] if groups is None else groups}

    def target_health(TargetGroupArn=None):
        if health_error is not None:
            raise health_error
        return {"TargetHealthDescriptions": (health or {}).get(TargetGroupArn, [])}

    elbv2.describe_target_health.side_effect = target_health
    ec2 = mock.Mock()
    ec2.describe_instances.return_value = {"Reservations": instances or []}
    clients = {"elbv2": elbv2, "ec2": ec2}
    helper = LoadBalancerHelper(
        session=object(),
        client_factory=lambda service, **kwargs: clients[service])
    return helper, clients


def _status(frontend):
    """Run the collector's ladder over a frontend() result.

    Injected via _load_balancer's frontend seam: patching
    admin_platform._cached_frontend is process-global under the parallel
    runner (#2558).
    """
    from mojo.apps.account.services import admin_platform
    return admin_platform._load_balancer(frontend=frontend)


@th.django_unit_test("a fully healthy serving tier reports healthy with its elastic IP")
def test_all_targets_healthy(opts):
    helper, clients = _helper(health={GROUP_ARN: _targets(["healthy", "healthy"])})
    frontend = helper.frontend()

    th.assert_eq(frontend["registered"], 2,
                 "both registered targets must be counted")
    th.assert_eq(frontend["healthy"], 2, "both healthy targets must be counted")
    th.assert_eq(frontend["balancer"]["elastic_ips"], ["52.14.9.201"],
                 "the elastic IP must be read off the balancer itself")
    th.assert_eq(clients["ec2"].describe_addresses.called, False,
                 "elastic IPs must not cost a separate describe_addresses call")

    result = _status(frontend)
    th.assert_eq(result["_collector_status"], "healthy",
                 f"a 2/2 serving tier is not healthy: {result!r}")
    th.assert_eq(result["elastic_ip_missing"], False,
                 "an allocated elastic IP was reported as missing")


@th.django_unit_test("one unhealthy target of two degrades, never reddens")
def test_partial_health_is_degraded(opts):
    helper, _ = _helper(health={GROUP_ARN: _targets(["healthy", "unhealthy"])})
    result = _status(helper.frontend())
    th.assert_eq(result["_collector_status"], "degraded",
                 f"1/2 healthy must be amber, not red or green: {result!r}")


@th.django_unit_test("registered targets with none healthy is a proven outage")
def test_no_healthy_target_is_unhealthy(opts):
    helper, _ = _helper(health={GROUP_ARN: _targets(["unhealthy", "draining"])})
    result = _status(helper.frontend())
    th.assert_eq(result["_collector_status"], "unhealthy",
                 f"a serving group with zero healthy targets must redden: {result!r}")


@th.django_unit_test("an empty target group is unconfigured, not an outage")
def test_zero_registered_is_unconfigured(opts):
    helper, _ = _helper(health={GROUP_ARN: []})
    frontend = helper.frontend()
    th.assert_eq(frontend["registered"], 0, "an empty group registered targets")
    result = _status(frontend)
    th.assert_eq(result["_collector_status"], "unconfigured",
                 f"nothing registered is absence of setup, not failure: {result!r}")


@th.django_unit_test("an NLB reporting no elastic IP is flagged without reddening")
def test_missing_elastic_ip_is_flagged(opts):
    helper, _ = _helper(
        balancers=[_balancer(addresses=[{"IpAddress": "52.14.9.201"}])],
        health={GROUP_ARN: _targets(["healthy"])})
    result = _status(helper.frontend())
    th.assert_eq(result["elastic_ip_missing"], True,
                 "an address with no allocation id is not an elastic IP")
    th.assert_eq(result["_collector_status"], "healthy",
                 f"a missing elastic IP is not itself an outage: {result!r}")


@th.django_unit_test("a denied target-health read keeps balancer facts and names the grant")
def test_denied_target_health_is_bounded(opts):
    helper, _ = _helper(health_error=DENIAL)
    frontend = helper.frontend()

    th.assert_eq(frontend["configured"], True,
                 "a denied health read must not erase the balancer that was read")
    th.assert_eq(frontend["balancer"]["name"], "mojo-api-nlb",
                 "balancer facts were lost to a later call's denial")
    th.assert_true(
        "elasticloadbalancing:DescribeTargetHealth" in frontend["denied"],
        f"the missing IAM action was not collected: {frontend['denied']!r}")
    rendered = str(frontend)
    for secret in ("assumed-role", "i-secret", "is not authorized"):
        th.assert_true(secret not in rendered,
                       f"raw provider text {secret!r} reached the helper result")

    result = _status(frontend)
    th.assert_eq(result["_collector_status"], "unknown",
                 f"incomplete evidence must be unknown, never green: {result!r}")
    th.assert_eq(result["_collector_reason_detail"],
                 {"iam_action": "elasticloadbalancing:DescribeTargetHealth"},
                 "the row cannot tell the operator which grant to add")


@th.django_unit_test("target groups are read once for the whole account")
def test_single_target_group_call(opts):
    helper, clients = _helper(
        groups=[
            {"TargetGroupArn": GROUP_ARN, "TargetGroupName": "mojo-api",
             "TargetType": "instance", "LoadBalancerArns": [BALANCER_ARN]},
            {"TargetGroupArn": OTHER_GROUP_ARN, "TargetGroupName": "unattached",
             "TargetType": "instance", "LoadBalancerArns": []},
        ],
        health={GROUP_ARN: _targets(["healthy"])})
    frontend = helper.frontend()

    th.assert_eq(clients["elbv2"].describe_target_groups.call_count, 1,
                 "target groups must be listed once, not once per balancer")
    th.assert_eq([row["name"] for row in frontend["groups"]], ["mojo-api"],
                 "an unattached target group was treated as part of the frontend")
    th.assert_eq(clients["elbv2"].describe_target_health.call_count, 1,
                 "health was read for a group that is attached to nothing")


@th.django_unit_test("instance names come from one describe_instances call")
def test_instance_names_single_call(opts):
    helper, clients = _helper(instances=[{"Instances": [
        {"InstanceId": "i-00", "Tags": [{"Key": "Name", "Value": "mojo-api-a"}]},
        {"InstanceId": "i-01", "Tags": []},
    ]}])
    names = helper.instance_names(["i-00", "i-01", "not-an-instance"])

    th.assert_eq(clients["ec2"].describe_instances.call_count, 1,
                 "instance names must not fan out one call per instance")
    th.assert_eq(names, {"i-00": "mojo-api-a", "i-01": "i-01"},
                 f"an untagged instance must fall back to its id: {names!r}")
    called = clients["ec2"].describe_instances.call_args.kwargs["InstanceIds"]
    th.assert_eq(called, ["i-00", "i-01"],
                 f"a non-instance id was passed to EC2: {called!r}")


# ── module-level mutations ──────────────────────────────────────────────────

@th.django_unit_test("a failed deregister NEVER claims nothing happened")
def test_failed_deregister_reports_mutation_state(opts):
    from mojo.helpers.aws import elbv2
    from mojo.helpers.aws.provider_call import ProviderCallError

    # ProviderClient derives "is this a mutation?" from the method-name prefix,
    # and neither register_ nor deregister_ is one of its prefixes. Left to
    # inference, this failure would report mutation_state="none" — "AWS did not
    # start draining your node" — when AWS may well have. That is the one
    # question that matters before deciding whether to retry.
    client = mock.Mock()
    client.deregister_targets.side_effect = DENIAL
    with th.assert_raises(ProviderCallError) as caught:
        elbv2.deregister_target(GROUP_ARN, "i-00", client=client)
    detail = caught.exception.detail()
    th.assert_true(detail["mutation_state"] != "none",
                   f"a failed deregister claimed nothing happened: {detail!r}")
    th.assert_eq(detail["mutation_state"], "attempted",
                 f"a non-retryable denial is 'attempted': {detail!r}")
    th.assert_eq(detail.get("iam_action"),
                 "elasticloadbalancing:DeregisterTargets",
                 f"the refused grant was not named: {detail!r}")
    rendered = str(detail)
    for secret in ("assumed-role", "i-secret", "is not authorized"):
        th.assert_true(secret not in rendered,
                       f"raw provider text {secret!r} reached the failure detail")

    # The register twin behaves the same way, for the same reason.
    client = mock.Mock()
    client.register_targets.side_effect = DENIAL
    with th.assert_raises(ProviderCallError) as caught:
        elbv2.register_target(GROUP_ARN, "i-00", 443, client=client)
    th.assert_true(caught.exception.detail()["mutation_state"] != "none",
                   "a failed register claimed nothing happened")


@th.django_unit_test("a drain poll reads target health without a Targets filter")
def test_target_health_never_filters_provider_side(opts):
    from mojo.helpers.aws import elbv2

    # AWS raises InvalidTarget for a target that is not registered — which is
    # exactly the answer a drain poll is waiting for. Filtering in process is
    # what lets "it is gone" be a value instead of an exception.
    client = mock.Mock()
    client.describe_target_health.return_value = {"TargetHealthDescriptions": [
        {"Target": {"Id": "i-00", "Port": 443}, "TargetHealth": {"State": "healthy"}},
        {"Target": {"Id": "i-01", "Port": 443}, "TargetHealth": {"State": "draining"}},
    ]}
    rows = elbv2.target_health(GROUP_ARN, "i-01", client=client)
    kwargs = client.describe_target_health.call_args.kwargs
    th.assert_eq(list(kwargs), ["TargetGroupArn"],
                 f"the drain poll filtered provider-side: {kwargs!r}")
    th.assert_eq([row["id"] for row in rows], ["i-01"],
                 f"the in-process narrowing returned the wrong rows: {rows!r}")
    th.assert_true(not elbv2.drained(rows),
                   "a draining target was reported as drained")
    th.assert_true(elbv2.drained(elbv2.target_health(
        GROUP_ARN, "i-99", client=client)),
        "a target absent from the group is drained")
