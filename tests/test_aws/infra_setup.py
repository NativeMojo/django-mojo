"""The `aws_infrastructure` readiness section: resolution, mapping, and the gate.

Every test drives the section the way System Setup does — through
``system_readiness.run("aws_infrastructure", context)`` — rather than calling
``check_infrastructure`` directly, because the ceiling that makes the aggregate
rows mandatory (64 checks, 16 detail keys, 500-char strings) lives in ``run``
and not in the check.

NOTHING HERE TALKS TO AWS, and two different seams keep it that way. The
observation itself is injected as ``context["aws_observe"]``: crafting a
``BLIND`` finding that names a denied IAM action out of twelve ``Stubber``-wrapped
clients would be a reimplementation of ``mojo.deploy.provision``'s own Stubber
suite, which already proves the observation. What is under test here is the
rendering. The second seam, ``context["aws_client_factory"]``, exists to prove a
negative: on an installation with no environment file the factory must never be
called at all, and a factory that raises is the only way to prove that.
"""

import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from unittest import mock

from testit import helpers as th


SECTION = "aws_infrastructure"




def _answers(project="mojoinfra", env="prod", region="us-east-1"):
    return {
        "schema_version": 1,
        "project": project, "env": env, "region": region,
        "apex_domain": "example.com", "operator_email": "ops@example.com",
        "preset": "small", "github_repo": "acme/api",
    }


@contextmanager
def _environments(*answer_sets):
    """A throwaway `aws/environments/` holding exactly these env files.

    Setup deletes before it creates: the directory is fresh per test, so a
    previous run's file can never make a "zero files" assertion pass by
    accident.
    """
    from mojo.apps.aws.services import infra_setup

    root = tempfile.mkdtemp(prefix="mojo-infra-")
    directory = os.path.join(root, "aws", "environments")
    if os.path.isdir(directory):
        shutil.rmtree(directory)
    os.makedirs(directory)
    for answers in answer_sets:
        path = os.path.join(directory, f"{answers['env']}.json")
        with open(path, "w") as handle:
            handle.write(json.dumps(answers, indent=2, sort_keys=True) + "\n")
    try:
        with mock.patch.object(infra_setup, "_project_root", return_value=root):
            yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _run(context=None):
    from mojo.apps.account.services import system_readiness

    report = system_readiness.run(SECTION, context or {})
    return report["sections"][0]


def _checks(section):
    return {row["code"]: row for row in section["checks"]}


def _finding(step, status, code, message="something happened", remedy=None):
    from mojo.deploy.provision import report

    return report.Finding(step, status, code, message, remedy)


def _run_object(step_names=("account",)):
    from objict import objict
    from mojo.deploy.provision import discover, plan, report

    steps = objict()
    for name in step_names:
        steps[name] = objict(status=plan.OK, depends_on=[], blocked_by=[],
                             values={})
    return objict(steps=steps, observed=discover.blank(), worst=report.PASS,
                  blocking=False, validated=True, problems=[])


def _observer(findings, step_names=("account",), captured=None):
    def observe(clients, spec):
        if captured is not None:
            captured.append({"clients": clients, "spec": spec})
        return list(findings), [], _run_object(step_names)
    return observe


class _ExplodingFactory:
    """A client factory that proves it was never reached."""

    def __init__(self):
        self.calls = 0

    def __call__(self, service, region=None):
        self.calls += 1
        raise AssertionError(
            f"a client for {service} was built before an environment resolved")


def _clear_cache(answers):
    from django.core.cache import cache
    from mojo.apps.aws.services import infra_setup
    from mojo.deploy.provision import inputs

    cache.delete(infra_setup._cache_key(inputs.to_spec(answers)))


# ── environment resolution ──────────────────────────────────────────────────

@th.django_unit_test("no environment file means one pending row and zero AWS calls")
def test_unresolved_environment_makes_no_aws_call(opts):
    factory = _ExplodingFactory()
    with _environments():
        section = _run({"aws_client_factory": factory})

    rows = _checks(section)
    assert factory.calls == 0, (
        f"an installation with no environment file built {factory.calls} AWS "
        f"client(s); it must build none"
    )
    assert section["status"] == "pending", (
        f"an unprovisioned installation must be pending, not "
        f"{section['status']!r} — it is not a failure"
    )
    assert set(rows) == {f"{SECTION}.mode", f"{SECTION}.environment"}, (
        f"an unresolved section must be exactly the mode and environment rows, "
        f"got {sorted(rows)}"
    )
    assert rows[f"{SECTION}.environment"]["status"] == "pending", (
        "the environment row for an unprovisioned installation must be pending"
    )
    assert "provision" in rows[f"{SECTION}.environment"]["remediation"], (
        "the remediation must name the provisioning CLI: "
        f"{rows[f'{SECTION}.environment']['remediation']!r}"
    )


