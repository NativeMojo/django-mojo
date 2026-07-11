"""ITEM-032 regression — REST batch save must run per-row instance permission checks.

Bug: on_rest_handle_batch gated once at class level (instance=None) and then
looped rows straight into update_from_dict/create_from_dict — skipping the
owner match, group/GROUP_FIELD tenant binding, and check_view/edit_permission
hooks that the single-instance path (on_rest_handle_save) applies per row. A
caller who cleared the class gate for group A (request.group = A) could update
group B's rows in the same batch.

Fix: each update row is re-checked with rest_check_permission(request,
["SAVE_PERMS", "VIEW_PERMS"], instance); create rows with ["CREATE_PERMS",
"SAVE_PERMS", "VIEW_PERMS"]. Denied rows are dropped with a per-row error
entry + a batch_row_denied incident (drop-with-audit, mirroring the FK-attach
gate) — the rest of the batch proceeds. request.group is restored between rows
so one row's tenant binding cannot leak into the next.

These tests run in-process — setattr(ChatRoom.RestMeta, "CAN_BATCH", True)
does not cross into the testit server process, so we call
on_rest_handle_batch directly (same pattern as feature_disabled_events.py).
"""
import json
import uuid as _uuid

import objict
from testit import helpers as th


def _build_request(user, group, data=None):
    req = objict.objict()
    req.user = user
    req.DATA = objict.objict(data or {})
    req.QUERY_PARAMS = objict.objict()
    req.method = "POST"
    req.group = group
    req.bearer = None
    req.ip = "127.0.0.1"
    req.path = "/api/chat/room"
    req.META = {}
    req.api_key = None
    return req


def _response_data(resp):
    body = json.loads(resp.content)
    assert body.get("status") is True, f"batch response status False: {body!r}"
    return body["data"]


def _enable_batch(model):
    setattr(model.RestMeta, "CAN_BATCH", True)


def _disable_batch(model):
    if hasattr(model.RestMeta, "CAN_BATCH"):
        delattr(model.RestMeta, "CAN_BATCH")


@th.django_unit_setup()
def setup_batch_row_permissions(opts):
    """Two tenants. user_a is a member of group A only, with a member-level
    manage_chat grant (no global perms). One room in each group."""
    from mojo.apps.account.models import User, Group, GroupMember
    from mojo.apps.chat.models import ChatRoom

    # delete-before-create — tests run against a long-lived DB
    ChatRoom.objects.filter(name__startswith="batchperm_").delete()
    User.objects.filter(email__startswith="batchperm_").delete()
    Group.objects.filter(name__startswith="batchperm_grp_").delete()

    tag = _uuid.uuid4().hex[:8]

    grp_a = Group.objects.create(name=f"batchperm_grp_a_{tag}", is_active=True)
    grp_b = Group.objects.create(name=f"batchperm_grp_b_{tag}", is_active=True)

    user_a = User.objects.create_user(
        username=f"batchperm_a_{tag}@test.com",
        email=f"batchperm_a_{tag}@test.com",
        password="testit##mojo",
    )
    user_a.is_active = True
    user_a.is_email_verified = True
    user_a.save()
    grp_a.add_member(user_a)
    gm = GroupMember.objects.get(group=grp_a, user=user_a)
    gm.add_permission("manage_chat")  # member-level grant only — no global perm
    gm.save()

    user_b = User.objects.create_user(
        username=f"batchperm_b_{tag}@test.com",
        email=f"batchperm_b_{tag}@test.com",
        password="testit##mojo",
    )
    user_b.is_active = True
    user_b.save()
    grp_b.add_member(user_b)

    opts.user_a = user_a
    opts.grp_a = grp_a
    opts.grp_b = grp_b
    opts.room_a = ChatRoom.objects.create(
        name=f"batchperm_room_a_{tag}", group=grp_a, user=user_a)
    opts.room_b = ChatRoom.objects.create(
        name=f"batchperm_room_b_{tag}", group=grp_b, user=user_b)


