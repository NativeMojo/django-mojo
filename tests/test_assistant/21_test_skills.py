"""
Tests for assistant learned skills — CRUD, tier scoping, permissions, limits, search.
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_EMAIL_ADMIN = 'skill-test-admin@example.com'
TEST_EMAIL_USER = 'skill-test-user@example.com'
TEST_EMAIL_OTHER = 'skill-test-other@example.com'
TEST_PASSWORD = 'TestPass1!'
TEST_GROUP_NAME = 'skill-test-group'

SAMPLE_STEPS = [
    {
        "tool": "query_model",
        "params": {"app_name": "sales", "model_name": "FeeTable"},
        "description": "Query FeeTable for recent changes",
    },
    {
        "tool": "query_model",
        "params": {"app_name": "jobs", "model_name": "Job"},
        "condition": "previous_step.count > 0",
        "description": "Publish report job if changes found",
    },
]


def _load_users():
    from mojo.apps.account.models import User
    admin = User.objects.get(email=TEST_EMAIL_ADMIN)
    user = User.objects.get(email=TEST_EMAIL_USER)
    other = User.objects.get(email=TEST_EMAIL_OTHER)
    return admin, user, other


def _load_group():
    from mojo.apps.account.models import Group
    return Group.objects.get(name=TEST_GROUP_NAME)


def _cleanup():
    from mojo.apps.assistant.models import Skill
    Skill.objects.filter(name__startswith="test-skill").delete()
    Skill.objects.filter(name__startswith="limit-skill").delete()


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_skills(opts):
    from mojo.apps.account.models import User, Group
    from mojo.apps.account.models.member import GroupMember

    # Clean up prior test data
    User.objects.filter(email__in=[TEST_EMAIL_ADMIN, TEST_EMAIL_USER, TEST_EMAIL_OTHER]).delete()
    Group.objects.filter(name=TEST_GROUP_NAME).delete()
    _cleanup()

    # Admin (superuser)
    admin = User.objects.create_user(
        username=TEST_EMAIL_ADMIN, email=TEST_EMAIL_ADMIN, password=TEST_PASSWORD,
    )
    admin.is_superuser = True
    admin.is_email_verified = True
    admin.save()
    admin.add_permission("assistant")

    # Regular user with assistant perm
    user = User.objects.create_user(
        username=TEST_EMAIL_USER, email=TEST_EMAIL_USER, password=TEST_PASSWORD,
    )
    user.is_email_verified = True
    user.save()
    user.add_permission("assistant")

    # Other user (no assistant perm)
    other = User.objects.create_user(
        username=TEST_EMAIL_OTHER, email=TEST_EMAIL_OTHER, password=TEST_PASSWORD,
    )

    # Group with user as member
    group = Group.objects.create(name=TEST_GROUP_NAME)
    member = GroupMember.objects.create(group=group, user=user)
    member.add_permission("assistant")


# ---------------------------------------------------------------------------
# save_skill tests
# ---------------------------------------------------------------------------

@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_save_user_skill(opts):
    """Save a skill in the user tier."""
    from mojo.apps.assistant.services.skills import save_skill
    _cleanup()
    _, user, _ = _load_users()

    result = save_skill(
        user, tier="user", name="test-skill-user",
        description="A test skill for user tier",
        triggers=["rebuild reports", "regenerate reports"],
        steps=SAMPLE_STEPS,
    )
    assert_true("error" not in result, "save_skill should succeed for user tier")
    assert_eq(result["skill"]["name"], "test-skill-user", "Skill name should match")
    assert_eq(result["skill"]["tier"], "user", "Skill tier should be user")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_save_global_skill(opts):
    """Save a global skill requires view_admin (superuser)."""
    from mojo.apps.assistant.services.skills import save_skill
    _cleanup()
    admin, user, _ = _load_users()

    # Regular assistant user should NOT be able to create global skills
    result = save_skill(
        user, tier="global", name="test-skill-global",
        description="A global skill",
        triggers=["check health"],
        steps=SAMPLE_STEPS,
    )
    assert_true("error" in result, "Regular assistant user should not save global skills")

    # Admin (superuser) should be able to create global skills
    result = save_skill(
        admin, tier="global", name="test-skill-global",
        description="A global skill",
        triggers=["check health"],
        steps=SAMPLE_STEPS,
    )
    assert_true("error" not in result, "Admin should save global skill")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_save_group_skill(opts):
    """Save a skill in the group tier."""
    from mojo.apps.assistant.services.skills import save_skill
    _cleanup()
    _, user, _ = _load_users()
    group = _load_group()

    result = save_skill(
        user, tier="group", name="test-skill-group",
        description="A group skill",
        triggers=["team report"],
        steps=SAMPLE_STEPS,
        group=group,
    )
    assert_true("error" not in result, "Group member with assistant perm should save group skill")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_save_group_no_permission(opts):
    """Non-member cannot save a group skill."""
    from mojo.apps.assistant.services.skills import save_skill
    _cleanup()
    _, _, other = _load_users()
    group = _load_group()

    result = save_skill(
        other, tier="group", name="test-skill-noperm",
        description="Should fail",
        triggers=["nope"],
        steps=SAMPLE_STEPS,
        group=group,
    )
    assert_true("error" in result, "Non-member should not save group skill")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_save_no_assistant_perm(opts):
    """User without assistant perm cannot save user skills."""
    from mojo.apps.assistant.services.skills import save_skill
    _cleanup()
    _, _, other = _load_users()

    result = save_skill(
        other, tier="user", name="test-skill-noperm",
        description="Should fail",
        triggers=["nope"],
        steps=SAMPLE_STEPS,
    )
    assert_true("error" in result, "User without assistant perm should not save user skill")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_save_upsert(opts):
    """Saving a skill with the same name in same scope updates it."""
    from mojo.apps.assistant.services.skills import save_skill
    from mojo.apps.assistant.models import Skill
    _cleanup()
    _, user, _ = _load_users()

    save_skill(
        user, tier="user", name="test-skill-upsert",
        description="Version 1",
        triggers=["v1"],
        steps=SAMPLE_STEPS,
    )
    result = save_skill(
        user, tier="user", name="test-skill-upsert",
        description="Version 2",
        triggers=["v2"],
        steps=SAMPLE_STEPS,
    )
    assert_true("updated" in result["message"].lower(), "Second save should update existing skill")
    skill = Skill.objects.get(tier="user", user=user, name="test-skill-upsert")
    assert_eq(skill.description, "Version 2", "Description should be updated")
    assert_eq(skill.triggers, ["v2"], "Triggers should be updated")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_save_step_validation(opts):
    """Steps without required fields are rejected."""
    from mojo.apps.assistant.services.skills import save_skill
    _cleanup()
    _, user, _ = _load_users()

    # Missing tool
    result = save_skill(
        user, tier="user", name="test-skill-badstep",
        description="Bad steps",
        triggers=["bad"],
        steps=[{"description": "no tool here"}],
    )
    assert_true("error" in result, "Step without 'tool' should be rejected")

    # Missing description
    result = save_skill(
        user, tier="user", name="test-skill-badstep2",
        description="Bad steps",
        triggers=["bad"],
        steps=[{"tool": "query_model"}],
    )
    assert_true("error" in result, "Step without 'description' should be rejected")

    # Empty steps
    result = save_skill(
        user, tier="user", name="test-skill-emptystep",
        description="Empty steps",
        triggers=["empty"],
        steps=[],
    )
    assert_true("error" in result, "Empty steps list should be rejected")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_save_trigger_validation(opts):
    """Invalid triggers are rejected."""
    from mojo.apps.assistant.services.skills import save_skill
    _cleanup()
    _, user, _ = _load_users()

    result = save_skill(
        user, tier="user", name="test-skill-badtrigger",
        description="Bad triggers",
        triggers=[""] ,  # empty string trigger
        steps=SAMPLE_STEPS,
    )
    assert_true("error" in result, "Empty string trigger should be rejected")

    result = save_skill(
        user, tier="user", name="test-skill-toomany",
        description="Too many triggers",
        triggers=[f"trigger-{i}" for i in range(11)],
        steps=SAMPLE_STEPS,
    )
    assert_true("error" in result, "More than 10 triggers should be rejected")


# ---------------------------------------------------------------------------
# find_skills tests
# ---------------------------------------------------------------------------

@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_find_by_name(opts):
    """find_skills matches on skill name."""
    from mojo.apps.assistant.services.skills import save_skill, find_skills
    _cleanup()
    _, user, _ = _load_users()

    save_skill(
        user, tier="user", name="test-skill-sales-report",
        description="Generate monthly sales report",
        triggers=["rebuild reports"],
        steps=SAMPLE_STEPS,
    )
    results = find_skills(user, "sales report")
    assert_true(len(results) >= 1, "Should find skill by name keywords")
    assert_eq(results[0]["name"], "test-skill-sales-report", "Should match the correct skill")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_find_by_trigger(opts):
    """find_skills matches on trigger phrases."""
    from mojo.apps.assistant.services.skills import save_skill, find_skills
    _cleanup()
    _, user, _ = _load_users()

    save_skill(
        user, tier="user", name="test-skill-triggered",
        description="A skill found by trigger",
        triggers=["regenerate monthly data", "rebuild monthly"],
        steps=SAMPLE_STEPS,
    )
    results = find_skills(user, "regenerate monthly")
    assert_true(len(results) >= 1, "Should find skill by trigger phrase keywords")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_find_by_description(opts):
    """find_skills matches on description."""
    from mojo.apps.assistant.services.skills import save_skill, find_skills
    _cleanup()
    _, user, _ = _load_users()

    save_skill(
        user, tier="user", name="test-skill-desc-match",
        description="Analyze FeeTable changes for merchant groups",
        triggers=["check fees"],
        steps=SAMPLE_STEPS,
    )
    results = find_skills(user, "FeeTable merchant")
    assert_true(len(results) >= 1, "Should find skill by description keywords")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_find_empty_query(opts):
    """Empty query returns no results."""
    from mojo.apps.assistant.services.skills import find_skills
    _, user, _ = _load_users()

    results = find_skills(user, "")
    assert_eq(len(results), 0, "Empty query should return no results")

    results = find_skills(user, "   ")
    assert_eq(len(results), 0, "Whitespace-only query should return no results")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_find_tier_scoping(opts):
    """Users only find skills in tiers they can read."""
    from mojo.apps.assistant.services.skills import save_skill, find_skills
    _cleanup()
    admin, user, other = _load_users()

    # Admin saves a user-tier skill
    save_skill(
        admin, tier="user", name="test-skill-admin-private",
        description="Admin's private skill",
        triggers=["admin only"],
        steps=SAMPLE_STEPS,
    )
    # Regular user saves their own
    save_skill(
        user, tier="user", name="test-skill-user-private",
        description="User's private skill",
        triggers=["user only"],
        steps=SAMPLE_STEPS,
    )

    # User should find their own but not admin's
    results = find_skills(user, "private skill")
    names = [r["name"] for r in results]
    assert_true("test-skill-user-private" in names, "User should find their own skill")
    assert_true("test-skill-admin-private" not in names, "User should NOT find admin's user-tier skill")

    # Admin (superuser) should find both
    results = find_skills(admin, "private skill")
    names = [r["name"] for r in results]
    assert_true("test-skill-admin-private" in names, "Admin should find their own skill")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_find_returns_steps(opts):
    """find_skills returns full step definitions."""
    from mojo.apps.assistant.services.skills import save_skill, find_skills
    _cleanup()
    _, user, _ = _load_users()

    save_skill(
        user, tier="user", name="test-skill-with-steps",
        description="Skill with detailed steps",
        triggers=["detailed steps"],
        steps=SAMPLE_STEPS,
    )
    results = find_skills(user, "detailed steps")
    assert_true(len(results) >= 1, "Should find the skill")
    assert_true("steps" in results[0], "Result should include steps")
    assert_eq(len(results[0]["steps"]), 2, "Should have 2 steps")
    assert_eq(results[0]["steps"][0]["tool"], "query_model", "First step tool should match")


# ---------------------------------------------------------------------------
# list_skills tests
# ---------------------------------------------------------------------------

@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_list_skills(opts):
    """list_skills returns all accessible skills grouped by tier."""
    from mojo.apps.assistant.services.skills import save_skill, list_skills
    _cleanup()
    admin, user, _ = _load_users()

    save_skill(
        user, tier="user", name="test-skill-list-user",
        description="User skill for listing",
        triggers=["list test"],
        steps=SAMPLE_STEPS,
    )
    # Global skills require admin
    save_skill(
        admin, tier="global", name="test-skill-list-global",
        description="Global skill for listing",
        triggers=["list test global"],
        steps=SAMPLE_STEPS,
    )

    result = list_skills(user)
    assert_true("user" in result, "Should have user tier in results")
    assert_true("global" in result, "Should have global tier in results")

    user_names = [s["name"] for s in result["user"]]
    assert_true("test-skill-list-user" in user_names, "User skill should be in list")

    global_names = [s["name"] for s in result["global"]]
    assert_true("test-skill-list-global" in global_names, "Global skill should be in list")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_list_skills_tier_filter(opts):
    """list_skills can filter by tier."""
    from mojo.apps.assistant.services.skills import save_skill, list_skills
    _cleanup()
    admin, user, _ = _load_users()

    save_skill(
        user, tier="user", name="test-skill-filter-user",
        description="User skill",
        triggers=["filter test"],
        steps=SAMPLE_STEPS,
    )
    save_skill(
        admin, tier="global", name="test-skill-filter-global",
        description="Global skill",
        triggers=["filter test"],
        steps=SAMPLE_STEPS,
    )

    result = list_skills(user, tier="user")
    assert_true("user" in result, "Should have user tier")
    assert_true("global" not in result, "Should NOT have global tier when filtered to user")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_list_no_steps(opts):
    """list_skills returns summaries without step details."""
    from mojo.apps.assistant.services.skills import save_skill, list_skills
    _cleanup()
    _, user, _ = _load_users()

    save_skill(
        user, tier="user", name="test-skill-no-steps",
        description="Summary only",
        triggers=["summary"],
        steps=SAMPLE_STEPS,
    )

    result = list_skills(user, tier="user")
    skill = result["user"][0]
    assert_true("steps" not in skill, "List view should not include steps")
    assert_true("step_count" in skill, "List view should include step_count")
    assert_eq(skill["step_count"], 2, "Step count should be 2")


# ---------------------------------------------------------------------------
# delete_skill tests
# ---------------------------------------------------------------------------

@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_delete_own_skill(opts):
    """User can delete their own skill."""
    from mojo.apps.assistant.services.skills import save_skill, delete_skill
    from mojo.apps.assistant.models import Skill
    _cleanup()
    _, user, _ = _load_users()

    save_skill(
        user, tier="user", name="test-skill-delete-me",
        description="To be deleted",
        triggers=["delete me"],
        steps=SAMPLE_STEPS,
    )
    skill = Skill.objects.get(tier="user", user=user, name="test-skill-delete-me")
    result = delete_skill(user, skill.pk)
    assert_true("error" not in result, "Owner should be able to delete own skill")
    assert_true(not Skill.objects.filter(pk=skill.pk).exists(), "Skill should be removed from DB")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_delete_other_user_skill(opts):
    """User cannot delete another user's skill."""
    from mojo.apps.assistant.services.skills import save_skill, delete_skill
    from mojo.apps.assistant.models import Skill
    _cleanup()
    admin, user, _ = _load_users()

    save_skill(
        admin, tier="user", name="test-skill-admin-owned",
        description="Admin's skill",
        triggers=["admin"],
        steps=SAMPLE_STEPS,
    )
    skill = Skill.objects.get(tier="user", user=admin, name="test-skill-admin-owned")
    result = delete_skill(user, skill.pk)
    assert_true("error" in result, "Non-owner should not be able to delete another user's skill")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_delete_admin_can_delete_any(opts):
    """Superuser can delete any skill."""
    from mojo.apps.assistant.services.skills import save_skill, delete_skill
    from mojo.apps.assistant.models import Skill
    _cleanup()
    admin, user, _ = _load_users()

    save_skill(
        user, tier="user", name="test-skill-user-owned",
        description="User's skill",
        triggers=["user skill"],
        steps=SAMPLE_STEPS,
    )
    skill = Skill.objects.get(tier="user", user=user, name="test-skill-user-owned")
    result = delete_skill(admin, skill.pk)
    assert_true("error" not in result, "Superuser should be able to delete any skill")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_delete_not_found(opts):
    """Deleting a nonexistent skill returns error."""
    from mojo.apps.assistant.services.skills import delete_skill
    _, user, _ = _load_users()

    result = delete_skill(user, 999999)
    assert_true("error" in result, "Deleting nonexistent skill should return error")


