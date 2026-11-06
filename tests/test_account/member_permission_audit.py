"""Every real change to a GroupMember's permissions writes one audit record
(maestro #10293).

Before the fix, group membership permissions were saved on every path (REST,
invite, server code, a direct assign) without any log, while User permissions
wrote "permission:added"/"permission:removed". Now GroupMember.save() writes
one "member:permissions_changed" Log row per real change: who (uid), whose
membership (model_id, member_id, user_id, username), which group (gid,
group_id), what was added and removed, and before/after as names with
true/false only. A no-op save writes nothing.

All data is test-owned (fresh users, a fresh group per test); nothing shared
is patched. The refusal case needs a protected MEMBER_PERMS_PROTECTION row and
lives in tests/test_global_perms_extended_serial/member_permission_audit_refusal.py.
"""
import json
import uuid as _uuid
from testit import helpers as th

TESTIT_TIER = "framework"

KIND = "member:permissions_changed"
PWORD = "Mpa##audit99"


def _make_user(email, perms=None):
    from mojo.apps.account.models import User
    User.objects.filter(email=email).delete()
    user = User.objects.create_user(username=email, email=email, password=PWORD)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    if perms:
        user.add_permission(list(perms))
    return user


def _fixture(opts, actor_perms=("manage_groups",), target_perms=None):
    """A fresh actor, target user, group and target member for one test."""
    from mojo.apps.account.models import Group, GroupMember
    tag = _uuid.uuid4().hex[:8]
    actor = _make_user(f"mpa_actor_{tag}@audit.test", actor_perms)
    target = _make_user(f"mpa_target_{tag}@audit.test")
    group = Group.objects.create(name=f"mpa_group_{tag}", kind="organization")
    member = GroupMember.objects.create(
        user=target, group=group, permissions=dict(target_perms or {}))
    _clear_records(member)
    return actor, target, group, member


def _clear_records(member):
    """Drop the fixture's own insert record so each test counts only its act."""
    from mojo.apps.logit.models import Log
    Log.objects.filter(kind=KIND, model_id=member.pk).delete()


def _cleanup(group, *users):
    from mojo.apps.account.models import GroupMember, User
    from mojo.apps.logit.models import Log
    member_ids = list(GroupMember.objects.filter(group=group).values_list("id", flat=True))
    Log.objects.filter(kind=KIND, model_id__in=member_ids).delete()
    GroupMember.objects.filter(group=group).delete()
    group.delete()
    User.objects.filter(pk__in=[u.pk for u in users if u is not None]).delete()


def _records(member):
    from mojo.apps.logit.models import Log
    return list(Log.objects.filter(
        kind=KIND, model_name="account.GroupMember", model_id=member.pk).order_by("id"))


def _body(record):
    return json.loads(record.log)


def _login(opts, user):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="login")
    assert opts.client.login(user.email, PWORD), \
        f"login failed for {user.email}: {opts.client.last_response.body}"


def _assert_one_rest_record(opts, member, actor, group, added, removed, before, after):
    recs = _records(member)
    assert len(recs) == 1, f"expected exactly one {KIND} record, got {len(recs)}: {[r.log for r in recs]}"
    rec = recs[0]
    body = _body(rec)
    assert body["added"] == added, f"added must be {added}, got {body['added']!r}"
    assert body["removed"] == removed, f"removed must be {removed}, got {body['removed']!r}"
    assert body["before"] == before, f"before must be {before}, got {body['before']!r}"
    assert body["after"] == after, f"after must be {after}, got {body['after']!r}"
    assert body["member_id"] == member.pk, f"member_id must be {member.pk}, got {body['member_id']!r}"
    assert body["user_id"] == member.user_id, f"user_id must be {member.user_id}, got {body['user_id']!r}"
    assert body["group_id"] == group.pk, f"group_id must be {group.pk}, got {body['group_id']!r}"
    assert rec.uid == actor.pk, f"uid must be the actor {actor.pk}, got {rec.uid}"
    assert rec.gid == group.pk, f"gid must be the group {group.pk}, got {rec.gid}"
    assert rec.created is not None, "the record must carry its time"
    return rec


