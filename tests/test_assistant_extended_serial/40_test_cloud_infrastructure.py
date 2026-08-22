"""Cloud tools under external infrastructure, plus the idempotency key.

Opt-in and serial (maestro item #1839) for two independent reasons: external
mode is read from the shared settings singleton through `settings.get_static`,
and the retry handler calls `deploy.request_deploy`, which writes the shared
deploy target key. Neither is safe beside the parallel default tier.

The rule under test is the epic's: a tool is unavailable exactly where its
Admin twin is unavailable. `infrastructure.refuse()` is the first statement in
framework update, maintenance apply and every capacity write — and is ABSENT
from deploy retry/verify/converge, which therefore stay available on an
installation whose estate is applied by external IaC.
"""

from unittest import mock

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


ROOT_EMAIL = "cloud-infra-root@example.com"
AWS_EMAIL = "cloud-infra-aws@example.com"
TEST_PASSWORD = "TestPass1!"
SOURCE = "assistant-cloud-test-infra"
SHA = "e" * 40

MANAGED_ONLY_TOOLS = ("apply_framework_update", "apply_managed_upgrade",
                      "apply_capacity_change", "apply_capacity_plan")
DEPLOY_TOOLS = ("retry_platform_deployment", "verify_platform_deployment",
                "converge_platform_deployment")


def _external_mode():
    """Patch the file-only INFRASTRUCTURE_MODE read to say `external`."""
    from mojo.helpers.settings import settings

    original = settings.get_static

    def get_static(name, *args, **kwargs):
        if name == "INFRASTRUCTURE_MODE":
            return "external"
        return original(name, *args, **kwargs)

    return mock.patch.object(settings, "get_static", side_effect=get_static)


def _clear_deploy_target():
    from mojo.apps.edge.services import deploy

    deploy.get_client().delete(deploy.TARGET_KEY, deploy.STATUS_KEY)


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_cloud_infrastructure(opts):
    from mojo.apps.account.models import User
    from mojo.apps.edge.models import PlatformDeployment

    PlatformDeployment.objects.filter(source__in=[SOURCE, "admin_retry"]).delete()
    _clear_deploy_target()
    User.objects.filter(email__in=[ROOT_EMAIL, AWS_EMAIL]).delete()

    opts.root = User.objects.create_user(
        username=ROOT_EMAIL, email=ROOT_EMAIL, password=TEST_PASSWORD)
    opts.root.is_email_verified = True
    opts.root.is_superuser = True
    opts.root.save()
    for perm in ["view_admin", "assistant"]:
        opts.root.add_permission(perm)

    opts.aws = User.objects.create_user(
        username=AWS_EMAIL, email=AWS_EMAIL, password=TEST_PASSWORD)
    opts.aws.is_email_verified = True
    opts.aws.save()
    for perm in ["view_admin", "assistant", "manage_aws"]:
        opts.aws.add_permission(perm)


def _conversation(opts, title, user=None):
    from mojo.apps.assistant.models import Conversation

    owner = user or opts.root
    Conversation.objects.filter(user=owner, title=title).delete()
    return Conversation.objects.create(user=owner, title=title)


def _cleanup(conversation):
    from mojo.apps.assistant.models import Message, PendingAction

    PendingAction.objects.filter(conversation=conversation).delete()
    Message.objects.filter(conversation=conversation).delete()
    conversation.delete()


# ---------------------------------------------------------------------------
# External mode
# ---------------------------------------------------------------------------

@th.django_unit_test("external infrastructure hides exactly the four gated cloud tools")
def test_external_mode_hides_the_gated_tools(opts):
    from mojo.apps.assistant import (
        get_available_domains, get_domain_tools_for_user, get_tools_for_user,
    )

    managed = {t["name"] for t in get_domain_tools_for_user(opts.root, ["cloud"])}
    for name in MANAGED_ONLY_TOOLS:
        assert_true(name in managed,
                    f"{name} must be visible on a managed installation")

    with _external_mode():
        domain_names = {t["name"] for t in
                        get_domain_tools_for_user(opts.root, ["cloud"])}
        every = {t["name"] for t in get_tools_for_user(opts.root)}
        listed = set(get_available_domains(opts.root).get("cloud", {}).get(
            "tools", []))

    for name in MANAGED_ONLY_TOOLS:
        assert_true(name not in domain_names,
                    f"external mode still offered {name} in the domain listing")
        assert_true(name not in every,
                    f"external mode still offered {name} in the full listing")
        assert_true(name not in listed,
                    f"external mode still offered {name} in get_available_domains")

    for name in DEPLOY_TOOLS:
        assert_true(name in domain_names,
                    f"{name} vanished under external mode, but its endpoint "
                    f"does not call infrastructure.refuse() — the tool must "
                    f"stay exactly as available as the Admin control")
    assert_true("get_fleet_capacity" in domain_names,
                f"the cloud READS must survive external mode: {sorted(domain_names)}")


@th.django_unit_test("external mode refuses a gated cloud proposal with no record")
def test_external_mode_refuses_the_proposal(opts):
    from mojo.apps.assistant import get_registry
    from mojo.apps.assistant.models import PendingAction
    from mojo.apps.assistant.services import approvals

    conv = _conversation(opts, "cloud-infra-proposal")
    try:
        entry = get_registry()["apply_framework_update"]
        with _external_mode():
            result, block = approvals.propose(
                opts.root, conv, "apply_framework_update", entry,
                {"version": "9.9.9"})
        assert_true(block is None,
                    "external mode created an approval card for a gated tool")
        assert_true("error" in result,
                    f"the refusal must reach the model as a tool error: {result}")
        assert_eq(PendingAction.objects.filter(conversation=conv).count(), 0,
                  "a refused proposal must leave no record behind")
    finally:
        _cleanup(conv)