# ---------------------------------------------------------------------------
# Limits test
# ---------------------------------------------------------------------------

@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_tier_limit_enforced(opts):
    """Cannot exceed max skills per tier."""
    from mojo.apps.assistant.services.skills import save_skill
    from mojo.apps.assistant.models import Skill
    _cleanup()
    _, user, _ = _load_users()

    # Set a low limit via direct override and create skills up to it
    # We'll create skills and check the limit is enforced
    # Default is 20, so we create 20 then try one more
    # To keep test fast, we'll use direct model creation for bulk
    for i in range(20):
        Skill.objects.create(
            user=user, tier="user", name=f"limit-skill-{i}",
            description="Filler", triggers=[], steps=SAMPLE_STEPS,
        )

    result = save_skill(
        user, tier="user", name="test-skill-over-limit",
        description="Should fail",
        triggers=["over limit"],
        steps=SAMPLE_STEPS,
    )
    assert_true("error" in result, "Should reject skill when tier limit is reached")
    assert_true("limit" in result["error"].lower(), "Error should mention limit")


# ---------------------------------------------------------------------------
# get_skill tests
# ---------------------------------------------------------------------------

@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_get_skill_by_id(opts):
    """get_skill loads a skill by ID with full details."""
    from mojo.apps.assistant.services.skills import save_skill, get_skill
    from mojo.apps.assistant.models import Skill
    _cleanup()
    _, user, _ = _load_users()

    save_skill(
        user, tier="user", name="test-skill-get-by-id",
        description="Skill to load by ID",
        triggers=["load me"],
        steps=SAMPLE_STEPS,
    )
    skill = Skill.objects.get(tier="user", user=user, name="test-skill-get-by-id")
    result = get_skill(user, skill.pk)
    assert_true("error" not in result, "get_skill should succeed for own skill")
    assert_eq(result["name"], "test-skill-get-by-id", "Name should match")
    assert_true("steps" in result, "Should include full step definitions")
    assert_eq(len(result["steps"]), 2, "Should have 2 steps")
    assert_true("triggers" in result, "Should include triggers")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_get_skill_not_found(opts):
    """get_skill returns error for nonexistent ID."""
    from mojo.apps.assistant.services.skills import get_skill
    _, user, _ = _load_users()

    result = get_skill(user, 999999)
    assert_true("error" in result, "Should return error for nonexistent skill")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_get_skill_permission_denied(opts):
    """Cannot load another user's skill."""
    from mojo.apps.assistant.services.skills import save_skill, get_skill
    from mojo.apps.assistant.models import Skill
    _cleanup()
    _, user, other = _load_users()

    save_skill(
        user, tier="user", name="test-skill-private-get",
        description="Private skill",
        triggers=["private"],
        steps=SAMPLE_STEPS,
    )
    skill = Skill.objects.get(tier="user", user=user, name="test-skill-private-get")
    result = get_skill(other, skill.pk)
    assert_true("error" in result, "Other user should not be able to load user-tier skill")