@th.django_unit_test("member audit: REST add writes one record with actor, member, group and diff")
def test_rest_add_writes_one_record(opts):
    actor, target, group, member = _fixture(opts)
    try:
        _login(opts, actor)
        resp = opts.client.post(f"/api/group/member/{member.pk}", {
            "permissions": {"maestro_admin": True}})
        assert resp.status_code == 200, \
            f"REST add must succeed, got {resp.status_code}: {opts.client.last_response.body}"
        _assert_one_rest_record(
            opts, member, actor, group, added=["maestro_admin"], removed=[],
            before={}, after={"maestro_admin": True})
    finally:
        opts.client.logout()
        _cleanup(group, actor, target)


@th.django_unit_test("member audit: REST remove writes one record")
def test_rest_remove_writes_one_record(opts):
    actor, target, group, member = _fixture(opts, target_perms={"maestro_admin": True})
    try:
        _login(opts, actor)
        resp = opts.client.post(f"/api/group/member/{member.pk}", {
            "permissions": {"maestro_admin": False}})
        assert resp.status_code == 200, \
            f"REST remove must succeed, got {resp.status_code}: {opts.client.last_response.body}"
        _assert_one_rest_record(
            opts, member, actor, group, added=[], removed=["maestro_admin"],
            before={"maestro_admin": True}, after={})
    finally:
        opts.client.logout()
        _cleanup(group, actor, target)


@th.django_unit_test("member audit: REST no-op (resubmitted catalog) writes nothing")
def test_rest_noop_writes_nothing(opts):
    actor, target, group, member = _fixture(opts, target_perms={"view_logs": True})
    try:
        _login(opts, actor)
        # The admin UI's shape: the stored catalog again plus False for keys
        # the member never had.
        resp = opts.client.post(f"/api/group/member/{member.pk}", {
            "permissions": {"view_logs": True, "maestro_admin": False, "manage_group": False}})
        assert resp.status_code == 200, \
            f"no-op save must succeed, got {resp.status_code}: {opts.client.last_response.body}"
        recs = _records(member)
        assert recs == [], f"a no-op save must write no record, got {[r.log for r in recs]}"
    finally:
        opts.client.logout()
        _cleanup(group, actor, target)


@th.django_unit_test("member audit: several toggles in one REST request write one record")
def test_rest_multi_toggle_writes_one_record(opts):
    actor, target, group, member = _fixture(opts, target_perms={"view_logs": True})
    try:
        _login(opts, actor)
        resp = opts.client.post(f"/api/group/member/{member.pk}", {
            "permissions": {"view_logs": False, "manage_members": True, "maestro_admin": True}})
        assert resp.status_code == 200, \
            f"multi-toggle save must succeed, got {resp.status_code}: {opts.client.last_response.body}"
        _assert_one_rest_record(
            opts, member, actor, group,
            added=["maestro_admin", "manage_members"], removed=["view_logs"],
            before={"view_logs": True},
            after={"maestro_admin": True, "manage_members": True})
    finally:
        opts.client.logout()
        _cleanup(group, actor, target)


@th.django_unit_test("member audit: invite with permissions writes one record from an empty before")
def test_invite_writes_one_record(opts):
    from mojo.apps.account.models import User, GroupMember
    actor, target, group, member = _fixture(opts)
    invitee_email = f"mpa_invitee_{_uuid.uuid4().hex[:8]}@audit.test"
    User.objects.filter(email=invitee_email).delete()
    invitee = None
    try:
        _login(opts, actor)
        resp = opts.client.post("/api/group/member/invite", {
            "group": group.pk, "email": invitee_email,
            "permissions": {"view_members": True}})
        assert resp.status_code == 200, \
            f"invite must succeed, got {resp.status_code}: {opts.client.last_response.body}"
        invitee = User.objects.get(email=invitee_email)
        im = GroupMember.objects.get(user=invitee, group=group)
        _assert_one_rest_record(
            opts, im, actor, group, added=["view_members"], removed=[],
            before={}, after={"view_members": True})
    finally:
        opts.client.logout()
        _cleanup(group, actor, target, invitee)


