from testit import helpers as th
from testit import faker

TEST_USER = "testit"
TEST_PWORD = "testit##mojo"

ADMIN_USER = "tadmin"
ADMIN_PWORD = "testit##mojo"

@th.django_unit_setup()
def setup_users(opts):
    from mojo.apps.account.models import User
    user = User.objects.filter(username=TEST_USER).last()
    if user is None:
        user = User(username=TEST_USER, display_name=TEST_USER, email=f"{TEST_USER}@example.com")
        user.save()
    user.save_password(TEST_PWORD)
    user.remove_all_permissions()

    user = User.objects.filter(username=ADMIN_USER).last()
    if user is None:
        user = User(username=ADMIN_USER, display_name=ADMIN_USER, email=f"{ADMIN_USER}@example.com")
        user.save()
    user.remove_permission(["manage_groups"])
    user.add_permission(["manage_users", "view_global", "view_admin"])
    user.is_staff = True
    user.is_superuser = True
    user.save_password(ADMIN_PWORD)

    user = User.objects.filter(username="admin").last()
    if user is None:
        user = User(username="admin", display_name="System Admin", email=f"admin@example.com")
        user.is_staff = True
        user.is_superuser = True
        user.save()
        user.save_password(ADMIN_PWORD)
    user.add_permission(["manage_groups", "manage_users", "view_global", "view_admin"])


@th.unit_test("user_jwt_login")
def test_user_jwt_login(opts):
    resp = opts.client.login(TEST_USER, TEST_PWORD)
    assert opts.client.is_authenticated, "authentication failed"
    assert opts.client.jwt_data.uid is not None, "missing user id"
    resp = opts.client.get(f"/api/user/{opts.client.jwt_data.uid}")
    assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"
    assert resp.response.data.id == opts.client.jwt_data.uid
    assert resp.response.data.username == TEST_USER, f"username: {resp.response.data.username }"
    opts.user_id = opts.client.jwt_data.uid

@th.unit_test("admin_jwt_login")
def test_admin_jwt_login(opts):
    resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "authentication failed"
    assert opts.client.jwt_data.uid is not None, "missing user id"
    resp = opts.client.get(f"/api/user/{opts.client.jwt_data.uid}")
    assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"
    assert resp.response.data.id == opts.client.jwt_data.uid, f"invalid user id {resp.response.data.id}"
    assert resp.response.data.username == ADMIN_USER, f"username: {resp.response.data.username }"
    opts.admin_id = opts.client.jwt_data.uid

@th.unit_test("user_access_admin")
def test_user_access_admin(opts):
    resp = opts.client.login(TEST_USER, TEST_PWORD)
    assert opts.client.is_authenticated, "authentication failed"
    assert opts.client.jwt_data.uid is not None, "missing user id"
    resp = opts.client.get(f"/api/user/{opts.admin_id}")
    assert resp.status_code == 403, f"Expected status_code is 403 but got {resp.status_code}"


@th.unit_test("user_save_self")
def test_user_save_self(opts):
    resp = opts.client.login(TEST_USER, TEST_PWORD)
    assert opts.client.is_authenticated, "authentication failed"
    assert opts.client.jwt_data.uid is not None, "missing user id"
    name = faker.fake.last_name()
    resp = opts.client.post(f"/api/user/{opts.client.jwt_data.uid}", dict(display_name=name))
    assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"
    assert resp.response.data.display_name == name, f"display_name: {resp.response.data.username }"



@th.unit_test("user_add_perm")
def test_user_add_perm(opts):
    resp = opts.client.login(TEST_USER, TEST_PWORD)
    assert opts.client.is_authenticated, "authentication failed"
    assert opts.client.jwt_data.uid is not None, "missing user id"
    resp = opts.client.post(f"/api/user/{opts.client.jwt_data.uid}", dict(permissions=dict(view_users=True)))
    assert resp.status_code == 403, f"Expected status_code is 403 but got {resp.status_code}"


@th.unit_test("admin_access_user")
def test_admin_access_user(opts):
    resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "authentication failed"
    resp = opts.client.get(f"/api/user/{opts.user_id}")
    assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"
    assert resp.response.data.id == opts.user_id, f"invalid user id {resp.response.data.id}"
    assert resp.response.data.username == TEST_USER, f"username: {resp.response.data.username }"


