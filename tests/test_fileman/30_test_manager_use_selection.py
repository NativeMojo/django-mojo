"""One file manager per purpose, picked the same way every time (#8662).

A group could hold two active file managers for the same purpose (`use`), and
the lookups every caller goes through took whichever row the database returned
first. An upload could land in a different storage location from one request
to the next, and in a public one when the duplicates differed in visibility.

Now the lookups order by default first, then the oldest id. A save that would
make a second active manager for the same group (or user) and purpose is
refused with `DuplicateUseError`; auto-create reuses the existing manager
instead of raising. Duplicates that already exist are left alone, still
editable, and listed by the read-only `fileman_duplicate_uses` command.

Legacy duplicates are made the way old data exists: the second row is saved
inactive, then switched on with `.update()`, which skips `save()`. A row
touched with `.update()` also moves to the end of the Postgres heap, so an
unordered `.first()` returns it after the others.

Test-owned data only (prefix `fm_dupuse_`), filesystem managers under a
temporary folder. No patch of shared code, no system default.
"""

TESTIT_TIER = "bug"

import io
import json
import tempfile

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


PREFIX = "fm_dupuse_"
PASSWORD = "fm-dupuse-pass-99!"
ADMIN = "fm_dupuse_admin"   # global `files` grant: passes the storage containment rule
OWNER = "fm_dupuse_owner"   # owns user-scoped managers
GROUP = "fm_dupuse_group"
DUPLICATE_MESSAGE = "An active file manager already exists for this purpose"


def _clear():
    from mojo.apps.account.models import Group, GroupMember, User
    from mojo.apps.fileman.models import FileManager

    FileManager.objects.filter(name__startswith=PREFIX).delete()
    FileManager.objects.filter(group__name__startswith=PREFIX).delete()
    FileManager.objects.filter(user__username__in=[ADMIN, OWNER]).delete()
    GroupMember.objects.filter(group__name__startswith=PREFIX).delete()
    Group.objects.filter(name__startswith=PREFIX).delete()
    User.objects.filter(username__in=[ADMIN, OWNER]).delete()


@th.django_unit_setup()
@th.requires_app("mojo.apps.fileman")
def setup_manager_use_selection(opts):
    from mojo.apps.account.models import Group, User

    _clear()

    def make_user(username):
        user = User(username=username, email=f"{username}@example.com")
        user.save()
        user.is_email_verified = True
        user.save_password(PASSWORD)
        user.save()
        return user

    admin = make_user(ADMIN)
    admin.add_permission("files")
    admin.save()
    owner = make_user(OWNER)

    group = Group(name=GROUP)
    group.save()

    opts.group_id = group.pk
    opts.owner_id = owner.pk
    opts.tmpdir = tempfile.mkdtemp(prefix=PREFIX)


def _group(opts):
    from mojo.apps.account.models import Group
    return Group.objects.get(pk=opts.group_id)


def _make(opts, name, use="", is_default=False, is_active=True, user=False):
    """One manager saved through the ORM, so the new refusal applies."""
    from mojo.apps.fileman.models import FileManager

    scope = {"user_id": opts.owner_id} if user else {"group_id": opts.group_id}
    manager = FileManager(name=f"{PREFIX}{name}", backend_type="file",
                          backend_url=f"file:///{PREFIX}{name}", use=use,
                          is_default=is_default, is_active=is_active, **scope)
    manager.set_secrets({"base_path": opts.tmpdir, "use_shortlinks": False})
    manager.save()
    return manager


def _legacy(opts, name, use="", is_default=False, user=False):
    """A duplicate as old data holds it: saved inactive, switched on without save()."""
    from mojo.apps.fileman.models import FileManager

    manager = _make(opts, name, use=use, is_default=is_default, is_active=False, user=user)
    FileManager.objects.filter(pk=manager.pk).update(is_active=True)
    manager.refresh_from_db()
    return manager


def _to_heap_end(manager):
    """Rewrite the row so an unordered scan returns it after the others."""
    from mojo.apps.fileman.models import FileManager
    FileManager.objects.filter(pk=manager.pk).update(backend_url=manager.backend_url)


