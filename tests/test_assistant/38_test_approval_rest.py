"""Approval resolution over REST — POST/GET /api/assistant/action.

`opts.client` reaches a SEPARATE server process that does not import `tests/`,
so these bind to genuinely registered tools: `block_ip` (mutating, no step-up)
and `update_user_permission` (mutating, `fresh_auth_seconds=600`). The pending
records are proposed in-process through the real service and resolved over the
wire, which is exactly the split the protocol has in production.

The `X-Mojo-Test-Fresh-Auth-Window` header is inert here: the window comes from
the tool's own declaration, and `resolve_window` consults the header only when
`seconds is None`. The 440 test therefore mints a stale `auth_time` token, the
way tests/test_auth/fresh_auth.py does.
"""
import time

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


ADMIN_EMAIL = "approval-rest-admin@example.com"
OTHER_EMAIL = "approval-rest-other@example.com"
TARGET_EMAIL = "approval-rest-target@example.com"
PASSWORD = "TestPass1!"

TEST_IP = "198.51.100.71"
TEST_IP_CANCEL = "198.51.100.72"
KEY_NAME = "approval-rest-apikey"
GROUP_NAME = "approval-rest-group"
TARGET_PERM = "testit_rest_perm"


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_approval_rest(opts):
    from mojo.apps.account.models import ApiKey, GeoLocatedIP, Group, User

    User.objects.filter(email__in=[ADMIN_EMAIL, OTHER_EMAIL, TARGET_EMAIL]).delete()
    GeoLocatedIP.objects.filter(ip_address__in=[TEST_IP, TEST_IP_CANCEL]).delete()
    ApiKey.objects.filter(name=KEY_NAME).delete()
    Group.objects.filter(name=GROUP_NAME).delete()

    opts.admin = User.objects.create_user(
        username=ADMIN_EMAIL, email=ADMIN_EMAIL, password=PASSWORD)
    opts.admin.is_email_verified = True
    opts.admin.requires_mfa = False
    opts.admin.save()
    for perm in ["view_admin", "assistant", "view_security", "manage_security",
                 "users", "manage_users"]:
        opts.admin.add_permission(perm)
    opts.admin.get_auth_key()

    opts.other = User.objects.create_user(
        username=OTHER_EMAIL, email=OTHER_EMAIL, password=PASSWORD)
    opts.other.is_email_verified = True
    opts.other.requires_mfa = False
    opts.other.save()
    for perm in ["view_admin", "assistant", "view_security", "manage_security"]:
        opts.other.add_permission(perm)

    opts.target = User.objects.create_user(
        username=TARGET_EMAIL, email=TARGET_EMAIL, password=PASSWORD)
    opts.target.is_email_verified = True
    opts.target.save()

    group = Group.objects.create(name=GROUP_NAME, kind="organization")
    _api_key, raw_token = ApiKey.create_for_group(
        group=group, name=KEY_NAME,
        permissions={"view_admin": True, "assistant": True})
    opts.api_token = raw_token


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _conversation(opts, title, user=None):
    from mojo.apps.assistant.models import Conversation

    owner = user or opts.admin
    Conversation.objects.filter(user=owner, title=title).delete()
    return Conversation.objects.create(user=owner, title=title)


def _propose(user, conversation, tool_name, args):
    from mojo.apps.assistant import get_registry
    from mojo.apps.assistant.services import approvals

    entry = get_registry()[tool_name]
    _payload, block = approvals.propose(user, conversation, tool_name, entry, args)
    assert_true(block is not None,
                f"proposing {tool_name} must produce an approval card")
    return block


def _login(opts, email=ADMIN_EMAIL, password=PASSWORD):
    opts.client.logout()
    assert_true(opts.client.login(email, password), f"login as {email} should succeed")


def _resolve(opts, action_id, decision):
    return opts.client.post("/api/assistant/action",
                            {"action_id": action_id, "decision": decision})


def _assert_unavailable(resp, why):
    assert_eq(resp.status_code, 409,
              f"{why} must return 409, got {resp.status_code}: {resp.json}")
    assert_eq(resp.json.get("error_code"), "action_unavailable",
              f"{why} must carry error_code=action_unavailable, got {resp.json}")
    return (resp.status_code, resp.json.get("error"), resp.json.get("error_code"))


# ---------------------------------------------------------------------------
# Approve / cancel
# ---------------------------------------------------------------------------

