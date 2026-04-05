"""
Tests for assistant memory service — CRUD, permissions, limits, prompt injection, Redis fallback.
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_EMAIL_ADMIN = 'assistant-mem-admin@example.com'
TEST_EMAIL_USER = 'assistant-mem-user@example.com'
TEST_EMAIL_OTHER = 'assistant-mem-other@example.com'
TEST_PASSWORD = 'TestPass1!'
TEST_GROUP_NAME = 'assistant-mem-test-group'


def _cleanup_redis():
    """Remove all assistant memory keys from Redis."""
    try:
        from mojo.helpers.redis import get_adapter
        adapter = get_adapter()
        client = adapter.get_client()
        for pattern in ["assistant:memory:*"]:
            cursor = 0
            while True:
                cursor, keys = client.scan(cursor, match=pattern, count=100)
                if keys:
                    client.delete(*keys)
                if cursor == 0:
                    break
    except Exception:
        pass


def _load_users():
    """Load test users from DB. Returns (admin, user, other)."""
    from mojo.apps.account.models import User
    admin = User.objects.get(email=TEST_EMAIL_ADMIN)
    user = User.objects.get(email=TEST_EMAIL_USER)
    other = User.objects.get(email=TEST_EMAIL_OTHER)
    return admin, user, other


def _load_group():
    """Load test group from DB."""
    from mojo.apps.account.models import Group
    return Group.objects.get(name=TEST_GROUP_NAME)


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_memory(opts):
    from mojo.apps.account.models import User, Group
    from mojo.apps.account.models.member import GroupMember

    # Clean up prior test data
    User.objects.filter(email__in=[TEST_EMAIL_ADMIN, TEST_EMAIL_USER, TEST_EMAIL_OTHER]).delete()
    Group.objects.filter(name__startswith='assistant-mem-test-group').delete()

    # Admin user with assistant perm (superuser)
    admin = User.objects.create_user(
        username=TEST_EMAIL_ADMIN, email=TEST_EMAIL_ADMIN, password=TEST_PASSWORD,
    )
    admin.is_email_verified = True
    admin.is_superuser = True
    admin.save()
    admin.add_permission("assistant")

    # Regular user with assistant perm
    user = User.objects.create_user(
        username=TEST_EMAIL_USER, email=TEST_EMAIL_USER, password=TEST_PASSWORD,
    )
    user.is_email_verified = True
    user.save()
    user.add_permission("assistant")

    # Other user without assistant perm
    other = User.objects.create_user(
        username=TEST_EMAIL_OTHER, email=TEST_EMAIL_OTHER, password=TEST_PASSWORD,
    )
    other.is_email_verified = True
    other.save()

    # Test group with user as member (with assistant perm on member)
    group = Group.objects.create(name=TEST_GROUP_NAME)
    member = GroupMember.objects.create(group=group, user=user)
    member.add_permission("assistant")

    # Admin also a member but without assistant on member
    GroupMember.objects.create(group=group, user=admin)

    # Clean Redis memory keys
    _cleanup_redis()


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_write_global_memory(opts):
    """Users with assistant perm can write global memory."""
    _cleanup_redis()
    from mojo.apps.assistant.services.memory import write_memory, read_memories
    _, user, _ = _load_users()

    result = write_memory(user, "global", "platform", "Healthcare SaaS on AWS")
    assert_eq(result.get("status"), "created", f"Expected 'created', got {result}")

    memories = read_memories(user)
    assert_true("global" in memories, f"Expected global tier in result, got {list(memories.keys())}")
    assert_eq(memories["global"]["platform"], "Healthcare SaaS on AWS",
              f"Expected platform memory, got {memories['global']}")


@th.django_unit_test()
def test_write_user_memory(opts):
    """Users can write to their own user tier."""
    _cleanup_redis()
    from mojo.apps.assistant.services.memory import write_memory, read_memories
    _, user, _ = _load_users()

    result = write_memory(user, "user", "preferred_channel", "Slack over email")
    assert_eq(result.get("status"), "created", f"Expected 'created', got {result}")

    memories = read_memories(user)
    assert_true("user" in memories, f"Expected user tier in result, got {list(memories.keys())}")
    assert_eq(memories["user"]["preferred_channel"], "Slack over email",
              f"Expected user memory, got {memories['user']}")


@th.django_unit_test()
def test_write_group_memory(opts):
    """Members with assistant perm on Member can write group memory."""
    _cleanup_redis()
    from mojo.apps.assistant.services.memory import write_memory, read_memories
    _, user, _ = _load_users()
    group = _load_group()

    result = write_memory(user, "group", "deploy_window", "2-4am UTC weekdays", group=group)
    assert_eq(result.get("status"), "created", f"Expected 'created', got {result}")

    memories = read_memories(user, group=group)
    assert_true("group" in memories, f"Expected group tier in result, got {list(memories.keys())}")
    assert_eq(memories["group"]["deploy_window"], "2-4am UTC weekdays",
              f"Expected group memory, got {memories['group']}")


@th.django_unit_test()
def test_update_existing_memory(opts):
    """Writing to an existing key updates it."""
    _cleanup_redis()
    from mojo.apps.assistant.services.memory import write_memory, read_memories
    _, user, _ = _load_users()

    write_memory(user, "global", "platform", "Old value")
    result = write_memory(user, "global", "platform", "New value")
    assert_eq(result.get("status"), "updated", f"Expected 'updated', got {result}")

    memories = read_memories(user)
    assert_eq(memories["global"]["platform"], "New value",
              f"Expected updated value, got {memories['global']['platform']}")


@th.django_unit_test()
def test_delete_memory(opts):
    """Delete removes an entry."""
    _cleanup_redis()
    from mojo.apps.assistant.services.memory import write_memory, delete_memory, read_memories
    _, user, _ = _load_users()

    write_memory(user, "global", "temp_fact", "Something temporary")
    result = delete_memory(user, "global", "temp_fact")
    assert_eq(result.get("status"), "deleted", f"Expected 'deleted', got {result}")

    memories = read_memories(user)
    global_entries = memories.get("global", {})
    assert_true("temp_fact" not in global_entries,
                f"Expected temp_fact to be deleted, got {global_entries}")


@th.django_unit_test()
def test_delete_nonexistent_key(opts):
    """Deleting a key that doesn't exist returns error."""
    _cleanup_redis()
    from mojo.apps.assistant.services.memory import delete_memory
    _, user, _ = _load_users()

    result = delete_memory(user, "global", "no_such_key")
    assert_true("error" in result, f"Expected error for missing key, got {result}")


