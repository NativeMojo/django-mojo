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

TESTIT_TIER = "edge"

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

@th.django_unit_test()
def test_categories_are_split_and_ruleset_is_outside_health_namespace(opts):
    from mojo.apps.aws.services import infra_drift

    assert infra_drift.CATEGORY == "system:health:infra_drift", (
        "The EVENT category must stay on the health strip; got "
        f"{infra_drift.CATEGORY}")
    assert infra_drift.RULESET_CATEGORY == "infra:drift", (
        "The RULESET category must stay OUT of the system:health: namespace so "
        "it can never satisfy the health-defaults bootstrap guard; got "
        f"{infra_drift.RULESET_CATEGORY}")
    assert not infra_drift.RULESET_CATEGORY.startswith("system:health:"), (
        "A prefix-matched bootstrap guard once suppressed 'Health - Runner "
        "Down' entirely; the RuleSet category must never re-enter that prefix")


# ── forward direction ───────────────────────────────────────────────────────

@th.django_unit_test()
def test_recorded_node_is_not_drift(opts):
    report = _scan(
        nodes=["ip-10-0-1-10"],
        serving=_serving([("prod-api-tg", ["i-0aaa1111"])]),
        instances={"i-0aaa1111": _instance("i-0aaa1111", "ip-10-0-1-10", "web-01")})

    assert report["status"] == "ok", f"A fully answered scan must be ok, got {report}"
    assert report["findings"] == [], (
        "A node whose private hostname IS the recorded node id is not drift; "
        f"got {report['findings']}")
    assert report["level"] == 1, \
        f"Nothing to report means level 1, got {report['level']}"


@th.django_unit_test()
def test_unrecorded_target_group_member_is_reported(opts):
    report = _scan(
        nodes=["ip-10-0-1-10"],
        serving=_serving([("prod-api-tg", ["i-0aaa1111", "i-0bbb2222"])]),
        instances={
            "i-0aaa1111": _instance("i-0aaa1111", "ip-10-0-1-10", "web-01"),
            "i-0bbb2222": _instance("i-0bbb2222", "ip-10-0-3-17", "web-04"),
        })

    assert len(report["findings"]) == 1, (
        "Exactly the one instance no recorded node matches is a finding; got "
        f"{report['findings']}")
    finding = report["findings"][0]
    assert finding["reason"] == "unrecorded_node", (
        "An untagged unrecorded node is `unrecorded_node`; got "
        f"{finding['reason']}")
    assert finding["instance_id"] == "i-0bbb2222", \
        f"The unrecorded instance must be the one reported, got {finding}"
    assert finding["target_groups"] == ["prod-api-tg"], (
        "The finding must name the target group by NAME so the operator can "
        f"act on it; got {finding['target_groups']}")
    assert "prod-api-tg" in finding["note"] and "EDGE_EXPECTED_TOPOLOGY" in finding["note"], \
        f"The note must name the group and the setting it is missing from; got {finding['note']}"
    assert "ip-10-0-3-17" in finding["remediation"], (
        "The remediation must quote the exact value to add to Expected fleet; "
        f"got {finding['remediation']}")
    assert report["level"] == 5, \
        f"Drift found is level 5, got {report['level']}"


@th.django_unit_test()
def test_capacity_added_node_missing_from_topology_is_named_as_such(opts):
    report = _scan(
        nodes=["ip-10-0-1-10"],
        serving=_serving([("prod-api-tg", ["i-0aaa1111", "i-0bbb2222"])]),
        instances={
            "i-0aaa1111": _instance("i-0aaa1111", "ip-10-0-1-10", "web-01"),
            "i-0bbb2222": _instance(
                "i-0bbb2222", "ip-10-0-3-17", "mojo-node-9999",
                tags={CREATED_BY_TAG: CAPACITY_VALUE}),
        })

    assert len(report["findings"]) == 1, \
        f"One unrecorded node is one finding, got {report['findings']}"
    finding = report["findings"][0]
    assert finding["reason"] == "capacity_added_not_recorded", (
        "A node carrying mojo:created-by=admin-capacity is the portal's own "
        "unrecorded node, which is a different sentence to the operator; got "
        f"{finding['reason']}")
    assert finding["added_by_capacity"] is True, \
        f"The structured flag must agree with the reason, got {finding}"
    assert f"{CREATED_BY_TAG}={CAPACITY_VALUE}" in finding["note"], (
        "The note must name the tag it read, so the operator can verify the "
        f"claim; got {finding['note']}")


