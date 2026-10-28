"""Maestro item #9734 — a Group row is locked FOR NO KEY UPDATE, not FOR UPDATE.

On PostgreSQL every INSERT of a row that references a group takes FOR KEY
SHARE on the group row until its transaction commits. `Group.save()` used to
lock the row FOR UPDATE, which conflicts with that: a save of a busy tenant's
row waited for a gap in child inserts and was passed by newer ones, and every
child insert waited while a save held the row. FOR NO KEY UPDATE does not
conflict with KEY SHARE and still excludes every other writer.

Each test drives a second database connection on its own thread, the way
`tests/test_chat/locks.py` does. A `lock_timeout` is set INSIDE that
connection's transaction so a blocked statement fails in about a second
instead of hanging the module. PostgreSQL only: the modes under test do not
exist elsewhere.
"""
import threading
import uuid as _uuid
from concurrent.futures import ThreadPoolExecutor

from testit import helpers as th

TESTIT_TIER = "bug"

PREFIX = "lock_mode_"
LOCK_TIMEOUT = "SET LOCAL lock_timeout = 1000"
# Child-row keys are literal TESTIT_ keys: the isolation scanner proves every
# Setting write outside the protected roster from the literal alone.


def _sqlstate(error):
    cause = error.__cause__
    return getattr(cause, "sqlstate", getattr(cause, "pgcode", None))


def _on_own_connection(work):
    """`work()` on this thread's own connection, closed when it returns."""
    from django.db import close_old_connections, connection
    close_old_connections()
    try:
        return work()
    finally:
        connection.close()


def _hold_open(write, started, release):
    """Run `write()` inside a transaction and keep that transaction open
    until `release` is set. `started` is set once the write is in.

    Django creates PostgreSQL foreign keys DEFERRABLE INITIALLY DEFERRED, so
    the referential check — and the FOR KEY SHARE it takes on the group row —
    would otherwise run only at commit. SET CONSTRAINTS ALL IMMEDIATE makes
    the insert take that lock now and hold it until release, which is what an
    immediate constraint, a raw insert or a long commit does in production."""
    from django.db import connection, transaction

    def work():
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            result = write()
            started.set()
            release.wait(timeout=15)
        return result
    return _on_own_connection(work)


def _postgres_only(opts):
    """Skip, rather than fail, where the lock modes under test do not exist."""
    if not opts.pg:
        raise th.TestitSkip("row lock modes are PostgreSQL-only")


@th.django_unit_setup()
def setup_group_lock_mode(opts):
    from django.db import connection
    from mojo.apps.account.models import Group, Setting
    opts.pg = connection.vendor == "postgresql"
    opts.group_id = None
    if not opts.pg:
        return
    Setting.objects.filter(key="TESTIT_lock_mode_a").delete()
    Setting.objects.filter(key="TESTIT_lock_mode_b").delete()
    Group.objects.filter(name__startswith=PREFIX).delete()
    opts.group_id = Group.objects.create(name=f"{PREFIX}{_uuid.uuid4().hex[:8]}").pk


@th.django_unit_test("#9734: a child row can be inserted while a Group save holds the row")
def test_child_insert_completes_while_save_is_held(opts):
    _postgres_only(opts)
    from django.db import transaction
    from mojo.apps.account.models import Group, Setting

    def insert_with_timeout():
        from django.db import connection, transaction as tx

        def work():
            with tx.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(LOCK_TIMEOUT)
                return Setting.objects.create(
                    group_id=opts.group_id, key="TESTIT_lock_mode_a", value="x").pk
        return _on_own_connection(work)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            group = Group.objects.get(pk=opts.group_id)
            metadata = dict(group.metadata or {})
            metadata["lock_mode"] = "held"
            group.metadata = metadata
            group.save(update_fields=["metadata", "modified"])
            # the save's row lock is held until this block exits
            try:
                child_pk = pool.submit(insert_with_timeout).result(timeout=10)
            except Exception as error:  # reported through the assert below
                assert False, \
                    f"a child insert must not wait for a held Group save (sqlstate {_sqlstate(error)}): {error!r}"
    assert Setting.objects.filter(pk=child_pk, group_id=opts.group_id).exists(), \
        "the child row inserted beside the held save must be stored"
    assert Group.objects.get(pk=opts.group_id).metadata.get("lock_mode") == "held", \
        "the held save must still have written its metadata"
    Setting.objects.filter(key="TESTIT_lock_mode_a").delete()


@th.django_unit_test("#9734: a Group save completes while a child insert holds its KEY SHARE")
def test_save_completes_while_child_insert_is_held(opts):
    _postgres_only(opts)
    from django.db import connection, transaction
    from mojo.apps.account.models import Group, Setting
    started, release = threading.Event(), threading.Event()

    def child_row():
        return Setting.objects.create(
            group_id=opts.group_id, key="TESTIT_lock_mode_b", value="x").pk

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(_hold_open, child_row, started, release)
        try:
            assert started.wait(timeout=10), "the child insert never started"
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(LOCK_TIMEOUT)
                group = Group.objects.get(pk=opts.group_id)
                metadata = dict(group.metadata or {})
                metadata["lock_mode"] = "beside-insert"
                group.metadata = metadata
                try:
                    group.save(update_fields=["metadata", "modified"])
                except Exception as error:
                    assert False, \
                        f"Group.save() must not wait for an in-flight child insert (sqlstate {_sqlstate(error)}): {error!r}"
        finally:
            # Always let the holder commit, or the teardown's DELETE of the
            # group waits on its KEY SHARE forever.
            release.set()
            child_pk = holder.result(timeout=10)
    assert Group.objects.get(pk=opts.group_id).metadata.get("lock_mode") == "beside-insert", \
        "the save made beside the held insert must be stored"
    assert Setting.objects.filter(pk=child_pk, group_id=opts.group_id).exists(), \
        "the held child insert must have committed after release"
    Setting.objects.filter(key="TESTIT_lock_mode_b").delete()


@th.django_unit_test("#9734: the weaker lock still excludes every other Group writer")
def test_writers_still_exclude_each_other(opts):
    _postgres_only(opts)
    from django.db import DatabaseError, transaction
    from mojo.apps.account.models import Group

    def writer_refused(no_key):
        def work():
            try:
                with transaction.atomic():
                    Group.objects.select_for_update(no_key=no_key, nowait=True).get(pk=opts.group_id)
                return None
            except DatabaseError as error:
                return _sqlstate(error)
        return _on_own_connection(work)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            group = Group.objects.get(pk=opts.group_id)
            metadata = dict(group.metadata or {})
            metadata["lock_mode"] = "writers"
            group.metadata = metadata
            group.save(update_fields=["metadata", "modified"])
            assert pool.submit(writer_refused, True).result(timeout=10) == "55P03", \
                "another NO KEY UPDATE writer must still be refused while a save holds the row"
            assert pool.submit(writer_refused, False).result(timeout=10) == "55P03", \
                "a FOR UPDATE writer must still be refused while a save holds the row"
        assert pool.submit(writer_refused, True).result(timeout=10) is None, \
            "the save's lock must be released at commit"
    # last test in the file: drop what setup created
    Group.objects.filter(pk=opts.group_id).delete()