@th.django_unit_test("approve over REST executes the tool and returns the resolved card")
def test_approve_executes(opts):
    from mojo.apps.account.models import GeoLocatedIP

    conv = _conversation(opts, "approval-rest-approve")
    block = _propose(opts.admin, conv, "block_ip",
                     {"ip": TEST_IP, "reason": "approval rest test", "ttl": 60})
    assert_eq(block["state"], "pending", "a fresh card must be pending")
    assert_true(GeoLocatedIP.objects.filter(ip_address=TEST_IP,
                                            is_blocked=True).first() is None,
                "the IP must NOT be blocked before the operator approves")

    _login(opts)
    resp = _resolve(opts, block["action_id"], "approve")
    opts.client.logout()

    assert_eq(resp.status_code, 200,
              f"approve should succeed, got {resp.status_code}: {resp.json}")
    action = resp.json.get("data", {}).get("action") or {}
    assert_eq(action.get("state"), "completed",
              f"the resolved card must be completed, got {action}")
    assert_eq(action.get("type"), "approval",
              f"the response must carry an approval block, got {action}")
    assert_true(resp.json.get("data", {}).get("message_id"),
                "approval must write a server-authored outcome message")

    geo = GeoLocatedIP.objects.filter(ip_address=TEST_IP).first()
    assert_true(geo is not None and geo.is_blocked,
                "the approved block_ip must actually have blocked the IP")


@th.django_unit_test("cancel over REST changes nothing")
def test_cancel_changes_nothing(opts):
    from mojo.apps.account.models import GeoLocatedIP

    conv = _conversation(opts, "approval-rest-cancel")
    block = _propose(opts.admin, conv, "block_ip",
                     {"ip": TEST_IP_CANCEL, "reason": "approval rest cancel", "ttl": 60})

    _login(opts)
    resp = _resolve(opts, block["action_id"], "cancel")
    opts.client.logout()

    assert_eq(resp.status_code, 200,
              f"cancel should succeed, got {resp.status_code}: {resp.json}")
    action = resp.json.get("data", {}).get("action") or {}
    assert_eq(action.get("state"), "canceled",
              f"the resolved card must be canceled, got {action}")
    assert_true(GeoLocatedIP.objects.filter(ip_address=TEST_IP_CANCEL,
                                            is_blocked=True).first() is None,
                "a canceled action must not have blocked the IP")


# ---------------------------------------------------------------------------
# The one non-oracular failure, over the wire
# ---------------------------------------------------------------------------

@th.django_unit_test("unknown, foreign, expired and used ids return one identical 409")
def test_unresolvable_cases_return_one_body(opts):
    import uuid as uuid_module

    from mojo.helpers import dates
    from mojo.apps.assistant.models import PendingAction

    conv = _conversation(opts, "approval-rest-refusals")
    foreign_conv = _conversation(opts, "approval-rest-foreign", user=opts.other)
    foreign = _propose(opts.other, foreign_conv, "block_ip",
                       {"ip": "198.51.100.73", "reason": "foreign", "ttl": 60})
    expired = _propose(opts.admin, conv, "block_ip",
                       {"ip": "198.51.100.74", "reason": "expired", "ttl": 60})
    PendingAction.objects.filter(uuid=uuid_module.UUID(expired["action_id"])).update(
        expires_at=dates.subtract(dates.utcnow(), seconds=30))
    used = _propose(opts.admin, conv, "block_ip",
                    {"ip": "198.51.100.75", "reason": "used", "ttl": 60})

    _login(opts)
    shapes = [
        _assert_unavailable(_resolve(opts, str(uuid_module.uuid4()), "approve"),
                            "an unknown action id"),
        _assert_unavailable(_resolve(opts, "not-a-uuid", "approve"),
                            "a malformed action id"),
        _assert_unavailable(_resolve(opts, foreign["action_id"], "approve"),
                            "another operator's action"),
        _assert_unavailable(_resolve(opts, expired["action_id"], "approve"),
                            "an expired action"),
    ]
    first = _resolve(opts, used["action_id"], "approve")
    assert_eq(first.status_code, 200,
              f"the first approval should succeed, got {first.status_code}: {first.json}")
    shapes.append(_assert_unavailable(_resolve(opts, used["action_id"], "approve"),
                                      "an already-consumed action"))
    opts.client.logout()

    assert_eq(len(set(shapes)), 1,
              f"every unresolvable case must return one identical body, got {set(shapes)}")

    foreign_row = PendingAction.objects.filter(
        uuid=uuid_module.UUID(foreign["action_id"])).first()
    assert_eq(foreign_row.state, "pending",
              "a refusal aimed at another operator's action must not disturb it")


# ---------------------------------------------------------------------------
# Step-up
# ---------------------------------------------------------------------------

