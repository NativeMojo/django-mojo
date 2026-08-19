"""
AWS Elastic Load Balancing (v2) Helper Module

TWO CLIENT PATHS, deliberately separate:

* ``LoadBalancerHelper`` — the read seam behind the Admin dashboard. Which
  balancer fronts the fleet, which target groups are attached to it, and how
  many registered targets are healthy. It mutates nothing and wraps every call
  in ``ProviderClient``.
* the module-level functions at the bottom — the serving-tier MUTATIONS
  (register/deregister) plus the two reads the capacity guards need. They use
  ``ProviderCaller`` directly with the IAM action and the mutation flag named
  EXPLICITLY, because ``ProviderClient`` derives mutation from the method-name
  prefix and neither ``register_`` nor ``deregister_`` is one of its prefixes.
  Left to inference, a failed DeregisterTargets would claim
  ``mutation_state="none"`` — a wrong answer on the one question that matters
  after a failed drain.

Raw botocore text (which can carry credentials, signed URLs, and request
parameters) never leaves this module: a failure raises ``ProviderCallError``,
whose ``detail()`` is the only shape safe for an API response or a log line.
"""

from .client import get_client, get_session
from .provider_call import ProviderCallError, ProviderClient, ProviderCaller
from mojo.helpers.settings import settings
from mojo.helpers import logit

logger = logit.get_logger("aws_elbv2", "aws.log")


DEFAULT_MAX_GROUPS = 4
MAX_TARGETS = 100
# A dashboard read may time out in 2s; a registration may not.
MUTATION_TIMEOUT = 5
# botocore names the client "elbv2" but IAM names the service
# "elasticloadbalancing"; ProviderClient would otherwise derive "elb".
IAM_SERVICE = "elasticloadbalancing"

# The two target-health states that mean "still carrying traffic". AWS reports
# `draining` (and, for an already-failing target, `unhealthy.draining`) for the
# whole deregistration delay: neither is drain-complete, and treating either as
# done is how a node gets terminated with live connections on it.
IN_FLIGHT_STATES = frozenset({"draining", "unhealthy.draining"})
# The state a fully drained but still-listed target settles on.
DRAINED_STATE = "unused"
DEREGISTRATION_DELAY_KEY = "deregistration_delay.timeout_seconds"

_caller = ProviderCaller(logger)


def _balancer_facts(balancer):
    """Project one describe_load_balancers row down to what an operator reads."""
    zones = balancer.get("AvailabilityZones") or []
    addresses = []
    for zone in zones:
        for address in zone.get("LoadBalancerAddresses") or []:
            addresses.append({
                "zone": zone.get("ZoneName"),
                "ip": address.get("IpAddress"),
                "allocation_id": address.get("AllocationId"),
            })
    return {
        "name": balancer.get("LoadBalancerName"),
        "arn": balancer.get("LoadBalancerArn"),
        "type": str(balancer.get("Type") or "").lower(),
        "scheme": balancer.get("Scheme"),
        "state": str(((balancer.get("State") or {}).get("Code")) or "").lower(),
        "dns_name": balancer.get("DNSName"),
        "zones": [zone.get("ZoneName") for zone in zones],
        "addresses": addresses,
        # An allocation id is the only proof the address is an elastic IP —
        # a plain NLB address moves when the balancer is replaced.
        "elastic_ips": [row["ip"] or row["allocation_id"] for row in addresses
                        if row.get("allocation_id")],
    }