def _active_count(opts, use, user=False):
    from mojo.apps.fileman.models import FileManager
    scope = {"user_id": opts.owner_id, "group": None} if user else {"group_id": opts.group_id, "user": None}
    return FileManager.objects.filter(use=use, is_active=True, **scope).count()


def _refused(fn):
    """Run fn and return the DuplicateUseError it raised, or None."""
    from mojo.apps.fileman.models.manager import DuplicateUseError
    try:
        fn()
    except DuplicateUseError as err:
        return err
    return None


def _login(opts):
    assert_true(opts.client.login(ADMIN, PASSWORD), "the files admin must log in")


def _clean_use(opts, *uses):
    from mojo.apps.fileman.models import FileManager
    FileManager.objects.filter(group_id=opts.group_id, use__in=uses).delete()
    FileManager.objects.filter(user_id=opts.owner_id, use__in=uses).delete()


# ---------------------------------------------------------------------------
# Selection: default first, then the oldest id.
# ---------------------------------------------------------------------------

@th.django_unit_test("selection: the purpose's default beats an older non-default")
def test_default_beats_older_non_default(opts):
    from mojo.apps.fileman.models import FileManager

    _clean_use(opts, "fm_dupuse_invoices")
    older = _make(opts, "sel_older", use="fm_dupuse_invoices")
    default = _legacy(opts, "sel_default", use="fm_dupuse_invoices", is_default=True)
    _to_heap_end(default)

    for _ in range(3):
        picked = FileManager.get_for_group(_group(opts), "fm_dupuse_invoices")
        assert_eq(picked.pk, default.pk,
                  f"the default ({default.pk}) must win over the older non-default "
                  f"({older.pk}), got {picked.pk}")


@th.django_unit_test("selection: between two non-defaults the oldest id wins")
def test_oldest_non_default_wins(opts):
    from mojo.apps.fileman.models import FileManager

    _clean_use(opts, "fm_dupuse_oldest")
    first = _make(opts, "sel_first", use="fm_dupuse_oldest")
    second = _legacy(opts, "sel_second", use="fm_dupuse_oldest")
    _to_heap_end(first)

    for _ in range(3):
        picked = FileManager.get_for_group(_group(opts), "fm_dupuse_oldest")
        assert_eq(picked.pk, first.pk,
                  f"the oldest manager ({first.pk}) must win over {second.pk}, got {picked.pk}")


@th.django_unit_test("selection: the shared lookups order by default, then id (SQL supplement)")
def test_lookup_sql_is_ordered(opts):
    from django.db import connection
    from mojo.apps.account.models import User
    from mojo.apps.fileman.models import FileManager

    owner = User.objects.get(pk=opts.owner_id)
    group = _group(opts)
    order = 'ORDER BY "fileman_filemanager"."is_default" DESC, "fileman_filemanager"."id" ASC'
    cases = [
        ("_existing_for_group with a purpose", lambda: FileManager._existing_for_group(group, "fm_dupuse_sql")),
        ("_existing_for_group without a purpose", lambda: FileManager._existing_for_group(group, "")),
        ("_existing_for_user", lambda: FileManager._existing_for_user(owner)),
    ]
    def last_sql(call):
        # An execute wrapper, not CaptureQueriesContext: the debug query log is
        # capped, and in a long run a full log slices to nothing.
        seen = []

        def record(execute, sql, params, many, context):
            seen.append((sql, tuple(params or ())))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(record):
            call()
        assert_true(seen, "the lookup must run a query")
        return seen[-1]

    for label, call in cases:
        sql, _ = last_sql(call)
        assert_true(order in sql, f"{label} must order by default then id, got: {sql}")

    sql, params = last_sql(lambda: FileManager._existing_for_group(group, ""))
    assert_true('"fileman_filemanager"."use" = %s' in sql and "" in params,
                f"the purpose-less lookup must filter use = '', got: {sql} {params}")


# ---------------------------------------------------------------------------
# Refusal: one active manager per scope and purpose.
# ---------------------------------------------------------------------------