@th.django_unit_test("a stale token gets 440 on a step-up action and executes nothing")
def test_stale_token_gets_440(opts):
    import uuid as uuid_module

    from mojo.apps.account.models import User
    from mojo.apps.account.utils.jwtoken import JWToken
    from mojo.apps.assistant.models import PendingAction

    conv = _conversation(opts, "approval-rest-freshauth")
    block = _propose(opts.admin, conv, "update_user_permission",
                     {"user_id": opts.target.pk, "permission": TARGET_PERM,
                      "action": "add"})
    assert_eq(block["requires_fresh_auth"], True,
              "update_user_permission mirrors an Admin twin gated at 600s")

    admin = User.objects.get(pk=opts.admin.pk)
    stale = JWToken(admin.get_auth_key()).create_access_token(
        uid=admin.pk, auth_time=int(time.time()) - 5000)

    opts.client.logout()
    opts.client.access_token = stale
    opts.client.is_authenticated = True
    opts.client.bearer = "bearer"
    resp = _resolve(opts, block["action_id"], "approve")
    opts.client.logout()

    assert_eq(resp.status_code, 440,
              f"a stale token on a step-up action must get 440, got "
              f"{resp.status_code}: {resp.json}")
    assert_eq(resp.json.get("error"), "reauth_required",
              f"the body must say reauth_required, got {resp.json}")

    opts.target.refresh_from_db()
    assert_true(not opts.target.has_permission(TARGET_PERM),
                "the gate must block BEFORE the permission is granted")
    row = PendingAction.objects.filter(
        uuid=uuid_module.UUID(block["action_id"])).first()
    assert_eq(row.state, "pending",
              "a reauth refusal must leave the action approvable after step-up")

    # And with a genuinely fresh login it goes through.
    _login(opts)
    resp = _resolve(opts, block["action_id"], "approve")
    opts.client.logout()
    assert_eq(resp.status_code, 200,
              f"a fresh login must be accepted, got {resp.status_code}: {resp.json}")
    opts.target.refresh_from_db()
    assert_true(opts.target.has_permission(TARGET_PERM),
                "the approved action must actually have granted the permission")
    opts.target.remove_permission(TARGET_PERM)


# ---------------------------------------------------------------------------
# Listing and credential confinement
# ---------------------------------------------------------------------------

@th.django_unit_test("GET /api/assistant/action is owner-scoped")
def test_action_list_is_owner_scoped(opts):
    conv = _conversation(opts, "approval-rest-list")
    mine = _propose(opts.admin, conv, "block_ip",
                    {"ip": "198.51.100.76", "reason": "mine", "ttl": 60})
    foreign_conv = _conversation(opts, "approval-rest-list-foreign", user=opts.other)
    foreign = _propose(opts.other, foreign_conv, "block_ip",
                       {"ip": "198.51.100.77", "reason": "theirs", "ttl": 60})

    _login(opts)
    resp = opts.client.get("/api/assistant/action")
    scoped = opts.client.get(f"/api/assistant/action?conversation={foreign_conv.pk}")
    opts.client.logout()

    assert_eq(resp.status_code, 200,
              f"listing should succeed, got {resp.status_code}: {resp.json}")
    ids = {a["action_id"] for a in resp.json.get("data", {}).get("actions", [])}
    assert_true(mine["action_id"] in ids,
                f"the caller's own action must be listed, got {ids}")
    assert_true(foreign["action_id"] not in ids,
                "another operator's action must never be listed")
    assert_eq(scoped.status_code, 404,
              f"another operator's conversation must not be readable, got "
              f"{scoped.status_code}: {scoped.json}")


@th.django_unit_test("the context endpoint cannot be used to read another operator's action")
def test_context_endpoint_refuses_pending_action(opts):
    conv = _conversation(opts, "approval-rest-context", user=opts.other)
    foreign = _propose(opts.other, conv, "block_ip",
                       {"ip": "198.51.100.79", "reason": "context probe", "ttl": 60})

    from mojo.apps.assistant.models import PendingAction
    import uuid as uuid_module

    row = PendingAction.objects.filter(
        uuid=uuid_module.UUID(foreign["action_id"])).first()

    _login(opts)
    resp = opts.client.post("/api/assistant/context",
                            {"model": "assistant.PendingAction", "pk": row.pk})
    opts.client.logout()

    # PendingAction is NO_REST and build_context reads by pk with no owner scope,
    # so without the guard this returned another operator's action verbatim.
    assert_true(resp.status_code >= 400,
                f"a NO_REST model must be refused, got {resp.status_code}: {resp.json}")
    body = str(resp.json)
    assert_true(foreign["action_id"] not in body,
                "no part of another operator's action may appear in the response")
    assert_true("context probe" not in body,
                "the refused response must not leak the action's summary or args")


@th.django_unit_test("a key-backed session cannot resolve an approval")
def test_key_backed_session_refused(opts):
    import uuid as uuid_module

    from mojo.apps.assistant.models import PendingAction

    conv = _conversation(opts, "approval-rest-apikey")
    block = _propose(opts.admin, conv, "block_ip",
                     {"ip": "198.51.100.78", "reason": "apikey", "ttl": 60})

    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = opts.api_token
    opts.client.is_authenticated = True
    resp = _resolve(opts, block["action_id"], "approve")
    opts.client.logout()

    assert_eq(resp.status_code, 403,
              f"a confined credential must be refused, got {resp.status_code}: {resp.json}")
    row = PendingAction.objects.filter(
        uuid=uuid_module.UUID(block["action_id"])).first()
    assert_eq(row.state, "pending",
              "a refused key-backed session must not consume the action")
