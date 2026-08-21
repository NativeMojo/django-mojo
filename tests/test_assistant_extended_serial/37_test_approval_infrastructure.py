"""Infrastructure mode and the approval TTL setting.

Opt-in and serial (maestro item #1839): both subjects are read from the shared
settings singleton — `INFRASTRUCTURE_MODE` through `settings.get_static` and
`LLM_ADMIN_APPROVAL_TTL` through `settings.get` — so exercising them means
patching a process-wide surface, which is unsafe under the parallel default tier.

The rule under test is the epic's: a tool is unavailable exactly where its Admin
twin is unavailable. On an installation whose infrastructure is declared by an
external IaC pipeline, a `requires_managed_infrastructure` tool is not merely
discouraged — it is invisible to the model, refused at proposal, and refused at
execution even for a record proposed while the installation was still managed.
"""
from unittest import mock

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


TEST_EMAIL = "approval-infra-admin@example.com"
TEST_PASSWORD = "TestPass1!"
TEST_PERM = "testit_infra_approvals"
TEST_DOMAIN = "testit_infra_approvals"

CALLS = []


def _tool_infra(params, user, approval=None):
    CALLS.append({"params": dict(params)})
    return {"ok": True}


def _tool_plain(params, user, approval=None):
    CALLS.append({"params": dict(params), "plain": True})
    return {"ok": True}


_SCHEMA = {
    "type": "object",
    "properties": {"target": {"type": "string"}},
    "required": ["target"],
}


def _register_infra_tools():
    from mojo.apps.assistant import get_registry, register_tool

    if "testit_infra_approval_managed" in get_registry():
        return
    register_tool(
        name="testit_infra_approval_managed",
        description="Infra fixture — managed infrastructure only",
        input_schema=_SCHEMA, handler=_tool_infra, permission=TEST_PERM,
        mutates=True, domain=TEST_DOMAIN, core=False,
        requires_managed_infrastructure=True,
    )
    register_tool(
        name="testit_infra_approval_plain",
        description="Infra fixture — ordinary mutating tool",
        input_schema=_SCHEMA, handler=_tool_plain, permission=TEST_PERM,
        mutates=True, domain=TEST_DOMAIN, core=False,
    )


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_approval_infrastructure(opts):
    from mojo.apps.account.models import User

    _register_infra_tools()
    User.objects.filter(email=TEST_EMAIL).delete()
    opts.admin = User.objects.create_user(
        username=TEST_EMAIL, email=TEST_EMAIL, password=TEST_PASSWORD)
    opts.admin.is_email_verified = True
    opts.admin.save()
    for perm in ["view_admin", "assistant", TEST_PERM]:
        opts.admin.add_permission(perm)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _conversation(opts, title):
    from mojo.apps.assistant.models import Conversation

    Conversation.objects.filter(user=opts.admin, title=title).delete()
    return Conversation.objects.create(user=opts.admin, title=title)


def _propose(opts, conversation, tool_name):
    from mojo.apps.assistant import get_registry
    from mojo.apps.assistant.services import approvals

    entry = get_registry()[tool_name]
    return approvals.propose(opts.admin, conversation, tool_name, entry,
                             {"target": "alpha"})


def _external_mode():
    """Patch the file-only INFRASTRUCTURE_MODE read to say `external`."""
    from mojo.helpers.settings import settings

    original = settings.get_static

    def get_static(name, *args, **kwargs):
        if name == "INFRASTRUCTURE_MODE":
            return "external"
        return original(name, *args, **kwargs)

    return mock.patch.object(settings, "get_static", side_effect=get_static)


def _ttl(value):
    from mojo.helpers.settings import settings

    original = settings.get

    def get(name, *args, **kwargs):
        if name == "LLM_ADMIN_APPROVAL_TTL":
            return value
        return original(name, *args, **kwargs)

    return mock.patch.object(settings, "get", side_effect=get)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@th.django_unit_test("external infrastructure hides a managed-infrastructure tool")