# ---------------------------------------------------------------------------
# Permission enforcement
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_global_write_denied_without_assistant_perm(opts):
    """Users without assistant perm cannot write global memory."""
    _cleanup_redis()
    from mojo.apps.assistant.services.memory import write_memory
    _, _, other = _load_users()

    result = write_memory(other, "global", "hacked", "should fail")
    assert_true("error" in result, f"Expected permission error, got {result}")
    assert_true("ermission" in result["error"], f"Expected permission error message, got {result['error']}")


@th.django_unit_test()
def test_user_tier_isolation(opts):
    """User A cannot read User B's memories."""
    _cleanup_redis()
    from mojo.apps.assistant.services.memory import write_memory, read_memories
    _, user, other = _load_users()

    write_memory(user, "user", "my_note", "User's private note")

    # Other user reads — should not see user's memories
    other_memories = read_memories(other)
    other_user = other_memories.get("user", {})
    assert_true("my_note" not in other_user,
                f"Other user should not see user's memories, got {other_user}")


@th.django_unit_test()
def test_superuser_can_read_any_user_memory(opts):
    """Superuser can read any user's tier via direct key access."""
    _cleanup_redis()
    from mojo.apps.assistant.services.memory import write_memory
    _, user, _ = _load_users()

    write_memory(user, "user", "secret_note", "User's secret")

    from mojo.helpers.redis import get_adapter
    from mojo.apps.assistant.services.memory import _get_entries, KEY_USER
    adapter = get_adapter()
    entries = _get_entries(adapter, KEY_USER.format(user_id=user.pk))
    assert_eq(entries.get("secret_note"), "User's secret",
              f"Expected to read user's memory, got {entries}")