@th.django_unit_test()
def test_matching_uses_the_instance_id_suffix_not_the_name_tag(opts):
    """A capacity-added node is recorded as <base>-<instance-id suffix>.

    Nothing in AWS carries that string, so the match has to come off the
    instance id's final label. The Name tag here matches no recorded node at
    all, which is what makes the assertion about the suffix and not the tag.
    """
    report = _scan(
        nodes=["mojo-node-0bbb2222"],
        serving=_serving([("prod-api-tg", ["i-0bbb2222"])]),
        instances={"i-0bbb2222": _instance(
            "i-0bbb2222", "ip-10-0-3-17", "some-unrelated-name")})

    assert report["findings"] == [], (
        "`mojo-node-0bbb2222` is the node id capacity derives from "
        "`i-0bbb2222`, so this node IS recorded and must not be drift; got "
        f"{report['findings']}")

    unrelated = _scan(
        nodes=["mojo-node-0ccc3333"],
        serving=_serving([("prod-api-tg", ["i-0bbb2222"])]),
        instances={"i-0bbb2222": _instance(
            "i-0bbb2222", "ip-10-0-3-17", "some-unrelated-name")})
    reasons = sorted(row["reason"] for row in unrelated["findings"])
    assert reasons == ["node_unserving", "unrecorded_node"], (
        "The suffix match is EXACT on the final label — a different suffix must "
        f"not match, in either direction; got {unrelated['findings']}")


@th.django_unit_test()
def test_fqdn_hostname_normalizes_to_the_topology_node_id(opts):
    """Regression: the two sides of the comparison disagree about the domain.

    Topology node ids come from `host_channel()` — `gethostname()` lowercased
    with dots turned into dashes, DOMAIN KEPT. `facts["private_hostname"]` is
    the private DNS name with the domain STRIPPED. Compared raw, a hand-built
    node whose `gethostname()` returns `ip-10-0-1-23.ec2.internal` misses every
    time and false-positives daily at level 5 — on exactly the nodes
    `private_hostname` was added to protect.
    """
    report = _scan(
        nodes=["ip-10-0-1-23-ec2-internal"],
        serving=_serving([("prod-api-tg", ["i-0ccc4444"])]),
        instances={"i-0ccc4444": _instance(
            "i-0ccc4444", "ip-10-0-1-23", name="web-07",
            dns="ip-10-0-1-23.ec2.internal")})

    assert report["findings"] == [], (
        "`ip-10-0-1-23.ec2.internal` normalizes to `ip-10-0-1-23-ec2-internal`, "
        "which IS the recorded node id — this must not be drift in either "
        f"direction; got {report['findings']}")
    assert report["level"] == 1, (
        "A correctly recorded FQDN-hostname fleet must stay level 1, not file a "
        f"daily level-5 event; got {report['level']}")


# ── reverse direction ───────────────────────────────────────────────────────

@th.django_unit_test()
def test_recorded_node_registered_nowhere_is_reported(opts):
    report = _scan(
        nodes=["ip-10-0-1-10", "web-retired"],
        serving=_serving([("prod-api-tg", ["i-0aaa1111"])]),
        instances={"i-0aaa1111": _instance("i-0aaa1111", "ip-10-0-1-10", "web-01")})

    assert len(report["findings"]) == 1, \
        f"Only the unmatched recorded node is a finding, got {report['findings']}"
    finding = report["findings"][0]
    assert finding["reason"] == "node_unserving", (
        "A recorded node behind no target group is `node_unserving`; got "
        f"{finding['reason']}")
    assert finding["name"] == "web-retired", \
        f"The finding must name the recorded node, got {finding}"
    assert finding["instance_id"] is None, (
        "This direction is computed from RECORDED names, so there is no "
        f"instance to name; got {finding['instance_id']}")
    assert "web-retired" in finding["remediation"], (
        "The remediation must quote the recorded name to remove; got "
        f"{finding['remediation']}")
    assert report["level"] == 5, \
        f"Drift found is level 5, got {report['level']}"