@th.django_unit_test("member audit: server add/remove_permission with no request write one record each")
def test_server_add_remove_write_system_records(opts):
    actor, target, group, member = _fixture(opts, actor_perms=None)
    try:
        member.add_permission("maestro_admin")
        member.remove_permission("maestro_admin")
        recs = _records(member)
        assert len(recs) == 2, f"add then remove must write two records, got {[r.log for r in recs]}"
        add_body, remove_body = _body(recs[0]), _body(recs[1])
        assert add_body["added"] == ["maestro_admin"] and add_body["removed"] == [], \
            f"first record must be the add, got {add_body!r}"
        assert remove_body["removed"] == ["maestro_admin"] and remove_body["added"] == [], \
            f"second record must be the remove, got {remove_body!r}"
        for rec, body in ((recs[0], add_body), (recs[1], remove_body)):
            assert rec.uid == 0, f"server code has no actor, uid must be 0, got {rec.uid}"
            assert body.get("actor") == "system", f"body must say actor system, got {body!r}"
            assert rec.gid == group.pk, f"gid must be the group {group.pk}, got {rec.gid}"
    finally:
        _cleanup(group, actor, target)


@th.django_unit_test("member audit: direct assign + save writes one record; touch writes none")
def test_direct_save_and_touch(opts):
    actor, target, group, member = _fixture(opts, actor_perms=None, target_perms={"view_logs": True})
    try:
        # In-place edit of the loaded dict: the before must come from the row.
        member.permissions["manage_members"] = True
        member.save()
        recs = _records(member)
        assert len(recs) == 1, f"direct assign + save must write one record, got {[r.log for r in recs]}"
        body = _body(recs[0])
        assert body["added"] == ["manage_members"], f"added must be manage_members, got {body!r}"
        assert body["before"] == {"view_logs": True}, f"before must be the stored row, got {body!r}"

        member.last_activity = None
        member.touch()
        assert len(_records(member)) == 1, "touch() must not write a permissions record"

        # A plain save with nothing changed writes nothing either.
        member.save()
        assert len(_records(member)) == 1, "a save with unchanged permissions must not write a record"
    finally:
        _cleanup(group, actor, target)


@th.django_unit_test("member audit: only global log readers can read the record")
def test_record_readers(opts):
    from mojo.apps.account.models import GroupMember
    actor, target, group, member = _fixture(opts, actor_perms=None)
    tag = _uuid.uuid4().hex[:8]
    reader = _make_user(f"mpa_reader_{tag}@audit.test", ["view_logs"])
    manager = _make_user(f"mpa_manager_{tag}@audit.test")
    GroupMember.objects.create(user=manager, group=group, permissions={"manage_group": True})
    try:
        member.add_permission("maestro_admin")
        rec = _records(member)[0]

        _login(opts, manager)
        for path in (f"/api/logs?kind={KIND}&model_id={member.pk}",
                     f"/api/logs?kind={KIND}&model_id={member.pk}&group={group.pk}",
                     f"/api/logs/{rec.pk}"):
            resp = opts.client.get(path)
            data = resp.response.get("data") if resp.status_code == 200 and resp.response else None
            ids = [d.get("id") for d in data] if isinstance(data, list) else (
                [data.get("id")] if isinstance(data, dict) else [])
            assert rec.pk not in ids, \
                f"a group-only manager must not read the record at {path}, got {resp.status_code}: {opts.client.last_response.body}"
        opts.client.logout()

        _login(opts, reader)
        resp = opts.client.get(f"/api/logs/{rec.pk}")
        assert resp.status_code == 200, \
            f"a global view_logs user must read the record, got {resp.status_code}: {opts.client.last_response.body}"
        assert resp.response.data.kind == KIND, f"wrong record returned: {opts.client.last_response.body}"
        # The documented query finds it.
        resp = opts.client.get(f"/api/logs?kind={KIND}&model_id={member.pk}")
        assert resp.status_code == 200, \
            f"the documented list query must succeed, got {resp.status_code}: {opts.client.last_response.body}"
        ids = [d.get("id") for d in resp.response.get("data") or []]
        assert ids == [rec.pk], f"the documented query must return exactly this record, got {ids}"
    finally:
        opts.client.logout()
        _cleanup(group, actor, target, reader, manager)