def test_external_mode_hides_the_tool(opts):
    from mojo.apps.assistant import (
        get_available_domains, get_domain_tools_for_user, get_tools_for_user,
    )

    del CALLS[:]
    managed_names = {t["name"] for t in get_tools_for_user(opts.admin)}
    assert_true("testit_infra_approval_managed" in managed_names,
                "the tool must be visible on a managed installation")

    with _external_mode():
        names = {t["name"] for t in get_tools_for_user(opts.admin)}
        domain_names = {t["name"] for t in
                        get_domain_tools_for_user(opts.admin, [TEST_DOMAIN])}
        listed = get_available_domains(opts.admin).get(TEST_DOMAIN, {}).get("tools", [])

    assert_true("testit_infra_approval_managed" not in names,
                f"external mode must hide the tool from get_tools_for_user, got {names}")
    assert_true("testit_infra_approval_managed" not in domain_names,
                "external mode must hide the tool from get_domain_tools_for_user")
    assert_true("testit_infra_approval_managed" not in listed,
                f"external mode must hide the tool from its domain listing, got {listed}")
    assert_true("testit_infra_approval_plain" in names,
                "external mode must not hide ordinary mutating tools")


@th.django_unit_test("external infrastructure refuses the proposal with no record")
def test_external_mode_refuses_proposal(opts):
    from mojo.apps.assistant.models import PendingAction

    del CALLS[:]
    conv = _conversation(opts, "infra-proposal")
    with _external_mode():
        payload, block = _propose(opts, conv, "testit_infra_approval_managed")

    assert_true(block is None,
                "external mode must produce no approval card")
    assert_true("error" in payload,
                f"external mode must return an ordinary tool error, got {payload}")
    assert_true("INFRASTRUCTURE_MODE" in payload["error"],
                f"the refusal must name the switch, got {payload['error']}")
    assert_eq(PendingAction.objects.filter(conversation=conv).count(), 0,
              "external mode must create no approval record")
    assert_eq(len(CALLS), 0, "external mode must never reach the handler")


@th.django_unit_test("a mode flip after proposal refuses at execution")
def test_external_mode_refuses_execution(opts):
    from mojo.apps.assistant.services import approvals

    del CALLS[:]
    conv = _conversation(opts, "infra-execution")
    _payload, block = _propose(opts, conv, "testit_infra_approval_managed")
    assert_true(block is not None,
                "the proposal must succeed while the installation is managed")

    refusal = None
    with _external_mode():
        try:
            approvals.resolve(opts.admin, block["action_id"], "approve")
        except approvals.ApprovalRefused as exc:
            refusal = exc

    assert_true(refusal is not None, "a mode flip must refuse the pending action")
    assert_eq(refusal.code, approvals.CODE_INFRASTRUCTURE,
              f"expected infrastructure_external, got {refusal.code}")
    assert_eq(len(CALLS), 0, "a mode flip must never reach the handler")

    # The refusal is about the installation, not the operator — the record stays
    # approvable if the installation goes back to managed.
    result = approvals.resolve(opts.admin, block["action_id"], "approve")
    assert_eq(result["block"]["state"], "completed",
              f"the action must still resolve once managed again, got {result['block']}")
    assert_eq(len(CALLS), 1, f"the handler must run exactly once, ran {len(CALLS)}")


@th.django_unit_test("LLM_ADMIN_APPROVAL_TTL is honoured and clamped")
def test_approval_ttl_setting(opts):
    from mojo.apps.assistant.services import approvals

    with _ttl(900):
        assert_eq(approvals.ttl_seconds(), 900,
                  "a value inside the range must be honoured")
    with _ttl(5):
        assert_eq(approvals.ttl_seconds(), approvals.MIN_TTL_SECONDS,
                  "a value below the floor must clamp up")
    with _ttl(99999):
        assert_eq(approvals.ttl_seconds(), approvals.MAX_TTL_SECONDS,
                  "a value above the ceiling must clamp down")
    with _ttl("nonsense"):
        assert_eq(approvals.ttl_seconds(), approvals.DEFAULT_TTL_SECONDS,
                  "an unparseable value must fall back to the default")


@th.django_unit_test("the configured TTL is what a new record expires on")
def test_ttl_applies_to_a_new_record(opts):
    from mojo.helpers import dates
    from mojo.apps.assistant.models import PendingAction

    del CALLS[:]
    conv = _conversation(opts, "infra-ttl")
    with _ttl(120):
        _payload, block = _propose(opts, conv, "testit_infra_approval_plain")

    row = PendingAction.objects.filter(conversation=conv).first()
    window = (row.expires_at - dates.utcnow()).total_seconds()
    assert_true(100 <= window <= 120,
                f"a 120s TTL must expire the record in ~120s, got {window:.0f}s")