@th.django_unit_test("external mode blocks every capacity offer while still "
                     "reporting the fleet")
def test_capacity_report_under_external_mode(opts):
    from mojo.apps.aws.services import capacity
    from mojo.apps.assistant.services.tools.cloud.reads import project_capacity

    empty = mock.Mock()
    empty.describe_load_balancers.return_value = {"LoadBalancers": []}
    empty.describe_target_groups.return_value = {"TargetGroups": []}
    empty.describe_instances.return_value = {"Reservations": []}
    empty.describe_addresses.return_value = {"Addresses": []}
    empty.describe_db_clusters.return_value = {"DBClusters": []}
    empty.describe_db_instances.return_value = {"DBInstances": []}
    empty.describe_replication_groups.return_value = {"ReplicationGroups": []}

    with _external_mode():
        # Injected clients: no provider call and no shared cache write.
        envelope = capacity.report(
            elbv2_client=empty, ec2_client=empty, rds_client=empty,
            cache_client=empty)
    projected = project_capacity(envelope)

    assert_eq(projected["mode"], "external",
              f"the report must say which kind of installation this is: "
              f"{projected['mode']}")
    offers = projected["actions"]
    assert_true(offers, "the report carried no actions map at all")
    for action, offer in offers.items():
        assert_eq(offer["offered"], False,
                  f"external mode still offered '{action}': {offer}")
        assert_eq(offer["blocked_reason"], "infrastructure_external",
                  f"'{action}' is blocked for the wrong reason: {offer}")


@th.django_unit_test("apply_managed_upgrade is refused for a manage_aws-only caller")
def test_manage_tier_refused_at_proposal(opts):
    from mojo.apps.assistant import get_registry, user_can_use_tool
    from mojo.apps.assistant.models import PendingAction
    from mojo.apps.assistant.services.agent import _execute_tool

    conv = _conversation(opts, "cloud-infra-tier", user=opts.aws)
    try:
        entry = get_registry()["apply_managed_upgrade"]
        assert_eq(user_can_use_tool(opts.aws, entry), False,
                  "a manage_aws-only caller passed the maintenance tier check")
        result = _execute_tool(
            {"type": "tool_use", "id": "fixture-tier",
             "name": "apply_managed_upgrade",
             "input": {"kind": "rds-instance", "resource": "mojo-db",
                       "target_version": "16", "apply_immediately": False}},
            get_registry(), opts.aws, conv, [], None, [], pending_actions=[])
        import ujson
        payload = ujson.loads(result["content"])
        assert_true("Permission denied" in payload.get("error", ""),
                    f"a manage_aws-only caller was not refused at dispatch: "
                    f"{payload}")
        assert_eq(PendingAction.objects.filter(conversation=conv).count(), 0,
                  "a refused dispatch must leave no approval record")
    finally:
        _cleanup(conv)


# ---------------------------------------------------------------------------
# The idempotency key
# ---------------------------------------------------------------------------

@th.django_unit_test("the approval id is the retry's idempotency key, so a "
                     "replayed execution creates ONE deployment")
def test_retry_is_idempotent_on_the_approval_id(opts):
    import uuid as uuid_module

    from mojo.apps import jobs
    from mojo.apps.assistant import get_registry
    from mojo.apps.edge.models import PlatformDeployment

    _clear_deploy_target()
    PlatformDeployment.objects.filter(source__in=[SOURCE, "admin_retry"]).delete()
    try:
        row = PlatformDeployment.objects.create(
            sha=SHA, source=SOURCE, actor="test",
            status=PlatformDeployment.STATUS_FAILED,
            framework_version="1.15.15",
            frozen_roster=["mojo-api-a-engine"])
        approval = mock.Mock(uuid=uuid_module.uuid4())
        handler = get_registry()["retry_platform_deployment"]["handler"]

        # A deploy freezes the live edge roster, and this environment has no
        # running edge runners — without one, create() fails the attempt closed
        # with `roster_unavailable` and the idempotency path is never reached.
        # Patching it here is exactly why this module is opt-in and serial.
        from mojo.apps.edge.services import platform_deploy

        with mock.patch.object(platform_deploy, "edge_roster",
                               return_value=["mojo-api-a-engine"]), \
                mock.patch.object(jobs, "publish", return_value=None):
            first = handler({"deployment": str(row.pk)}, opts.root,
                            approval=approval)
            second = handler({"deployment": str(row.pk)}, opts.root,
                             approval=approval)

        for label, result in (("first", first), ("second", second)):
            assert_true("error" not in result,
                        f"the {label} retry failed: {result}")
        made = PlatformDeployment.objects.filter(source="admin_retry")
        assert_eq(made.count(), 1,
                  f"the same approval ran twice and created {made.count()} "
                  f"deployments — str(approval.uuid) is not reaching "
                  f"deploy.request_deploy(idempotency_key=...)")
        assert_eq(first["deployment"]["id"], second["deployment"]["id"],
                  "the replay returned a different deployment")
        assert_true("stderr_tail" not in str(first),
                    "the retry result carried deploy stderr")
        assert_true("retried automatically" in first["reconciliation"],
                    f"the result must state the reconciliation posture: "
                    f"{first.get('reconciliation')}")
    finally:
        PlatformDeployment.objects.filter(
            source__in=[SOURCE, "admin_retry"]).delete()
        _clear_deploy_target()