# ---------------------------------------------------------------------------
# update_skill tests
# ---------------------------------------------------------------------------

@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_update_skill_description(opts):
    """update_skill can change just the description."""
    from mojo.apps.assistant.services.skills import save_skill, update_skill
    from mojo.apps.assistant.models import Skill
    _cleanup()
    _, user, _ = _load_users()

    save_skill(
        user, tier="user", name="test-skill-update-desc",
        description="Original description",
        triggers=["original"],
        steps=SAMPLE_STEPS,
    )
    skill = Skill.objects.get(tier="user", user=user, name="test-skill-update-desc")
    result = update_skill(user, skill.pk, description="Updated description")
    assert_true("error" not in result, "update_skill should succeed")
    assert_true("updated" in result["message"].lower(), "Should confirm update")

    skill.refresh_from_db()
    assert_eq(skill.description, "Updated description", "Description should be changed")
    assert_eq(skill.triggers, ["original"], "Triggers should be unchanged")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_update_skill_triggers(opts):
    """update_skill can change just the triggers."""
    from mojo.apps.assistant.services.skills import save_skill, update_skill
    from mojo.apps.assistant.models import Skill
    _cleanup()
    _, user, _ = _load_users()

    save_skill(
        user, tier="user", name="test-skill-update-triggers",
        description="Trigger test",
        triggers=["old trigger"],
        steps=SAMPLE_STEPS,
    )
    skill = Skill.objects.get(tier="user", user=user, name="test-skill-update-triggers")
    result = update_skill(user, skill.pk, triggers=["old trigger", "new trigger"])
    assert_true("error" not in result, "update_skill should succeed")

    skill.refresh_from_db()
    assert_eq(skill.triggers, ["old trigger", "new trigger"], "Triggers should be updated")
    assert_eq(skill.description, "Trigger test", "Description should be unchanged")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_update_skill_auto_execute(opts):
    """update_skill can toggle auto_execute."""
    from mojo.apps.assistant.services.skills import save_skill, update_skill
    from mojo.apps.assistant.models import Skill
    _cleanup()
    _, user, _ = _load_users()

    save_skill(
        user, tier="user", name="test-skill-update-auto",
        description="Auto test",
        triggers=["auto"],
        steps=SAMPLE_STEPS,
    )
    skill = Skill.objects.get(tier="user", user=user, name="test-skill-update-auto")
    assert_true(not skill.auto_execute, "Should start with auto_execute=False")

    result = update_skill(user, skill.pk, auto_execute=True)
    assert_true("error" not in result, "update_skill should succeed")

    skill.refresh_from_db()
    assert_true(skill.auto_execute, "auto_execute should now be True")

    # Toggle back to False — must not be silently dropped
    result = update_skill(user, skill.pk, auto_execute=False)
    assert_true("error" not in result, "update_skill should succeed setting auto_execute=False")
    skill.refresh_from_db()
    assert_true(not skill.auto_execute, "auto_execute should be back to False")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_update_skill_deactivate(opts):
    """update_skill can set is_active=False."""
    from mojo.apps.assistant.services.skills import save_skill, update_skill
    from mojo.apps.assistant.models import Skill
    _cleanup()
    _, user, _ = _load_users()

    save_skill(
        user, tier="user", name="test-skill-deactivate",
        description="Deactivation test",
        triggers=["deactivate"],
        steps=SAMPLE_STEPS,
    )
    skill = Skill.objects.get(tier="user", user=user, name="test-skill-deactivate")
    assert_true(skill.is_active, "Should start active")

    result = update_skill(user, skill.pk, is_active=False)
    assert_true("error" not in result, "update_skill should succeed setting is_active=False")
    skill.refresh_from_db()
    assert_true(not skill.is_active, "Skill should now be inactive")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_update_skill_name_collision(opts):
    """update_skill rejects name that collides with another skill in same scope."""
    from mojo.apps.assistant.services.skills import save_skill, update_skill
    from mojo.apps.assistant.models import Skill
    _cleanup()
    _, user, _ = _load_users()

    save_skill(
        user, tier="user", name="test-skill-name-a",
        description="Skill A",
        triggers=["a"],
        steps=SAMPLE_STEPS,
    )
    save_skill(
        user, tier="user", name="test-skill-name-b",
        description="Skill B",
        triggers=["b"],
        steps=SAMPLE_STEPS,
    )
    skill_b = Skill.objects.get(tier="user", user=user, name="test-skill-name-b")
    result = update_skill(user, skill_b.pk, name="test-skill-name-a")
    assert_true("error" in result, "Should reject name that collides with existing skill")
    assert_true("already exists" in result["error"].lower(), "Error should mention collision")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_update_skill_permission_denied(opts):
    """Cannot update another user's skill."""
    from mojo.apps.assistant.services.skills import save_skill, update_skill
    from mojo.apps.assistant.models import Skill
    _cleanup()
    _, user, other = _load_users()

    save_skill(
        user, tier="user", name="test-skill-update-denied",
        description="Private",
        triggers=["private"],
        steps=SAMPLE_STEPS,
    )
    skill = Skill.objects.get(tier="user", user=user, name="test-skill-update-denied")
    result = update_skill(other, skill.pk, description="Hacked")
    assert_true("error" in result, "Non-owner should not be able to update user-tier skill")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_update_skill_not_found(opts):
    """update_skill returns error for nonexistent ID."""
    from mojo.apps.assistant.services.skills import update_skill
    _, user, _ = _load_users()

    result = update_skill(user, 999999, description="Nope")
    assert_true("error" in result, "Should return error for nonexistent skill")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_update_skill_no_fields(opts):
    """update_skill with no valid fields returns error."""
    from mojo.apps.assistant.services.skills import save_skill, update_skill
    from mojo.apps.assistant.models import Skill
    _cleanup()
    _, user, _ = _load_users()

    save_skill(
        user, tier="user", name="test-skill-update-nofields",
        description="No change",
        triggers=["noop"],
        steps=SAMPLE_STEPS,
    )
    skill = Skill.objects.get(tier="user", user=user, name="test-skill-update-nofields")
    result = update_skill(user, skill.pk)
    assert_true("error" in result, "Should return error when no fields provided")


