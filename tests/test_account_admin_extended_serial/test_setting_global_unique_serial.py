"""Maestro item #8201 — the concurrent and DDL halves of one-row-per-key.

Kept out of the parallel ring on purpose: the race holds a row lock across two
threads, and the migration test drops and restores a unique index, which takes
an exclusive lock on the settings table. The single-connection contracts are in
`test_account/test_setting_global_unique.py`.
"""
import threading
import time

from testit import helpers as th


LOCK_WAIT_TIMEOUT = 10.0


def _drop(rows):
    for row in rows:
        row.remove_from_cache()
    rows.delete()


class _Rollback(Exception):
    """Raised to undo a test's DDL and seeded rows."""


def _editor():
    """What RunPython hands the repair: a schema editor. It reads only
    `.connection`."""
    from types import SimpleNamespace
    from django.db import connection
    return SimpleNamespace(connection=connection)


def _backend_pid(connection):
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return cursor.fetchone()[0]


def _blocked_by(connection, waiter_pid, holder_pid):
    """True when `waiter_pid`'s backend is waiting on a lock `holder_pid` holds."""
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


@th.django_unit_test("#8201: Setting.set racing an uncommitted global insert ends with one row")
def test_setting_set_race_recovers(opts):
    """Thread A inserts the global row and holds its transaction open. Thread B
    calls Setting.set for the same key: it must wait on A's backend, then
    recover through get_or_create and update A's row, not add a second one."""
    from django.db import connection, transaction
    from mojo.apps.account.models import Setting

    _drop(Setting.objects.filter(key="TESTIT_UNIQ_RACE"))
    inserted = threading.Event()
    b_ready = threading.Event()
    release = threading.Event()
    pids = {}
    errors = []

    def holder():
        try:
            with transaction.atomic():
                pids["a"] = _backend_pid(connection)
                Setting.objects.bulk_create([
                    Setting(key="TESTIT_UNIQ_RACE", value="a")])
                inserted.set()
                release.wait(LOCK_WAIT_TIMEOUT * 2)
        except Exception as exc:
            errors.append(f"holder: {exc!r}")
        finally:
            inserted.set()
            connection.close()

    def setter():
        try:
            pids["b"] = _backend_pid(connection)
            b_ready.set()
            Setting.set("TESTIT_UNIQ_RACE", "b")
        except Exception as exc:
            errors.append(f"setter: {exc!r}")
        finally:
            b_ready.set()
            connection.close()

    a = threading.Thread(target=holder)
    b = threading.Thread(target=setter)
    try:
        a.start()
        assert inserted.wait(LOCK_WAIT_TIMEOUT), "the holder never inserted its row"
        assert "a" in pids, f"the holder never reported its backend: {errors}"
        b.start()
        assert b_ready.wait(LOCK_WAIT_TIMEOUT) and "b" in pids, \
            f"the setter never reported its backend: {errors}"
        waited = _wait_blocked(connection, pids["b"], pids["a"], b)
        release.set()
        a.join(LOCK_WAIT_TIMEOUT)
        b.join(LOCK_WAIT_TIMEOUT)
        assert not a.is_alive() and not b.is_alive(), "a race thread never finished"
        assert not errors, f"the race raised: {errors}"
        assert waited, "Setting.set never waited on the holder's uncommitted global row"
        rows = list(Setting.objects.filter(
            key="TESTIT_UNIQ_RACE", group=None).values_list("value", flat=True))
        assert rows == ["b"], (
            f"the race must end with one global row holding the setter's value, got {rows}")
    finally:
        release.set()
        a.join(LOCK_WAIT_TIMEOUT)
        b.join(LOCK_WAIT_TIMEOUT)
        _drop(Setting.objects.filter(key="TESTIT_UNIQ_RACE"))


def _without_constraint(body):
    """Run body(Setting, migration) with the global unique index dropped, then
    roll everything back: the DDL and every row body seeded."""
    import importlib
    from django.db import connection, transaction
    from mojo.apps.account.models import Setting

    migration = importlib.import_module(
        "mojo.apps.account.migrations.0055_setting_global_key_unique")
    constraint = next(c for c in Setting._meta.constraints
                      if c.name == "account_setting_global_key_uniq")
    try:
        with transaction.atomic():
            with connection.schema_editor() as editor:
                editor.remove_constraint(Setting, constraint)
            body(Setting, migration)
            raise _Rollback()
    except _Rollback:
        pass


