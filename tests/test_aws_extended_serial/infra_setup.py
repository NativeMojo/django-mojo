"""Moved from the default-tier sibling (maestro item #1839): these tests mutate django.conf.settings process-wide via a save/restore helper, which is unsafe under the parallel default tier.
"""
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


@contextmanager
def _override_setting(name, value):
    """In-process Django settings override (th.server_settings only affects the
    separate server process; override_settings is banned by testing rules)."""
    import django.conf
    sentinel = object()
    original = getattr(django.conf.settings, name, sentinel)
    setattr(django.conf.settings, name, value)
    try:
        yield
    finally:
        if original is sentinel:
            delattr(django.conf.settings, name)
        else:
            setattr(django.conf.settings, name, original)


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







@th.django_unit_test("MOJO_ENVIRONMENT picks the matching file out of several")
def test_selected_environment_resolves(opts):
    prod = _answers(project="mojosel", env="prod")
    staging = _answers(project="mojosel", env="staging")
    captured = []
    _clear_cache(staging)
    with _environments(prod, staging):
        with _override_setting("MOJO_ENVIRONMENT", "staging"):
            _run({"aws_observe": _observer([], captured=captured)})

    assert len(captured) == 1, "the named environment must have been observed"
    assert captured[0]["spec"].env == "staging", (
        f"MOJO_ENVIRONMENT named staging, but {captured[0]['spec'].env!r} was "
        f"observed"
    )


@th.django_unit_test("MOJO_ENVIRONMENT naming a file that is not there is pending")
def test_selected_environment_missing_pends(opts):
    factory = _ExplodingFactory()
    with _environments(_answers(env="prod")):
        with _override_setting("MOJO_ENVIRONMENT", "qa"):
            section = _run({"aws_client_factory": factory})

    row = _checks(section)[f"{SECTION}.environment"]
    assert factory.calls == 0, (
        "a named environment with no file must not fall back to an AWS call"
    )
    assert row["status"] == "pending", (
        f"a missing named environment is pending, got {row['status']!r}"
    )
    assert "qa" in row["explanation"], (
        f"the explanation must name the environment that is missing, got "
        f"{row['explanation']!r}"
    )


# ── finding → readiness mapping ─────────────────────────────────────────────











# ── containment ─────────────────────────────────────────────────────────────





# ── the external-mode gate ──────────────────────────────────────────────────

@th.django_unit_test("external mode warns on the mode row and names the setting")
def test_external_mode_row(opts):
    from mojo.helpers import infrastructure

    with _environments():
        with _override_setting("INFRASTRUCTURE_MODE", "external"):
            section = _run({})
        managed = _run({})

    external_row = _checks(section)[f"{SECTION}.mode"]
    assert external_row["status"] == "warn", (
        f"external mode must warn on the mode row, got "
        f"{external_row['status']!r}"
    )
    assert infrastructure.SETTING in external_row["explanation"], (
        f"the mode row must name {infrastructure.SETTING}, got "
        f"{external_row['explanation']!r}"
    )
    assert external_row["fixable"] is False, (
        "the mode row must never be fixable — a fixer on this section hard-fails "
        "every Fix-all run on an external install"
    )
    assert _checks(managed)[f"{SECTION}.mode"]["status"] == "pass", (
        "a managed installation's mode row must be pass"
    )


@th.django_unit_test("refuse_external is the backstop: silent when managed, raising when external")
def test_refuse_external(opts):
    from mojo.apps.account.services import system_readiness
    from mojo.apps.aws.services import infra_setup

    assert infra_setup.refuse_external("Infrastructure repair") is None, (
        "a managed installation must not be refused"
    )
    with _override_setting("INFRASTRUCTURE_MODE", "external"):
        with th.assert_raises(system_readiness.DefinitiveSetupFailure) as caught:
            infra_setup.refuse_external("Infrastructure repair")
    assert "INFRASTRUCTURE_MODE" in str(caught.exception), (
        f"the refusal must name the setting, got {str(caught.exception)!r}"
    )


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






# ── the registry ────────────────────────────────────────────────────────────

