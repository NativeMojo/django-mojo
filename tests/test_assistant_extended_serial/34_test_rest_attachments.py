"""REST attachment contract that needs the assistant enabled at the server.

Moved here from tests/test_assistant/34_test_attachments.py (maestro #2791):
the assertion is a REST-path input-validation edge case (an explicitly null
`attachments` field must return a bounded 400, distinct from an omitted field).
The request hits the separate server process, so the in-process enable patch
the parallel siblings use cannot reach it — the server must actually see
LLM_ADMIN_ENABLED=True. That key is protected (Setting.set is refused), so the
only way to set it for the server is a reload via th.server_settings(), which is
legal only in a serial/opt-in package like this one.
"""
from testit import helpers as th
from testit.helpers import assert_eq, assert_true


OWNER = "a1486_rest_owner"
PASSWORD = "a1486##Files99"
INVALID = "Invalid assistant attachments"


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_rest_attachments(opts):
    from mojo.apps.account.models import User

    # Clean up before creating — long-lived test database.
    User.objects.filter(username=OWNER).delete()
    owner = User.objects.create_user(
        username=OWNER, email=f"{OWNER}@example.com", password=PASSWORD)
    owner.is_active = True
    owner.is_email_verified = True
    owner.requires_mfa = False
    owner.remove_all_permissions()
    owner.add_permission("view_admin")
    owner.save()
    opts.owner_id = owner.pk


@th.django_unit_test("assistant attachments: REST distinguishes omitted from explicit null")
def test_rest_explicit_null_rejected(opts):
    # LLM_ADMIN_ENABLED is a protected setting, so the server can only be made to
    # see it via a reload (maestro #2791) — hence server_settings here, in the
    # serial sibling where reloads are permitted.
    with th.server_settings(LLM_ADMIN_ENABLED=True, LLM_ADMIN_API_KEY="sk-a1486"):
        assert_true(opts.client.login(OWNER, PASSWORD), "owner REST login must succeed")
        resp = opts.client.post("/api/assistant", {
            "message": "a1486 REST null",
            "attachments": None,
        })
        opts.client.logout()

    assert_eq(resp.status_code, 400,
              f"an explicitly null REST attachments field must return 400: {resp.json}")
    assert_eq(resp.json.error, INVALID,
              "REST null must use the bounded invalid-attachment response")
