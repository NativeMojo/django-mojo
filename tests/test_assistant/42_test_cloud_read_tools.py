"""Cloud read handlers that need no provider call.

Three things are under test, all of them boundaries rather than plumbing:

* the actor shim really carries authority — a section the caller may not read
  comes back `unauthorized`, per section, not as a blanket refusal;
* `sections` narrows WORK, not just output: naming one section must collect
  one, because the value of requiring it is that a chat turn never pays for the
  ten-section fan-out;
* the two System Setup reads refuse anything that is not provably an
  interactive superuser session, and refuse by RETURNING — a raise would be
  reported to the operator as "the tool encountered an internal error", which
  is the wrong report for "you are not a superuser".
"""

import objict

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


PLATFORM_EMAIL = "cloud-reads-platform@example.com"
PLAIN_EMAIL = "cloud-reads-plain@example.com"
NOBODY_EMAIL = "cloud-reads-nobody@example.com"
ROOT_EMAIL = "cloud-reads-root@example.com"
TEST_PASSWORD = "TestPass1!"


def _handler(name):
    from mojo.apps.assistant import get_registry

    return get_registry()[name]["handler"]


def _meta(bearer="bearer", key_backed=False):
    return objict.objict(ip="assistant", user_agent="", path="/assistant/cloud",
                         method="POST", bearer=bearer, key_backed=key_backed)


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_cloud_reads(opts):
    from mojo.apps.account.models import User

    User.objects.filter(
        email__in=[PLATFORM_EMAIL, PLAIN_EMAIL, NOBODY_EMAIL,
                   ROOT_EMAIL]).delete()

    def make(email, perms, superuser=False):
        user = User.objects.create_user(
            username=email, email=email, password=TEST_PASSWORD)
        user.is_email_verified = True
        if superuser:
            user.is_superuser = True
        user.save()
        for perm in perms:
            user.add_permission(perm)
        return user

    # view_platform ONLY — deliberately no view_platform_security.
    opts.platform = make(PLATFORM_EMAIL,
                         ["view_admin", "assistant", "view_platform"])
    # No platform grant at all, but holds `admin` — enough to be OFFERED the
    # setup reads' permission, not enough to pass their superuser check.
    opts.plain = make(PLAIN_EMAIL, ["view_admin", "assistant", "admin"])
    # No platform-tier grant of any kind — `admin` itself satisfies every
    # section tuple, so proving "no grant reads nothing" needs a user without it.
    opts.nobody = make(NOBODY_EMAIL, ["view_admin", "assistant"])
    opts.root = make(ROOT_EMAIL, ["view_admin", "assistant"], superuser=True)


# ---------------------------------------------------------------------------
# The actor shim carries authority, per section
# ---------------------------------------------------------------------------

@th.django_unit_test("naming one section collects exactly that section")
def test_sections_narrow_the_work(opts):
    result = _handler("get_platform_overview")({"sections": ["database"]},
                                               opts.platform)
    assert_eq(result["requested"], ["database"],
              f"the tool did not record what it was asked for: {result}")
    assert_eq(sorted(result["sections"]), ["database"],
              f"one named section must collect ONE section, got "
              f"{sorted(result['sections'])} — the sections filter is what "
              f"keeps a chat turn off the ten-section fan-out")


@th.django_unit_test("a section the caller may not read comes back unauthorized")
def test_sections_keep_their_own_permission(opts):
    result = _handler("get_platform_overview")(
        {"sections": ["database", "security"]}, opts.platform)
    assert_eq(result["sections"]["security"]["status"], "unauthorized",
              f"a view_platform-only caller read the security section: "
              f"{result['sections']['security']}")
    assert_true(result["sections"]["database"]["status"] != "unauthorized",
                f"the caller's OWN section was refused: "
                f"{result['sections']['database']}")


@th.django_unit_test("a caller with no platform grant gets unauthorized for every section")
def test_no_platform_grant_reads_nothing(opts):
    result = _handler("get_platform_overview")(
        {"sections": ["database", "redis"]}, opts.nobody)
    for name, envelope in result["sections"].items():
        assert_eq(envelope["status"], "unauthorized",
                  f"a caller with no platform grant read '{name}': {envelope}")


@th.django_unit_test("get_platform_overview refuses an empty or over-long section list")
def test_sections_are_bounded(opts):
    empty = _handler("get_platform_overview")({"sections": []}, opts.platform)
    assert_eq(empty.get("error_code"), "invalid_request",
              f"an empty sections list must be refused, got {empty}")
    too_many = _handler("get_platform_overview")(
        {"sections": ["api", "fleet", "jobs", "database", "redis"]},
        opts.platform)
    assert_eq(too_many.get("error_code"), "invalid_request",
              f"five sections must be refused, got {too_many}")
    unknown = _handler("get_platform_overview")({"sections": ["nope"]},
                                                opts.platform)
    assert_eq(unknown.get("error_code"), "invalid_request",
              f"a section list naming nothing known must be refused, got {unknown}")


@th.django_unit_test("the framework status read reports the same facts the Admin shows")
def test_framework_status(opts):
    result = _handler("get_framework_status")({}, opts.root)
    for key in ("installed", "latest", "can_update", "blocked_reason", "pin"):
        assert_true(key in result, f"the framework read dropped '{key}': {result}")
    assert_true("error" not in result,
                f"a superuser's framework read failed: {result}")


# ---------------------------------------------------------------------------
# System Setup reads: superuser AND an interactive session
# ---------------------------------------------------------------------------