# ── the two AWS failure shapes ──────────────────────────────────────────────

@th.django_unit_test()
def test_credentials_unavailable_files_nothing(opts):
    """Regression: `except NoCredentialsError` can never fire on this path.

    `serving_map` and `instance_map` go through `ProviderCaller.call`, which
    catches `Exception` and re-raises `ProviderCallError`. Branching on the
    exception TYPE would make `status="unavailable"` unreachable, so every
    credential-less box — every dev machine, every suite run — would file a
    level-4 event that falls through to the handler-less catch-all RuleSet and
    manufactures a permanent Incident.
    """
    from mojo.apps.aws.services import infra_drift
    from mojo.apps.incident.models import Event

    Event.objects.filter(category=infra_drift.CATEGORY).delete()
    th.clear_jobs(channel=CHANNEL)

    report = _scan(
        nodes=["ip-10-0-1-10"],
        serving_error=_provider_error("elbv2.describe_load_balancers",
                                      "credentials_unavailable"))
    assert report["status"] == "unavailable", (
        "A ProviderCallError carrying provider_code=credentials_unavailable "
        f"must read as unavailable, not as drift; got {report}")
    assert report["findings"] == [], \
        f"An unavailable scan invents no findings, got {report['findings']}"
    assert report["level"] == 1, \
        f"An unavailable scan must stay at the do-not-file level, got {report['level']}"

    network = _scan(
        nodes=["ip-10-0-1-10"],
        serving=_serving([("prod-api-tg", ["i-0aaa1111"])]),
        instances_error=_provider_error("ec2.describe_instances", "network_unavailable"))
    assert network["status"] == "unavailable", (
        "network_unavailable on the SECOND read must be unavailable too; got "
        f"{network}")

    executed = _run_job(report)
    assert executed >= 1, f"The drift job must have run, executed={executed}"
    assert Event.objects.filter(category=infra_drift.CATEGORY).count() == 0, (
        "An unavailable scan must file no event — a daily 'couldn't check' on "
        "every dev box is pure noise")


@th.django_unit_test()
def test_denied_api_warns_by_exact_iam_action_and_still_reports(opts):
    report = _scan(
        nodes=["ip-10-0-1-10"],
        serving=_serving([("prod-api-tg", ["i-0aaa1111"])]),
        instances_error=_provider_error(
            "ec2.describe_instances", "AccessDenied",
            iam_action="ec2:DescribeInstances", denied=True))

    assert report["status"] == "ok", \
        f"A denial is a partial answer, not an unavailable box; got {report}"
    actions = [warning.get("iam_action") for warning in report["warnings"]]
    assert "ec2:DescribeInstances" in actions, (
        f"The warning must name the EXACT missing IAM action, got {actions}")
    assert report["findings"] == [], (
        "Without instance facts every hostname-recorded node would read as "
        f"drift; the scan must invent nothing; got {report['findings']}")
    assert report["level"] == 4, (
        "A scan that could not compare because of IAM must still be level 4 so "
        f"an event is filed; got {report['level']}")

    # ProviderCallError.detail() includes `iam_action` ONLY when denied is
    # True, so a non-denial must still be attributed — from the exception
    # attribute or the literal, never from detail()[...] (a KeyError).
    undenied = _scan(
        nodes=["ip-10-0-1-10"],
        serving_error=_provider_error("elbv2.describe_target_groups", "InternalFailure"))
    assert undenied["status"] == "ok", \
        f"A non-credential provider error is a warning, not unavailable; got {undenied}"
    undenied_actions = [warning.get("iam_action") for warning in undenied["warnings"]]
    assert undenied_actions == ["elasticloadbalancing:DescribeTargetGroups"], (
        "A non-denied failure carries no iam_action in detail(); the caller-known "
        f"literal must fill in; got {undenied_actions}")
    assert undenied["level"] == 4, \
        f"An unread balancer tier is level 4, got {undenied['level']}"