@th.unit_test("admin_add_perm")
def test_admin_add_perm(opts):
    # resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "authentication failed"
    resp = opts.client.post(f"/api/user/{opts.user_id}", dict(permissions=dict(view_users=True)))
    assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"
    assert resp.response.data.id == opts.user_id, f"invalid user id {resp.response.data.id}"
    assert resp.response.data.username == TEST_USER, f"username: {resp.response.data.username }"
    assert resp.response.data.permissions.view_users is True, f"permissions: {resp.response.data.permissions}"

    resp = opts.client.post(f"/api/user/{opts.user_id}", {"permissions.invite_users":True})
    assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"
    assert resp.response.data.id == opts.user_id, f"invalid user id {resp.response.data.id}"
    assert resp.response.data.username == TEST_USER, f"username: {resp.response.data.username }"
    assert resp.response.data.permissions.invite_users is True, f"missing invite_users permissions: {resp.response.data.permissions}"


@th.unit_test("admin_remove_perm")
def test_admin_remove_perm(opts):
    # resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "authentication failed"
    resp = opts.client.post(f"/api/user/{opts.user_id}", dict(permissions=dict(view_users=None)))
    assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"
    assert resp.response.data.id == opts.user_id, f"invalid user id {resp.response.data.id}"
    assert resp.response.data.username == TEST_USER, f"username: {resp.response.data.username }"
    assert resp.response.data.permissions.view_users is None, f"permissions: {resp.response.data.permissions}"


@th.unit_test("admin_add_group")
def test_admin_add_group(opts):
    # resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "authentication failed"
    resp = opts.client.post(f"/api/user/{opts.admin_id}", dict(permissions=dict(manage_groups=True)))
    assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"
    assert resp.response.data.id == opts.admin_id, f"invalid user id {resp.response.data.id}"
    assert resp.response.data.username == ADMIN_USER, f"username: {resp.response.data.username }"
    assert resp.response.data.permissions.manage_groups is not None, f"permissions: {resp.response.data.permissions}"

    name=faker.generate_name()
    resp = opts.client.post("/api/group", dict(name=name))
    assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"
    assert resp.response.data.name == name, f"name: {resp.response.data.name }"
    opts.group_id = resp.response.data.id


@th.unit_test("user_cannot_list_group")
def test_user_cannot_list_group(opts):
    resp = opts.client.login(TEST_USER, TEST_PWORD)
    assert opts.client.is_authenticated, "authentication failed"
    resp = opts.client.get("/api/group", params=dict(id=opts.group_id))
    assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"
    assert resp.response.count == 0, "count is not 0"
    # groups = resp.response.data
    # for group in groups:
    #     if group.id == opts.group_id:
    #         assert group.name == opts.group_id, f"Expected group name was {opts.group_id}, but got {group.name}"
    #         break
    # else:
    #     assert False, f"Group with id {opts.group_id} not found in the list"



@th.unit_test("add_group_member")
def test_add_group_member(opts):
    resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "authentication failed"
    resp = opts.client.post("/api/group/member", dict(user=opts.user_id, group=opts.group_id))
    assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"
    assert resp.response.data.user.id == opts.user_id, f"user: {resp.response.data.user.id } vs {opts.user_id}"
    assert resp.response.data.group.id == opts.group_id, f"group: {resp.response.data.group.id }"
    opts.member_id = resp.response.data.id


@th.unit_test("user_can_list_group")
def test_user_can_list_group(opts):
    resp = opts.client.login(TEST_USER, TEST_PWORD)
    assert opts.client.is_authenticated, "authentication failed"
    resp = opts.client.get("/api/group", params=dict(id=opts.group_id))
    assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"
    assert resp.response.count == 1, f"size is not 1: {resp.response.count}"


@th.unit_test("user_can_get_group")
def test_user_can_get_group(opts):
    resp = opts.client.login(TEST_USER, TEST_PWORD)
    assert opts.client.is_authenticated, "authentication failed"
    resp = opts.client.get(f"/api/group/{opts.group_id}")
    assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"
    assert resp.response.data.id == opts.group_id, "id does not match"

@th.unit_test("edit_group_member")
def test_edit_group_member(opts):
    resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "authentication failed"

    name = faker.fake.last_name()
    resp = opts.client.post(f"/api/group/member/{opts.member_id}", dict(user=dict(display_name=name)))
    assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"
    assert resp.response.data.user.id == opts.user_id, f"user: {resp.response.data.user.id }"
    assert resp.response.data.user.display_name == name, f"display_name: {resp.response.data.user.display_name }"