@th.django_unit_test("#8201: migration 0055 merges identical global copies, keeping the oldest")
def test_migration_collapses_identical_copies(opts):
    from django.apps import apps

    seen = {}

    def body(Setting, migration):
        Setting.objects.bulk_create([
            Setting(key="TESTIT_UNIQ_MIG_SAME", value="on"),
            Setting(key="TESTIT_UNIQ_MIG_SAME", value="on"),
            Setting(key="TESTIT_UNIQ_MIG_SAME", value="on"),
        ])
        pks = sorted(Setting.objects.filter(
            key="TESTIT_UNIQ_MIG_SAME", group=None).values_list("pk", flat=True))
        assert len(pks) == 3, f"the constraint was not dropped: seeded {pks}"
        migration.collapse_global_duplicates(apps, _editor())
        seen["kept"] = list(Setting.objects.filter(
            key="TESTIT_UNIQ_MIG_SAME", group=None).values_list("pk", "value"))
        seen["oldest"] = pks[0]

    _without_constraint(body)
    assert seen.get("kept") == [(seen.get("oldest"), "on")], (
        f"identical copies must collapse to the oldest row: {seen}")


@th.django_unit_test("#8201: migration 0055 keeps the oldest conflicting copy, logs ids only and clears the cache on commit")
def test_migration_keeps_oldest_conflicting_copy(opts):
    """Option B (Ian, #8201): conflicting platform-wide copies never stop the
    migration. The oldest row stays with its value untouched, the rest go, the
    log names keys and row ids but no value, and each repaired key is cleared
    from the settings:global hash after commit."""
    import contextlib
    import io
    import types
    from unittest import mock
    from django.apps import apps

    seen = {"callbacks": []}

    def capture_on_commit(func, using=None):
        seen["callbacks"].append(func)

    def body(Setting, migration):
        Setting.objects.bulk_create([
            Setting(key="TESTIT_UNIQ_MIG_SAME", value="on"),
            Setting(key="TESTIT_UNIQ_MIG_SAME", value="on"),
            Setting(key="TESTIT_UNIQ_MIG_DIFF", value="value-one"),
            Setting(key="TESTIT_UNIQ_MIG_DIFF", value="value-two"),
            Setting(key="TESTIT_UNIQ_MIG_DIFF", value="value-three"),
        ])
        diff = list(Setting.objects.filter(key="TESTIT_UNIQ_MIG_DIFF", group=None)
                    .order_by("pk").values_list("pk", "value"))
        same = sorted(Setting.objects.filter(key="TESTIT_UNIQ_MIG_SAME", group=None)
                      .values_list("pk", flat=True))
        seen["diff_before"], seen["same_oldest"] = diff, same[0]
        out = io.StringIO()
        # Module-local patch: the migration's own reference to transaction,
        # never Django's shared on_commit (see #7494).
        fake_transaction = types.SimpleNamespace(on_commit=capture_on_commit)
        with mock.patch.object(migration, "transaction", fake_transaction), \
                contextlib.redirect_stdout(out):
            migration.collapse_global_duplicates(apps, _editor())
        seen["log"] = out.getvalue()
        seen["diff_after"] = list(Setting.objects.filter(
            key="TESTIT_UNIQ_MIG_DIFF", group=None).values_list("pk", "value"))
        seen["same_after"] = list(Setting.objects.filter(
            key="TESTIT_UNIQ_MIG_SAME", group=None).values_list("pk", flat=True))

    _without_constraint(body)
    diff_before = seen["diff_before"]
    assert seen["diff_after"] == [diff_before[0]], (
        f"the oldest conflicting row must stay, value untouched: {seen['diff_after']} "
        f"from {diff_before}")
    assert seen["same_after"] == [seen["same_oldest"]], (
        f"identical copies must collapse to the oldest row: {seen['same_after']}")
    log = seen["log"]
    deleted = ", ".join(str(pk) for pk, _ in diff_before[1:])
    assert "TESTIT_UNIQ_MIG_DIFF" in log and f"kept Setting row {diff_before[0][0]}" in log \
        and deleted in log, f"the log must name the key, kept row and deleted rows: {log!r}"
    assert "TESTIT_UNIQ_MIG_SAME" not in log, f"identical copies are not a conflict: {log!r}"
    for value in ("value-one", "value-two", "value-three"):
        assert value not in log, f"the log exposed a value: {log!r}"
    assert len(seen["callbacks"]) == 1, (
        f"exactly one after-commit cache clear expected, got {len(seen['callbacks'])}")

    # The after-commit callback clears every repaired key from the real hash.
    from mojo.apps.account.models import Setting
    from mojo.apps.account.models.setting import REDIS_GLOBAL_KEY
    r = Setting._redis()
    assert r is not None, "the test needs Redis to check the cache clear"
    keys = ("TESTIT_UNIQ_MIG_DIFF", "TESTIT_UNIQ_MIG_SAME")
    try:
        for key in keys:
            r.hset(REDIS_GLOBAL_KEY, key, "stale")
        r.hset(REDIS_GLOBAL_KEY, "TESTIT_UNIQ_MIG_UNTOUCHED", "keep")
        seen["callbacks"][0]()
        left = {key: r.hget(REDIS_GLOBAL_KEY, key) for key in keys}
        assert left == {key: None for key in keys}, (
            f"repaired keys must be cleared from {REDIS_GLOBAL_KEY}: {left}")
        assert r.hget(REDIS_GLOBAL_KEY, "TESTIT_UNIQ_MIG_UNTOUCHED") is not None, \
            "the cache clear must touch only the repaired keys"
    finally:
        r.hdel(REDIS_GLOBAL_KEY, *keys, "TESTIT_UNIQ_MIG_UNTOUCHED")