class LoadBalancerHelper:
    """Bounded ELBv2 reader for Admin evidence.

    Credentials are read from settings by default (AWS_KEY, AWS_SECRET,
    AWS_REGION). ``client_factory`` is the injection seam used by tests and by
    callers that already hold a session.
    """

    def __init__(self, access_key=None, secret_key=None, region=None,
                 session=None, client_factory=None, timeout=2):
        self.access_key = access_key or settings.AWS_KEY
        self.secret_key = secret_key or settings.AWS_SECRET
        self.region = region or getattr(settings, "AWS_REGION", "us-east-1")
        self.session = session
        self.client_factory = client_factory or get_client
        self.timeout = timeout
        self._elbv2 = None
        self._ec2 = None

    def _aws_client(self, service, action_service=None):
        if self.session is None:
            self.session = get_session(
                self.access_key, self.secret_key, self.region,
            )
        client = self.client_factory(
            service,
            session=self.session,
            region=self.region,
            timeout=self.timeout,
            # A dashboard read has a response budget of seconds; a retried
            # denial or timeout burns it three times over for the same answer.
            max_attempts=1,
        )
        return ProviderClient(
            client, service, action_service=action_service or service)

    # ------------------------------------------------------------------
    # Lazy client accessors
    # ------------------------------------------------------------------

    @property
    def elbv2(self):
        if self._elbv2 is None:
            self._elbv2 = self._aws_client("elbv2", action_service=IAM_SERVICE)
        return self._elbv2

    @property
    def ec2(self):
        if self._ec2 is None:
            self._ec2 = self._aws_client("ec2")
        return self._ec2

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def _call(self, result, callback):
        """Run one bounded call, recording a failure instead of aborting.

        A denied DescribeTargetHealth must still leave the caller with the
        balancer facts it already read, so every failure is collected rather
        than raised — the IAM action an operator has to grant included.
        """
        try:
            return callback()
        except ProviderCallError as err:
            detail = err.detail()
            result["failures"].append(detail)
            if err.denied:
                action = err.iam_action or err.operation
                if action not in result["denied"]:
                    result["denied"].append(action)
            return None

    def _group_health(self, row, result):
        group = {
            "name": row.get("TargetGroupName"),
            "arn": row.get("TargetGroupArn"),
            "target_type": row.get("TargetType"),
            "protocol": row.get("Protocol"),
            "port": row.get("Port"),
            "registered": 0,
            "healthy": 0,
            "targets": [],
            "observed": False,
        }
        health = self._call(result, lambda: self.elbv2.describe_target_health(
            TargetGroupArn=row.get("TargetGroupArn")))
        if health is None:
            return group
        group["observed"] = True
        for item in (health.get("TargetHealthDescriptions") or [])[:MAX_TARGETS]:
            state = str(((item.get("TargetHealth") or {}).get("State")) or "").lower()
            group["targets"].append({
                "id": (item.get("Target") or {}).get("Id"), "state": state})
            group["registered"] += 1
            if state == "healthy":
                group["healthy"] += 1
        return group

    def frontend(self, max_groups=DEFAULT_MAX_GROUPS):
        """One bounded read of the public serving tier.

        ``describe_target_groups`` is called ONCE for the whole account and the
        attachment is read off each group's ``LoadBalancerArns`` — the same
        shape ``aws_check`` and ``check_setup`` use. A per-balancer call would
        be an N+1 against a rate-limited API for an identical answer.
        """
        max_groups = max(1, min(int(max_groups or DEFAULT_MAX_GROUPS), 20))
        result = {
            "configured": False, "balancer": None, "groups": [],
            "registered": 0, "healthy": 0, "instance_ids": [],
            "healthy_instance_ids": [], "denied": [], "failures": [],
            "truncated": False,
        }
        page = self._call(result, lambda: self.elbv2.describe_load_balancers())
        balancers = (page or {}).get("LoadBalancers") or []
        if not balancers:
            return result
        balancer = balancers[0]
        result["configured"] = True
        result["balancer"] = _balancer_facts(balancer)
        arn = balancer.get("LoadBalancerArn")

        groups_page = self._call(
            result, lambda: self.elbv2.describe_target_groups())
        attached = [row for row in (groups_page or {}).get("TargetGroups") or []
                    if arn in (row.get("LoadBalancerArns") or [])]
        result["truncated"] = len(attached) > max_groups
        for row in attached[:max_groups]:
            group = self._group_health(row, result)
            result["groups"].append(group)
            result["registered"] += group["registered"]
            result["healthy"] += group["healthy"]
            if group["target_type"] not in (None, "instance"):
                continue
            for target in group["targets"]:
                identifier = str(target.get("id") or "")
                if not identifier.startswith("i-"):
                    continue
                if identifier not in result["instance_ids"]:
                    result["instance_ids"].append(identifier)
                if target["state"] == "healthy" \
                        and identifier not in result["healthy_instance_ids"]:
                    result["healthy_instance_ids"].append(identifier)
        return result

    def instance_names(self, instance_ids):
        """Name tags for the given instances, in one describe_instances call."""
        ids = [str(value) for value in (instance_ids or [])
               if str(value).startswith("i-")][:MAX_TARGETS]
        if not ids:
            return {}
        page = self.ec2.describe_instances(InstanceIds=ids)
        names = {}
        for reservation in page.get("Reservations") or []:
            for row in reservation.get("Instances") or []:
                identifier = row.get("InstanceId")
                tags = {tag.get("Key"): tag.get("Value")
                        for tag in row.get("Tags") or []}
                names[identifier] = tags.get("Name") or identifier
        return names


# ── module-level: serving-tier mutations and the guard reads ────────────────
#
# Used by the Admin capacity actions. See the module docstring for why these
# are not methods on LoadBalancerHelper and why `mutation=` is explicit.

def _setting(name, default=None):
    try:
        return settings.get_static(name, default)
    except Exception:
        return default


def _elbv2(client=None, region=None, timeout=MUTATION_TIMEOUT):
    """The injection seam. Tests and callers holding a session pass ``client``."""
    if client is not None:
        return client
    region = region or _setting("AWS_REGION", "us-east-1")
    session = get_session(_setting("AWS_KEY"), _setting("AWS_SECRET"), region)
    # One attempt: a retried register/deregister is the one thing this module
    # must not do on the operator's behalf.
    return get_client("elbv2", session=session, region=region,
                      timeout=timeout, max_attempts=1)


def _target(instance_id, port=None):
    target = {"Id": instance_id}
    if port:
        target["Port"] = int(port)
    return target


def register_target(group_arn, instance_id, port=None, client=None, region=None):
    """Put one instance into one target group. The LAST step of an add."""
    elbv2 = _elbv2(client, region)
    return _caller.call(
        "elbv2.register_targets",
        lambda: elbv2.register_targets(
            TargetGroupArn=group_arn, Targets=[_target(instance_id, port)]),
        iam_action=f"{IAM_SERVICE}:RegisterTargets", mutation=True)