# ---------------------------------------------------------------------------
# build_skill_catalog tests
# ---------------------------------------------------------------------------

@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_build_skill_catalog_with_skills(opts):
    """build_skill_catalog returns markdown listing accessible skills."""
    from mojo.apps.assistant.services.skills import save_skill, build_skill_catalog
    _cleanup()
    admin, user, _ = _load_users()

    save_skill(
        user, tier="user", name="test-skill-catalog-user",
        description="A user skill for catalog test",
        triggers=["catalog test"],
        steps=SAMPLE_STEPS,
    )
    save_skill(
        admin, tier="global", name="test-skill-catalog-global",
        description="A global skill for catalog test",
        triggers=["global catalog"],
        steps=SAMPLE_STEPS,
        auto_execute=True,
    )

    catalog = build_skill_catalog(user)
    assert_true(len(catalog) > 0, "Catalog should not be empty when skills exist")
    assert_true("test-skill-catalog-user" in catalog, "Catalog should include user skill")
    assert_true("test-skill-catalog-global" in catalog, "Catalog should include global skill")
    assert_true("AUTO-EXECUTE" in catalog, "Catalog should mark auto_execute skills")
    assert_true("catalog test" in catalog, "Catalog should include trigger phrases")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_build_skill_catalog_empty(opts):
    """build_skill_catalog returns empty string when no skills exist."""
    from mojo.apps.assistant.services.skills import build_skill_catalog
    _cleanup()
    _, user, _ = _load_users()

    catalog = build_skill_catalog(user)
    assert_eq(catalog, "", "Catalog should be empty string when no skills exist")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_build_skill_catalog_tier_scoping(opts):
    """build_skill_catalog respects tier permissions."""
    from mojo.apps.assistant.services.skills import save_skill, build_skill_catalog
    _cleanup()
    _, user, other = _load_users()

    save_skill(
        user, tier="user", name="test-skill-catalog-scoped",
        description="Only visible to owner",
        triggers=["scoped"],
        steps=SAMPLE_STEPS,
    )

    catalog_user = build_skill_catalog(user)
    assert_true("test-skill-catalog-scoped" in catalog_user, "Owner should see own skill in catalog")

    catalog_other = build_skill_catalog(other)
    assert_true("test-skill-catalog-scoped" not in catalog_other, "Other user should not see user-tier skill")