@th.django_unit_test()
def test_superuser_can_write_other_user_memory(opts):
    """Superuser can write to another user's tier."""
    _cleanup_redis()
    from mojo.apps.assistant.services.memory import write_memory
    admin, user, _ = _load_users()

    result = write_memory(admin, "user", "admin_note", "Note from admin", target_user=user)
    assert_eq(result.get("status"), "created", f"Expected 'created', got {result}")

    from mojo.helpers.redis import get_adapter
    from mojo.apps.assistant.services.memory import _get_entries, KEY_USER
    adapter = get_adapter()
    user_entries = _get_entries(adapter, KEY_USER.format(user_id=user.pk))
    assert_true("admin_note" in user_entries,
                f"Expected admin_note in user's tier, got {user_entries}")


@th.django_unit_test()
def test_non_superuser_cannot_write_other_user(opts):
    """Non-superuser cannot write to another user's tier."""
    _cleanup_redis()
    from mojo.apps.assistant.services.memory import write_memory
    admin, user, _ = _load_users()

    result = write_memory(user, "user", "hacked", "should fail", target_user=admin)
    assert_true("error" in result, f"Expected error, got {result}")


@th.django_unit_test()
def test_group_write_denied_without_member_assistant_perm(opts):
    """Non-member cannot write group memory; superuser can."""
    _cleanup_redis()
    from mojo.apps.assistant.services.memory import write_memory
    admin, _, other = _load_users()
    group = _load_group()

    # Admin is superuser — should succeed
    result = write_memory(admin, "group", "admin_entry", "admin wrote this", group=group)
    assert_eq(result.get("status"), "created",
              f"Superuser should bypass member perm check, got {result}")

    # Other is not a member — should fail
    result2 = write_memory(other, "group", "hacked2", "should fail", group=group)
    assert_true("error" in result2, f"Non-member should get error, got {result2}")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_key_format_validation(opts):
    """Invalid key formats are rejected."""
    _cleanup_redis()
    from mojo.apps.assistant.services.memory import write_memory
    _, user, _ = _load_users()

    # Capital letters
    result = write_memory(user, "global", "BadKey", "value")
    assert_true("error" in result, f"Expected error for uppercase key, got {result}")

    # Spaces
    result = write_memory(user, "global", "bad key", "value")
    assert_true("error" in result, f"Expected error for key with space, got {result}")

    # Reserved _meta key
    result = write_memory(user, "global", "_meta", "value")
    assert_true("error" in result, f"Expected error for reserved key, got {result}")

    # Empty key
    result = write_memory(user, "global", "", "value")
    assert_true("error" in result, f"Expected error for empty key, got {result}")

    # Valid key with colons and underscores
    result = write_memory(user, "global", "rule:internal_ips", "10.0.0.0/8")
    assert_eq(result.get("status"), "created", f"Expected valid key accepted, got {result}")


@th.django_unit_test()
def test_value_too_long(opts):
    """Values exceeding max chars are rejected."""
    _cleanup_redis()
    from mojo.apps.assistant.services.memory import write_memory
    _, user, _ = _load_users()

    long_value = "x" * 501
    result = write_memory(user, "global", "too_long", long_value)
    assert_true("error" in result, f"Expected error for long value, got {result}")
    assert_true("too long" in result["error"].lower(),
                f"Expected 'too long' in error, got {result['error']}")


@th.django_unit_test()
def test_empty_value_rejected(opts):
    """Empty values are rejected."""
    _cleanup_redis()
    from mojo.apps.assistant.services.memory import write_memory
    _, user, _ = _load_users()

    result = write_memory(user, "global", "empty", "")
    assert_true("error" in result, f"Expected error for empty value, got {result}")

    result2 = write_memory(user, "global", "whitespace", "   ")
    assert_true("error" in result2, f"Expected error for whitespace value, got {result2}")


