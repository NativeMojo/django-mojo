"""REST attachment contract that needs an enabled guarded Assistant route.

Moved here from tests/test_assistant/34_test_attachments.py (maestro #2791):
the assertion is a REST-path input-validation edge case (an explicitly null
`attachments` field must return a bounded 400, distinct from an omitted field).
The request hits the separate server process, so the in-process enable patch
the parallel siblings use cannot reach it — the server must actually see
LLM_ADMIN_ENABLED=True. That key is protected (Setting.set is refused), so the
only way to set it for the server is a reload via th.server_settings(), which is
legal only in a serial/opt-in package like this one.
"""
from contextlib import contextmanager

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


OWNER = "a1486_rest_owner"
PASSWORD = "a1486##Files99"
INVALID = "Invalid assistant attachments"
POLICY_HASH_KEY = "LLM_SAFETY_POLICY_EXPECTED_HASH"
LIMITS = {
    "requests_minute": 30,
    "requests_hour": 600,
    "requests_day": 5000,
    "tokens_minute": 200000,
    "tokens_hour": 2000000,
    "tokens_day": 10000000,
    "concurrency": 4,
    "max_input_bytes": 200000,
    "max_output_tokens": 8192,
    "timeout_seconds": 60,
    "max_loop_calls": 25,
}
POLICY = {
    "version": 1,
    "routes": {
        "assistant": {
            "provider": "anthropic",
            "model": "claude-sonnet-test",
            "credential": "admin",
            "capabilities": ["text", "tools", "images", "prompt_cache"],
        },
    },
    "shared": dict(LIMITS),
    "features": {"assistant": dict(LIMITS)},
    "breaker": {
        "auth_failures": 2,
        "rate_failures": 3,
        "server_failures": 5,
        "open_seconds": 300,
    },
}


def _clear_policy_hash():
    from mojo.apps.account.models import Setting

    Setting.objects.filter(key=POLICY_HASH_KEY, group=None).delete()
    redis = Setting._redis()
    if redis:
        redis.hdel(Setting._redis_key(), POLICY_HASH_KEY)


@contextmanager
def _enabled_route():
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import llm_safety

    _clear_policy_hash()
    Setting.objects.bulk_create([Setting(
        key=POLICY_HASH_KEY,
        group=None,
        value=llm_safety._canonical_hash(POLICY),
    )])
    try:
        with th.server_settings(
                LLM_ADMIN_ENABLED=True,
                LLM_ADMIN_API_KEY="sk-a1486",
                LLM_SAFETY_POLICY=POLICY):
            yield
    finally:
        _clear_policy_hash()


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_rest_attachments(opts):
    from mojo.apps.account.models import User

    # Clean up before creating — long-lived test database.
    _clear_policy_hash()
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
    with _enabled_route():
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
