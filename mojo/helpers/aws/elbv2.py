"""
AWS Elastic Load Balancing (v2) Helper Module

Read-only view of the serving tier: which balancer fronts the fleet, which
target groups are attached to it, and how many registered targets are healthy.

Nothing here mutates. Every call is wrapped in ``ProviderClient`` so a denial
or a network failure raises ``ProviderCallError`` and never puts raw botocore
text (which can carry credentials, signed URLs, and request parameters) into an
API response.
"""

from .client import get_client, get_session
from .provider_call import ProviderCallError, ProviderClient
from mojo.helpers.settings import settings
from mojo.helpers import logit

logger = logit.get_logger("aws_elbv2", "aws.log")


DEFAULT_MAX_GROUPS = 4
MAX_TARGETS = 100
# botocore names the client "elbv2" but IAM names the service
# "elasticloadbalancing"; ProviderClient would otherwise derive "elb".
IAM_SERVICE = "elasticloadbalancing"


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