@th.django_unit_test("exactly one environment file resolves with no setting needed")
def test_single_environment_file_resolves(opts):
    answers = _answers(project="mojoone", env="prod")
    captured = []
    _clear_cache(answers)
    with _environments(answers):
        section = _run({"aws_observe": _observer(
            [_finding("account", "PASS", "account.ok", "account 1 in us-east-1")],
            captured=captured)})

    assert len(captured) == 1, (
        f"one environment file must resolve and observe exactly once, "
        f"observed {len(captured)} time(s)"
    )
    spec = captured[0]["spec"]
    assert (spec.project, spec.env) == ("mojoone", "prod"), (
        f"the only environment file must be the one observed, got "
        f"{spec.project!r}/{spec.env!r}"
    )
    assert f"{SECTION}.environment" not in _checks(section), (
        "a resolved environment must not also emit an unresolved environment row"
    )


@th.django_unit_test("two environment files and no MOJO_ENVIRONMENT stays pending")
def test_ambiguous_environments_pend(opts):
    factory = _ExplodingFactory()
    with _environments(_answers(env="prod"), _answers(env="staging")):
        section = _run({"aws_client_factory": factory})

    row = _checks(section)[f"{SECTION}.environment"]
    assert factory.calls == 0, (
        "an ambiguous environment must not be guessed at with an AWS call"
    )
    assert row["status"] == "pending", (
        f"ambiguity is pending, not {row['status']!r} — nothing is broken"
    )
    assert "MOJO_ENVIRONMENT" in row["remediation"], (
        f"the remediation must name the setting that disambiguates, got "
        f"{row['remediation']!r}"
    )






# ── finding → readiness mapping ─────────────────────────────────────────────

@th.django_unit_test("a converged observation is green on every row")
def test_converged_observation_is_green(opts):
    answers = _answers(project="mojogreen")
    _clear_cache(answers)
    findings = [
        _finding("account", "PASS", "account.ok", "account 1 in us-east-1"),
        _finding("network", "PASS", "vpc.ok", "vpc-0a exists"),
        _finding("db", "PASS", "db.ok", "the cluster is available"),
    ]
    with _environments(answers):
        section = _run({"aws_observe": _observer(
            findings, step_names=("account", "network", "db"))})

    rows = _checks(section)
    assert section["status"] == "pass", (
        f"a fully converged observation must be pass, got {section['status']!r}"
    )
    assert set(rows) == {f"{SECTION}.mode", f"{SECTION}.summary"}, (
        f"a converged account costs exactly the mode and summary rows — every "
        f"other row is a problem detail; got {sorted(rows)}"
    )
    summary = rows[f"{SECTION}.summary"]
    assert summary["status"] == "pass", (
        f"the summary row must be pass on a converged account, got "
        f"{summary['status']!r}"
    )
    assert summary["details"]["steps"] == 3, (
        f"the summary must count every step it observed, got "
        f"{summary['details']['steps']}"
    )


@th.django_unit_test("MISSING is pending, DRIFT is warn, and the section takes the worst")
def test_missing_and_drift_rollup(opts):
    answers = _answers(project="mojoroll")
    _clear_cache(answers)
    findings = [
        _finding("account", "PASS", "account.ok", "account 1"),
        _finding("network", "DRIFT", "vpc.tags", "vpc-0a is missing a tag",
                 "re-run apply"),
        _finding("db", "MISSING", "db.absent", "the cluster does not exist",
                 "run apply"),
    ]
    with _environments(answers):
        section = _run({"aws_observe": _observer(
            findings, step_names=("account", "network", "db"))})

    rows = _checks(section)
    assert rows[f"{SECTION}.network"]["status"] == "warn", (
        f"DRIFT must render as warn, got "
        f"{rows[f'{SECTION}.network']['status']!r}"
    )
    assert rows[f"{SECTION}.db"]["status"] == "pending", (
        f"MISSING must render as pending, got "
        f"{rows[f'{SECTION}.db']['status']!r}"
    )
    assert rows[f"{SECTION}.db"]["remediation"] == "run apply", (
        "the worst finding's remedy must be the row's remediation, got "
        f"{rows[f'{SECTION}.db']['remediation']!r}"
    )
    assert section["status"] == "pending", (
        f"readiness precedence is fail > pending > warn, so a pending row plus "
        f"a warn row is pending; got {section['status']!r}"
    )


