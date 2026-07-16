"""DM-038 regression — REST batch save must honor the per-verb feature flags.

Bug: on_rest_handle_batch enforced per-row *permission* checks (DM-032) but never
read the per-verb feature flags. Update rows skipped CAN_UPDATE (which
on_rest_handle_save enforces) and create rows skipped CAN_CREATE (which
on_rest_handle_create enforces). A model that hard-disables a verb
(CAN_UPDATE = False for an immutable ledger, or CAN_CREATE = False) but opts
into CAN_BATCH = True had that explicit control bypassed via the batch endpoint.

Fix: on_rest_handle_batch resolves the two static flags once (can_update via the
shared _resolve_can_update helper, honoring the CAN_SAVE deprecated alias;
can_create via CAN_CREATE default True) and drops the matching rows with the same
drop-with-audit convention as the permission checks — update rows when
CAN_UPDATE is False, create rows when CAN_CREATE is False. Per-row, not
whole-batch: a mixed batch on a CAN_UPDATE=False model still applies its creates.

These tests run in-process — setattr(ChatRoom.RestMeta, "CAN_BATCH", True) does
not cross into the testit server process, so we call on_rest_handle_batch
directly (same pattern as batch_row_permissions.py / feature_disabled_events.py).
Own-tenant rows are used so the per-row permission check always passes and ONLY
the feature flag can block a row.
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


def _set(model, **flags):
    for k, v in flags.items():
        setattr(model.RestMeta, k, v)


def _clear(model, *flags):
    for k in flags:
        if hasattr(model.RestMeta, k):
            delattr(model.RestMeta, k)


@th.django_unit_setup()
def setup_batch_feature_flags(opts):
    """One tenant with an owner who can update its room and create rooms."""
    from mojo.apps.account.models import User, Group, GroupMember
    from mojo.apps.chat.models import ChatRoom

    # delete-before-create — tests run against a long-lived DB
    ChatRoom.objects.filter(name__startswith="batchff_").delete()
    User.objects.filter(email__startswith="batchff_").delete()
    Group.objects.filter(name__startswith="batchff_grp_").delete()

    tag = _uuid.uuid4().hex[:8]

    grp_a = Group.objects.create(name=f"batchff_grp_a_{tag}", is_active=True)

    user_a = User.objects.create_user(
        username=f"batchff_a_{tag}@test.com",
        email=f"batchff_a_{tag}@test.com",
        password="testit##mojo",
    )
    user_a.is_active = True
    user_a.is_email_verified = True
    user_a.save()
    grp_a.add_member(user_a)
    gm = GroupMember.objects.get(group=grp_a, user=user_a)
    gm.add_permission("manage_chat")
    gm.save()

    opts.tag = tag
    opts.user_a = user_a
    opts.grp_a = grp_a
    opts.room_a = ChatRoom.objects.create(
        name=f"batchff_room_a_{tag}", group=grp_a, user=user_a)


@th.django_unit_test("DM-038: CAN_UPDATE=False drops batch update rows, still applies create rows")
def test_batch_can_update_false_blocks_updates(opts):
    """The core regression. Pre-fix the update row was written despite the
    single-instance verb being hard-disabled."""
    from mojo.apps.chat.models import ChatRoom

    _set(ChatRoom, CAN_BATCH=True, CAN_UPDATE=False)
    created_name = f"batchff_created_{opts.tag}"
    try:
        original_name = opts.room_a.name
        req = _build_request(opts.user_a, opts.grp_a, data={
            "batched": [
                {"id": opts.room_a.pk, "name": "should-not-apply"},
                {"name": created_name},
            ],
        })
        resp = ChatRoom.on_rest_handle_batch(req)
        data = _response_data(resp)

        opts.room_a.refresh_from_db()
        assert opts.room_a.name == original_name, (
            f"CAN_UPDATE=False must block the batch update row — "
            f"room_a.name={opts.room_a.name!r}"
        )
        assert ChatRoom.objects.filter(name=created_name).exists(), (
            "create row must still succeed (CAN_CREATE defaults True), "
            f"response data={data!r}"
        )
        assert data["count"] == 1, (
            f"only the create row counts as a result, got count={data['count']}"
        )
        errors = data.get("errors") or []
        assert len(errors) == 1 and errors[0].get("index") == 0, (
            f"expected exactly one error entry for the update row (index 0), "
            f"got errors={errors!r}"
        )
    finally:
        _clear(ChatRoom, "CAN_BATCH", "CAN_UPDATE")
        ChatRoom.objects.filter(name=created_name).delete()


@th.django_unit_test("DM-038: CAN_CREATE=False drops batch create rows, still applies update rows")
def test_batch_can_create_false_blocks_creates(opts):
    from mojo.apps.chat.models import ChatRoom

    _set(ChatRoom, CAN_BATCH=True, CAN_CREATE=False)
    created_name = f"batchff_created_{opts.tag}"
    try:
        req = _build_request(opts.user_a, opts.grp_a, data={
            "batched": [
                {"id": opts.room_a.pk, "name": "renamed-by-owner"},
                {"name": created_name},
            ],
        })
        resp = ChatRoom.on_rest_handle_batch(req)
        data = _response_data(resp)

        opts.room_a.refresh_from_db()
        assert opts.room_a.name == "renamed-by-owner", (
            f"update row must still apply (CAN_UPDATE defaults True), "
            f"got {opts.room_a.name!r}"
        )
        assert not ChatRoom.objects.filter(name=created_name).exists(), (
            "CAN_CREATE=False must block the batch create row"
        )
        assert data["count"] == 1, (
            f"only the update row counts as a result, got count={data['count']}"
        )
        errors = data.get("errors") or []
        assert len(errors) == 1 and errors[0].get("index") == 1, (
            f"expected exactly one error entry for the create row (index 1), "
            f"got errors={errors!r}"
        )
    finally:
        _clear(ChatRoom, "CAN_BATCH", "CAN_CREATE")
        # restore fixture name for other tests
        ChatRoom.objects.filter(pk=opts.room_a.pk).update(name=opts.room_a.name)


@th.django_unit_test("DM-038: CAN_SAVE=False alias also blocks batch update rows")
def test_batch_can_save_alias_blocks_updates(opts):
    """CAN_SAVE is the deprecated alias for CAN_UPDATE. Batch must honor it
    identically to the single-instance path via the shared resolver."""
    from mojo.apps.chat.models import ChatRoom

    _set(ChatRoom, CAN_BATCH=True, CAN_SAVE=False)
    try:
        original_name = opts.room_a.name
        req = _build_request(opts.user_a, opts.grp_a, data={
            "batched": [{"id": opts.room_a.pk, "name": "should-not-apply"}],
        })
        resp = ChatRoom.on_rest_handle_batch(req)
        data = _response_data(resp)

        opts.room_a.refresh_from_db()
        assert opts.room_a.name == original_name, (
            f"CAN_SAVE=False (alias) must block the batch update row — "
            f"room_a.name={opts.room_a.name!r}"
        )
        assert data["count"] == 0, (
            f"the only row was dropped, expected count=0, got {data['count']}"
        )
        errors = data.get("errors") or []
        assert len(errors) == 1 and errors[0].get("index") == 0, (
            f"expected one error entry for the update row (index 0), "
            f"got errors={errors!r}"
        )
    finally:
        _clear(ChatRoom, "CAN_BATCH", "CAN_SAVE")
