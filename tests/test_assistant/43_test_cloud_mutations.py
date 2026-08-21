"""The deploy tools' fail-closed pre-flight, driven through the real gate.

`preview` raising is the mechanism: at proposal it refuses as an ordinary tool
error with NO record created. So every case here goes through
`approvals.propose` rather than calling the preview directly — a check that is
only correct when called directly is not a gate.

Deliberately provider-free: nothing here reaches AWS, and nothing calls
`deploy.request_deploy`, which writes the shared deploy target key (that
coverage lives in the opt-in serial module). Rows are created with a
module-unique `source` and only those rows are deleted — tests/test_edge owns
this table too.
"""

import uuid as uuid_module

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


TEST_EMAIL = "cloud-mutations-platform@example.com"
TEST_PASSWORD = "TestPass1!"
SOURCE = "assistant-cloud-test-mutations"
SHA = "d" * 40

DEPLOY_TOOLS = ("retry_platform_deployment", "verify_platform_deployment",
                "converge_platform_deployment")


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_cloud_mutations(opts):
    from mojo.apps.account.models import User
    from mojo.apps.edge.models import PlatformDeployment

    PlatformDeployment.objects.filter(source=SOURCE).delete()
    User.objects.filter(email=TEST_EMAIL).delete()
    opts.admin = User.objects.create_user(
        username=TEST_EMAIL, email=TEST_EMAIL, password=TEST_PASSWORD)
    opts.admin.is_email_verified = True
    opts.admin.save()
    for perm in ["view_admin", "assistant", "manage_platform"]:
        opts.admin.add_permission(perm)


def _row(status):
    from mojo.apps.edge.models import PlatformDeployment

    PlatformDeployment.objects.filter(source=SOURCE).delete()
    return PlatformDeployment.objects.create(
        sha=SHA, source=SOURCE, actor="test", status=status,
        framework_version="1.15.15",
        frozen_roster=["mojo-api-a-engine", "mojo-api-b-engine"])


def _conversation(opts, title):
    from mojo.apps.assistant.models import Conversation

    Conversation.objects.filter(user=opts.admin, title=title).delete()
    return Conversation.objects.create(user=opts.admin, title=title)


def _propose(opts, conversation, tool_name, deployment):
    from mojo.apps.assistant import get_registry
    from mojo.apps.assistant.services import approvals

    entry = get_registry()[tool_name]
    return approvals.propose(opts.admin, conversation, tool_name, entry,
                             {"deployment": str(deployment)})


def _cleanup(opts, conversation):
    from mojo.apps.assistant.models import Message, PendingAction
    from mojo.apps.edge.models import PlatformDeployment

    PendingAction.objects.filter(conversation=conversation).delete()
    Message.objects.filter(conversation=conversation).delete()
    conversation.delete()
    PlatformDeployment.objects.filter(source=SOURCE).delete()


# ---------------------------------------------------------------------------

@th.django_unit_test("a deployment id the model invented creates no approval record")
def test_unknown_deployment_refuses_with_no_record(opts):
    from mojo.apps.assistant.models import PendingAction

    conv = _conversation(opts, "cloud-unknown-deployment")
    try:
        for tool_name in DEPLOY_TOOLS:
            for invented in (str(uuid_module.uuid4()), "not-a-uuid"):
                result, block = _propose(opts, conv, tool_name, invented)
                assert_true(block is None,
                            f"{tool_name} created an approval card for an "
                            f"unknown deployment {invented!r}")
                assert_true("not on record" in result.get("error", ""),
                            f"{tool_name} did not explain the refusal: {result}")
        assert_eq(PendingAction.objects.filter(conversation=conv).count(), 0,
                  "a refused proposal must leave no record behind")
    finally:
        _cleanup(opts, conv)


@th.django_unit_test("an attempt the orchestrator is still driving earns no control")
def test_active_attempt_refuses_every_control(opts):
    from mojo.apps.assistant.models import PendingAction

    conv = _conversation(opts, "cloud-active-deployment")
    try:
        for status in ("requested", "canary", "fleet"):
            row = _row(status)
            for tool_name in DEPLOY_TOOLS:
                result, block = _propose(opts, conv, tool_name, row.pk)
                assert_true(block is None,
                            f"{tool_name} offered a control on a {status} "
                            f"attempt the orchestrator is driving")
                assert_true("orchestrator" in result.get("error", ""),
                            f"{tool_name}'s refusal for {status} does not say "
                            f"why: {result}")
        assert_eq(PendingAction.objects.filter(conversation=conv).count(), 0,
                  "an active attempt must leave no approval records")
    finally:
        _cleanup(opts, conv)


