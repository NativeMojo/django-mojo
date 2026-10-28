"""#8662 — two connections racing to make a second manager for one purpose.

The single-connection contracts are in `test_fileman/30_test_manager_use_selection.py`.
These hold a row lock across two threads, so they stay out of the parallel
ring. Each thread uses its own Django connection under READ COMMITTED; Events
order the steps and `pg_blocking_pids` proves the second one waited, so no
result rests on a sleep. The pattern is #8201's
`test_account_admin_extended_serial/test_setting_global_unique_serial.py`.

Limit: two connections only. A save racing a save, and a save racing the
uploader's auto-create.
"""
import tempfile
import threading
import time

from testit import helpers as th


PREFIX = "fm_dupuse_race_"
GROUP = "fm_dupuse_race_group"
OWNER = "fm_dupuse_race_owner"
SYSTEM_DEFAULT_PK = -88662
LOCK_WAIT_TIMEOUT = 10.0
JOIN_TIMEOUT = 15.0


def _clear():
    from mojo.apps.account.models import Group, User
    from mojo.apps.fileman.models import FileManager

    FileManager.objects.filter(parent_id=SYSTEM_DEFAULT_PK).update(parent=None)
    FileManager.objects.filter(pk=SYSTEM_DEFAULT_PK).delete()
    FileManager.objects.filter(name__startswith=PREFIX).delete()
    FileManager.objects.filter(group__name=GROUP).delete()
    FileManager.objects.filter(user__username=OWNER).delete()
    Group.objects.filter(name=GROUP).delete()
    User.objects.filter(username=OWNER).delete()


@th.django_unit_setup()
@th.requires_app("mojo.apps.fileman")
def setup_manager_use_race(opts):
    from mojo.apps.account.models import Group, User

    _clear()
    group = Group(name=GROUP)
    group.save()
    owner = User(username=OWNER, email=f"{OWNER}@example.com")
    owner.save()
    opts.group_id = group.pk
    opts.owner_id = owner.pk
    opts.tmpdir = tempfile.mkdtemp(prefix=PREFIX)


def _backend_pid(connection):
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return cursor.fetchone()[0]


def _blocked_by(connection, waiter_pid, holder_pid):
    with connection.cursor() as cursor:
        cursor.execute("SELECT %s = ANY(pg_blocking_pids(%s))", [holder_pid, waiter_pid])
        return bool(cursor.fetchone()[0])


def _wait_blocked(connection, waiter_pid, holder_pid, thread):
    deadline = time.monotonic() + LOCK_WAIT_TIMEOUT
    while time.monotonic() < deadline and thread.is_alive():
        if _blocked_by(connection, waiter_pid, holder_pid):
            return True
        time.sleep(0.05)
    return False


def _manager(opts, name, use, user=False):
    from mojo.apps.fileman.models import FileManager

    scope = {"user_id": opts.owner_id} if user else {"group_id": opts.group_id}
    manager = FileManager(name=f"{PREFIX}{name}", backend_type="file",
                          backend_url=f"file:///{PREFIX}{name}", use=use,
                          is_active=True, **scope)
    manager.set_secrets({"base_path": opts.tmpdir, "use_shortlinks": False})
    return manager


def _race(holder_body, racer_body):
    """Run holder_body inside an open transaction, then racer_body on a second
    connection, and release the holder once the racer is seen waiting.

    Returns (waited, outcome, errors): outcome holds what each body returned
    or raised, keyed "a" and "b".
    """
    from django.db import connection, transaction

    a_saved = threading.Event()
    b_ready = threading.Event()
    release = threading.Event()
    pids = {}
    outcome = {}
    errors = []

    def holder():
        try:
            with transaction.atomic():
                pids["a"] = _backend_pid(connection)
                outcome["a"] = holder_body()
                a_saved.set()
                release.wait(LOCK_WAIT_TIMEOUT * 2)
        except Exception as exc:
            errors.append(f"holder: {exc!r}")
        finally:
            a_saved.set()
            connection.close()

    def racer():
        try:
            pids["b"] = _backend_pid(connection)
            b_ready.set()
            outcome["b"] = racer_body()
        except Exception as exc:
            outcome["b"] = exc
        finally:
            b_ready.set()
            connection.close()

    a = threading.Thread(target=holder)
    b = threading.Thread(target=racer)
    waited = False
    try:
        a.start()
        assert a_saved.wait(LOCK_WAIT_TIMEOUT), "the holder never saved"
        assert "a" in pids, f"the holder never reported its backend: {errors}"
        b.start()
        assert b_ready.wait(LOCK_WAIT_TIMEOUT) and "b" in pids, "the racer never reported its backend"
        waited = _wait_blocked(connection, pids["b"], pids["a"], b)
    finally:
        release.set()
        a.join(JOIN_TIMEOUT)
        b.join(JOIN_TIMEOUT)
    assert not a.is_alive() and not b.is_alive(), "a race thread never finished"
    return waited, outcome, errors


