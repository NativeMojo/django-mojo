"""Maestro #8591 — the locked-row disabled-account check on set-password-from-link.

POST /api/auth/password/reset/token checks `is_active` twice (#1955): once on
the plain first read, and again on the row it locks before consuming the
token. The invite_flow tests disable the account before the request, so they
only reach the first check. Here a holder thread disables the account in an
open transaction: the server's plain read still sees the committed active row,
then its `SELECT ... FOR UPDATE` waits on the holder. Once the holder is seen
blocking that read it commits, and only the locked check can refuse.

Serial on purpose: it holds a user row lock across two connections and keeps a
server worker waiting on it.
"""
import threading
import time

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


LOCK_WAIT_TIMEOUT = 10.0
TEST_USER = "locked_guard_user"
STRONG_PASSWORD = "Unrelated-Strong-Passphrase-2026"


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


def _disable_while_server_waits(uid, result, locked):
    """Disable the user in an open transaction, wait until the server's locked
    read is blocked on it, then commit. Never waits on the server itself."""
    from django.db import connection, transaction
    from mojo.apps.account.models import User

    try:
        with transaction.atomic():
            holder_pid = _backend_pid(connection)
            User.objects.filter(pk=uid).update(is_active=False)
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


@th.django_unit_setup()
def setup_disabled_locked_guard(opts):
    from mojo.apps.account.models import User

    User.objects.filter(username=TEST_USER).delete()
    user = User(username=TEST_USER, email=f"{TEST_USER}@example.com")
    user.set_unusable_password()
    user.last_login = None
    user.is_email_verified = False
    user.is_active = True
    user.save()
    opts.user_id = user.pk


def _assert_locked_refusal(opts, generate, verify, label):
    from mojo.apps.account.models import User

    User.objects.filter(pk=opts.user_id).update(
        is_active=True, last_login=None, is_email_verified=False)
    user = User.objects.get(pk=opts.user_id)
    tok = generate(user)
    user.refresh_from_db()
    password_before = user.password
    invite_secret_before = user.get_secret("invite_token")

    result = {}
    locked = threading.Event()
    holder = threading.Thread(
        target=_disable_while_server_waits, args=(user.pk, result, locked), daemon=True)
    holder.start()
    try:
        assert_true(locked.wait(LOCK_WAIT_TIMEOUT), "the holder never took the row lock")
        resp = opts.client.post("/api/auth/password/reset/token", {
            "token": tok,
            "new_password": STRONG_PASSWORD,
        })
        holder.join(LOCK_WAIT_TIMEOUT * 2)
        assert_true(not holder.is_alive(), "the holder thread did not finish")
        assert_true("error" not in result, f"the holder failed: {result.get('error')!r}")
        assert_true(result.get("waiter"),
                    "the request never reached the locked read; user.py:1431 not exercised")
        assert_eq(resp.status_code, 403,
                  f"an account disabled at the locked read must refuse its {label} token "
                  f"with 403, got {resp.status_code}: {resp.response.data}")
        assert_eq(resp.response.error, "Account is disabled",
                  f"expected 'Account is disabled', got '{resp.response.error}'")
        user.refresh_from_db()
        assert_eq(user.password, password_before,
                  f"no password may be set from a {label} token once the account is disabled")
        assert_true(not user.is_email_verified,
                    f"a refused {label} token must not mark the email verified")
    finally:
        holder.join(LOCK_WAIT_TIMEOUT * 2)
        User.objects.filter(pk=opts.user_id).update(is_active=True)

    # Re-activated, the same link still verifies: the refusal did not use it up.
    verify(tok, consume=False)
    if label == "iv:":
        user.refresh_from_db()
        assert_eq(user.get_secret("invite_token"), invite_secret_before,
                  "the cached invite token must survive a refused redemption")
    again = opts.client.post("/api/auth/password/reset/token", {
        "token": tok,
        "new_password": STRONG_PASSWORD,
    })
    assert_eq(again.status_code, 200,
              f"the unused {label} token must work once the account is active again, "
              f"got {again.status_code}: {again.response.data}")


@th.django_unit_test("#8591: an iv: token is refused when the account is disabled at the locked read")
def test_invite_token_refused_at_locked_read(opts):
    from mojo.apps.account.utils import tokens

    _assert_locked_refusal(opts, tokens.generate_invite_token,
                           tokens.verify_invite_token, "iv:")


@th.django_unit_test("#8591: a pr: token is refused when the account is disabled at the locked read")
def test_reset_token_refused_at_locked_read(opts):
    from mojo.apps.account.utils import tokens

    _assert_locked_refusal(opts, tokens.generate_password_reset_token,
                           tokens.verify_password_reset_token, "pr:")