@th.tier("core")
@th.django_unit_test("refusal: a second active manager for a purpose is refused")
def test_new_active_duplicate_refused(opts):
    _clean_use(opts, "fm_dupuse_new")
    _make(opts, "ref_new_a", use="fm_dupuse_new")

    err = _refused(lambda: _make(opts, "ref_new_b", use="fm_dupuse_new"))
    assert_true(err is not None, "a second active manager for the purpose must be refused")
    assert_true(DUPLICATE_MESSAGE in str(err.reason),
                f"the refusal must say why, got {err.reason!r}")
    assert_eq(err.status, 400, f"the refusal must be a 400, got {err.status}")
    assert_eq(_active_count(opts, "fm_dupuse_new"), 1, "the refused save must write nothing")

    _make(opts, "ref_new_u1", use="fm_dupuse_new", user=True)
    err = _refused(lambda: _make(opts, "ref_new_u2", use="fm_dupuse_new", user=True))
    assert_true(err is not None, "a user's second active manager for a purpose must be refused")
    assert_eq(_active_count(opts, "fm_dupuse_new", user=True), 1,
              "the refused user save must write nothing")


@th.django_unit_test("refusal: switching on an inactive duplicate, or moving a manager into a taken purpose")
def test_transition_into_taken_purpose_refused(opts):
    _clean_use(opts, "fm_dupuse_taken", "fm_dupuse_other")
    _make(opts, "ref_taken_a", use="fm_dupuse_taken")

    sleeper = _make(opts, "ref_taken_off", use="fm_dupuse_taken", is_active=False)
    sleeper.is_active = True
    assert_true(_refused(sleeper.save) is not None,
                "switching on an inactive manager for a taken purpose must be refused")
    sleeper.refresh_from_db()
    assert_true(not sleeper.is_active, "the refused switch must leave the row inactive")

    mover = _make(opts, "ref_taken_mover", use="fm_dupuse_other")
    mover.use = "fm_dupuse_taken"
    assert_true(_refused(mover.save) is not None,
                "changing a manager's purpose into a taken one must be refused")
    mover.refresh_from_db()
    assert_eq(mover.use, "fm_dupuse_other", "the refused change must leave the purpose as it was")
    assert_eq(_active_count(opts, "fm_dupuse_taken"), 1, "the taken purpose keeps one active manager")


@th.django_unit_test("refusal: a partial save is judged by what it writes, not by memory")
def test_partial_save_judged_by_written_fields(opts):
    from mojo.apps.fileman.models import FileManager

    _clean_use(opts, "fm_dupuse_part_a", "fm_dupuse_part_b")
    a = _make(opts, "part_a", use="fm_dupuse_part_a")
    _make(opts, "part_b", use="fm_dupuse_part_b")

    # Only `use` is written; A stays active in the database, so it would duplicate B.
    a.is_active = False
    a.use = "fm_dupuse_part_b"
    assert_true(_refused(lambda: a.save(update_fields=["use"])) is not None,
                "a partial save that moves an active row into a taken purpose must be refused")
    stored = FileManager.objects.get(pk=a.pk)
    assert_eq((stored.use, stored.is_active), ("fm_dupuse_part_a", True),
              "the refused partial save must leave the row as stored")

    # An unsaved in-memory purpose change must not block a name-only save.
    a = FileManager.objects.get(pk=a.pk)
    a.use = "fm_dupuse_part_b"
    a.name = f"{PREFIX}part_a_renamed"
    a.save(update_fields=["name"])
    stored = FileManager.objects.get(pk=a.pk)
    assert_eq((stored.name, stored.use), (f"{PREFIX}part_a_renamed", "fm_dupuse_part_a"),
              "a name-only save must be written and leave the purpose alone")

    # A row loaded without `use` saves only its loaded fields.
    a = FileManager.objects.defer("use").get(pk=a.pk)
    a.is_public = False
    a.save()
    assert_eq(_active_count(opts, "fm_dupuse_part_b"), 1, "the purpose keeps one active manager")