@th.django_unit_test("BLOCKED fails, PENDING pends, and MANUAL warns rather than reds the page")
def test_blocked_pending_and_manual_mapping(opts):
    answers = _answers(project="mojomap")
    _clear_cache(answers)
    findings = [
        _finding("db", "PENDING", "db.creating", "the cluster is still creating"),
        _finding("balancer", "MANUAL", "tg.protocol",
                 "the target group's protocol cannot be changed in place",
                 "recreate the target group"),
        _finding("nodes", "BLOCKED", "nodes.blocked", "nodes did not run"),
    ]
    with _environments(answers):
        section = _run({"aws_observe": _observer(
            findings, step_names=("db", "balancer", "nodes"))})

    rows = _checks(section)
    assert rows[f"{SECTION}.db"]["status"] == "pending", (
        f"PENDING is the normal state of a five-minute resource and must be "
        f"pending, got {rows[f'{SECTION}.db']['status']!r}"
    )
    assert rows[f"{SECTION}.balancer"]["status"] == "warn", (
        f"MANUAL is a real difference nothing in this portal can repair, so it "
        f"warns rather than reds the page; got "
        f"{rows[f'{SECTION}.balancer']['status']!r}"
    )
    assert rows[f"{SECTION}.nodes"]["status"] == "fail", (
        f"BLOCKED must render as fail, got "
        f"{rows[f'{SECTION}.nodes']['status']!r}"
    )
    assert section["status"] == "fail", (
        f"a failed step must take the section to fail, got {section['status']!r}"
    )


@th.django_unit_test("BLIND splits by cause: a denied IAM action fails, a throttle pends")
def test_blind_splits_by_cause(opts):
    answers = _answers(project="mojoblind")
    _clear_cache(answers)
    findings = [
        _finding("network", "BLIND", "ec2.describe_vpcs.denied",
                 "the provisioning credential cannot call ec2.describe_vpcs",
                 "grant the provisioning credential this action"),
        _finding("db", "BLIND", "rds.describe_db_clusters.throttled",
                 "AWS throttled rds.describe_db_clusters",
                 "re-run — the next pass picks up where this one stopped"),
    ]
    with _environments(answers):
        section = _run({"aws_observe": _observer(
            findings, step_names=("network", "db"))})

    rows = _checks(section)
    assert rows[f"{SECTION}.network"]["status"] == "fail", (
        f"a BLIND finding naming a denied IAM action is a permanent operator "
        f"problem and must fail, got {rows[f'{SECTION}.network']['status']!r}"
    )
    assert rows[f"{SECTION}.db"]["status"] == "pending", (
        f"a throttled BLIND finding must be pending — still blocking green, but "
        f"not a red page for a transient blip; got "
        f"{rows[f'{SECTION}.db']['status']!r}"
    )


@th.django_unit_test("far more findings and steps than fit, and no failure is lost")
def test_aggregation_survives_the_report_ceilings(opts):
    answers = _answers(project="mojobulk")
    _clear_cache(answers)
    steps = tuple(f"step{index:03d}" for index in range(120))
    findings = []
    for name in steps:
        # Several findings per step, so the roll-up is doing real work.
        findings.append(_finding(name, "PASS", f"{name}.ok", f"{name} is fine"))
        findings.append(_finding(name, "DRIFT", f"{name}.tag",
                                 f"{name} is missing a tag", "re-run apply"))
    # The one failure sorts LAST by step name, exactly where a truncation that
    # kept list order rather than severity would drop it.
    findings.append(_finding(
        "step119", "BLOCKED", "step119.blocked", "step119 did not run"))
    with _environments(answers):
        section = _run({"aws_observe": _observer(findings, step_names=steps)})

    assert len(section["checks"]) <= 64, (
        f"the section returned {len(section['checks'])} checks; readiness keeps "
        f"64, so anything past that is silently dropped"
    )
    assert section["status"] == "fail", (
        f"the single failure among 120 steps must survive aggregation; the "
        f"section reported {section['status']!r}"
    )
    rows = _checks(section)
    assert rows[f"{SECTION}.step119"]["status"] == "fail", (
        "the failing step's own row must survive truncation — severity, not "
        "list position, decides what is kept"
    )
    summary = rows[f"{SECTION}.summary"]
    assert summary["details"]["failed"] == 1, (
        f"the summary counts are authoritative including anything truncated "
        f"below them; got failed={summary['details']['failed']}"
    )
    assert summary["details"]["steps"] == 120, (
        f"the summary must count every step, not just the rendered ones; got "
        f"{summary['details']['steps']}"
    )
    assert f"{SECTION}.additional_steps" in rows, (
        "the steps that did not fit must be acknowledged by an overflow row, "
        "not silently disappear"
    )


# ── containment ─────────────────────────────────────────────────────────────