def deregister_target(group_arn, instance_id, port=None, client=None, region=None):
    """Start draining one instance out of one target group.

    Returning does NOT mean the target is drained — it means AWS accepted the
    request and started the deregistration delay. ``target_health`` is what
    proves the drain finished.
    """
    elbv2 = _elbv2(client, region)
    return _caller.call(
        "elbv2.deregister_targets",
        lambda: elbv2.deregister_targets(
            TargetGroupArn=group_arn, Targets=[_target(instance_id, port)]),
        iam_action=f"{IAM_SERVICE}:DeregisterTargets", mutation=True)


def target_health(group_arn, instance_id=None, client=None, region=None):
    """``[{id, port, state, reason}]`` for one group, newest read every time.

    Deliberately ONE unfiltered describe narrowed in process rather than a
    ``Targets=`` filtered describe: AWS raises ``InvalidTarget`` for a target
    that is not registered, and "not registered" is exactly the answer a drain
    poll is waiting for. An exception is a terrible way to say "done".
    """
    elbv2 = _elbv2(client, region)
    page = _caller.call(
        "elbv2.describe_target_health",
        lambda: elbv2.describe_target_health(TargetGroupArn=group_arn),
        iam_action=f"{IAM_SERVICE}:DescribeTargetHealth", mutation=False)
    rows = []
    for item in (page.get("TargetHealthDescriptions") or [])[:MAX_TARGETS]:
        target = item.get("Target") or {}
        health = item.get("TargetHealth") or {}
        identifier = target.get("Id")
        if instance_id and identifier != instance_id:
            continue
        rows.append({
            "id": identifier,
            "port": target.get("Port"),
            "state": str(health.get("State") or "").lower(),
            "reason": str(health.get("Reason") or "").lower(),
        })
    return rows


def target_group_attributes(group_arn, client=None, region=None):
    """``{key: value}`` for one group. Read for ``deregistration_delay``."""
    elbv2 = _elbv2(client, region)
    page = _caller.call(
        "elbv2.describe_target_group_attributes",
        lambda: elbv2.describe_target_group_attributes(TargetGroupArn=group_arn),
        iam_action=f"{IAM_SERVICE}:DescribeTargetGroupAttributes", mutation=False)
    return {row.get("Key"): row.get("Value")
            for row in (page.get("Attributes") or []) if row.get("Key")}


def deregistration_delay(group_arn, default=300, client=None, region=None):
    """The group's configured drain window in seconds, or ``default``."""
    try:
        raw = target_group_attributes(group_arn, client=client, region=region).get(
            DEREGISTRATION_DELAY_KEY)
        return max(0, int(raw))
    except (ProviderCallError, TypeError, ValueError):
        return int(default)


def drained(rows):
    """True only when nothing in ``rows`` is still carrying traffic.

    An empty list is drained (the target is gone). A `draining` row is NOT,
    however long it has been there.
    """
    for row in rows or []:
        state = row.get("state")
        if state in IN_FLIGHT_STATES:
            return False
        if state != DRAINED_STATE:
            return False
    return True


def serving_map(client=None, region=None, max_groups=20):
    """EVERY attached target group of EVERY balancer, described fresh.

    ``LoadBalancerHelper.frontend`` reads the FIRST balancer only and caches
    for a minute — right for a dashboard row, wrong for a guard. A node that is
    the last healthy target of an internal group behind a second balancer is
    still the thing keeping that group up, and a 60-second-old answer is not
    evidence about a fleet somebody is actively changing.
    """
    elbv2 = _elbv2(client, region)
    page = _caller.call(
        "elbv2.describe_load_balancers",
        lambda: elbv2.describe_load_balancers(),
        iam_action=f"{IAM_SERVICE}:DescribeLoadBalancers", mutation=False)
    balancers = [_balancer_facts(row) for row in page.get("LoadBalancers") or []]
    arns = {row["arn"] for row in balancers if row.get("arn")}
    # ONE describe_target_groups for the whole account, attachment read off
    # each group's LoadBalancerArns — a per-balancer call would be an N+1
    # against a rate-limited API for an identical answer.
    groups_page = _caller.call(
        "elbv2.describe_target_groups",
        lambda: elbv2.describe_target_groups(),
        iam_action=f"{IAM_SERVICE}:DescribeTargetGroups", mutation=False)
    groups = []
    for row in groups_page.get("TargetGroups") or []:
        attached = [arn for arn in (row.get("LoadBalancerArns") or []) if arn in arns]
        if not attached or len(groups) >= max(1, int(max_groups)):
            continue
        arn = row.get("TargetGroupArn")
        groups.append({
            "arn": arn,
            "name": row.get("TargetGroupName"),
            "target_type": row.get("TargetType"),
            "protocol": row.get("Protocol"),
            "port": row.get("Port"),
            "balancers": attached,
            "targets": target_health(arn, client=elbv2, region=region),
        })
    return {"balancers": balancers, "groups": groups}
