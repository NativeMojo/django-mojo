"""
migrate_locked and sanity_check (maestro item #1458, D5/D8).

The lock tests open a SECOND raw database session — an advisory lock is
session-scoped, so proving mutual exclusion requires two sessions, not two
cursors. Everything runs in this process (management commands, not
`opts.client`), so `mock.patch` reaches all of it. The mutual-exclusion tests
are Postgres-only by design: the command's non-Postgres branch deliberately
migrates without locking, and its own test stubs the vendor.
"""
from unittest import mock

from testit import helpers as th


@th.django_unit_setup()
def setup_lock(opts):
    from django.db import connection

    opts.pg = connection.vendor == "postgresql"


def _raw_connection():
    """A second, independent database session using the same driver/params."""
    from django.db import connection

    params = dict(connection.get_connection_params())
    # psycopg2's cursor_factory kwarg is Django plumbing, not a connect param
    # every driver accepts; the raw session does not need it.
    params.pop("cursor_factory", None)
    params.pop("context", None)
    return connection.Database.connect(**params)


def _try_lock(raw, namespace, lock_id):
    cursor = raw.cursor()
    cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", (namespace, lock_id))
    acquired = bool(cursor.fetchone()[0])
    cursor.close()
    return acquired


def _unlock(raw, namespace, lock_id):
    cursor = raw.cursor()
    cursor.execute("SELECT pg_advisory_unlock(%s, %s)", (namespace, lock_id))
    cursor.close()


@th.django_unit_test("migrate_locked exits non-zero while another session holds the lock")
def test_lock_mutual_exclusion(opts):
    if not opts.pg:
        return
    from django.core.management import call_command
    from django.core.management.base import CommandError
    from mojo.apps.edge.management.commands.migrate_locked import LOCK_ID, LOCK_NAMESPACE

    raw = _raw_connection()
    try:
        th.assert_true(
            _try_lock(raw, LOCK_NAMESPACE, LOCK_ID),
            "test setup: the second session could not take the advisory lock")
        with th.assert_raises(CommandError):
            call_command("migrate_locked", noinput=True)
    finally:
        raw.close()


@th.django_unit_test("migrate_locked succeeds and releases the lock afterwards")
def test_lock_released_after_success(opts):
    if not opts.pg:
        return
    from django.core.management import call_command
    from mojo.apps.edge.management.commands import migrate_locked as cmd_module
    from mojo.apps.edge.management.commands.migrate_locked import LOCK_ID, LOCK_NAMESPACE

    # The inner migrate is stubbed: the test project builds its schema with
    # `migrate --run-syncdb` and records nothing in django_migrations, so a
    # REAL migrate here would replay 0001 into existing tables. The property
    # under test is the lock lifecycle, not Django's migrate.
    calls = []
    with mock.patch.object(
            cmd_module, "call_command",
            side_effect=lambda *a, **kw: calls.append((a, kw))):
        call_command("migrate_locked", noinput=True)

    th.assert_eq(calls, [(("migrate",), {"interactive": False})],
                 f"--noinput must pass through to migrate, got {calls!r}")

    raw = _raw_connection()
    try:
        acquired = _try_lock(raw, LOCK_NAMESPACE, LOCK_ID)
        th.assert_true(
            acquired,
            "the lock must be free after a successful migrate_locked run")
        _unlock(raw, LOCK_NAMESPACE, LOCK_ID)
    finally:
        raw.close()


@th.django_unit_test("the lock is released after a FAILED migrate")
def test_lock_released_after_failure(opts):
    if not opts.pg:
        return
    from django.core.management import call_command
    from mojo.apps.edge.management.commands import migrate_locked as cmd_module
    from mojo.apps.edge.management.commands.migrate_locked import LOCK_ID, LOCK_NAMESPACE

    with mock.patch.object(
            cmd_module, "call_command",
            side_effect=RuntimeError("simulated migration failure")):
        with th.assert_raises(RuntimeError):
            call_command("migrate_locked", noinput=True)

    raw = _raw_connection()
    try:
        acquired = _try_lock(raw, LOCK_NAMESPACE, LOCK_ID)
        th.assert_true(
            acquired,
            "a failed migrate must still release the advisory lock — a wedged "
            "lock blocks every later deploy")
        _unlock(raw, LOCK_NAMESPACE, LOCK_ID)
    finally:
        raw.close()