@th.django_unit_test("a ProviderCallError becomes a fail row and never escapes the check")
def test_provider_call_error_is_contained(opts):
    from mojo.helpers.aws.provider_call import ProviderCallError

    answers = _answers(project="mojoprov")
    _clear_cache(answers)

    def observe(clients, spec):
        raise ProviderCallError("ec2.describe_vpcs", "AccessDenied",
                                "ec2:DescribeVpcs")

    with _environments(answers):
        section = _run({"aws_observe": observe})

    rows = _checks(section)
    assert f"{SECTION}.check_error" not in rows, (
        "the exception escaped check_infrastructure and replaced the whole "
        "section with readiness's opaque check_error row"
    )
    row = rows[f"{SECTION}.observation"]
    assert row["status"] == "fail", (
        f"a provider denial must be a fail row, got {row['status']!r}"
    )
    assert "ec2:DescribeVpcs" in row["remediation"], (
        f"the remediation must name the missing IAM action, got "
        f"{row['remediation']!r}"
    )
    assert rows[f"{SECTION}.mode"]["status"] in ("pass", "warn"), (
        "the mode row must survive an observation failure"
    )


@th.django_unit_test("any other exception from the observation is contained too")
def test_unexpected_exception_is_contained(opts):
    answers = _answers(project="mojoboom")
    _clear_cache(answers)

    def observe(clients, spec):
        raise RuntimeError("boto3 fell over")

    with _environments(answers):
        section = _run({"aws_observe": observe})

    rows = _checks(section)
    assert f"{SECTION}.check_error" not in rows, (
        "an unclassified exception escaped and collapsed the whole section"
    )
    assert rows[f"{SECTION}.observation"]["status"] == "fail", (
        "an unclassified observation failure must still be reported as fail"
    )


# ── the external-mode gate ──────────────────────────────────────────────────





# ── caching ─────────────────────────────────────────────────────────────────

class _CountingObserver:
    def __init__(self):
        self.calls = 0
        self.regions = []

    def __call__(self, clients, spec):
        self.calls += 1
        self.regions.append(spec.region)
        return ([_finding("account", "PASS", "account.ok", "account 1")], [],
                _run_object())


@th.django_unit_test("the observation is cached, and the final-readiness path bypasses it")
def test_cache_hit_and_operation_bypass(opts):
    answers = _answers(project="mojocache")
    _clear_cache(answers)
    observer = _CountingObserver()
    with _environments(answers):
        _run({"aws_observe": observer})
        _run({"aws_observe": observer})
        assert observer.calls == 1, (
            f"a second check inside the TTL must be served from cache; the "
            f"account was observed {observer.calls} times"
        )
        _run({"aws_observe": observer, "operation": object()})
        assert observer.calls == 2, (
            "the final-readiness path must bypass the cache — serving a pre-fix "
            "observation as proof of a post-fix state would be wrong, not stale"
        )
    _clear_cache(answers)


@th.django_unit_test("two regions with the same declaration do not share a cache entry")
def test_cache_is_keyed_by_region(opts):
    east = _answers(project="mojoregion", env="prod", region="us-east-1")
    west = _answers(project="mojoregion", env="prod", region="us-west-2")
    _clear_cache(east)
    _clear_cache(west)
    observer = _CountingObserver()
    with _environments(east):
        _run({"aws_observe": observer})
    with _environments(west):
        _run({"aws_observe": observer})

    assert observer.calls == 2, (
        f"an identical spec in a different region must not read the first "
        f"region's cached observation; observed {observer.calls} time(s)"
    )
    assert observer.regions == ["us-east-1", "us-west-2"], (
        f"both regions must have been observed in order, got {observer.regions}"
    )
    _clear_cache(east)
    _clear_cache(west)


# ── the registry ────────────────────────────────────────────────────────────

@th.django_unit_test("the section registers read-only at order 34")
def test_section_registration(opts):
    from mojo.apps.account.services import system_readiness

    entry = system_readiness.get_section(SECTION)
    assert entry is not None, (
        "aws_infrastructure is not registered — mojo/apps/aws/apps.py's ready() "
        "must call infra_setup.register_sections()"
    )
    assert entry["order"] == 34, (
        f"order 34 sorts this directly after the AWS block; got {entry['order']}"
    )
    assert entry["fix"] is None, (
        "aws_infrastructure MUST stay read-only. system_setup._build_steps() "
        "adds a step for every fixable section on every Fix-all run regardless "
        "of status, and _execute_planned treats a raised DefinitiveSetupFailure "
        "as terminal — so a fixer here hard-fails every Fix-all run on an "
        "external-mode install, before the operator reaches any repairable "
        "section."
    )
    assert system_readiness.sections()[0]["code"] == "django", (
        "order 34 must not displace the Django section from first position"
    )