@th.django_unit_test()
def test_truncated_groups_and_instances_warn_instead_of_inventing_drift(opts):
    """Three silent truncations live upstream; none may become a finding."""
    from mojo.apps.aws.services import infra_drift

    hostnames = [f"ip-10-0-{index // 250}-{index % 250}" for index in range(105)]
    ids = [f"i-0{index:015d}" for index in range(105)]
    instances = {identifier: _instance(identifier, hostname)
                 for identifier, hostname in zip(ids, hostnames)}
    groups = [("prod-api-tg", ids)]
    groups += [(f"filler-{index}", []) for index in range(infra_drift.MAX_GROUPS - 1)]

    # Only the first MAX_INSTANCE_IDS are described, so only those are recorded
    # — a recorded node for a truncated-away instance would be a false
    # `node_unserving`, which is the very invention this guard exists to stop.
    report = _scan(
        nodes=hostnames[:infra_drift.MAX_INSTANCE_IDS],
        serving=_serving(groups), instances=instances)

    kinds = sorted({warning["kind"] for warning in report["warnings"]})
    assert kinds == ["groups_truncated", "instance_truncated"], (
        "Both upstream truncations must be visible to the operator; got "
        f"{report['warnings']}")
    unchecked = 105 - infra_drift.MAX_INSTANCE_IDS
    assert any(str(unchecked) in warning["message"] for warning in report["warnings"]), (
        f"The instance warning must name the {unchecked} unchecked instances; "
        f"got {report['warnings']}")
    assert report["findings"] == [], (
        "Truncation must never manufacture drift for the rows nobody read; got "
        f"{report['findings']}")
    assert report["level"] == 4, \
        f"Warnings without findings are level 4, got {report['level']}"


# ── the external reframe ────────────────────────────────────────────────────

@th.django_unit_test()
def test_external_mode_reframes_but_does_not_suppress(opts):
    from mojo.apps.aws import asyncjobs

    fixture = dict(
        nodes=["ip-10-0-1-10"],
        serving=_serving([("prod-api-tg", ["i-0aaa1111", "i-0bbb2222"])]),
        instances={
            "i-0aaa1111": _instance("i-0aaa1111", "ip-10-0-1-10", "web-01"),
            "i-0bbb2222": _instance("i-0bbb2222", "ip-10-0-3-17", "web-04"),
        })
    managed = _scan(mode="managed", **fixture)
    external = _scan(mode="external", **fixture)

    assert len(external["findings"]) == len(managed["findings"]) == 1, (
        "External mode must not suppress a finding — an externally-managed "
        "estate is not less drifted; got "
        f"{external['findings']} vs {managed['findings']}")
    assert external["level"] == managed["level"] == 5, (
        "External mode must not lower the level either; got "
        f"{external['level']} vs {managed['level']}")
    assert external["mode"] == "external", \
        f"The report must carry the mode it was framed for, got {external['mode']}"

    remediation = external["findings"][0]["remediation"]
    assert "INFRASTRUCTURE_MODE=external" in remediation, (
        "The external remediation must say WHY the node is expected; got "
        f"{remediation}")
    assert "deregister" not in remediation.lower(), (
        "Telling an external installation to deregister a node its pipeline "
        f"owns is the wrong instruction; got {remediation}")
    assert external["findings"][0]["note"] == managed["findings"][0]["note"], (
        "External mode reframes ONLY the 'What to do' sentence; the observation "
        "itself is identical")

    details = asyncjobs._infra_details(external)
    assert "Infrastructure mode: external" in details, (
        f"The rendered event must state the mode up front; got:\n{details}")
    assert "the portal only observes" in details, (
        f"The mode line must say what the portal will and will not do; got:\n{details}")
    assert "prod-api-tg" in details, (
        "Identifiers must survive the wrap intact — a hyphen-broken target "
        f"group name is not copy-pasteable; got:\n{details}")


# ── the asyncjob ────────────────────────────────────────────────────────────