@th.django_unit_test()
def test_secret_pattern_rejected(opts):
    """Values matching secret patterns are rejected."""
    _cleanup_redis()
    from mojo.apps.assistant.services.memory import write_memory
    _, user, _ = _load_users()

    secrets = [
        "The API key is sk-1234567890abcdefghij",
        "password=hunter2",
        "token=abc123secrettoken",
        "AKIA1234567890ABCDEF",
        "postgres://user:pass@host/db",
    ]
    for secret in secrets:
        result = write_memory(user, "global", "secret_test", secret)
        assert_true("error" in result, f"Expected error for secret value '{secret[:30]}...', got {result}")


@th.django_unit_test()
def test_max_entries_enforced(opts):
    """Cannot exceed max entries per tier."""
    _cleanup_redis()
    from mojo.apps.assistant.services.memory import write_memory
    from mojo.helpers.settings import settings
    _, user, _ = _load_users()

    max_entries = settings.get("LLM_ADMIN_MEMORY_GLOBAL_MAX", 50, kind="int")

    for i in range(max_entries):
        result = write_memory(user, "global", f"entry-{i}", f"Value {i}")
        assert_eq(result.get("status"), "created",
                  f"Expected entry {i} created, got {result}")

    # One more should fail
    result = write_memory(user, "global", "one-too-many", "Should fail")
    assert_true("error" in result, f"Expected error at limit, got {result}")
    assert_true("full" in result["error"].lower(),
                f"Expected 'full' in error, got {result['error']}")


@th.django_unit_test()
def test_invalid_tier_rejected(opts):
    """Invalid tier names are rejected."""
    _cleanup_redis()
    from mojo.apps.assistant.services.memory import write_memory
    _, user, _ = _load_users()

    result = write_memory(user, "invalid", "key", "value")
    assert_true("error" in result, f"Expected error for invalid tier, got {result}")


# ---------------------------------------------------------------------------
# System prompt injection
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_build_memory_prompt_format(opts):
    """build_memory_prompt returns formatted markdown."""
    _cleanup_redis()
    from mojo.apps.assistant.services.memory import write_memory, build_memory_prompt
    _, user, _ = _load_users()

    write_memory(user, "global", "platform", "Healthcare SaaS")
    write_memory(user, "user", "focus", "Auth service")

    prompt = build_memory_prompt(user)
    assert_true("## Memory" in prompt, f"Expected '## Memory' header, got {prompt[:100]}")
    assert_true("### Platform" in prompt, f"Expected '### Platform' section, got {prompt}")
    assert_true("### Your Notes" in prompt, f"Expected '### Your Notes' section, got {prompt}")
    assert_true("Healthcare SaaS" in prompt, f"Expected global memory in prompt, got {prompt}")
    assert_true("Auth service" in prompt, f"Expected user memory in prompt, got {prompt}")


@th.django_unit_test()
def test_build_memory_prompt_empty_tiers_omitted(opts):
    """Empty tiers are not included in the prompt."""
    _cleanup_redis()
    from mojo.apps.assistant.services.memory import write_memory, build_memory_prompt
    _, user, _ = _load_users()

    write_memory(user, "global", "platform", "Test platform")

    prompt = build_memory_prompt(user)
    assert_true("### Platform" in prompt, f"Expected Platform section, got {prompt}")
    assert_true("### Your Notes" not in prompt,
                f"Expected no Your Notes section for empty tier, got {prompt}")


@th.django_unit_test()
def test_build_memory_prompt_empty_returns_empty_string(opts):
    """No memories at all returns empty string."""
    _cleanup_redis()
    from mojo.apps.assistant.services.memory import build_memory_prompt
    _, user, _ = _load_users()

    prompt = build_memory_prompt(user)
    assert_eq(prompt, "", f"Expected empty string for no memories, got '{prompt}'")