@th.django_unit_test("each control is offered exactly where the Admin offers it")
def test_controls_match_the_admin(opts):
    # status -> the tools that must PROPOSE, per platform_deploy.ACTIONS_BY_STATUS
    expected = {
        "failed": {"retry_platform_deployment", "verify_platform_deployment"},
        "verified": {"verify_platform_deployment",
                     "converge_platform_deployment"},
        "partial": {"verify_platform_deployment",
                    "converge_platform_deployment"},
        "unknown": {"verify_platform_deployment",
                    "converge_platform_deployment"},
        "converged": {"verify_platform_deployment"},
        "superseded": set(),
    }
    conv = _conversation(opts, "cloud-control-matrix")
    try:
        for status, allowed in expected.items():
            row = _row(status)
            for tool_name in DEPLOY_TOOLS:
                result, block = _propose(opts, conv, tool_name, row.pk)
                if tool_name in allowed:
                    assert_true(block is not None,
                                f"{tool_name} refused a {status} attempt the "
                                f"Admin offers it for: {result}")
                    assert_eq(block["state"], "pending",
                              f"{tool_name} on {status} did not propose: {block}")
                else:
                    assert_true(block is None,
                                f"{tool_name} was offered on a {status} "
                                f"attempt; the Admin does not offer it")
                    assert_true("does not offer" in result.get("error", "")
                                or "orchestrator" in result.get("error", ""),
                                f"{tool_name} on {status} refused without "
                                f"saying why: {result}")
    finally:
        _cleanup(opts, conv)


@th.django_unit_test("the bound revision moves when the attempt's status moves")
def test_revision_binds_the_status(opts):
    # Two conversations on purpose: an identical proposal inside ONE
    # conversation is deduped to the live card by design, which would hand back
    # the revision as PROPOSED rather than as it stands now. The stale card is
    # not a hole — resolving it re-runs preview and refuses precondition_failed
    # (covered by the approval gate's own tests); what is asserted here is that
    # this preview's revision actually tracks the status.
    first_conv = _conversation(opts, "cloud-revision-before")
    second_conv = _conversation(opts, "cloud-revision-after")
    try:
        row = _row("verified")
        _result, first = _propose(
            opts, first_conv, "verify_platform_deployment", row.pk)
        assert_true(first is not None, "the verified attempt did not propose")
        assert_eq(first["preview"]["revision"], f"{row.pk}:verified",
                  f"the revision must bind THIS attempt in THIS state: "
                  f"{first['preview']}")
        row.status = "converged"
        row.save()
        _result, second = _propose(
            opts, second_conv, "verify_platform_deployment", row.pk)
        assert_true(second is not None, "the converged attempt did not propose")
        assert_eq(second["preview"]["revision"], f"{row.pk}:converged",
                  f"the revision did not move with the status: "
                  f"{second['preview']['revision']}")
    finally:
        _cleanup(opts, first_conv)
        _cleanup(opts, second_conv)


@th.django_unit_test("a proposal explains the operation and never executes it")
def test_proposal_is_not_execution(opts):
    from mojo.apps.edge.models import PlatformDeployment

    conv = _conversation(opts, "cloud-proposal-shape")
    try:
        row = _row("failed")
        before = PlatformDeployment.objects.count()
        result, block = _propose(opts, conv, "retry_platform_deployment", row.pk)

        assert_eq(result.get("status"), "approval_required",
                  f"a mutating cloud tool must PROPOSE, not run: {result}")
        assert_eq(PlatformDeployment.objects.count(), before,
                  "proposing a retry created a deployment — the handler ran")
        assert_eq(block["requires_fresh_auth"], True,
                  f"the retry card must demand a step-up: {block}")
        assert_eq(block["requires_superuser"], False,
                  f"the deploy controls are not superuser-gated: {block}")
        assert_true(SHA[:7] in block["preview"]["summary"],
                    f"the plan sentence must name the commit: "
                    f"{block['preview']['summary']}")
        assert_true("does not undo" in block["preview"]["summary"],
                    f"the plan sentence must say a deploy is not undone: "
                    f"{block['preview']['summary']}")
        assert_eq(block["preview"]["details"]["node_summary"]["expected"], 2,
                  f"the card must name the frozen roster size: "
                  f"{block['preview']['details']}")
        assert_true("stderr_tail" not in str(block),
                    "a card carried deploy stderr")
    finally:
        _cleanup(opts, conv)


@th.django_unit_test("the verify and converge cards say what they read and publish")
def test_verify_and_converge_summaries(opts):
    conv = _conversation(opts, "cloud-verify-converge")
    try:
        row = _row("partial")
        _result, verify = _propose(opts, conv, "verify_platform_deployment", row.pk)
        assert_true("proof" in verify["preview"]["summary"],
                    f"verify's sentence must say it collects proof: "
                    f"{verify['preview']['summary']}")
        assert_true("changes no code" in verify["preview"]["summary"],
                    f"verify's sentence must say it changes nothing: "
                    f"{verify['preview']['summary']}")
        _result, converge = _propose(
            opts, conv, "converge_platform_deployment", row.pk)
        assert_true("does not choose a new commit"
                    in converge["preview"]["summary"],
                    f"converge's sentence must rule out a new commit: "
                    f"{converge['preview']['summary']}")
    finally:
        _cleanup(opts, conv)