@th.django_unit_test()
def test_level_1_files_no_event(opts):
    """A level-1 'all clear' would manufacture a permanent Incident every run.

    `Event.publish` creates an Incident whenever any RuleSet matched, and the
    catch-all matches `Level >= 1` through the `"*"` fallback — with no handler.
    """
    from mojo.apps.aws.services import infra_drift
    from mojo.apps.incident.models import Event

    Event.objects.filter(category=infra_drift.CATEGORY).delete()
    th.clear_jobs(channel=CHANNEL)

    report = _scan(
        nodes=["ip-10-0-1-10"],
        serving=_serving([("prod-api-tg", ["i-0aaa1111"])]),
        instances={"i-0aaa1111": _instance("i-0aaa1111", "ip-10-0-1-10", "web-01")})
    assert report["level"] == 1, f"The fixture must be clean, got {report}"

    executed = _run_job(report)
    assert executed >= 1, f"The drift job must have run, executed={executed}"
    assert Event.objects.filter(category=infra_drift.CATEGORY).count() == 0, (
        "A level-1 report must file NOTHING; a liveness event here becomes a "
        "permanent handler-less Incident on every single run")


@th.django_unit_test()
def test_asyncjob_files_one_event_with_findings_in_metadata(opts):
    from mojo.apps.aws.services import infra_drift
    from mojo.apps.incident.models import Event

    Event.objects.filter(category=infra_drift.CATEGORY).delete()
    th.clear_jobs(channel=CHANNEL)

    report = _scan(
        nodes=["ip-10-0-1-10", "web-retired"],
        serving=_serving([("prod-api-tg", ["i-0aaa1111", "i-0bbb2222"])]),
        instances={
            "i-0aaa1111": _instance("i-0aaa1111", "ip-10-0-1-10", "web-01"),
            "i-0bbb2222": _instance("i-0bbb2222", "ip-10-0-3-17", "web-04"),
        })
    assert len(report["findings"]) == 2, (
        "The fixture must produce one finding in each direction, got "
        f"{report['findings']}")

    executed = _run_job(report)
    assert executed >= 1, f"The drift job must have run, executed={executed}"

    events = list(Event.objects.filter(category=infra_drift.CATEGORY))
    assert len(events) == 1, f"The job must file exactly ONE event, got {events}"
    event = events[0]
    assert event.level == 5, f"Drift found is level 5, got {event.level}"
    assert event.scope == infra_drift.RULESET_CATEGORY, (
        "RuleSets are matched by scope first, so the scope must be the ruleset "
        f"category; got {event.scope}")
    rows = event.metadata.get("findings") or []
    assert len(rows) == 2, \
        f"Both finding rows must reach the event metadata, got {rows}"
    reasons = sorted(row["reason"] for row in rows)
    assert reasons == ["node_unserving", "unrecorded_node"], \
        f"Both directions must be represented, got {reasons}"
    assert "1 node(s) are serving traffic but are not in the portal's recorded fleet" \
        in event.title, (
            "The title must lead with the serving-side count in the operator's "
            f"words; got {event.title}")
    assert "Nothing here changes AWS for you." in event.details, (
        "Every finding must say plainly that this scanner changed nothing; got "
        f"{event.details}")


@th.django_unit_test()
def test_cronjob_is_daily_and_gated_by_the_setting(opts):
    from mojo.apps.aws import cronjobs
    from mojo.decorators.cron import schedule

    specs = [spec for spec in getattr(schedule, "scheduled_functions", [])
             if spec["func"].__module__ == "mojo.apps.aws.cronjobs"
             and spec["func"].__name__ == "check_infra_drift"]
    assert len(specs) == 1, \
        f"check_infra_drift must be registered exactly once, got {len(specs)}"
    spec = specs[0]
    assert (spec["minutes"], spec["hours"]) == ("20", "7"), (
        "The scan runs daily at 07:20 — offset from the version-drift scan so "
        f"the two AWS reads never share a tick; got {spec}")
    assert (spec["days"], spec["months"], spec["weekdays"]) == ("*", "*", "*"), \
        f"Daily means every day/month/weekday, got {spec}"

    with mock.patch.object(cronjobs, "jobs") as disabled, \
            mock.patch.object(cronjobs, "_setting", return_value=False):
        cronjobs.check_infra_drift()
    assert disabled.publish.call_count == 0, \
        "AWS_INFRA_DRIFT_ENABLED=False must publish nothing"

    with mock.patch.object(cronjobs, "jobs") as enabled, \
            mock.patch.object(cronjobs, "_setting", return_value=True):
        cronjobs.check_infra_drift()
    enabled.publish.assert_called_once_with(
        func="mojo.apps.aws.asyncjobs.check_infra_drift", channel="cleanup", payload={})


