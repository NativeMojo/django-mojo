"""
GroupScopedToken — a bearer that authenticates as a real user but is capped at
one group (mojo/apps/account/services/group_token.py).

Every test here is a confinement claim: the token authenticates as the visitor
and reaches exactly its own group's data, never another tenant's, and can never
be traded for a full platform JWT.

Minting happens IN THE TEST PROCESS via the service — the token is stateless
and the test server shares this Postgres database, so a token minted here
validates over there (same for epoch bumps, which are uncached).
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

def gt(token):
    """Authorization header for a group token."""
    return {"Authorization": f"grouptoken {token}"}


def apikey(token):
    return {"Authorization": f"apikey {token}"}


def _make_page(Page, book, title, owner, content):
    """Create a fixture Page WITHOUT queueing a docit_kb embed job.

    Page.save() publishes one fire-and-forget. Test modules run as parallel
    threads in one process, and other modules mock.patch `jobs.publish`
    globally for their own assertions — a stray publish from a fixture lands in
    someone else's mock. This test embeds its pages explicitly instead (see the
    docit search test), so the job is pure noise. Suppressed per INSTANCE, never
    on the class: a class-level patch would be the same cross-thread hazard.
    """
    page = Page(book=book, title=title, user=owner, created_by=owner,
                content=content)
    page._publish_embed_job = lambda: None
    page.save()
    return page


@th.django_unit_setup()
def setup_group_tokens(opts):
    from mojo.apps.account.models import User, Group, ApiKey
    from mojo.apps.docit.models import Book, Page
    from mojo.apps.chat.models import ChatRoom
    from mojo.apps.account.services import group_token
    from mojo.decorators.limits import clear_rate_limits

    clear_rate_limits(ip="127.0.0.1")

    # Long-lived DB: remove anything a previous run left behind BEFORE creating.
    # Group deletes cascade to Book / ChatRoom / GroupMember / ApiKey.
    Page.objects.filter(book__title__startswith="gt_").delete()
    Book.objects.filter(title__startswith="gt_").delete()
    ChatRoom.objects.filter(name__startswith="gt_").delete()
    ApiKey.objects.filter(name__startswith="gt_").delete()
    User.objects.filter(username__startswith="gt_").delete()
    Group.objects.filter(name__startswith="gt_").delete()

    # --- tenants -----------------------------------------------------------
    group_a = Group.objects.create(name="gt_tenant_a", kind="organization")
    group_b = Group.objects.create(name="gt_tenant_b", kind="organization")
    child_a = Group.objects.create(name="gt_child_a", kind="organization", parent=group_a)
    dark_parent = Group.objects.create(name="gt_dark_parent", kind="organization")
    dark_child = Group.objects.create(name="gt_dark_child", kind="organization",
                                      parent=dark_parent)
    revoke_group = Group.objects.create(name="gt_revoke_group", kind="organization")

    # --- identities --------------------------------------------------------
    # The visitor: an ordinary member of BOTH tenants, no global grants.
    visitor = User(username="gt_visitor", email="gt_visitor@example.com",
                   display_name="GT Visitor")
    visitor.save()

    # A staff visitor: the same shape, but holding every global grant this
    # suite probes. None of them may help inside a group token.
    staff = User(username="gt_staff", email="gt_staff@example.com",
                 display_name="GT Staff")
    staff.save()
    staff.add_permission(["manage_groups", "view_groups", "manage_users",
                          "view_users", "view_metrics", "view_docit",
                          "manage_chat", "geoip_sync", "view_admin"])
    staff.save()

    # The ApiKey acting member — separate from `staff` so the group-token tests
    # keep a member row with NO grants in tenant A.
    key_user = User(username="gt_keyuser", email="gt_keyuser@example.com")
    key_user.save()
    key_user.add_permission(["view_metrics"])
    key_user.save()

    superu = User(username="gt_super", email="gt_super@example.com",
                  is_superuser=True)
    superu.save()

    outsider = User(username="gt_outsider", email="gt_outsider@example.com")
    outsider.save()

    # Users used only by destructive (revocation) tests.
    temp_user = User(username="gt_temp_user", email="gt_temp@example.com")
    temp_user.save()

    ms_a = group_a.add_member(visitor)
    ms_a.add_permission("chat")
    ms_b = group_b.add_member(visitor)
    ms_b.add_permission("chat")
    group_a.add_member(staff)          # deliberately NO member-level grants
    group_b.add_member(staff)
    dark_child.add_member(visitor)
    revoke_group.add_member(visitor)
    group_a.add_member(temp_user)
    ms_key_a = group_a.add_member(key_user)
    ms_key_a.add_permission("view_metrics")
    group_b.add_member(key_user)       # member of B, but no grants there

    # --- group-scoped content (docit.Book: VIEW_PERMS includes "member") ----
    # Book.user / created_by are non-null PROTECT FKs — always pass them, and
    # always delete Books before Users in the cleanup above.
    book_a = Book.objects.create(title="gt_book_a", slug="gt-book-a", group=group_a,
                                 user=staff, created_by=staff)
    book_b = Book.objects.create(title="gt_book_b", slug="gt-book-b", group=group_b,
                                 user=staff, created_by=staff)
    book_child = Book.objects.create(title="gt_book_child", slug="gt-book-child",
                                     group=child_a, user=staff, created_by=staff)
    _make_page(Page, book_a, "gt_page_a", staff,
               "# A\n\nThe marker GTSEARCHMARKER77 lives in tenant A.\n")
    _make_page(Page, book_b, "gt_page_b", staff,
               "# B\n\nThe marker GTSEARCHMARKER77 lives in tenant B.\n")

    # --- chat rooms ---------------------------------------------------------
    room_a = ChatRoom.objects.create(name="gt_room_a", kind="channel", group=group_a)
    room_b = ChatRoom.objects.create(name="gt_room_b", kind="channel", group=group_b)
    room_none = ChatRoom.objects.create(name="gt_room_none", kind="channel", group=None)

    # --- api keys (for the reference-mode vs override pin) -------------------
    ref_key, ref_token = ApiKey.create_for_group(
        group_a, "gt_ref_key", permissions={"view_metrics": True})
    ovr_key, ovr_token = ApiKey.create_for_group(
        group_a, "gt_override_key", permissions={}, user=key_user,
        override_user=True)

    opts.group_a_id = group_a.id
    opts.group_b_id = group_b.id
    opts.child_a_id = child_a.id
    opts.dark_parent_id = dark_parent.id
    opts.dark_child_id = dark_child.id
    opts.revoke_group_id = revoke_group.id
    opts.visitor_id = visitor.id
    opts.staff_id = staff.id
    opts.super_id = superu.id
    opts.outsider_id = outsider.id
    opts.temp_user_id = temp_user.id
    opts.key_user_id = key_user.id
    opts.book_a_id = book_a.id
    opts.book_b_id = book_b.id
    opts.book_child_id = book_child.id
    opts.room_a_id = room_a.id
    opts.room_b_id = room_b.id
    opts.room_none_id = room_none.id
    opts.ref_key_token = ref_token
    opts.override_key_token = ovr_token

    # The tokens under test.
    opts.token_a = group_token.mint(visitor, group_a)
    opts.token_b = group_token.mint(visitor, group_b)
    opts.token_staff_a = group_token.mint(staff, group_a)


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------

@th.django_unit_test("group token reads its own tenant's rows")
def test_happy_path_reads_own_group(opts):
    resp = opts.client.get(f"/api/docit/book/{opts.book_a_id}", headers=gt(opts.token_a))
    assert_eq(resp.status_code, 200,
              f"A-token must read a Book in tenant A, got {resp.status_code}: {resp.response}")
    assert_eq(resp.response.data.id, opts.book_a_id,
              f"expected book {opts.book_a_id}, got {resp.response.data}")


@th.django_unit_test("group token list is confined to its own tenant")
def test_happy_path_list_confined(opts):
    resp = opts.client.get("/api/docit/book", headers=gt(opts.token_a))
    assert_eq(resp.status_code, 200,
              f"A-token book list must succeed, got {resp.status_code}: {resp.response}")
    ids = [row["id"] for row in resp.response.data]
    assert_true(opts.book_a_id in ids,
                f"A-token list must include tenant A's book {opts.book_a_id}: {ids}")
    assert_true(opts.book_b_id not in ids,
                f"A-token list must NOT include tenant B's book {opts.book_b_id}: {ids}")
    assert_true(opts.book_child_id not in ids,
                f"A-token list must NOT include the child group's book "
                f"{opts.book_child_id} (strict equality, no descendants): {ids}")


# ---------------------------------------------------------------------------
# 2. Cross-tenant reach
# ---------------------------------------------------------------------------

@th.django_unit_test("group token cannot read another tenant's row")
def test_cross_tenant_detail_denied(opts):
    resp = opts.client.get(f"/api/docit/book/{opts.book_b_id}", headers=gt(opts.token_a))
    assert_eq(resp.status_code, 403,
              f"A-token must be denied tenant B's book, got {resp.status_code}: {resp.response}")


@th.django_unit_test("group token cannot reach a DESCENDANT group's row")
def test_descendant_group_denied(opts):
    resp = opts.client.get(f"/api/docit/book/{opts.book_child_id}",
                           headers=gt(opts.token_a))
    assert_eq(resp.status_code, 403,
              f"A-token must be denied a child group's book (no descendants), "
              f"got {resp.status_code}: {resp.response}")


@th.django_unit_test("dispatcher refuses ?group= outside the token's group")
def test_group_param_rebind_denied(opts):
    resp = opts.client.get("/api/docit/book", params={"group": opts.group_b_id},
                           headers=gt(opts.token_a))
    assert_eq(resp.status_code, 403,
              f"?group=<B> under an A-token must be refused by the dispatcher, "
              f"got {resp.status_code}: {resp.response}")


@th.django_unit_test("?group= for the token's own group still works")
def test_group_param_own_group_allowed(opts):
    resp = opts.client.get("/api/docit/book", params={"group": opts.group_a_id},
                           headers=gt(opts.token_a))
    assert_eq(resp.status_code, 200,
              f"?group=<A> under an A-token must be allowed, "
              f"got {resp.status_code}: {resp.response}")


@th.django_unit_test("metrics: account=group-<other> is refused")
def test_metrics_cross_tenant_denied(opts):
    resp = opts.client.get("/api/metrics/fetch",
                           params={"slug": "gt_probe",
                                   "account": f"group-{opts.group_b_id}"},
                           headers=gt(opts.token_a))
    assert_eq(resp.status_code, 403,
              f"A-token must not read tenant B metrics, "
              f"got {resp.status_code}: {resp.response}")


@th.django_unit_test("metrics: account=user-<other> is refused")
def test_metrics_other_user_denied(opts):
    resp = opts.client.get("/api/metrics/fetch",
                           params={"slug": "gt_probe",
                                   "account": f"user-{opts.staff_id}"},
                           headers=gt(opts.token_a))
    assert_eq(resp.status_code, 403,
              f"A-token must not read another user's metrics, "
              f"got {resp.status_code}: {resp.response}")


@th.django_unit_test("metrics: own user account still readable")
def test_metrics_own_user_allowed(opts):
    resp = opts.client.get("/api/metrics/fetch",
                           params={"slug": "gt_probe",
                                   "account": f"user-{opts.visitor_id}"},
                           headers=gt(opts.token_a))
    assert_eq(resp.status_code, 200,
              f"a group token must still read its OWN user metrics, "
              f"got {resp.status_code}: {resp.response}")


@th.django_unit_test("group/<pk>/member is confined to the token's group")
def test_group_member_endpoint_confined(opts):
    denied = opts.client.get(f"/api/group/{opts.group_b_id}/member",
                             headers=gt(opts.token_a))
    assert_eq(denied.status_code, 403,
              f"A-token must not read the visitor's member row in tenant B, "
              f"got {denied.status_code}: {denied.response}")
    allowed = opts.client.get(f"/api/group/{opts.group_a_id}/member",
                              headers=gt(opts.token_a))
    assert_eq(allowed.status_code, 200,
              f"A-token must still read its OWN member row, "
              f"got {allowed.status_code}: {allowed.response}")


# ---------------------------------------------------------------------------
# 3. Group records: detail AND list are opaque
# ---------------------------------------------------------------------------

@th.django_unit_test("group token cannot read a Group record — own group included")
def test_group_detail_denied(opts):
    own = opts.client.get(f"/api/group/{opts.group_a_id}", headers=gt(opts.token_a))
    assert_eq(own.status_code, 403,
              f"group token must not read the Group record (own group included), "
              f"got {own.status_code}: {own.response}")
    other = opts.client.get(f"/api/group/{opts.group_b_id}", headers=gt(opts.token_a))
    assert_eq(other.status_code, 403,
              f"group token must not read another tenant's Group record, "
              f"got {other.status_code}: {other.response}")


@th.django_unit_test("global manage_groups does not open Group records to a token")
def test_group_detail_denied_for_global_admin(opts):
    resp = opts.client.get(f"/api/group/{opts.group_a_id}",
                           headers=gt(opts.token_staff_a))
    assert_eq(resp.status_code, 403,
              f"a global manage_groups holder under a group token must STILL be "
              f"denied the Group record, got {resp.status_code}: {resp.response}")


@th.django_unit_test("group token cannot write a Group record")
def test_group_write_denied(opts):
    resp = opts.client.post(f"/api/group/{opts.group_a_id}", {"name": "gt_pwned"},
                            headers=gt(opts.token_staff_a))
    assert_eq(resp.status_code, 403,
              f"group token must not write a Group record, "
              f"got {resp.status_code}: {resp.response}")


@th.django_unit_test("GET /api/group returns an empty list for a group token")
def test_group_list_empty(opts):
    resp = opts.client.get("/api/group", params={"size": 1000},
                           headers=gt(opts.token_staff_a))
    assert_eq(resp.status_code, 200,
              f"group LIST must answer 200 with an empty payload, "
              f"got {resp.status_code}: {resp.response}")
    assert_eq(len(resp.response.data), 0,
              f"group LIST under a group token must be EMPTY even though the "
              f"visitor belongs to two tenants, got {resp.response.data}")


# ---------------------------------------------------------------------------
# 4. Groupless models
# ---------------------------------------------------------------------------

@th.django_unit_test("groupless model list denied even with global perms")
def test_groupless_list_denied(opts):
    resp = opts.client.get("/api/user", params={"size": 100},
                           headers=gt(opts.token_staff_a))
    assert_eq(resp.status_code, 403,
              f"a global manage_users holder under a group token must not list "
              f"users (groupless model), got {resp.status_code}: {resp.response}")


@th.django_unit_test("groupless model detail by pk denied")
def test_groupless_detail_denied(opts):
    resp = opts.client.get(f"/api/user/{opts.visitor_id}",
                           headers=gt(opts.token_staff_a))
    assert_eq(resp.status_code, 403,
              f"a group token must not read another user by pk, "
              f"got {resp.status_code}: {resp.response}")


@th.django_unit_test("owner-scoped credential models denied to a group token")
def test_owner_scoped_credential_list_denied(opts):
    resp = opts.client.get("/api/account/passkeys", headers=gt(opts.token_a))
    assert_eq(resp.status_code, 403,
              f"a group token must never satisfy owner perms on a credential "
              f"model, got {resp.status_code}: {resp.response}")


# ---------------------------------------------------------------------------
# 5. Revocation
# ---------------------------------------------------------------------------

@th.django_unit_test("epoch bump revokes outstanding tokens")
def test_epoch_bump_revokes(opts):
    from mojo.apps.account.models import Group, User
    from mojo.apps.account.services import group_token

    group = Group.objects.get(pk=opts.revoke_group_id)
    visitor = User.objects.get(pk=opts.visitor_id)
    token = group_token.mint(visitor, group)
    before = opts.client.get("/api/user/me", headers=gt(token))
    assert_eq(before.status_code, 200,
              f"freshly minted token must authenticate, got {before.status_code}")

    group.bump_group_token_epoch()
    after = opts.client.get("/api/user/me", headers=gt(token))
    assert_eq(after.status_code, 401,
              f"an epoch bump must revoke the outstanding token, "
              f"got {after.status_code}: {after.response}")

    # And a token minted after the bump works again.
    group.refresh_from_db()
    fresh = group_token.mint(visitor, group)
    resp = opts.client.get("/api/user/me", headers=gt(fresh))
    assert_eq(resp.status_code, 200,
              f"a token minted after the bump must authenticate, "
              f"got {resp.status_code}: {resp.response}")


@th.django_unit_test("revoke_group_tokens action bumps the epoch")
def test_revoke_action_bumps_epoch(opts):
    from mojo.apps.account.models import Group

    group = Group.objects.get(pk=opts.revoke_group_id)
    before = group.get_group_token_epoch()
    result = group.on_action_revoke_group_tokens(True)
    group.refresh_from_db()
    assert_eq(group.get_group_token_epoch(), before + 1,
              f"on_action_revoke_group_tokens must increment the epoch from "
              f"{before}, got {group.get_group_token_epoch()}")
    assert_true(result["status"] is True,
                f"revoke action must report success, got {result}")


@th.django_unit_test("epoch lives under protected metadata")
def test_epoch_is_protected_metadata(opts):
    from mojo.apps.account.models import Group

    group = Group.objects.get(pk=opts.revoke_group_id)
    protected = (group.metadata or {}).get("protected") or {}
    assert_true("group_token_epoch" in protected,
                f"the epoch must live under metadata['protected'] so tenant "
                f"admins cannot rewind it, got metadata={group.metadata}")


@th.django_unit_test("auth_key rotation revokes that user's tokens")
def test_auth_key_rotation_revokes(opts):
    import uuid
    from mojo.apps.account.models import Group, User
    from mojo.apps.account.services import group_token

    group = Group.objects.get(pk=opts.revoke_group_id)
    user = User.objects.get(pk=opts.temp_user_id)
    group.add_member(user)
    token = group_token.mint(user, group)
    before = opts.client.get("/api/user/me", headers=gt(token))
    assert_eq(before.status_code, 200,
              f"token must authenticate before rotation, got {before.status_code}")

    user.auth_key = uuid.uuid4().hex
    user.save(update_fields=["auth_key", "modified"])
    after = opts.client.get("/api/user/me", headers=gt(token))
    assert_eq(after.status_code, 401,
              f"rotating auth_key must revoke the user's group tokens, "
              f"got {after.status_code}: {after.response}")


@th.django_unit_test("membership removal revokes the token")
def test_membership_removal_revokes(opts):
    from mojo.apps.account.models import Group, User
    from mojo.apps.account.services import group_token

    group = Group.objects.get(pk=opts.revoke_group_id)
    user = User.objects.get(pk=opts.outsider_id)
    group.add_member(user)
    token = group_token.mint(user, group)
    assert_eq(opts.client.get("/api/user/me", headers=gt(token)).status_code, 200,
              "token must authenticate while the membership exists")

    group.members.filter(user=user).delete()
    resp = opts.client.get("/api/user/me", headers=gt(token))
    assert_eq(resp.status_code, 401,
              f"removing the GroupMember row must revoke the token, "
              f"got {resp.status_code}: {resp.response}")


@th.django_unit_test("deactivating the group revokes the token")
def test_group_deactivation_revokes(opts):
    from mojo.apps.account.models import Group, User
    from mojo.apps.account.services import group_token

    group = Group.objects.create(name="gt_temp_group", kind="organization")
    user = User.objects.get(pk=opts.visitor_id)
    group.add_member(user)
    token = group_token.mint(user, group)
    assert_eq(opts.client.get("/api/user/me", headers=gt(token)).status_code, 200,
              "token must authenticate while the group is active")

    group.is_active = False
    group.save(update_fields=["is_active", "modified"])
    resp = opts.client.get("/api/user/me", headers=gt(token))
    assert_eq(resp.status_code, 401,
              f"deactivating the group must revoke its tokens, "
              f"got {resp.status_code}: {resp.response}")
    group.delete()


@th.django_unit_test("deactivating an ANCESTOR revokes the token")
def test_ancestor_deactivation_revokes(opts):
    from mojo.apps.account.models import Group, User
    from mojo.apps.account.services import group_token

    parent = Group.objects.get(pk=opts.dark_parent_id)
    child = Group.objects.get(pk=opts.dark_child_id)
    user = User.objects.get(pk=opts.visitor_id)
    token = group_token.mint(user, child)
    assert_eq(opts.client.get("/api/user/me", headers=gt(token)).status_code, 200,
              "token must authenticate while the whole chain is active")

    parent.is_active = False
    parent.save(update_fields=["is_active", "modified"])
    resp = opts.client.get("/api/user/me", headers=gt(token))
    assert_eq(resp.status_code, 401,
              f"a deactivated ANCESTOR must revoke the child group's tokens, "
              f"got {resp.status_code}: {resp.response}")
    parent.is_active = True
    parent.save(update_fields=["is_active", "modified"])


@th.django_unit_test("deactivating the user revokes the token")
def test_user_deactivation_revokes(opts):
    from mojo.apps.account.models import Group, User
    from mojo.apps.account.services import group_token

    group = Group.objects.get(pk=opts.group_a_id)
    user = User(username="gt_deact_user", email="gt_deact@example.com")
    user.save()
    group.add_member(user)
    token = group_token.mint(user, group)
    assert_eq(opts.client.get("/api/user/me", headers=gt(token)).status_code, 200,
              "token must authenticate while the user is active")

    user.is_active = False
    user.save(update_fields=["is_active", "modified"])
    resp = opts.client.get("/api/user/me", headers=gt(token))
    assert_eq(resp.status_code, 401,
              f"deactivating the user must revoke their group tokens, "
              f"got {resp.status_code}: {resp.response}")
    user.delete()


# ---------------------------------------------------------------------------
# 6. Expiry and clock skew
# ---------------------------------------------------------------------------

@th.django_unit_test("expired token 401s with the one distinct message")
def test_expired_token(opts):
    from mojo.helpers import dates
    from mojo.apps.account.models import Group, User
    from mojo.apps.account.services import group_token

    group = Group.objects.get(pk=opts.group_a_id)
    user = User.objects.get(pk=opts.visitor_id)
    old = int(dates.utcnow().timestamp()) - 999999
    token = group_token.mint(user, group, issued_at=old)
    resp = opts.client.get("/api/user/me", headers=gt(token))
    assert_eq(resp.status_code, 401,
              f"an expired token must 401, got {resp.status_code}: {resp.response}")
    assert_eq(resp.response.error, group_token.EXPIRED_TOKEN,
              f"expiry is the ONE distinct failure message, got {resp.response}")


@th.django_unit_test("a token issued in the future is refused")
def test_future_iat_refused(opts):
    from mojo.helpers import dates
    from mojo.apps.account.models import Group, User
    from mojo.apps.account.services import group_token

    group = Group.objects.get(pk=opts.group_a_id)
    user = User.objects.get(pk=opts.visitor_id)
    future = int(dates.utcnow().timestamp()) + 120
    token = group_token.mint(user, group, issued_at=future)
    resp = opts.client.get("/api/user/me", headers=gt(token))
    assert_eq(resp.status_code, 401,
              f"an iat 120s in the future exceeds the skew tolerance and must "
              f"401, got {resp.status_code}: {resp.response}")
    assert_eq(resp.response.error, group_token.INVALID_TOKEN,
              f"a future iat must use the generic message (no oracle), "
              f"got {resp.response}")


# ---------------------------------------------------------------------------
# 7. Tampering — all clean 401s, never a 500
# ---------------------------------------------------------------------------

@th.django_unit_test("tampered / malformed tokens 401 and never 500")
def test_tampering_never_500(opts):
    token = opts.token_a
    version, payload, sig = token.split(".")
    flipped_payload = payload[:-2] + ("AA" if payload[-2:] != "AA" else "BB")

    cases = {
        "flipped payload byte": f"{version}.{flipped_payload}.{sig}",
        "truncated signature": f"{version}.{payload}.{sig[:6]}",
        "empty signature": f"{version}.{payload}.",
        "non-ascii signature": f"{version}.{payload}.{'ü' * 8}",
        "garbage b64 payload": f"{version}.@@@not-base64@@@.{sig}",
        "non-dict payload": "gt1.aGVsbG8=." + sig,
        "two parts": f"{version}.{payload}",
        "four parts": f"{version}.{payload}.{sig}.extra",
        "wrong version tag": f"gt9.{payload}.{sig}",
        "empty payload and signature": "gt1..",
    }
    for name, bad in cases.items():
        resp = opts.client.get("/api/user/me", headers=gt(bad))
        assert_eq(resp.status_code, 401,
                  f"{name}: must be a clean 401, got {resp.status_code}: "
                  f"{resp.response}")


@th.django_unit_test("a gt1 token is not a JWT — bearer replay 401s")
def test_gt1_under_bearer_denied(opts):
    resp = opts.client.get("/api/user/me",
                           headers={"Authorization": f"bearer {opts.token_a}"})
    assert_eq(resp.status_code, 401,
              f"a gt1 token replayed as a JWT must 401, "
              f"got {resp.status_code}: {resp.response}")


@th.django_unit_test("a gt1 token cannot be spent as a refresh_token")
def test_gt1_as_refresh_token_denied(opts):
    resp = opts.client.post("/api/refresh_token", {"refresh_token": opts.token_a})
    assert_eq(resp.status_code, 401,
              f"a gt1 token must not be exchangeable for a JWT pair, "
              f"got {resp.status_code}: {resp.response}")


@th.django_unit_test("a JWT pasted under grouptoken 401s")
def test_jwt_under_grouptoken_denied(opts):
    from mojo.apps.account.models import User

    user = User.objects.get(pk=opts.visitor_id)
    package = user.generate_jwt()
    resp = opts.client.get("/api/user/me", headers=gt(package.access_token))
    assert_eq(resp.status_code, 401,
              f"a JWT presented under the grouptoken scheme must 401, "
              f"got {resp.status_code}: {resp.response}")


@th.django_unit_test("a token signed with another user's key is refused")
def test_cross_user_forgery_refused(opts):
    from mojo.helpers import crypto, dates
    from mojo.apps.account.models import User

    # Forge a payload naming the visitor but sign it with the outsider's key.
    outsider = User.objects.get(pk=opts.outsider_id)
    payload = crypto.b64_encode({
        "u": opts.visitor_id, "g": opts.group_a_id, "e": 0,
        "iat": int(dates.utcnow().timestamp()),
    })
    signed = f"gt1.{payload}"
    forged = f"{signed}.{crypto.sign(signed, outsider.get_auth_key())}"
    resp = opts.client.get("/api/user/me", headers=gt(forged))
    assert_eq(resp.status_code, 401,
              f"a token signed with a different user's key must 401, "
              f"got {resp.status_code}: {resp.response}")


@th.django_unit_test("a bare-payload signature (utils.tokens shape) is refused")
def test_domain_separation(opts):
    from mojo.helpers import crypto, dates
    from mojo.apps.account.models import Group, User

    # Same payload, signed WITHOUT the "gt1." domain tag — the shape
    # mojo/apps/account/utils/tokens.py produces with the same per-user key.
    user = User.objects.get(pk=opts.visitor_id)
    group = Group.objects.get(pk=opts.group_a_id)
    payload = crypto.b64_encode({
        "u": user.pk, "g": group.pk, "e": group.get_group_token_epoch(),
        "iat": int(dates.utcnow().timestamp()),
    })
    undomained = f"gt1.{payload}.{crypto.sign(payload, user.get_auth_key())}"
    resp = opts.client.get("/api/user/me", headers=gt(undomained))
    assert_eq(resp.status_code, 401,
              f"a signature over the BARE payload must be refused — the version "
              f"tag is inside the HMAC, got {resp.status_code}: {resp.response}")


# ---------------------------------------------------------------------------
# 8. Upgrade paths
# ---------------------------------------------------------------------------

@th.django_unit_test("auth/handoff refuses a group token")
def test_handoff_denied(opts):
    resp = opts.client.post("/api/auth/handoff",
                            {"redirect_uri": "https://example.com/"},
                            headers=gt(opts.token_a))
    assert_eq(resp.status_code, 403,
              f"a group token must not mint a handoff code, "
              f"got {resp.status_code}: {resp.response}")


@th.django_unit_test("credential-mutating User actions refuse a group token")
def test_credential_actions_denied(opts):
    revoke = opts.client.post("/api/user/me", {"revoke_sessions": True},
                              headers=gt(opts.token_a))
    assert_eq(revoke.status_code, 403,
              f"revoke_sessions must be refused under a group token, "
              f"got {revoke.status_code}: {revoke.response}")

    totp = opts.client.post("/api/account/totp/setup", {}, headers=gt(opts.token_a))
    assert_eq(totp.status_code, 403,
              f"TOTP enrollment must be refused under a group token, "
              f"got {totp.status_code}: {totp.response}")

    key = opts.client.post("/api/account/api_keys", {"name": "gt_pwned"},
                           headers=gt(opts.token_a))
    assert_true(key.status_code in (403, 404),
                f"minting a user API key must not succeed under a group token, "
                f"got {key.status_code}: {key.response}")


@th.django_unit_test("requires_global_perms refuses a group token")
def test_requires_global_perms_denied(opts):
    resp = opts.client.post("/api/system/geoip/sync",
                            {"ip": "203.0.113.7", "threat_level": 3},
                            headers=gt(opts.token_staff_a))
    assert_eq(resp.status_code, 403,
              f"a group token must not satisfy requires_global_perms even with "
              f"allow_api_keys=True and a global geoip_sync grant, "
              f"got {resp.status_code}: {resp.response}")


# ---------------------------------------------------------------------------
# 10. /api/user/me — read-only self
# ---------------------------------------------------------------------------

@th.django_unit_test("GET /api/user/me works under a group token")
def test_user_me_get(opts):
    resp = opts.client.get("/api/user/me", headers=gt(opts.token_a))
    assert_eq(resp.status_code, 200,
              f"the documented client bootstrap must keep working, "
              f"got {resp.status_code}: {resp.response}")
    assert_eq(resp.response.data.id, opts.visitor_id,
              f"/me must return the token's own user, got {resp.response.data}")


@th.django_unit_test("POST /api/user/me is refused under a group token")
def test_user_me_write_denied(opts):
    resp = opts.client.post("/api/user/me", {"display_name": "gt_pwned"},
                            headers=gt(opts.token_a))
    assert_eq(resp.status_code, 403,
              f"tenant-page JS must never mutate the platform account, "
              f"got {resp.status_code}: {resp.response}")

    from mojo.apps.account.models import User
    user = User.objects.get(pk=opts.visitor_id)
    assert_true(user.display_name != "gt_pwned",
                f"display_name must be unchanged, got {user.display_name!r}")


# ---------------------------------------------------------------------------
# 11-13. Mint guards, superuser hard block, WebSocket refusal
# ---------------------------------------------------------------------------

@th.django_unit_test("mint refuses superusers, non-members and inactive groups")
def test_mint_guards(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import Group, User
    from mojo.apps.account.services import group_token

    group_a = Group.objects.get(pk=opts.group_a_id)
    child_a = Group.objects.get(pk=opts.child_a_id)
    visitor = User.objects.get(pk=opts.visitor_id)
    superu = User.objects.get(pk=opts.super_id)
    outsider = User.objects.get(pk=opts.outsider_id)

    group_a.add_member(superu)
    with th.assert_raises(merrors.PermissionDeniedException):
        group_token.mint(superu, group_a)

    with th.assert_raises(merrors.PermissionDeniedException):
        group_token.mint(outsider, group_a)

    # Member of the PARENT only — delegation must not exceed the delegator.
    with th.assert_raises(merrors.PermissionDeniedException):
        group_token.mint(visitor, child_a)

    inactive = Group.objects.create(name="gt_inactive_mint", kind="organization")
    inactive.add_member(visitor)
    inactive.is_active = False
    inactive.save(update_fields=["is_active", "modified"])
    with th.assert_raises(merrors.PermissionDeniedException):
        group_token.mint(visitor, inactive)
    inactive.delete()


@th.django_unit_test("a promoted superuser's outstanding token is refused")
def test_superuser_promotion_blocks_validate(opts):
    from mojo.apps.account.models import Group, User
    from mojo.apps.account.services import group_token

    group = Group.objects.get(pk=opts.group_a_id)
    user = User(username="gt_promote_user", email="gt_promote@example.com")
    user.save()
    group.add_member(user)
    token = group_token.mint(user, group)
    assert_eq(opts.client.get("/api/user/me", headers=gt(token)).status_code, 200,
              "token must authenticate before the promotion")

    user.is_superuser = True
    user.save(update_fields=["is_superuser", "modified"])
    resp = opts.client.get("/api/user/me", headers=gt(token))
    assert_eq(resp.status_code, 401,
              f"a token whose user was promoted to superuser must be refused at "
              f"auth time, got {resp.status_code}: {resp.response}")
    user.delete()


@th.django_unit_test("validate_token(request=None) is refused (WebSocket path)")
def test_validate_without_request_refused(opts):
    from mojo.apps.account.services import group_token

    instance, error = group_token.validate_token(opts.token_a, None)
    assert_true(instance is None,
                f"a handler called with request=None must not authenticate, "
                f"got {instance}")
    assert_eq(error, group_token.INVALID_TOKEN,
              f"the request=None refusal must use the generic message, got {error}")


# ---------------------------------------------------------------------------
# 14. Chat — every room-resolving endpoint
# ---------------------------------------------------------------------------

@th.django_unit_test("room/join in another tenant is refused and writes nothing")
def test_chat_join_cross_tenant_denied(opts):
    from mojo.apps.chat.models import ChatMembership

    ChatMembership.objects.filter(room_id=opts.room_b_id,
                                  user_id=opts.visitor_id).delete()
    resp = opts.client.post("/api/chat/room/join", {"room_id": opts.room_b_id},
                            headers=gt(opts.token_a))
    assert_eq(resp.status_code, 403,
              f"an A-token must not join a room in tenant B, "
              f"got {resp.status_code}: {resp.response}")
    exists = ChatMembership.objects.filter(room_id=opts.room_b_id,
                                           user_id=opts.visitor_id).exists()
    assert_true(not exists,
                "a refused cross-tenant join must create no ChatMembership row")


@th.django_unit_test("room/join in the token's own tenant works")
def test_chat_join_own_tenant_allowed(opts):
    from mojo.apps.chat.models import ChatMembership

    ChatMembership.objects.filter(room_id=opts.room_a_id,
                                  user_id=opts.visitor_id).delete()
    resp = opts.client.post("/api/chat/room/join", {"room_id": opts.room_a_id},
                            headers=gt(opts.token_a))
    assert_eq(resp.status_code, 200,
              f"an A-token must be able to join a channel in tenant A, "
              f"got {resp.status_code}: {resp.response}")


@th.django_unit_test("room/members and room/messages are tenant-bound")
def test_chat_reads_cross_tenant_denied(opts):
    members = opts.client.get("/api/chat/room/members",
                              params={"room_id": opts.room_b_id},
                              headers=gt(opts.token_a))
    assert_eq(members.status_code, 403,
              f"an A-token must not read tenant B's roster, "
              f"got {members.status_code}: {members.response}")

    messages = opts.client.get("/api/chat/room/messages",
                               params={"room_id": opts.room_b_id},
                               headers=gt(opts.token_a))
    assert_eq(messages.status_code, 403,
              f"an A-token must not read tenant B's messages, "
              f"got {messages.status_code}: {messages.response}")


@th.django_unit_test("a groupless room denies a group token")
def test_chat_groupless_room_denied(opts):
    resp = opts.client.post("/api/chat/room/join", {"room_id": opts.room_none_id},
                            headers=gt(opts.token_a))
    assert_eq(resp.status_code, 403,
              f"a room with no group belongs to no tenant and must deny a "
              f"confined credential, got {resp.status_code}: {resp.response}")

    dm = opts.client.post("/api/chat/dm", {"user_id": opts.staff_id},
                          headers=gt(opts.token_a))
    assert_eq(dm.status_code, 403,
              f"DM rooms are groupless — a group token must not open one, "
              f"got {dm.status_code}: {dm.response}")


@th.django_unit_test("chat room list is confined to the token's tenant")
def test_chat_room_list_confined(opts):
    resp = opts.client.get("/api/chat/rooms", headers=gt(opts.token_a))
    assert_eq(resp.status_code, 200,
              f"the room list must answer 200, got {resp.status_code}: {resp.response}")
    ids = [row["id"] for row in resp.response.data]
    assert_true(opts.room_b_id not in ids,
                f"an A-token must not see tenant B's rooms in the list: {ids}")


# ---------------------------------------------------------------------------
# 15. Docit search
# ---------------------------------------------------------------------------

@th.django_unit_test("visible_groups confines a group token to its own tenant")
def test_docit_visible_groups_unit(opts):
    from mojo.apps.account.models import Group, User
    from mojo.apps.account.services.group_token import GroupScopedToken
    from mojo.apps.docit.services.search import visible_groups

    group_a = Group.objects.get(pk=opts.group_a_id)
    staff = User.objects.get(pk=opts.staff_id)
    visitor = User.objects.get(pk=opts.visitor_id)

    # A staff visitor holds the GLOBAL docit read grant — unrestricted as a
    # plain user, confined to A under a token.
    assert_true(visible_groups(user=staff) is None,
                "a global view_docit holder is unrestricted without a token")
    ids = list(visible_groups(
        user=staff, api_key=GroupScopedToken(staff, group_a)
    ).values_list("id", flat=True))
    assert_eq(ids, [opts.group_a_id],
              f"under an A-token a global docit reader must see ONLY tenant A, "
              f"got {ids}")

    ids = list(visible_groups(
        user=visitor, api_key=GroupScopedToken(visitor, group_a)
    ).values_list("id", flat=True))
    assert_eq(ids, [opts.group_a_id],
              f"a member-grade visitor under an A-token must see ONLY tenant A "
              f"even though they belong to two tenants, got {ids}")


@th.django_unit_test("docit search under a group token returns only its tenant")
def test_docit_search_confined(opts):
    from mojo.apps.docit.models import Page
    from mojo.apps.docit_kb.services.knowledge import embed_page_now

    for page in Page.objects.filter(book__title__startswith="gt_"):
        embed_page_now(page)

    resp = opts.client.get("/api/docit/search",
                           params={"q": "GTSEARCHMARKER77", "limit": 50},
                           headers=gt(opts.token_staff_a))
    assert_eq(resp.status_code, 200,
              f"search must answer 200, got {resp.status_code}: {resp.response}")
    results = resp.response.data["results"]
    book_ids = {row["book_id"] for row in results if "book_id" in row}
    assert_true(opts.book_b_id not in book_ids,
                f"a global docit reader under an A-token must not see tenant B "
                f"content, got books {book_ids}")
    assert_true(opts.book_a_id in book_ids,
                f"the A-tenant hit must still be returned, got books {book_ids} "
                f"({results})")


# ---------------------------------------------------------------------------
# 16-17. Assistant + invite confinement
# ---------------------------------------------------------------------------

@th.django_unit_test("assistant context endpoint refuses a group token")
def test_assistant_context_denied(opts):
    resp = opts.client.post("/api/assistant/context",
                            {"model": "account.User", "pk": opts.staff_id},
                            headers=gt(opts.token_staff_a))
    assert_eq(resp.status_code, 403,
              f"the assistant context endpoint reads arbitrary model rows and "
              f"must refuse a group token, got {resp.status_code}: {resp.response}")


@th.django_unit_test("invite refuses a global admin acting through a group token")
def test_invite_confinement(opts):
    resp = opts.client.post("/api/group/member/invite",
                            {"email": "gt_invitee@example.com",
                             "group": opts.group_a_id},
                            headers=gt(opts.token_staff_a))
    assert_eq(resp.status_code, 403,
              f"a global manage_groups holder with NO member-level grant in the "
              f"token group must not invite, got {resp.status_code}: {resp.response}")

    from mojo.apps.account.models import User
    created = User.objects.filter(email="gt_invitee@example.com").exists()
    assert_true(not created,
                "a refused invite must not create the invited user")


# ---------------------------------------------------------------------------
# 18. Opt-in registration
# ---------------------------------------------------------------------------

# test_scheme_is_opt_in moved to
# tests/test_auth_extended_serial/group_token_opt_in.py (maestro #2791):
# AUTH_BEARER_HANDLERS is read at module load, so unregistering the handler
# needs a server reload — legal only in a serial/opt-in package.


# ---------------------------------------------------------------------------
# 19. Reference-mode ApiKey pin vs the override-key confinement fix
# ---------------------------------------------------------------------------

@th.django_unit_test("reference-mode ApiKey still reads its own group metrics")
def test_reference_key_metrics_pin(opts):
    resp = opts.client.get("/api/metrics/fetch",
                           params={"slug": "gt_probe",
                                   "account": f"group-{opts.group_a_id}"},
                           headers=apikey(opts.ref_key_token))
    assert_eq(resp.status_code, 200,
              f"an unlinked key whose OWN permissions dict holds view_metrics "
              f"must still read its group's metrics — the global short-circuit "
              f"is only skipped for assumed-member sessions. "
              f"got {resp.status_code}: {resp.response}")


@th.django_unit_test("override ApiKey is confined to its own group's metrics")
def test_override_key_metrics_confined(opts):
    own = opts.client.get("/api/metrics/fetch",
                          params={"slug": "gt_probe",
                                  "account": f"group-{opts.group_a_id}"},
                          headers=apikey(opts.override_key_token))
    assert_eq(own.status_code, 200,
              f"an override key must read its OWN group's metrics via the "
              f"member's grant in that group, got {own.status_code}: {own.response}")

    other = opts.client.get("/api/metrics/fetch",
                            params={"slug": "gt_probe",
                                    "account": f"group-{opts.group_b_id}"},
                            headers=apikey(opts.override_key_token))
    assert_eq(other.status_code, 403,
              f"an override key must NOT read another tenant's metrics through "
              f"the acting member's global grant (deliberate behavior fix), "
              f"got {other.status_code}: {other.response}")