# ============================================================================
# Hierarchical Permission Tests
# ============================================================================

@th.unit_test("create_parent_child_groups")
def test_create_parent_child_groups(opts):
    """Create a hierarchy: Organization > Department > Team"""
    resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "authentication failed"

    # Create parent organization
    org_name = f"TestOrg_{faker.fake.company()}"
    resp = opts.client.post("/api/group", dict(name=org_name))
    assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"
    assert resp.response.data.name == org_name, f"name: {resp.response.data.name}"
    opts.org_group_id = resp.response.data.id

    # Create child department
    dept_name = f"TestDept_{faker.fake.word()}"
    resp = opts.client.post("/api/group", dict(name=dept_name, parent=opts.org_group_id))
    assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"
    assert resp.response.data.name == dept_name, f"name: {resp.response.data.name}"
    # Parent may be returned as an object or ID
    parent_id = resp.response.data.parent.id if hasattr(resp.response.data.parent, 'id') else resp.response.data.parent
    assert parent_id == opts.org_group_id, f"parent should be {opts.org_group_id}, got {parent_id}"
    opts.dept_group_id = resp.response.data.id

    # Create grandchild team
    team_name = f"TestTeam_{faker.fake.word()}"
    resp = opts.client.post("/api/group", dict(name=team_name, parent=opts.dept_group_id))
    assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"
    assert resp.response.data.name == team_name, f"name: {resp.response.data.name}"
    # Parent may be returned as an object or ID
    parent_id = resp.response.data.parent.id if hasattr(resp.response.data.parent, 'id') else resp.response.data.parent
    assert parent_id == opts.dept_group_id, f"parent should be {opts.dept_group_id}, got {parent_id}"
    opts.team_group_id = resp.response.data.id


@th.unit_test("add_user_to_parent_group_only")
def test_add_user_to_parent_group_only(opts):
    """Add user to parent organization with view_groups permission"""
    resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "authentication failed"

    # Add user to organization with view_groups permission
    resp = opts.client.post("/api/group/member", dict(
        user=opts.user_id,
        group=opts.org_group_id,
        permissions=dict(view_groups=True)
    ))
    assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"
    assert resp.response.data.user.id == opts.user_id, f"user: {resp.response.data.user.id}"
    assert resp.response.data.group.id == opts.org_group_id, f"group: {resp.response.data.group.id}"
    opts.org_member_id = resp.response.data.id


@th.unit_test("get_member_for_user_finds_parent_membership")
def test_get_member_for_user_finds_parent_membership(opts):
    """Test that get_member_for_user finds parent membership for child group"""
    resp = opts.client.login(TEST_USER, TEST_PWORD)
    assert opts.client.is_authenticated, "authentication failed"

    # User should be able to get their member info for the team (via parent org membership)
    resp = opts.client.get(f"/api/group/{opts.team_group_id}/member")
    assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"
    # Should return parent org membership
    assert resp.response.data.id == opts.org_member_id, f"Should return org membership, got {resp.response.data.id}"
    assert resp.response.data.permissions.view_groups is True, f"Should have view_groups permission"


@th.unit_test("user_can_list_child_groups_via_parent")
def test_user_can_list_child_groups_via_parent(opts):
    """Test that user with parent membership can list child groups"""
    resp = opts.client.login(TEST_USER, TEST_PWORD)
    assert opts.client.is_authenticated, "authentication failed"

    # User should see all groups in hierarchy (org, dept, team)
    resp = opts.client.get("/api/group")
    assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"

    # Verify specific groups are in the list
    group_ids = [g.id for g in resp.response.data]
    assert opts.org_group_id in group_ids, f"Should see org group {opts.org_group_id}"
    assert opts.dept_group_id in group_ids, f"Should see dept group {opts.dept_group_id}"
    assert opts.team_group_id in group_ids, f"Should see team group {opts.team_group_id}"


@th.unit_test("user_can_access_child_group_via_parent")
def test_user_can_access_child_group_via_parent(opts):
    """Test that user with parent membership can access child group"""
    resp = opts.client.login(TEST_USER, TEST_PWORD)
    assert opts.client.is_authenticated, "authentication failed"

    # User should be able to access department group
    resp = opts.client.get(f"/api/group/{opts.dept_group_id}")
    assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"
    assert resp.response.data.id == opts.dept_group_id, "id does not match"

    # User should be able to access team group
    resp = opts.client.get(f"/api/group/{opts.team_group_id}")
    assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"
    assert resp.response.data.id == opts.team_group_id, "id does not match"