@th.django_unit_test("the setup reads refuse a non-superuser by returning, never raising")
def test_setup_reads_refuse_non_superuser(opts):
    for name, params in (("get_setup_readiness", {"section": "django"}),
                         ("get_setup_operation", {})):
        result = _handler(name)(params, opts.plain, request_meta=_meta())
        assert_eq(result.get("error_code"), "permission_denied",
                  f"{name} did not refuse a non-superuser holding 'admin': {result}")
        assert_true(isinstance(result.get("error"), str) and result["error"],
                    f"{name}'s refusal carries no operator-readable sentence: {result}")


@th.django_unit_test("the setup reads refuse a missing, non-bearer or key-backed session")
def test_setup_reads_require_an_interactive_session(opts):
    cases = {
        "no request_meta at all": None,
        "an api key": _meta(bearer="apikey", key_backed=True),
        "a group token": _meta(bearer="grouptoken", key_backed=True),
        "a bearer that is somehow key-backed": _meta(bearer="bearer",
                                                     key_backed=True),
    }
    for name, params in (("get_setup_readiness", {"section": "django"}),
                         ("get_setup_operation", {})):
        for label, meta in cases.items():
            result = _handler(name)(params, opts.root, request_meta=meta)
            assert_eq(result.get("error_code"), "interactive_session_required",
                      f"{name} did not refuse {label}: {result}")


@th.django_unit_test("an unknown readiness section is refused with the real list")
def test_setup_readiness_unknown_section(opts):
    from mojo.apps.account.services import system_readiness

    result = _handler("get_setup_readiness")(
        {"section": "not-a-section"}, opts.root, request_meta=_meta())
    assert_eq(result.get("error_code"), "invalid_request",
              f"an unknown readiness section was not refused: {result}")
    codes = [entry["code"] for entry in system_readiness.sections()]
    if codes:
        assert_true(codes[0] in result["error"],
                    f"the refusal must name the sections this installation has: "
                    f"{result['error']}")


@th.django_unit_test("a port-80 local probe is reported unavailable, not failing")
def test_local_probe_note(opts):
    from mojo.apps.assistant.services.tools.cloud.reads import local_probe_note

    fallback = local_probe_note("default_80")
    assert_eq(fallback["status"], "unavailable",
              f"a port-80 fallback probe must not be reported as a failure the "
              f"Admin would never show: {fallback}")
    assert_true("SYSTEM_SETUP_LOCAL_API_URL" in fallback["message"],
                f"the note must name the setting that makes it real evidence: "
                f"{fallback}")
    configured = local_probe_note("configured_static")
    assert_eq(configured["status"], "attempted",
              f"a configured probe must be reported as attempted: {configured}")


@th.django_unit_test("no active fix operation is an answer, not an error")
def test_setup_operation_without_an_active_fix(opts):
    from mojo.apps.account.models import SystemSetupOperation

    if SystemSetupOperation.objects.filter(
            mode="fix", status__in=SystemSetupOperation.ACTIVE_STATUSES).exists():
        # Another module owns that row; the branch under test is unreachable
        # without deleting somebody else's data, which this must never do.
        return
    result = _handler("get_setup_operation")({}, opts.root, request_meta=_meta())
    assert_eq(result.get("active"), False,
              f"no active fix operation must answer active=false, got {result}")
    assert_true("error" not in result,
                f"'nothing is running' is not an error: {result}")


@th.django_unit_test("an unknown setup operation id is refused, not guessed")
def test_setup_operation_unknown_id(opts):
    for invented in ("99999999", "not-a-uuid",
                     "0f8fad5b-d9cb-469f-a165-70867728950e"):
        result = _handler("get_setup_operation")(
            {"operation": invented}, opts.root, request_meta=_meta())
        assert_eq(result.get("error_code"), "unknown_resource",
                  f"the invented id {invented!r} was not refused cleanly: {result}")


# ---------------------------------------------------------------------------
# Recorded drift
# ---------------------------------------------------------------------------

@th.django_unit_test("recorded drift is projected from the event, never scanned")
def test_version_drift_projection(opts):
    from datetime import datetime, timezone as dt_timezone

    from mojo.apps.assistant.services.tools.cloud.reads import project_drift

    recorded = project_drift({
        "created": datetime(2026, 8, 20, 12, 0, tzinfo=dt_timezone.utc),
        "metadata": {"region": "us-east-1", "findings": [{
            "kind": "rds-instance", "resource_id": "mojo-db",
            "engine": "postgres", "current_version": "14.9",
            "available_major": "16", "deadline": "2027-02-01",
            "days_remaining": 160, "note": "standard support ends",
            # Not a drift field, and must not be carried through blindly.
            "secret": "leak-me",
        }]},
    })
    assert_eq(recorded["status"], "recorded",
              f"a recorded scan must report as recorded: {recorded}")
    finding = recorded["findings"][0]
    assert_eq(finding["resource"], "mojo-db",
              f"the finding's resource was lost: {finding}")
    assert_eq(finding["available_major"], "16",
              f"the offered target was lost: {finding}")
    assert_true("secret" not in finding,
                f"the projection is an allowlist, not a passthrough: {finding}")
    assert_eq(recorded["recorded_at"], "2026-08-20T12:00:00+00:00",
              f"the projection lost when the evidence was recorded: {recorded}")

    empty = project_drift(None)
    assert_eq(empty["status"], "no_recent_scan",
              f"no in-window event must say so plainly: {empty}")
    assert_eq(empty["findings"], [],
              f"no evidence must not be reported as findings: {empty}")
    assert_true("error" not in empty,
                f"'nothing recorded' is not an error: {empty}")
