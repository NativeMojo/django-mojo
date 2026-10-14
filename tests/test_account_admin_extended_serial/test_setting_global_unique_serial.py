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


@th.django_unit_test("#8201: Setting.set racing an uncommitted global insert ends with one row")
def test_setting_set_race_recovers(opts):
    """Thread A inserts the global row and holds its transaction open. Thread B
    calls Setting.set for the same key: it must wait on the unique index, then
    recover through get_or_create and update A's row, not add a second one."""
    from django.db import connection, transaction
    from mojo.apps.account.models import Setting

    _drop(Setting.objects.filter(key="TESTIT_UNIQ_RACE"))
    inserted = threading.Event()
    release = threading.Event()
    errors = []

    def holder():
        try:
            with transaction.atomic():
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
            Setting.set("TESTIT_UNIQ_RACE", "b")
        except Exception as exc:
            errors.append(f"setter: {exc!r}")
        finally:
            connection.close()

    a = threading.Thread(target=holder)
    b = threading.Thread(target=setter)
    try:
        a.start()
        assert inserted.wait(LOCK_WAIT_TIMEOUT), "the holder never inserted its row"
        b.start()
        waited = False
        deadline = time.monotonic() + LOCK_WAIT_TIMEOUT
        while time.monotonic() < deadline and b.is_alive():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'")
                if cursor.fetchone()[0]:
                    waited = True
                    break
            time.sleep(0.05)
        release.set()
        a.join(LOCK_WAIT_TIMEOUT)
        b.join(LOCK_WAIT_TIMEOUT)
        assert not errors, f"the race raised: {errors}"
        assert waited, "Setting.set never waited on the uncommitted global row"
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
        migration.collapse_global_duplicates(apps, None)
        seen["kept"] = list(Setting.objects.filter(
            key="TESTIT_UNIQ_MIG_SAME", group=None).values_list("pk", "value"))
        seen["oldest"] = pks[0]

    _without_constraint(body)
    assert seen.get("kept") == [(seen.get("oldest"), "on")], (
        f"identical copies must collapse to the oldest row: {seen}")


@th.django_unit_test("#8201: migration 0055 stops on conflicting global copies and changes nothing")
def test_migration_stops_on_conflicting_copies(opts):
    from django.apps import apps

    seen = {}

    def body(Setting, migration):
        Setting.objects.bulk_create([
            Setting(key="TESTIT_UNIQ_MIG_SAME", value="on"),
            Setting(key="TESTIT_UNIQ_MIG_SAME", value="on"),
            Setting(key="TESTIT_UNIQ_MIG_DIFF", value="value-one"),
            Setting(key="TESTIT_UNIQ_MIG_DIFF", value="value-two"),
        ])
        before = sorted(Setting.objects.filter(
            key__startswith="TESTIT_UNIQ_MIG_", group=None).values_list("pk", flat=True))
        try:
            migration.collapse_global_duplicates(apps, None)
        except migration.ConflictingGlobalSettings as exc:
            seen["message"] = str(exc)
        seen["unchanged"] = before == sorted(Setting.objects.filter(
            key__startswith="TESTIT_UNIQ_MIG_", group=None).values_list("pk", flat=True))
        seen["diff_pks"] = sorted(Setting.objects.filter(
            key="TESTIT_UNIQ_MIG_DIFF").values_list("pk", flat=True))

    _without_constraint(body)
    message = seen.get("message")
    assert message, "conflicting copies must stop the migration"
    assert seen["unchanged"], "a stopped migration deleted rows"
    pks = ", ".join(str(pk) for pk in seen["diff_pks"])
    assert "TESTIT_UNIQ_MIG_DIFF" in message and pks in message, (
        f"the stop message must name the key and rows {pks}: {message}")
    assert "TESTIT_UNIQ_MIG_SAME" not in message, (
        f"identical copies are not a conflict: {message}")
    assert "value-one" not in message and "value-two" not in message, (
        f"the stop message exposed a value: {message}")