@th.django_unit_test("member audit: a non-boolean stored value is recorded only as true")
def test_value_never_recorded(opts):
    actor, target, group, member = _fixture(opts, actor_perms=None)
    secret = f"mpa-value-{_uuid.uuid4().hex}"
    try:
        member.permissions["custom_flag"] = secret
        member.save()
        recs = _records(member)
        assert len(recs) == 1, f"one record expected, got {[r.log for r in recs]}"
        assert secret not in recs[0].log, f"the stored value must never be recorded: {recs[0].log}"
        body = _body(recs[0])
        assert body["after"] == {"custom_flag": True}, f"after must hold only true, got {body!r}"
    finally:
        _cleanup(group, actor, target)


def _stored(member):
    from mojo.apps.account.models import GroupMember
    return GroupMember.objects.filter(pk=member.pk).values("permissions", "metadata").first()


@th.django_unit_test("member audit: empty update_fields saves nothing and writes no record")
def test_empty_update_fields_writes_nothing(opts):
    actor, target, group, member = _fixture(opts, actor_perms=None)
    try:
        for empty in ([], (), set()):
            member.permissions["review_grant"] = True
            member.save(update_fields=empty)
            stored = _stored(member)
            assert stored["permissions"] == {}, \
                f"update_fields={empty!r} must save nothing, stored {stored['permissions']!r}"
            assert _records(member) == [], \
                f"update_fields={empty!r} must write no record, got {[r.log for r in _records(member)]}"
    finally:
        _cleanup(group, actor, target)


@th.django_unit_test("member audit: generator update_fields saves the grant and writes one record")
def test_generator_update_fields(opts):
    actor, target, group, member = _fixture(opts, actor_perms=None)
    try:
        member.permissions["review_grant"] = True
        member.save(update_fields=(f for f in ["permissions"]))
        assert _stored(member)["permissions"] == {"review_grant": True}, \
            f"a generator naming permissions must save them, stored {_stored(member)!r}"
        recs = _records(member)
        assert len(recs) == 1 and _body(recs[0])["added"] == ["review_grant"], \
            f"expected one record adding review_grant, got {[r.log for r in recs]}"

        # Permissions with an unrelated field after it in the same generator.
        member.permissions["second_grant"] = True
        member.metadata = {"note": "tail"}
        member.save(update_fields=(f for f in ["permissions", "metadata"]))
        stored = _stored(member)
        assert stored["permissions"] == {"review_grant": True, "second_grant": True}, \
            f"both grants must be stored, got {stored['permissions']!r}"
        assert stored["metadata"] == {"note": "tail"}, f"metadata must be stored, got {stored['metadata']!r}"
        recs = _records(member)
        assert len(recs) == 2 and _body(recs[1])["added"] == ["second_grant"], \
            f"expected a second record adding second_grant, got {[r.log for r in recs]}"

        # A generator without permissions saves only its fields and writes nothing.
        member.permissions["unsaved_grant"] = True
        member.metadata = {"note": "only"}
        member.save(update_fields=(f for f in ["metadata"]))
        stored = _stored(member)
        assert "unsaved_grant" not in stored["permissions"], \
            f"permissions outside update_fields must not be stored, got {stored['permissions']!r}"
        assert stored["metadata"] == {"note": "only"}, f"metadata must be stored, got {stored['metadata']!r}"
        assert len(_records(member)) == 2, \
            f"a save without permissions must write no record, got {[r.log for r in _records(member)]}"
    finally:
        _cleanup(group, actor, target)


@th.django_unit_test("member audit: legacy positional update_fields matches the keyword form")
def test_positional_update_fields(opts):
    import warnings
    actor, target, group, member = _fixture(opts, actor_perms=None)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            member.permissions["review_grant"] = True
            member.save(False, False, None, [])
            assert _stored(member)["permissions"] == {}, \
                f"positional empty update_fields must save nothing, stored {_stored(member)!r}"
            assert _records(member) == [], "positional empty update_fields must write no record"

            member.save(False, False, None, (f for f in ["permissions"]))
        assert _stored(member)["permissions"] == {"review_grant": True}, \
            f"positional generator must save the grant, stored {_stored(member)!r}"
        recs = _records(member)
        assert len(recs) == 1 and _body(recs[0])["added"] == ["review_grant"], \
            f"expected one record adding review_grant, got {[r.log for r in recs]}"
    finally:
        _cleanup(group, actor, target)