def _active(opts, use, user=False):
    from mojo.apps.fileman.models import FileManager
    scope = {"user_id": opts.owner_id, "group": None} if user else {"group_id": opts.group_id, "user": None}
    return list(FileManager.objects.filter(use=use, is_active=True, **scope).values_list("name", flat=True))


def _check_save_race(opts, use, user):
    from mojo.apps.fileman.models import FileManager

    FileManager.objects.filter(name__startswith=PREFIX).delete()
    a = _manager(opts, "a", use, user=user)
    b = _manager(opts, "b", use, user=user)
    try:
        waited, outcome, errors = _race(lambda: a.save(), lambda: b.save())
        assert not errors, f"the holder raised: {errors}"
        assert waited, "the second save never waited on the first one's lock"
        from mojo.apps.fileman.models.manager import DuplicateUseError
        assert isinstance(outcome.get("b"), DuplicateUseError), (
            f"the second save must be refused with DuplicateUseError, got {outcome.get('b')!r}")
        names = _active(opts, use, user=user)
        assert names == [f"{PREFIX}a"], f"exactly one active manager, A's, must remain, got {names}"
        assert not FileManager.objects.filter(name=f"{PREFIX}b").exists(), "no row for B may exist"
    finally:
        FileManager.objects.filter(name__startswith=PREFIX).delete()


@th.django_unit_test("#8662: two group saves for one purpose end with one active manager")
def test_group_save_race(opts):
    _check_save_race(opts, "fm_dupuse_race", user=False)


@th.django_unit_test("#8662: two user saves for one purpose end with one active manager")
def test_user_save_race(opts):
    _check_save_race(opts, "fm_dupuse_race", user=True)


@th.django_unit_test("#8662: the uploader's auto-create waits and returns the racing save's manager")
def test_auto_create_race(opts):
    from mojo.apps.account.models import Group
    from mojo.apps.fileman.models import FileManager

    use = "fm_dupuse_reports"
    FileManager.objects.filter(name__startswith=PREFIX).delete()
    FileManager.objects.filter(group_id=opts.group_id, use=use).delete()
    FileManager.objects.filter(pk=SYSTEM_DEFAULT_PK).delete()
    system = FileManager(pk=SYSTEM_DEFAULT_PK, name=f"{PREFIX}system", backend_type="file",
                         backend_url="file://", is_active=True, is_default=True)
    system.set_secrets({"base_path": opts.tmpdir, "use_shortlinks": False})
    system.save()
    a = _manager(opts, "r", use)
    try:
        def holder_body():
            a.save()
            return a.pk

        def racer_body():
            return FileManager.get_for_group(Group.objects.get(pk=opts.group_id), use)

        waited, outcome, errors = _race(holder_body, racer_body)
        assert not errors, f"the holder raised: {errors}"
        assert waited, "the auto-create never waited on the racing save's lock"
        picked = outcome.get("b")
        assert isinstance(picked, FileManager), f"the uploader must get a manager, not {picked!r}"
        assert picked.pk == outcome.get("a"), (
            f"the uploader must get the racing save's manager {outcome.get('a')}, got {picked.pk}")
        names = _active(opts, use)
        assert names == [f"{PREFIX}r"], f"exactly one active manager must remain, got {names}"
    finally:
        FileManager.objects.filter(group_id=opts.group_id, use=use).delete()
        FileManager.objects.filter(parent_id=SYSTEM_DEFAULT_PK).update(parent=None)
        FileManager.objects.filter(pk=SYSTEM_DEFAULT_PK).delete()