@th.django_unit_test("test_get_groups_with_permission")
def test_get_groups_with_permission(opts):
    """Test User.get_groups_with_permission() includes child groups"""
    from mojo.apps.account.models import User

    # Get the test user
    user = User.objects.get(id=opts.user_id)

    # User should have view_groups permission via org membership
    groups_with_perm = user.get_groups_with_permission(['view_groups'])

    # Should return org, dept, and team (all in hierarchy)
    group_ids = [g.id for g in groups_with_perm]
    assert opts.org_group_id in group_ids, "Should include org group"
    assert opts.dept_group_id in group_ids, "Should include dept group"
    assert opts.team_group_id in group_ids, "Should include team group"

    # Test with permission user doesn't have
    groups_without_perm = user.get_groups_with_permission(['manage_users'])
    assert groups_without_perm.count() == 0, "Should not have any groups with manage_users permission"


@th.unit_test("test_max_depth_protection")
def test_max_depth_protection(opts):
    """Test that max_depth prevents infinite loops"""
    from mojo.apps.account.models import User, Group

    resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "authentication failed"

    # Create a deep hierarchy (10 levels)
    parent_id = None
    group_ids = []

    for i in range(10):
        group_name = f"Level_{i}_{faker.fake.word()}"
        data = dict(name=group_name)
        if parent_id:
            data['parent'] = parent_id

        resp = opts.client.post("/api/group", data)
        assert resp.status_code == 200, f"Failed to create group at level {i}"
        group_ids.append(resp.response.data.id)
        parent_id = resp.response.data.id

    # Add user to the root (level 0) with permission
    resp = opts.client.post("/api/group/member", dict(
        user=opts.user_id,
        group=group_ids[0],
        permissions=dict(view_groups=True)
    ))
    assert resp.status_code == 200, f"Failed to add member"

    # Get the deepest group and check membership
    deepest_group = Group.objects.get(id=group_ids[9])
    user = User.objects.get(id=opts.user_id)

    # Should find member within max_depth (8), but level 9 is at depth 9
    # So this should NOT find the membership
    member = deepest_group.get_member_for_user(user, check_parents=True, max_depth=8)
    assert member is None, "Should not find member beyond max_depth of 8"

    # Test with level 7 (depth 7 from level 0) - should work
    level7_group = Group.objects.get(id=group_ids[7])
    member = level7_group.get_member_for_user(user, check_parents=True, max_depth=8)
    assert member is not None, "Should find member at depth 7"


@th.unit_test("user_without_parent_membership_cannot_access_child")
def test_user_without_parent_membership_cannot_access_child(opts):
    """Test that user without any membership cannot access groups"""
    # Create a new user without any group memberships
    from mojo.apps.account.models import User
    import time

    # Use timestamp to ensure unique username
    new_username = f"testuser_{int(time.time())}_{faker.fake.random_int(1000, 9999)}"
    new_email = f"{new_username}@example.com"

    # Delete any existing user with this username (cleanup)
    User.objects.filter(username=new_username).delete()

    new_user = User(
        username=new_username,
        email=new_email,
        display_name=new_username,
        is_active=True
    )
    new_user.save()
    new_user.save_password(TEST_PWORD)

    # Remove all memberships to ensure clean slate
    new_user.members.all().delete()

    # Login as new user
    resp = opts.client.login(new_username, TEST_PWORD)
    assert opts.client.is_authenticated, f"authentication failed for {new_username}"

    # Should not be able to access team group
    resp = opts.client.get(f"/api/group/{opts.team_group_id}")
    assert resp.status_code == 403, f"Expected status_code is 403 but got {resp.status_code}"

    # Should not see any groups in list (returns 200 with empty list)
    resp = opts.client.get("/api/group")
    # Debug: print response to see what's happening
    if resp.status_code != 200:
        print(f"Response status: {resp.status_code}")
        print(f"Response body: {resp.content if hasattr(resp, 'content') else resp.response}")
        print(f"Client authenticated: {opts.client.is_authenticated}")
    assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"
    assert resp.response.count == 0, f"Should not see any groups, got {resp.response.count}"
