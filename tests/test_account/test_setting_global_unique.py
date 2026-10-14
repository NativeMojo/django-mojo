"""Maestro item #8201 — one Setting row per key in each scope.

Bug: `unique_together = ("key", "group")` cannot keep platform-wide keys
distinct, because their group is NULL and NULLs never collide in a PostgreSQL
unique index. A second global create succeeded, and readers then picked one of
the two rows unpredictably. In a group, the same duplicate was a raw 500.

Fix: the partial unique constraint `account_setting_global_key_uniq` (migration
0055) refuses the second global row for every writer, and the REST save hook
answers a duplicate create, or a rename onto a taken key, with a readable 400.

Isolation-scanner rules this file follows (as `test_setting_scope.py`): every
create posts to the literal path `/api/settings` with an inline dict carrying a
literal `TESTIT_` key; updates use `f"/api/settings/{pk}"`; every row is
removed by its literal key in `finally`. The race and the migration end to end
need threads and DDL, so they live in
`test_account_admin_extended_serial/test_setting_global_unique_serial.py`.
"""
import importlib
import uuid as _uuid

from testit import helpers as th


IP = "127.0.0.1"
PASSWORD = "Uniq##set99"
ADMIN_USER = "setuniq_admin"
GLOBAL_TEXT = "A platform-wide setting named"
GROUP_TEXT = "This group already has a setting named"


def _mk_user(email, username=None, superuser=False):
    from mojo.apps.account.models import User
    user = User.objects.create_user(
        username=username or email, email=email, password=PASSWORD)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.is_superuser = superuser
    user.save()
    return user


def _login(opts, username):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip=IP, key="login")
    muid = opts.client.session.cookies.get("_muid")
    if muid:
        clear_rate_limits(key="login", muid=muid)
    ok = opts.client.login(username, PASSWORD)
    assert ok, f"login failed for {username}: {opts.client.last_response.body}"


def _drop(rows):
    """Remove test-owned rows from Redis and the database."""
    for row in rows:
        row.remove_from_cache()
    rows.delete()


def _body(opts):
    return str(opts.client.last_response.body)


@th.django_unit_setup()
def setup_setting_global_unique(opts):
    """A superuser for platform-wide writes, and a group with a
    `manage_settings` member for group-scoped writes."""
    from mojo.apps.account.models import User, Group, GroupMember, Setting

    # delete-before-create — tests run against a long-lived DB
    _drop(Setting.objects.filter(key__startswith="TESTIT_UNIQ_"))
    User.objects.filter(email__startswith="setuniq_").delete()
    User.objects.filter(username=ADMIN_USER).delete()
    Group.objects.filter(name__startswith="setuniq_grp_").delete()

    tag = _uuid.uuid4().hex[:8]
    grp = Group.objects.create(
        name=f"setuniq_grp_{tag}", kind="organization", is_active=True)
    opts.grp = grp.pk

    opts.settings_email = f"setuniq_settings_{tag}@account.test"
    manager = _mk_user(opts.settings_email)
    grp.add_member(manager)
    member = GroupMember.objects.get(group=grp, user=manager)
    member.add_permission("manage_settings")  # member-level grant only
    member.save()

    _mk_user(f"setuniq_admin_{tag}@account.test", username=ADMIN_USER,
             superuser=True)


@th.django_unit_test("#8201: a second platform-wide create of a key is a readable 400")
def test_rest_second_global_create_refused(opts):
    """The regression. Before the fix the second create returned 200 and the
    key had two global rows."""
    from mojo.apps.account.models import Setting

    _login(opts, ADMIN_USER)
    try:
        first = opts.client.post("/api/settings", {
            "key": "TESTIT_UNIQ_GLOBAL", "value": "first"})
        assert first.status_code == 200, (
            f"the first global create must work, got {first.status_code}: {_body(opts)}")
        second = opts.client.post("/api/settings", {
            "key": "TESTIT_UNIQ_GLOBAL", "value": "second"})
        rows = list(Setting.objects.filter(
            key="TESTIT_UNIQ_GLOBAL", group=None).values_list("value", flat=True))
        assert rows == ["first"], (
            f"a second platform-wide create changed the stored rows to {rows} "
            f"(status {second.status_code})")
        assert second.status_code == 400, (
            f"a second platform-wide create must be refused with 400, got "
            f"{second.status_code}: {_body(opts)}")
        assert GLOBAL_TEXT in _body(opts), (
            f"the 400 must say the platform-wide setting exists: {_body(opts)}")
    finally:
        opts.client.logout()
        _drop(Setting.objects.filter(key="TESTIT_UNIQ_GLOBAL"))


@th.django_unit_test("#8201: renaming a platform-wide key onto a taken one is a readable 400")
def test_rest_global_rename_onto_taken_key_refused(opts):
    from mojo.apps.account.models import Setting

    Setting.objects.create(key="TESTIT_UNIQ_TAKEN", value="a")
    other = Setting.objects.create(key="TESTIT_UNIQ_RENAME", value="b")
    _login(opts, ADMIN_USER)
    try:
        resp = opts.client.post(f"/api/settings/{other.pk}", {
            "key": "TESTIT_UNIQ_TAKEN"})
        other.refresh_from_db()
        assert other.key == "TESTIT_UNIQ_RENAME", (
            f"a rename onto a taken key changed the row to {other.key} "
            f"(status {resp.status_code})")
        assert resp.status_code == 400, (
            f"a rename onto a taken key must be refused with 400, got "
            f"{resp.status_code}: {_body(opts)}")
        assert GLOBAL_TEXT in _body(opts), (
            f"the 400 must say the platform-wide setting exists: {_body(opts)}")
    finally:
        opts.client.logout()
        _drop(Setting.objects.filter(key="TESTIT_UNIQ_TAKEN"))
        _drop(Setting.objects.filter(key="TESTIT_UNIQ_RENAME"))