# ── rules ───────────────────────────────────────────────────────────────────

@th.django_unit_test()
def test_ensure_infra_drift_rules_is_idempotent_and_notify_only(opts):
    from mojo.apps.aws.services import infra_drift
    from mojo.apps.incident.models import RuleSet

    RuleSet.objects.filter(name=RULESET_NAME).delete()
    first, created = RuleSet.ensure_infra_drift_rules()
    assert created, "The first call must create the RuleSet"
    second, created_again = RuleSet.ensure_infra_drift_rules()
    assert not created_again, "The second call must reuse the existing RuleSet"
    assert first.pk == second.pk, \
        f"Idempotent means one row, got {first.pk} and {second.pk}"
    assert RuleSet.objects.filter(name=RULESET_NAME).count() == 1, \
        "Calling twice must leave exactly one RuleSet"

    assert first.category == infra_drift.RULESET_CATEGORY, (
        "The RuleSet must live outside the system:health: namespace so it can "
        f"never satisfy the health-defaults guard; got {first.category}")
    assert first.handler == "notify://perm@manage_security", (
        "Drift notifies and does not open a ticket — on an external estate the "
        f"same two lines would recur until someone records the node; got {first.handler}")
    assert "ticket://" not in (first.handler or ""), \
        f"No ticket handler may appear on this RuleSet, got {first.handler}"

    rules = list(first.rules.all())
    assert len(rules) == 1, f"Exactly one gate rule is expected, got {rules}"
    rule = rules[0]
    assert (rule.field_name, rule.comparator, rule.value) == ("level", ">=", "5"), (
        "Level 4 is 'a read did not answer' and must not notify; got "
        f"{rule.field_name} {rule.comparator} {rule.value}")




@th.django_unit_test()
def test_aws_check_rules_section_creates_the_drift_ruleset(opts):
    from mojo.apps.aws.services import aws_check, infra_drift
    from mojo.apps.incident.models import RuleSet

    RuleSet.objects.filter(name=RULESET_NAME).delete()
    # Pre-create the version-drift RuleSet so this run takes its `present`
    # branch: tests/test_aws/version_drift.py owns that row, and testit runs
    # modules concurrently in one process.
    RuleSet.ensure_aws_version_rules()

    runner = aws_check.AWSCheckRunner(apply=True, yes=True)
    runner.check_rules()

    codes = [item["code"] for item in runner.results]
    assert "rules.infra_drift_created" in codes, (
        "An --apply run of the rules section must create the fleet drift "
        f"RuleSet; got {codes}")
    created = RuleSet.objects.filter(
        category=infra_drift.RULESET_CATEGORY, name=RULESET_NAME).first()
    assert created is not None, \
        "The RuleSet must actually exist afterwards, not just be reported"

    again = aws_check.AWSCheckRunner(apply=True, yes=True)
    again.check_rules()
    repeat_codes = [item["code"] for item in again.results]
    assert "rules.infra_drift_present" in repeat_codes, (
        f"A second run must report the RuleSet as present, got {repeat_codes}")
    assert RuleSet.objects.filter(name=RULESET_NAME).count() == 1, \
        "Two --apply runs must leave exactly one RuleSet"

    checked = aws_check.AWSCheckRunner()
    checked.check_rules()
    statuses = {item["status"] for item in checked.results
                if item["code"].startswith("rules.infra_drift")}
    assert statuses == {"pass"}, (
        "With the RuleSet installed a --check run must pass, never fail; got "
        f"{[item for item in checked.results if item['code'].startswith('rules.infra_drift')]}")
