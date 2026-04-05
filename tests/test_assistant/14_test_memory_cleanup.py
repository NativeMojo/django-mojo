"""
Tests for memory cleanup job — mechanical cleanup and dreaming.
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_EMAIL = 'assistant-cleanup@example.com'
TEST_PASSWORD = 'TestPass1!'


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


def _load_user():
    from mojo.apps.account.models import User
    return User.objects.get(email=TEST_EMAIL)


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_cleanup(opts):
    from mojo.apps.account.models import User

    User.objects.filter(email=TEST_EMAIL).delete()
    user = User.objects.create_user(
        username=TEST_EMAIL, email=TEST_EMAIL, password=TEST_PASSWORD,
    )
    user.is_email_verified = True
    user.save()
    user.add_permission("assistant")

    _cleanup_redis()


# ---------------------------------------------------------------------------
# Mechanical cleanup
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_orphan_user_cleanup(opts):
    """Memories for deleted users are cleaned up."""
    _cleanup_redis()
    from mojo.helpers.redis import get_adapter
    from mojo.apps.assistant.services.memory import cleanup_mechanical

    # Write a fake user memory directly to Redis for a non-existent user
    adapter = get_adapter()
    adapter.hset("assistant:memory:user:999999", {"fake_key": "fake_value"})

    stats = cleanup_mechanical()
    assert_true(stats.get("orphans_deleted", 0) > 0,
                f"Expected orphan deletion, got {stats}")

    # Verify it's gone
    entries = adapter.hgetall("assistant:memory:user:999999")
    assert_true(not entries, f"Expected orphan memory deleted, got {entries}")


@th.django_unit_test()
def test_orphan_group_cleanup(opts):
    """Memories for deleted groups are cleaned up."""
    _cleanup_redis()
    from mojo.helpers.redis import get_adapter
    from mojo.apps.assistant.services.memory import cleanup_mechanical

    adapter = get_adapter()
    adapter.hset("assistant:memory:group:999999", {"fake_key": "fake_value"})

    stats = cleanup_mechanical()
    assert_true(stats.get("orphans_deleted", 0) > 0,
                f"Expected orphan deletion, got {stats}")

    entries = adapter.hgetall("assistant:memory:group:999999")
    assert_true(not entries, f"Expected orphan memory deleted, got {entries}")


@th.django_unit_test()
def test_suspicious_pattern_detection(opts):
    """Cleanup detects suspicious patterns in memory values."""
    _cleanup_redis()
    from mojo.helpers.redis import get_adapter
    from mojo.apps.assistant.services.memory import cleanup_mechanical, KEY_GLOBAL

    adapter = get_adapter()
    adapter.hset(KEY_GLOBAL, {"bad_key": "password=hunter2"})

    stats = cleanup_mechanical()
    assert_true(stats.get("suspicious_found", 0) > 0,
                f"Expected suspicious detection, got {stats}")


@th.django_unit_test()
def test_size_enforcement_pruning(opts):
    """Cleanup prunes oldest entries when over limit."""
    _cleanup_redis()
    import time
    import ujson
    from mojo.helpers.redis import get_adapter
    from mojo.helpers.settings import settings
    from mojo.apps.assistant.services.memory import cleanup_mechanical, KEY_GLOBAL, _get_entries

    adapter = get_adapter()
    max_entries = settings.get("LLM_ADMIN_MEMORY_GLOBAL_MAX", 50, kind="int")

    # Write more than the limit directly to Redis
    meta = {}
    for i in range(max_entries + 5):
        adapter.hset(KEY_GLOBAL, {f"over-{i}": f"Value {i}"})
        meta[f"over-{i}"] = {"created": time.time() - (max_entries + 5 - i), "last_touched": time.time() - (max_entries + 5 - i)}
        time.sleep(0.001)

    adapter.hset(KEY_GLOBAL, {"_meta": ujson.dumps(meta)})

    stats = cleanup_mechanical()
    assert_true(stats.get("size_pruned", 0) >= 5,
                f"Expected at least 5 entries pruned, got {stats}")

    entries = _get_entries(adapter, KEY_GLOBAL)
    assert_true(len(entries) <= max_entries,
                f"Expected at most {max_entries} entries, got {len(entries)}")


# ---------------------------------------------------------------------------
# Dreaming conditional logic
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_should_dream_false_when_no_changes(opts):
    """should_dream returns False when no modifications and no interval passed."""
    _cleanup_redis()
    from mojo.apps.assistant.services.memory import should_dream

    result = should_dream("global")
    assert_true(not result, f"Expected False when no changes, got {result}")


@th.django_unit_test()
def test_should_dream_true_when_modified(opts):
    """should_dream returns True when memory was modified since last dream."""
    _cleanup_redis()
    from mojo.apps.assistant.services.memory import should_dream, mark_modified

    mark_modified("global")
    result = should_dream("global")
    assert_true(result, f"Expected True when modified, got {result}")


@th.django_unit_test()
def test_should_dream_false_after_dream(opts):
    """should_dream returns False when dreamed after last modification."""
    _cleanup_redis()
    import time
    from mojo.apps.assistant.services.memory import should_dream, mark_modified, mark_dreamed

    mark_modified("global")
    time.sleep(0.05)
    mark_dreamed("global")

    result = should_dream("global")
    assert_true(not result, f"Expected False after dream, got {result}")


@th.django_unit_test()
def test_should_dream_true_when_interval_passed(opts):
    """should_dream returns True when dream interval has passed."""
    _cleanup_redis()
    from mojo.helpers.redis import get_adapter
    from mojo.apps.assistant.services.memory import should_dream, KEY_LAST_DREAM, mark_modified
    import time

    # Set last_dream to 8 days ago (interval default is 7)
    adapter = get_adapter()
    mark_modified("global")
    time.sleep(0.01)
    old_time = str(time.time() - 8 * 86400)
    adapter.set(KEY_LAST_DREAM.format(tier="global"), old_time)

    result = should_dream("global")
    assert_true(result, f"Expected True when interval passed, got {result}")


@th.django_unit_test()
def test_last_dream_updated_after_dream(opts):
    """mark_dreamed sets the last_dream timestamp."""
    _cleanup_redis()
    from mojo.helpers.redis import get_adapter
    from mojo.apps.assistant.services.memory import mark_dreamed, KEY_LAST_DREAM

    mark_dreamed("global")
    adapter = get_adapter()
    val = adapter.get(KEY_LAST_DREAM.format(tier="global"))
    assert_true(val is not None, f"Expected last_dream timestamp set, got None")


# ---------------------------------------------------------------------------
# Dream action application
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_apply_dream_delete_action(opts):
    """Dreaming delete action removes the entry."""
    _cleanup_redis()
    from mojo.helpers.redis import get_adapter
    from mojo.apps.assistant.services.memory import apply_dream_actions, KEY_GLOBAL, _get_entries

    adapter = get_adapter()
    adapter.hset(KEY_GLOBAL, {"old_entry": "outdated info"})

    actions = [{"action": "delete", "key": "old_entry", "reason": "expired"}]
    stats = apply_dream_actions(KEY_GLOBAL, actions)
    assert_eq(stats["deleted"], 1, f"Expected 1 deleted, got {stats}")

    entries = _get_entries(adapter, KEY_GLOBAL)
    assert_true("old_entry" not in entries, f"Expected old_entry removed, got {entries}")


@th.django_unit_test()
def test_apply_dream_rewrite_action(opts):
    """Dreaming rewrite action updates the value."""
    _cleanup_redis()
    from mojo.helpers.redis import get_adapter
    from mojo.apps.assistant.services.memory import apply_dream_actions, KEY_GLOBAL, _get_entries

    adapter = get_adapter()
    adapter.hset(KEY_GLOBAL, {"verbose": "This is a very long and verbose entry that says too much"})

    actions = [{"action": "rewrite", "key": "verbose", "new_value": "Compact version", "reason": "compress"}]
    stats = apply_dream_actions(KEY_GLOBAL, actions)
    assert_eq(stats["rewritten"], 1, f"Expected 1 rewritten, got {stats}")

    entries = _get_entries(adapter, KEY_GLOBAL)
    assert_eq(entries.get("verbose"), "Compact version",
              f"Expected rewritten value, got {entries.get('verbose')}")


@th.django_unit_test()
def test_apply_dream_merge_action(opts):
    """Dreaming merge action combines entries."""
    _cleanup_redis()
    from mojo.helpers.redis import get_adapter
    from mojo.apps.assistant.services.memory import apply_dream_actions, KEY_GLOBAL, _get_entries

    adapter = get_adapter()
    adapter.hset(KEY_GLOBAL, {"fact_a": "Healthcare platform", "fact_b": "HIPAA compliant"})

    actions = [{
        "action": "merge",
        "keys": ["fact_a", "fact_b"],
        "new_key": "platform",
        "new_value": "Healthcare platform (HIPAA compliant)",
        "reason": "redundant",
    }]
    stats = apply_dream_actions(KEY_GLOBAL, actions)
    assert_eq(stats["merged"], 1, f"Expected 1 merged, got {stats}")

    entries = _get_entries(adapter, KEY_GLOBAL)
    assert_true("fact_a" not in entries, f"Expected fact_a removed after merge, got {entries}")
    assert_true("fact_b" not in entries, f"Expected fact_b removed after merge, got {entries}")
    assert_eq(entries.get("platform"), "Healthcare platform (HIPAA compliant)",
              f"Expected merged value, got {entries.get('platform')}")


@th.django_unit_test()
def test_apply_dream_keep_action_no_change(opts):
    """Dreaming keep action makes no changes."""
    _cleanup_redis()
    from mojo.helpers.redis import get_adapter
    from mojo.apps.assistant.services.memory import apply_dream_actions, KEY_GLOBAL, _get_entries

    adapter = get_adapter()
    adapter.hset(KEY_GLOBAL, {"good_entry": "Still valid"})

    actions = [{"action": "keep", "key": "good_entry", "reason": "still accurate"}]
    stats = apply_dream_actions(KEY_GLOBAL, actions)
    assert_eq(stats["kept"], 1, f"Expected 1 kept, got {stats}")

    entries = _get_entries(adapter, KEY_GLOBAL)
    assert_eq(entries.get("good_entry"), "Still valid",
              f"Expected entry unchanged, got {entries.get('good_entry')}")
