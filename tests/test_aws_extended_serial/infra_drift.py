"""Moved from the default-tier sibling (maestro item #1839): these tests mutate shared testit/production module state process-wide (seam rebinding, module-attribute save/restore), which races every parallel module.
"""
"""Fleet drift: scanner, prose, cronjob, asyncjob and rule wiring.

AWS is faked at the HELPER-FUNCTION seam — `serving_map` and `instance_map` —
never at the raw boto client, for two reasons:

* `ec2.instance_map` has no test coverage anywhere in this repo, so a
  hand-written `{"Reservations": [...]}` Mock would be asserting a response
  shape that nothing else in the suite validates. Faking the helper's return
  value asserts only the contract this scanner actually consumes.
* botocore.Stubber buys nothing here either: v1 introduces no new request shape,
  every call is one the capacity service already makes.

Every patch replaces a MODULE-LOCAL reference on `infra_drift` (its
`elbv2_helper` / `ec2_helper` / `system_settings` / `infrastructure` names),
never an attribute of the shared helper module itself. testit runs test modules
as threads in ONE process, so patching `mojo.helpers.aws.elbv2.serving_map`
would reach into every other module running at the same time.
"""

from types import SimpleNamespace
from unittest import mock

from testit import helpers as th


CHANNEL = "testit_aws_infra_drift"
RULESET_NAME = "Health - Infrastructure Drift"

CREATED_BY_TAG = "mojo:created-by"
CAPACITY_VALUE = "admin-capacity"


# ── fixtures ────────────────────────────────────────────────────────────────

def _instance(instance_id, hostname="", name=None, dns=None, tags=None, state="running"):
    """One `ec2_helper.instance_map` row, with the keys `_facts` really emits."""
    private_dns = dns if dns is not None else (f"{hostname}.ec2.internal" if hostname else "")
    return {
        "instance_id": instance_id,
        "state": state,
        "private_dns_name": private_dns,
        "private_hostname": hostname,
        "name": name or instance_id,
        "tags": dict(tags or {}),
    }


def _serving(groups):
    """`elbv2_helper.serving_map`'s shape: `{"balancers": [], "groups": [...]}`."""
    return {
        "balancers": [{"arn": "arn:aws:elbv2:::loadbalancer/app/prod/1", "name": "prod"}],
        "groups": [
            {
                "arn": f"arn:aws:elbv2:::targetgroup/{name}/{index}",
                "name": name,
                "target_type": "instance",
                "protocol": "HTTP",
                "port": 80,
                "balancers": ["arn:aws:elbv2:::loadbalancer/app/prod/1"],
                "targets": [{"id": target, "port": 80, "state": "healthy", "reason": ""}
                            for target in targets],
            }
            for index, (name, targets) in enumerate(groups)
        ],
    }


def _fakes(serving=None, instances=None, serving_error=None, instances_error=None):
    """Module-local stand-ins for the two AWS helpers infra_drift imports."""
    def serving_map(client=None, region=None, max_groups=20):
        if serving_error is not None:
            raise serving_error
        return serving

    def instance_map(ids, client=None, region=None):
        if instances_error is not None:
            raise instances_error
        return {key: value for key, value in (instances or {}).items() if key in set(ids)}

    return (SimpleNamespace(serving_map=serving_map),
            SimpleNamespace(instance_map=instance_map, CREATED_BY_TAG=CREATED_BY_TAG))