@th.django_unit_test("#8201: migration 0055's cache clear never raises when Redis fails")
def test_migration_cache_clear_survives_redis_failure(opts):
    import contextlib
    import importlib
    import io
    from unittest import mock

    migration = importlib.import_module(
        "mojo.apps.account.migrations.0055_setting_global_key_unique")
    out = io.StringIO()
    with mock.patch("mojo.helpers.redis.get_connection",
                    side_effect=ConnectionError("redis down")), \
            contextlib.redirect_stdout(out):
        migration.clear_cached_globals(["TESTIT_UNIQ_MIG_DIFF"])
    assert "could not clear 1 repaired key" in out.getvalue(), (
        f"a failed cache clear must say so: {out.getvalue()!r}")


@th.django_unit_test("#8201: migration 0055 holds writers off, so it never deletes a committed change")
def test_migration_repair_blocks_concurrent_writer(opts):
    """Review 103840's scenario, on a probe table built from the 0054 Setting
    schema. Two identical copies exist. Once the repair has read them, a second
    connection updates the later copy. Before the fix that update committed and
    the repair then deleted the changed row. Now the writer must wait on the
    migration's own backend until it commits, so no committed write is lost."""
    import importlib
    from django.db import connection, transaction
    from django.db.migrations.loader import MigrationLoader

    migration = importlib.import_module(
        "mojo.apps.account.migrations.0055_setting_global_key_unique")
    state = MigrationLoader(connection).project_state(
        [("account", "0054_geolocatedip_firewall_reconciliation")])
    state.models["account", "setting"].options["db_table"] = "testit_8201_setting_probe"
    Probe = state.apps.get_model("account", "Setting")
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS testit_8201_setting_probe")
    with connection.schema_editor() as editor:
        editor.create_model(Probe)

    original_plan = migration.plan_global_dedup
    seen = {"errors": []}
    writer_pid = {}
    pid_ready = threading.Event()
    go = threading.Event()

    def writer(pk):
        try:
            writer_pid["pid"] = _backend_pid(connection)
            pid_ready.set()
            go.wait(LOCK_WAIT_TIMEOUT)
            seen["updated"] = Probe.objects.filter(pk=pk).update(value="changed-after-read")
        except Exception as exc:
            seen["errors"].append(repr(exc))
        finally:
            pid_ready.set()
            connection.close()

    try:
        first = Probe.objects.create(key="TESTIT_UNIQ_PROBE", value="same")
        second = Probe.objects.create(key="TESTIT_UNIQ_PROBE", value="same")
        thread = threading.Thread(target=writer, args=(second.pk,))
        thread.start()
        assert pid_ready.wait(LOCK_WAIT_TIMEOUT) and "pid" in writer_pid, \
            f"the writer never reported its backend: {seen['errors']}"

        def plan_after_concurrent_write(rows):
            go.set()
            seen["blocked"] = _wait_blocked(
                connection, writer_pid["pid"], _backend_pid(connection), thread)
            return original_plan(rows)

        migration.plan_global_dedup = plan_after_concurrent_write
        with transaction.atomic():
            migration.collapse_global_duplicates(state.apps, _editor())
        thread.join(LOCK_WAIT_TIMEOUT)
        assert not thread.is_alive(), "the writer never finished"
        assert not seen["errors"], f"the writer raised: {seen['errors']}"
        assert seen.get("blocked"), (
            "the writer was not held off by the migration: it could change a "
            f"copy after the repair read it (updated={seen.get('updated')})")
        rows = list(Probe.objects.values_list("pk", "value"))
        assert rows == [(first.pk, "same")], f"the repair left {rows}"
        assert seen.get("updated") == 0, (
            "the writer's update landed on a row the repair then deleted: "
            f"updated={seen.get('updated')}")
    finally:
        migration.plan_global_dedup = original_plan
        go.set()
        with connection.schema_editor() as editor:
            editor.delete_model(Probe)