@th.django_unit_test("refusal: a REST create of a duplicate is a 400 and changes no default")
def test_rest_duplicate_create_refused(opts):
    from mojo.apps.fileman.models import FileManager

    _clean_use(opts, "fm_dupuse_rest")
    FileManager.objects.filter(name=f"{PREFIX}rest_b").delete()
    existing = _make(opts, "rest_a", use="fm_dupuse_rest", is_default=True)

    _login(opts)
    resp = opts.client.post("/api/fileman/manager", {
        "name": f"{PREFIX}rest_b", "group": opts.group_id, "backend_type": "file",
        "backend_url": f"file:///{PREFIX}rest_b", "use": "fm_dupuse_rest",
        "is_default": True, "is_active": True, "is_public": False,
    })
    assert_eq(resp.status_code, 400, f"a duplicate REST create must be a 400, got {resp.status_code}")
    assert_true(DUPLICATE_MESSAGE in json.dumps(resp.response),
                f"the 400 must carry the message, got {resp.response}")
    assert_true(not FileManager.objects.filter(name=f"{PREFIX}rest_b").exists(),
                "the refused create must not leave a row")
    existing.refresh_from_db()
    assert_true(existing.is_default, "the refused create must not demote the existing default")


@th.django_unit_test("refusal: a refused REST-path save leaves the other default flag alone")
def test_rest_path_refusal_keeps_defaults(opts):
    from mojo.apps.fileman.models import FileManager

    _clean_use(opts, "fm_dupuse_restpath")
    FileManager.objects.filter(name=f"{PREFIX}restpath_a").delete()
    # B is the purpose's default, switched off when A's precheck runs.
    b = _make(opts, "restpath_b", use="fm_dupuse_restpath", is_default=True, is_active=False)
    a = FileManager(name=f"{PREFIX}restpath_a", backend_type="file",
                    backend_url=f"file:///{PREFIX}restpath_a", use="fm_dupuse_restpath",
                    is_default=True, is_active=True, group_id=opts.group_id)
    a.set_secrets({"base_path": opts.tmpdir, "use_shortlinks": False})
    a.on_rest_pre_save({"use": "", "is_default": False, "is_active": False}, True)
    # B is switched on between A's precheck and A's save.
    FileManager.objects.filter(pk=b.pk).update(is_active=True)

    assert_true(_refused(a.atomic_save) is not None,
                "the REST-path save into a purpose B now holds must be refused")
    b.refresh_from_db()
    assert_true(b.is_default, "the refused REST-path save must not demote B's default")
    assert_true(not FileManager.objects.filter(name=f"{PREFIX}restpath_a").exists(),
                "the refused REST-path save must not leave a row")


# ---------------------------------------------------------------------------
# Controls: what keeps working.
# ---------------------------------------------------------------------------

@th.django_unit_test("controls: a legacy duplicate can be renamed, edited and deactivated")
def test_legacy_duplicate_stays_editable(opts):
    _clean_use(opts, "fm_dupuse_legacy")
    _make(opts, "ctl_legacy_a", use="fm_dupuse_legacy")
    dup = _legacy(opts, "ctl_legacy_b", use="fm_dupuse_legacy")

    dup.name = f"{PREFIX}ctl_legacy_renamed"
    dup.save()
    dup.is_public = False
    dup.save()
    dup.is_active = False
    dup.save()
    dup.refresh_from_db()
    assert_eq(dup.name, f"{PREFIX}ctl_legacy_renamed", "the rename must be saved")
    assert_true(not dup.is_active, "the deactivation must be saved")


