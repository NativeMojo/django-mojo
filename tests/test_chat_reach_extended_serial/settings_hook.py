"""
`CHAT_REACHABILITY_HANDLER` read from the settings file by the live server
(maestro #8345). The hook's logic is covered in-process in
`tests/test_chat/reachability.py`; this is the one end-to-end case, here
because `th.server_settings()` reloads the shared server and so cannot run in
a parallel-safe package.
"""
from testit import helpers as th
from testit.helpers import assert_eq, assert_true

TESTIT_TIER = "extended"

PASSWORD = "TestPass1!"
PREFIX = "chat-reach-hook-"
EMAILS = {name: f"{PREFIX}{name}@example.com" for name in ("a", "b", "c")}
GROUP = f"test-{PREFIX}g"
HOOKS = "mojo.apps.chat.services.reachability"


@th.django_unit_setup()
@th.requires_app("mojo.apps.chat")
def setup_settings_hook(opts):
    from mojo.apps.account.models import Group, User
    from mojo.apps.chat.models import ChatRoom

    users = User.objects.filter(email__in=EMAILS.values())
    ChatRoom.objects.filter(memberships__user__in=users).delete()
    users.delete()
    Group.objects.filter(name=GROUP).delete()
    for name, email in EMAILS.items():
        user = User.objects.create_user(username=email, email=email, password=PASSWORD)
        user.is_email_verified = True
        user.save()
        setattr(opts, name, user)
    group = Group.objects.create(name=GROUP)
    group.add_member(opts.a)
    group.add_member(opts.b)


def _dm(opts, target):
    from mojo.apps.chat.models import ChatRoom
    ChatRoom.objects.filter(kind="direct", memberships__user=target).delete()
    assert_true(opts.client.login(EMAILS["a"], PASSWORD), "login as A failed")
    return opts.client.post("/api/chat/dm", {"user_id": target.pk})


@th.django_unit_test()
def test_settings_hook_controls_the_dm_door(opts):
    """deny_all refuses A -> B (shared group); allow_all lets A reach C (no
    shared group); with the setting gone the default refuses A -> C again."""
    with th.server_settings(CHAT_REACHABILITY_HANDLER=f"{HOOKS}.deny_all"):
        resp = _dm(opts, opts.b)
        assert_eq(resp.status_code, 404, f"deny_all refuses A -> B, got {resp.status_code}: {resp.json}")
        assert_eq(resp.json.error, "User not found", "with the uniform refusal")
    with th.server_settings(CHAT_REACHABILITY_HANDLER=f"{HOOKS}.allow_all"):
        resp = _dm(opts, opts.c)
        assert_eq(resp.status_code, 200, f"allow_all lets A reach C, got {resp.status_code}: {resp.json}")
    resp = _dm(opts, opts.c)
    assert_eq(resp.status_code, 404, f"the default rule refuses A -> C again, got {resp.status_code}")
