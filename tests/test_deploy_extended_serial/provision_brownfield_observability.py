from objict import objict
from testit import helpers as th

from test_deploy.brownfield_fixture import topology


class _Clients:
    def __init__(self, **clients):
        self.clients = clients

    def get(self, name):
        return self.clients[name]


class _NoCalls:
    def __getattr__(self, name):
        raise AssertionError(f"idempotent/collision telemetry must not call {name}")


@th.django_unit_test()
def test_failed_log_group_create_does_not_set_collision_retention(opts):
    from botocore.exceptions import ClientError
    from mojo.deploy.provision import brownfield_observability

    class _Logs:
        def __init__(self):
            self.calls = []

        def create_log_group(self, **kwargs):
            self.calls.append("create")
            raise ClientError({"Error": {"Code": "ResourceAlreadyExistsException",
                                         "Message": "collision"}},
                              "CreateLogGroup")

        def put_retention_policy(self, **kwargs):
            self.calls.append("retention")

    logs = _Logs()
    observed = {"log_groups": {}, "brownfield_alarms": [],
                "target_groups": {}}
    brownfield_observability.ensure_observability(
        _Clients(logs=logs, cloudwatch=_NoCalls()), topology(), observed,
        apply=True)
    th.assert_eq("retention" in logs.calls, False,
                 f"retention must not touch a failed-create collision: {logs.calls}")


@th.django_unit_test()
def test_owned_telemetry_is_idempotent(opts):
    from mojo.deploy.provision import brownfield_observability

    spec = topology()
    root = f"/mojo/{spec.project}-{spec.fleet}"
    groups = {f"{root}/{kind}": {"retentionInDays": 90}
              for kind in brownfield_observability.LOG_KINDS}
    target_groups = {
        "api": {"TargetGroupArn": "arn:aws:elasticloadbalancing:us-west-2:"
                "123456789012:targetgroup/api/aaa"},
        "certbot": {"TargetGroupArn": "arn:aws:elasticloadbalancing:us-west-2:"
                    "123456789012:targetgroup/http/bbb"},
    }
    observed = {"log_groups": groups, "target_groups": target_groups,
                "balancer": {"LoadBalancerArn":
                    "arn:aws:elasticloadbalancing:us-west-2:123456789012:"
                    "loadbalancer/net/maestro-shadow-nlb/ccc"}}
    alarms = brownfield_observability._alarm_specs(spec, observed)
    for alarm in alarms:
        alarm.pop("_ready")
    observed["brownfield_alarms"] = alarms
    findings, actions, _result = brownfield_observability.ensure_observability(
        _Clients(logs=_NoCalls(), cloudwatch=_NoCalls()), spec, observed,
        apply=True)
    th.assert_eq(actions, [],
                 f"a converged owned telemetry set must be a no-op: {actions}")


@th.django_unit_test()
def test_alarm_wrong_actions_or_load_balancer_dimension_drift(opts):
    from mojo.deploy.provision import brownfield_observability

    spec = topology()
    observed = {
        "log_groups": {f"/mojo/{spec.project}-{spec.fleet}/{kind}": {
            "retentionInDays": 90}
            for kind in brownfield_observability.LOG_KINDS},
        "target_groups": {
            "api": {"TargetGroupArn": "arn:aws:elasticloadbalancing:us-west-2:"
                    "123456789012:targetgroup/api/aaa"},
            "certbot": {"TargetGroupArn": "arn:aws:elasticloadbalancing:us-west-2:"
                        "123456789012:targetgroup/http/bbb"}},
        "balancer": {"LoadBalancerArn": "arn:aws:elasticloadbalancing:us-west-2:"
                     "123456789012:loadbalancer/net/right/ccc"},
    }
    alarms = brownfield_observability._alarm_specs(spec, observed)
    for alarm in alarms:
        alarm.pop("_ready")
    alarms[0]["AlarmActions"] = ["arn:aws:sns:us-west-2:123456789012:wrong"]
    alarms[1]["Dimensions"][1]["Value"] = "net/wrong/ddd"
    observed["brownfield_alarms"] = alarms
    _findings, actions, _result = (
        brownfield_observability.ensure_observability(
            _Clients(logs=_NoCalls(), cloudwatch=_NoCalls()), spec, observed,
            apply=False))
    th.assert_eq({row.target for row in actions}, {
        "maestro-shadow-api-unhealthy",
        "maestro-shadow-certbot-unhealthy"},
        f"wrong alarm actions/dimensions must both converge: {actions}")


@th.django_unit_test()
def test_log_group_create_action_covers_retention_subordinate(opts):
    from mojo.deploy.provision import brownfield_observability

    class _Logs:
        def __init__(self):
            self.calls = []

        def create_log_group(self, **kwargs):
            self.calls.append(("create", kwargs["logGroupName"]))
            return {}

        def put_retention_policy(self, **kwargs):
            self.calls.append(("retention", kwargs["logGroupName"]))
            return {}

    logs = _Logs()
    spec = topology()
    observed = {"log_groups": {}, "brownfield_alarms": [],
                "target_groups": {}, "balancer": {}}
    _findings, actions, _result = (
        brownfield_observability.ensure_observability(
            _Clients(logs=logs, cloudwatch=_NoCalls()), spec, observed,
            apply=True))
    create_targets = {row.target for row in actions if row.step == "telemetry"
                      and row.verb == "create" and row.target.startswith("/")}
    retained = {target for verb, target in logs.calls if verb == "retention"}
    th.assert_eq(retained, create_targets,
                 f"each logical log create must cover its retention call: {logs.calls}")