@th.django_unit_test()
def test_is_global_empty(opts):
    """is_global_empty returns True when no global memories exist."""
    _cleanup_redis()
    from mojo.apps.assistant.services.memory import is_global_empty, write_memory
    _, user, _ = _load_users()

    assert_true(is_global_empty(), "Expected global to be empty initially")

    write_memory(user, "global", "platform", "Test")
    assert_true(not is_global_empty(), "Expected global to be non-empty after write")


@th.django_unit_test()
def test_onboarding_prompt_when_global_empty(opts):
    """System prompt includes onboarding when global memory is empty."""
    _cleanup_redis()
    from mojo.apps.assistant.services.agent import _get_system_prompt
    _, user, _ = _load_users()

    prompt = _get_system_prompt(user=user)
    assert_true("Getting Started" in prompt,
                f"Expected onboarding prompt when global is empty, got last 200 chars: ...{prompt[-200:]}")


@th.django_unit_test()
def test_no_onboarding_when_memories_exist(opts):
    """System prompt has memory section instead of onboarding when memories exist."""
    _cleanup_redis()
    from mojo.apps.assistant.services.memory import write_memory
    from mojo.apps.assistant.services.agent import _get_system_prompt
    _, user, _ = _load_users()

    write_memory(user, "global", "platform", "Test SaaS")
    prompt = _get_system_prompt(user=user)
    assert_true("Getting Started" not in prompt,
                f"Expected no onboarding when memories exist, got last 200 chars: ...{prompt[-200:]}")
    assert_true("## Memory" in prompt,
                f"Expected memory section when memories exist, got last 200 chars: ...{prompt[-200:]}")


# ---------------------------------------------------------------------------
# Timestamp tracking
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_last_touched_bumped_on_read(opts):
    """Reading memories via build_memory_prompt bumps last_touched."""
    _cleanup_redis()
    import time
    from mojo.apps.assistant.services.memory import write_memory, build_memory_prompt, _get_meta
    from mojo.helpers.redis import get_adapter
    from mojo.apps.assistant.services.memory import KEY_GLOBAL
    _, user, _ = _load_users()

    write_memory(user, "global", "ts_test", "Timestamp test")
    adapter = get_adapter()
    meta_before = _get_meta(adapter, KEY_GLOBAL)
    touched_before = meta_before.get("ts_test", {}).get("last_touched", 0)

    time.sleep(0.1)
    build_memory_prompt(user)

    meta_after = _get_meta(adapter, KEY_GLOBAL)
    touched_after = meta_after.get("ts_test", {}).get("last_touched", 0)
    assert_true(touched_after > touched_before,
                f"Expected last_touched to increase, before={touched_before} after={touched_after}")


@th.django_unit_test()
def test_last_modified_bumped_on_write(opts):
    """Writing memory bumps the last_modified tracker."""
    _cleanup_redis()
    from mojo.apps.assistant.services.memory import write_memory
    from mojo.helpers.redis import get_adapter
    from mojo.apps.assistant.services.memory import KEY_LAST_MODIFIED
    _, user, _ = _load_users()

    write_memory(user, "global", "mod_test", "Modification test")
    adapter = get_adapter()
    mod_val = adapter.get(KEY_LAST_MODIFIED.format(tier="global"))
    assert_true(mod_val is not None,
                f"Expected last_modified to be set after write, got None")


# ---------------------------------------------------------------------------
# Group memory isolation
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_group_memory_isolation(opts):
    """Group A's memories are not visible to Group B."""
    _cleanup_redis()
    from mojo.apps.account.models import Group
    from mojo.apps.assistant.services.memory import write_memory, read_memories
    _, user, _ = _load_users()
    group = _load_group()

    Group.objects.filter(name="assistant-mem-test-group-b").delete()
    group_b = Group.objects.create(name="assistant-mem-test-group-b")

    write_memory(user, "group", "secret_rule", "Group A only", group=group)

    # Read with group_b context — should not see group A's memory
    memories = read_memories(user, group=group_b)
    group_entries = memories.get("group", {})
    assert_true("secret_rule" not in group_entries,
                f"Group B should not see Group A's memory, got {group_entries}")

    group_b.delete()