@th.django_unit_test("ITEM-032: batch update of a foreign tenant's row is denied per-row")
def test_batch_cross_tenant_update_denied(opts):
    """The core regression. Pre-fix the foreign row was written."""
    from mojo.apps.chat.models import ChatRoom

    _enable_batch(ChatRoom)
    try:
        original_name = opts.room_b.name
        req = _build_request(opts.user_a, opts.grp_a, data={
            "batched": [{"id": opts.room_b.pk, "name": "pwned-by-a"}],
        })
        resp = ChatRoom.on_rest_handle_batch(req)
        data = _response_data(resp)

        opts.room_b.refresh_from_db()
        assert opts.room_b.name == original_name, (
            f"SECURITY: batch wrote a foreign tenant's row — "
            f"room_b.name={opts.room_b.name!r}"
        )
        assert data["count"] == 0, (
            f"denied row must not count as a result, got count={data['count']}"
        )
        errors = data.get("errors") or []
        assert any(e.get("index") == 0 for e in errors), (
            f"expected a per-row error entry for index 0, got errors={errors!r}"
        )
    finally:
        _disable_batch(ChatRoom)


@th.django_unit_test("ITEM-032: mixed batch applies own-tenant row, drops foreign row")
def test_batch_mixed_tenants_partial(opts):
    from mojo.apps.chat.models import ChatRoom

    _enable_batch(ChatRoom)
    try:
        original_b_name = opts.room_b.name
        req = _build_request(opts.user_a, opts.grp_a, data={
            "batched": [
                {"id": opts.room_a.pk, "name": "renamed-by-owner"},
                {"id": opts.room_b.pk, "name": "pwned-by-a"},
            ],
        })
        resp = ChatRoom.on_rest_handle_batch(req)
        data = _response_data(resp)

        opts.room_a.refresh_from_db()
        opts.room_b.refresh_from_db()
        assert opts.room_a.name == "renamed-by-owner", (
            f"own-tenant row must still be updated, got {opts.room_a.name!r}"
        )
        assert opts.room_b.name == original_b_name, (
            f"SECURITY: batch wrote a foreign tenant's row — "
            f"room_b.name={opts.room_b.name!r}"
        )
        assert data["count"] == 1, (
            f"only the own-tenant row counts as a result, got count={data['count']}"
        )
        errors = data.get("errors") or []
        assert len(errors) == 1 and errors[0].get("index") == 1, (
            f"expected exactly one error entry for index 1, got errors={errors!r}"
        )
    finally:
        _disable_batch(ChatRoom)
        # restore fixture name for other tests
        ChatRoom.objects.filter(pk=opts.room_a.pk).update(name=opts.room_a.name)


@th.django_unit_test("ITEM-032: create row after a denied row still works, no tenant leak")
def test_batch_create_after_denied_row(opts):
    """CREATE_PERMS is ['authenticated'] on ChatRoom, so the create row must
    succeed — and the denied foreign row before it must not leak group B into
    the created row via the request.group binding side effect."""
    from mojo.apps.chat.models import ChatRoom

    _enable_batch(ChatRoom)
    try:
        new_name = f"batchperm_created_{_uuid.uuid4().hex[:8]}"
        req = _build_request(opts.user_a, opts.grp_a, data={
            "batched": [
                {"id": opts.room_b.pk, "name": "pwned-by-a"},
                {"name": new_name},
            ],
        })
        resp = ChatRoom.on_rest_handle_batch(req)
        data = _response_data(resp)

        created = ChatRoom.objects.filter(name=new_name).first()
        assert created is not None, (
            f"create row must still succeed, response data={data!r}"
        )
        assert created.group_id != opts.grp_b.pk, (
            f"SECURITY: denied row's tenant leaked into the created row — "
            f"group_id={created.group_id}, grp_b={opts.grp_b.pk}"
        )
        assert data["count"] == 1, (
            f"one created row expected in results, got count={data['count']}"
        )
        errors = data.get("errors") or []
        assert len(errors) == 1 and errors[0].get("index") == 0, (
            f"expected exactly one error entry for index 0, got errors={errors!r}"
        )
    finally:
        _disable_batch(ChatRoom)