@th.django_unit_test("non-postgres vendors migrate without attempting to lock")
def test_vendor_bypass(opts):
    from django.core.management import call_command
    from mojo.apps.edge.management.commands import migrate_locked as cmd_module

    class NoCursorConnection:
        vendor = "sqlite3"

        def cursor(self):
            raise AssertionError(
                "the non-postgres branch must not touch a cursor — there is "
                "no advisory lock to take")

    calls = []
    with mock.patch.object(cmd_module, "connection", NoCursorConnection()):
        with mock.patch.object(
                cmd_module, "call_command",
                side_effect=lambda *a, **kw: calls.append((a, kw))):
            call_command("migrate_locked", noinput=True)

    th.assert_eq(len(calls), 1, f"expected exactly one migrate call, got {calls!r}")
    th.assert_eq(calls[0][0], ("migrate",),
                 f"the bypass must still run migrate, ran {calls[0]!r}")


def _clean_executor():
    """A MigrationExecutor stub reporting a clean graph.

    Needed because the test project builds its schema with
    `migrate --run-syncdb` and records NOTHING in django_migrations — the
    real executor here reports the entire migration set unapplied, which is a
    property of this test environment, not of the node being checked.
    """
    executor = mock.Mock()
    executor.migration_plan.return_value = []
    executor.loader.graph.leaf_nodes.return_value = []
    return executor


@th.django_unit_test("sanity_check passes against the live test environment")
def test_sanity_check_passes(opts):
    from django.core.management import call_command
    from mojo.apps.edge.management.commands import sanity_check as cmd_module

    url = f"{opts.client.host}api/version"
    with mock.patch.object(cmd_module, "MigrationExecutor",
                           return_value=_clean_executor()):
        call_command("sanity_check", url=url, retries=3, delay=0.2, timeout=5.0)


@th.django_unit_test("sanity_check fails when migrations are unapplied")
def test_sanity_check_fails_on_migrations(opts):
    from django.core.management import call_command
    from django.core.management.base import CommandError
    from mojo.apps.edge.management.commands import sanity_check as cmd_module

    executor = mock.Mock()
    executor.migration_plan.return_value = [("account", "0001_initial")]
    executor.loader.graph.leaf_nodes.return_value = []
    try:
        with mock.patch.object(cmd_module, "MigrationExecutor",
                               return_value=executor):
            call_command("sanity_check", url=f"{opts.client.host}api/version",
                         retries=1, delay=0.0, timeout=5.0)
        raise AssertionError("sanity_check must fail on an unapplied migration")
    except CommandError as err:
        th.assert_in("migrations", str(err),
                     f"the failure must name the migrations check, got: {err}")


@th.django_unit_test("sanity_check fails on the local request even when checks 1-4 pass")
def test_sanity_check_fails_on_request(opts):
    from django.core.management import call_command
    from django.core.management.base import CommandError
    from mojo.apps.edge.management.commands import sanity_check as cmd_module

    # Port 9 (discard) on loopback: connection refused immediately. Checks
    # 1-4 (apps, db, migrations, redis) all pass — the request check alone
    # must fail, naming itself.
    try:
        with mock.patch.object(cmd_module, "MigrationExecutor",
                               return_value=_clean_executor()):
            call_command(
                "sanity_check", url="http://127.0.0.1:9/api/version",
                retries=1, delay=0.0, timeout=1.0)
        raise AssertionError("sanity_check must fail when the local request fails")
    except CommandError as err:
        th.assert_in(
            "local request", str(err),
            f"the failure must name the request check, got: {err}")