@th.django_unit_test("#8201: a second create of a key in one group is a readable 400, not a 500")
def test_rest_second_group_create_refused(opts):
    from mojo.apps.account.models import Setting

    _login(opts, opts.settings_email)
    try:
        first = opts.client.post("/api/settings", {
            "group": opts.grp, "key": "TESTIT_UNIQ_GROUP", "value": "first"})
        assert first.status_code == 200, (
            f"the first group create must work, got {first.status_code}: {_body(opts)}")
        second = opts.client.post("/api/settings", {
            "group": opts.grp, "key": "TESTIT_UNIQ_GROUP", "value": "second"})
        count = Setting.objects.filter(
            key="TESTIT_UNIQ_GROUP", group_id=opts.grp).count()
        assert count == 1, f"the group holds {count} rows for one key"
        assert second.status_code == 400, (
            f"a second group create must be refused with 400, got "
            f"{second.status_code}: {_body(opts)}")
        assert GROUP_TEXT in _body(opts), (
            f"the 400 must say the group's setting exists: {_body(opts)}")
    finally:
        opts.client.logout()
        _drop(Setting.objects.filter(key="TESTIT_UNIQ_GROUP"))


@th.django_unit_test("#8201: the database refuses a second platform-wide row from a direct save")
def test_direct_save_second_global_row_refused(opts):
    from django.db import IntegrityError, transaction
    from mojo.apps.account.models import Setting

    try:
        Setting(key="TESTIT_UNIQ_SAVE", value="first").save()
        with th.assert_raises(IntegrityError):
            with transaction.atomic():
                Setting(key="TESTIT_UNIQ_SAVE", value="second").save()
        count = Setting.objects.filter(key="TESTIT_UNIQ_SAVE", group=None).count()
        assert count == 1, f"a direct save left {count} global rows for one key"
    finally:
        _drop(Setting.objects.filter(key="TESTIT_UNIQ_SAVE"))


@th.django_unit_test("#8201: Setting.set updates the one platform-wide row")
def test_setting_set_keeps_one_global_row(opts):
    from mojo.apps.account.models import Setting

    try:
        Setting.set("TESTIT_UNIQ_SET", "first")
        Setting.set("TESTIT_UNIQ_SET", "second")
        rows = list(Setting.objects.filter(
            key="TESTIT_UNIQ_SET", group=None).values_list("value", flat=True))
        assert rows == ["second"], f"Setting.set left global rows {rows}"
    finally:
        _drop(Setting.objects.filter(key="TESTIT_UNIQ_SET"))


@th.django_unit_test("#8201: the platform-wide unique constraint is in the database")
def test_global_key_constraint_present(opts):
    from django.db import connection
    from mojo.apps.account.models import Setting

    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(
            cursor, Setting._meta.db_table)
    found = constraints.get("account_setting_global_key_uniq")
    assert found and found["unique"] and found["columns"] == ["key"], (
        f"account_setting_global_key_uniq is missing or wrong: {found}")


@th.django_unit_test("#8201: migration 0055 keeps the oldest copy of every duplicated key and logs conflicts")
def test_migration_dedup_planner(opts):
    migration = importlib.import_module(
        "mojo.apps.account.migrations.0055_setting_global_key_unique")

    def row(pk, key, value, is_secret=False, mojo_secrets=None):
        return {"pk": pk, "key": key, "value": value,
                "is_secret": is_secret, "mojo_secrets": mojo_secrets}

    rows = [
        row(7, "SAME", "on"), row(3, "SAME", "on"), row(9, "SAME", "on"),
        row(4, "SINGLE", "x"),
        row(12, "DIFFERENT", "secret-text-one"), row(5, "DIFFERENT", "secret-text-two"),
        row(6, "SECRET", "", True, "cipher-a"), row(8, "SECRET", "", True, "cipher-b"),
    ]
    delete_pks, conflicts = migration.plan_global_dedup(rows)
    assert sorted(delete_pks) == [7, 8, 9, 12], (
        f"every duplicated key must keep only its lowest pk, deleted {delete_pks}")
    assert conflicts == [("DIFFERENT", 5, [12]), ("SECRET", 6, [8])], (
        f"conflicting keys must be listed with the kept and deleted pks, got {conflicts}")
    message = migration.repair_message(conflicts)
    for key, kept, deleted in conflicts:
        assert key in message and f"kept Setting row {kept}" in message, (
            f"the log must name {key} and its kept row {kept}: {message}")
        assert ", ".join(map(str, deleted)) in message, (
            f"the log must name {key}'s deleted rows {deleted}: {message}")
    assert "SAME" not in message, f"identical copies are not a conflict: {message}"
    for value in ("secret-text-one", "secret-text-two", "cipher-a", "cipher-b"):
        assert value not in message, f"the log exposed a value: {message}"
    assert migration.plan_global_dedup([row(1, "A", "v")]) == ([], []), \
        "a clean database must plan nothing"