def _scan(nodes, serving=None, instances=None, serving_error=None,
          instances_error=None, mode="managed"):
    """Run one scan with every AWS and settings seam replaced."""
    from mojo.apps.aws.services import infra_drift

    elbv2_fake, ec2_fake = _fakes(serving, instances, serving_error, instances_error)
    topology = None if nodes is None else {"nodes": list(nodes), "pools": ["api"]}
    settings_fake = SimpleNamespace(
        EXPECTED_EDGE_TOPOLOGY="EDGE_EXPECTED_TOPOLOGY",
        get_value=lambda key, default=None: topology)
    infra_fake = SimpleNamespace(
        MANAGED="managed", EXTERNAL="external",
        infrastructure_mode=lambda: mode)

    with mock.patch.object(infra_drift, "elbv2_helper", elbv2_fake), \
            mock.patch.object(infra_drift, "ec2_helper", ec2_fake), \
            mock.patch.object(infra_drift, "system_settings", settings_fake), \
            mock.patch.object(infra_drift, "infrastructure", infra_fake), \
            mock.patch.object(infra_drift, "_setting",
                              side_effect=lambda name, default=None, kind=None: default):
        scanner = infra_drift.InfraDriftScanner(
            region="us-east-1",
            # Sentinels: _client() short-circuits on an injected client, so no
            # boto session is ever built.
            elbv2_client=object(), ec2_client=object())
        return scanner.scan()


def _provider_error(operation, code, iam_action="", denied=False):
    from mojo.helpers.aws.provider_call import ProviderCallError
    return ProviderCallError(operation, code, iam_action=iam_action, denied=denied)


def _run_job(report):
    """Publish and execute the drift asyncjob against a canned report."""
    from mojo.apps import jobs
    from mojo.apps.aws import asyncjobs

    with mock.patch.object(asyncjobs.infra_drift, "scan", return_value=report):
        jobs.publish(func="mojo.apps.aws.asyncjobs.check_infra_drift",
                     channel=CHANNEL, payload={})
        return th.run_pending_jobs(channel=CHANNEL)


@th.django_unit_setup()
def setup_infra_drift(opts):
    """Long-lived DB: clear anything a previous run of this module created."""
    from mojo.apps.aws.services import infra_drift
    from mojo.apps.incident.models import Event, RuleSet

    Event.objects.filter(category=infra_drift.CATEGORY).delete()
    RuleSet.objects.filter(category=infra_drift.RULESET_CATEGORY).delete()
    RuleSet.objects.filter(name=RULESET_NAME).delete()
    th.clear_jobs(channel=CHANNEL)


# ── the two categories ──────────────────────────────────────────────────────



# ── forward direction ───────────────────────────────────────────────────────











# ── reverse direction ───────────────────────────────────────────────────────



# ── the two AWS failure shapes ──────────────────────────────────────────────







# ── the external reframe ────────────────────────────────────────────────────



# ── the asyncjob ────────────────────────────────────────────────────────────







# ── rules ───────────────────────────────────────────────────────────────────



@th.django_unit_test()
def test_infra_drift_ruleset_does_not_block_health_defaults(opts):
    """Regression: the health bootstrap guarded on the system:health: PREFIX.

    Any RuleSet in that namespace made it permanently true, so Runner Down /
    Scheduler Missing / TCP Overload were never installed and a level-10
    runner-down event fell through to the handler-less catch-all.
    """
    from mojo.apps.incident import cronjobs as incident_cronjobs
    from mojo.apps.incident.models import RuleSet

    RuleSet.objects.filter(name__in=incident_cronjobs.HEALTH_RULE_NAMES).delete()
    RuleSet.objects.filter(name=RULESET_NAME).delete()
    RuleSet.ensure_infra_drift_rules()

    incident_cronjobs._health_defaults_checked = False
    incident_cronjobs._ensure_health_defaults()

    installed = set(RuleSet.objects.filter(
        name__in=incident_cronjobs.HEALTH_RULE_NAMES).values_list("name", flat=True))
    assert installed == set(incident_cronjobs.HEALTH_RULE_NAMES), (
        "Installing the fleet-drift RuleSet first must not suppress the real "
        f"health defaults; missing {set(incident_cronjobs.HEALTH_RULE_NAMES) - installed}")
    runner = RuleSet.objects.filter(name="Health - Runner Down").first()
    assert runner is not None and "ticket://" in (runner.handler or ""), (
        "Health - Runner Down must exist WITH its notify+ticket handler, "
        f"got {runner and runner.handler}")