@th.django_unit_test("controls: purpose-less managers keep their one-default switch")
def test_purposeless_managers_unchanged(opts):
    from mojo.apps.fileman.models import FileManager

    _clean_use(opts, "", "fm_dupuse_purpose")
    general = _make(opts, "ctl_general", is_default=True)
    second = _make(opts, "ctl_general_2")
    assert_true(second.pk is not None, "a second purpose-less non-default manager must be allowed")

    _login(opts)
    resp = opts.client.post(f"/api/fileman/manager/{second.pk}", {"is_default": True})
    assert_eq(resp.status_code, 200, f"making a purpose-less manager the default must work, got {resp.status_code}")
    general.refresh_from_db()
    second.refresh_from_db()
    assert_true(second.is_default and not general.is_default,
                "the new purpose-less default must demote the previous one")

    purpose = _make(opts, "ctl_purpose", use="fm_dupuse_purpose")
    resp = opts.client.post(f"/api/fileman/manager/{purpose.pk}", {"is_default": True})
    assert_eq(resp.status_code, 200, f"making a purpose manager the default must work, got {resp.status_code}")
    second.refresh_from_db()
    purpose.refresh_from_db()
    assert_true(purpose.is_default, "the purpose manager must become its purpose's default")
    assert_true(second.is_default,
                "making a purpose manager the default must not demote the purpose-less default")
    picked = FileManager.get_for_group(_group(opts), "")
    assert_eq(picked.pk, second.pk, f"an upload with no purpose keeps the general default, got {picked.pk}")


# ---------------------------------------------------------------------------
# Auto-create reuses the existing manager and never raises.
# ---------------------------------------------------------------------------

@th.django_unit_test("auto-create: an existing purpose manager under another name is reused")
def test_auto_create_reuses_existing(opts):
    from mojo.apps.fileman.models import FileManager

    _clean_use(opts, "fm_dupuse_reports")
    FileManager.objects.filter(name=f"{PREFIX}auto_parent").delete()
    parent = FileManager(name=f"{PREFIX}auto_parent", backend_type="file",
                         backend_url="file://", is_active=True, is_default=False)
    parent.set_secrets({"base_path": opts.tmpdir, "use_shortlinks": False})
    parent.save()
    try:
        existing = _make(opts, "auto_reports", use="fm_dupuse_reports")
        group = _group(opts)
        made = FileManager._create_for_group(group, "fm_dupuse_reports", parent)
        assert_true(made is not None and made.pk == existing.pk,
                    f"auto-create must return the existing manager {existing.pk}, got {getattr(made, 'pk', None)}")
        assert_eq(_active_count(opts, "fm_dupuse_reports"), 1, "auto-create must not add a duplicate")
        picked = FileManager.get_for_group(group, "fm_dupuse_reports")
        assert_eq(picked.pk, existing.pk, "get_for_group must return the existing manager")
    finally:
        FileManager.objects.filter(parent=parent).update(parent=None)
        parent.delete()


# ---------------------------------------------------------------------------
# Report: read-only, lists each duplicated scope and the id selection picks.
# ---------------------------------------------------------------------------

@th.django_unit_test("report: fileman_duplicate_uses lists duplicates and changes nothing")
def test_duplicate_report(opts):
    from django.core.management import call_command
    from mojo.apps.fileman.models import FileManager

    _clean_use(opts, "fm_dupuse_report")
    first = _make(opts, "rep_a", use="fm_dupuse_report")
    dup = _legacy(opts, "rep_b", use="fm_dupuse_report", is_default=True)
    before = list(FileManager.objects.filter(group_id=opts.group_id).order_by("id").values_list(
        "id", "is_active", "is_default", "use"))

    out = io.StringIO()
    call_command("fileman_duplicate_uses", "--json", stdout=out)
    rows = json.loads(out.getvalue())
    mine = [r for r in rows if r.get("group_id") == opts.group_id and r.get("use") == "fm_dupuse_report"]
    assert_eq(len(mine), 1, f"the duplicated scope must be listed once, got {mine}")
    ids = sorted(m["id"] for m in mine[0]["managers"])
    assert_eq(ids, sorted([first.pk, dup.pk]), f"both duplicates must be listed, got {mine[0]}")
    assert_eq(mine[0]["selected_id"], dup.pk,
              f"the report must name the default as the one selection picks, got {mine[0]}")

    after = list(FileManager.objects.filter(group_id=opts.group_id).order_by("id").values_list(
        "id", "is_active", "is_default", "use"))
    assert_eq(after, before, "the report must not change any manager")
