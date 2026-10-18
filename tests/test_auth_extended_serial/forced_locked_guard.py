"""Maestro #8593 — the locked-row check on POST /api/auth/password/forced.

The endpoint checks twice that the account is active and still requires a
password change: once on the plain first read, and again on the row it locks
before consuming the tp credential (the same shape #8591 tests for invite and
reset links). Both checks raise the same 403, so a refusal alone cannot say
which one fired: the proof is that the server was seen waiting at the locked
read. A holder thread changes the account in an open transaction, so the plain
read still sees the committed eligible row and the `SELECT ... FOR UPDATE`
waits on the holder. Once that wait is seen the holder commits, and only the
locked check can refuse.

Serial on purpose: it holds a user row lock across two connections and keeps a
server worker waiting on it.
"""
import threading
import time
import uuid

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


LOCK_WAIT_TIMEOUT = 10.0
TEST_USER = "forced_locked_guard_user"
TEMP_PASSWORD = "Forced-locked-guard-temp-71!"
NEW_PASSWORD = "Unrelated-Strong-Passphrase-2026"


def _backend_pid(connection):
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return cursor.fetchone()[0]


def _locked_read_waiter(connection, holder_pid, table):
    """The pid of a backend in this database waiting on `holder_pid` in a
    `FOR UPDATE` read of `table`, or None.

    The holder polls inside its open transaction, where Postgres keeps one
    pg_stat_activity snapshot for the whole transaction, so drop it first."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_stat_clear_snapshot()")
        cursor.execute(
            "SELECT pid FROM pg_stat_activity"
            " WHERE datname = current_database()"
            " AND %s = ANY(pg_blocking_pids(pid))"
            " AND query ILIKE %s AND query ILIKE %s",
            [holder_pid, "%FOR UPDATE%", f"%{table}%"])
        row = cursor.fetchone()
        return row[0] if row else None


def _change_while_server_waits(uid, changes, result, locked):
    """Apply `changes` to the user in an open transaction, wait until the
    server's locked read is blocked on it, then commit. Never waits on the
    server itself."""
    from django.db import connection, transaction
    from mojo.apps.account.models import User

    try:
        with transaction.atomic():
            holder_pid = _backend_pid(connection)
            User.objects.filter(pk=uid).update(**changes)
            locked.set()
            table = User._meta.db_table
            result["waiter"] = None
            deadline = time.monotonic() + LOCK_WAIT_TIMEOUT
            while time.monotonic() < deadline:
                waiter = _locked_read_waiter(connection, holder_pid, table)
                if waiter:
                    result["waiter"] = waiter
                    break
                time.sleep(0.05)
    except Exception as exc:
        result["error"] = exc
    finally:
        locked.set()
        connection.close()


def _client(opts):
    """A client with its own source address, so the endpoint's strict rate
    limit counts this module's requests only."""
    from testit.client import RestClient
    octets = uuid.uuid4().int
    client = RestClient(opts.client.host)
    client.headers["X-Real-IP"] = "10.%d.%d.%d" % (
        (octets >> 16) & 0xFF, (octets >> 8) & 0xFF, octets & 0xFF)
    return client


@th.django_unit_setup()
def setup_forced_locked_guard(opts):
    from mojo.apps.account.models import User

    User.objects.filter(username=TEST_USER).delete()
    user = User.objects.create_user(
        username=TEST_USER, email=f"{TEST_USER}@example.com", password=TEMP_PASSWORD)
    User.objects.filter(pk=user.pk).update(is_active=True, requires_password_change=True)
    opts.user_id = user.pk
    opts.forced_client = _client(opts)


def _assert_locked_refusal(opts, changes, restore, label):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    User.objects.filter(pk=opts.user_id).update(is_active=True, requires_password_change=True)
    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_forced_password_token(user)
    user.refresh_from_db()
    password_before = user.password
    auth_key_before = user.auth_key

    result = {}
    locked = threading.Event()
    holder = threading.Thread(
        target=_change_while_server_waits, args=(user.pk, changes, result, locked),
        daemon=True)
    holder.start()
    try:
        assert_true(locked.wait(LOCK_WAIT_TIMEOUT), "the holder never took the row lock")
        resp = opts.forced_client.post("/api/auth/password/forced", {
            "token": tok,
            "new_password": NEW_PASSWORD,
        })
        holder.join(LOCK_WAIT_TIMEOUT * 2)
        assert_true(not holder.is_alive(), "the holder thread did not finish")
        assert_true("error" not in result, f"the holder failed: {result.get('error')!r}")
        assert_true(result.get("waiter"),
                    "the request never reached the locked read; user.py:1476 not exercised")
        assert_eq(resp.status_code, 403,
                  f"an account {label} at the locked read must refuse the tp credential "
                  f"with 403, got {resp.status_code}: {resp.response.data}")
        assert_eq(resp.response.error, "Forced password change is unavailable",
                  f"expected 'Forced password change is unavailable', "
                  f"got '{resp.response.error}'")
        user.refresh_from_db()
        assert_eq(user.password, password_before,
                  f"no password may be set once the account is {label}")
        assert_eq(user.auth_key, auth_key_before,
                  f"the auth key must not rotate once the account is {label}")
    finally:
        holder.join(LOCK_WAIT_TIMEOUT * 2)
        User.objects.filter(pk=opts.user_id).update(**restore)

    # Eligible again, the same credential still verifies: the refusal did not use it up.
    tokens.verify_forced_password_token(tok, consume=False)
    again = opts.forced_client.post("/api/auth/password/forced", {
        "token": tok,
        "new_password": NEW_PASSWORD,
    })
    assert_eq(again.status_code, 200,
              f"the unused tp credential must work once the account is eligible again, "
              f"got {again.status_code}: {again.response.data}")


@th.django_unit_test("#8593: forced change is refused when the account is disabled at the locked read")
def test_forced_refused_when_disabled_at_locked_read(opts):
    _assert_locked_refusal(opts, {"is_active": False}, {"is_active": True}, "disabled")


@th.django_unit_test("#8593: forced change is refused when no longer required at the locked read")
def test_forced_refused_when_not_required_at_locked_read(opts):
    _assert_locked_refusal(opts, {"requires_password_change": False},
                           {"requires_password_change": True}, "no longer required to change")
