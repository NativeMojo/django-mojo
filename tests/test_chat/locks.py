"""Real PostgreSQL row-mode/ordering regressions; no user data is deleted."""
from testit import helpers as th
from .sequences import _fixture


def _writer_waits_before_room(mode):
    from concurrent.futures import ThreadPoolExecutor
    from queue import Queue
    from time import monotonic, sleep
    from django.db import close_old_connections, connection, transaction, DatabaseError
    from mojo.apps.account.models import User
    from mojo.apps.chat.models import ChatRoom, ChatMembership, ChatMessage

    room, user, member = _fixture(f"user-fence-{mode}")
    if mode in ("membership", "bulk"):
        member.delete()
    pids = Queue()

    def writer():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                pids.put(cursor.fetchone()[0])
            if mode == "membership":
                return ChatMembership.objects.create(room=room, user=user, read_backend="cursor").pk
            if mode == "bulk":
                return ChatMembership.objects.bulk_create([
                    ChatMembership(room=room, user=user)], read_backend="cursor")[0].pk
            return ChatMessage.objects.create(
                room=room, user=user if mode == "message" else None,
                flagged_by=user if mode == "flagger" else None, body="fenced create",
                read_backend="cursor", read_handler=None, read_publisher=False).pk
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            # Account closure/key-changing operations take this strict mode.
            # Holding only the User lets us observe whether the writer acquired
            # a Room before waiting for its author/member/flagger FK fence.
            User.objects.select_for_update().get(pk=user.pk)
            pending = pool.submit(writer)
            pid = pids.get(timeout=5)
            deadline = monotonic() + 5
            blockers = []
            while monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_blocking_pids(%s)", [pid])
                    blockers = cursor.fetchone()[0]
                if blockers:
                    break
                if pending.done():
                    pending.result()
                    break
                sleep(0.01)
            assert blockers, f"{mode} create must wait for the strict User lock before acquiring its Room"
            try:
                ChatRoom.objects.select_for_update(nowait=True).get(pk=room.pk)
            except DatabaseError as error:
                raise AssertionError(f"{mode} create waiting for User must not already hold the Room lock") from error
        assert pending.result(timeout=5), f"{mode} create must complete after the outer User transaction commits"


@th.django_unit_test()
def test_message_author_fence_precedes_room_lock(opts):
    _writer_waits_before_room("message")


@th.django_unit_test()
def test_message_flagger_fence_precedes_room_lock(opts):
    _writer_waits_before_room("flagger")


@th.django_unit_test()
def test_membership_user_fence_precedes_room_lock(opts):
    _writer_waits_before_room("membership")


@th.django_unit_test()
def test_bulk_membership_users_fenced_before_rooms(opts):
    _writer_waits_before_room("bulk")


@th.django_unit_test()
def test_user_key_share_mode_and_outer_transaction_lifetime(opts):
    from concurrent.futures import ThreadPoolExecutor
    from django.db import close_old_connections, connection, transaction, DatabaseError
    from mojo.apps.account.models import User
    from mojo.apps.chat.services.locks import lock_users
    room, user, member = _fixture("user-fence-modes")

    def key_share():
        close_old_connections()
        try:
            with transaction.atomic():
                return lock_users([user.pk])
        finally:
            connection.close()

    def exclusive_nowait():
        close_old_connections()
        try:
            try:
                with transaction.atomic():
                    User.objects.select_for_update(nowait=True).get(pk=user.pk)
                return False
            except DatabaseError as error:
                cause = error.__cause__
                if getattr(cause, "sqlstate", getattr(cause, "pgcode", None)) != "55P03":
                    raise
                return True
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            User.objects.select_for_update(no_key=True).get(pk=user.pk)
            assert pool.submit(key_share).result(timeout=5) == (user.pk,), "Stable key-share fences must remain compatible with ordinary User NO KEY UPDATE ownership locks"
        with transaction.atomic():
            held = lock_users([None, user.pk, user.pk, 9223372036854775807])
            assert held == (user.pk,), "User fences must return existing sorted unique IDs without adding active-user policy"
            assert pool.submit(exclusive_nowait).result(timeout=5), "Returned User key-share locks must remain held and block the strict mode used for deletes/key changes until outer commit"
        assert not pool.submit(exclusive_nowait).result(timeout=5), "User key-share fences must release at the outer transaction commit"


@th.django_unit_test()
def test_user_fences_require_primary_atomic_scope(opts):
    from django.db import transaction
    from django.db.transaction import TransactionManagementError
    from mojo.apps.chat.services.locks import lock_users
    try:
        lock_users([])
        assert False, "Even an empty user fence call must require an explicit owning transaction"
    except TransactionManagementError:
        pass
    with transaction.atomic():
        try:
            lock_users([], using="reader")
            assert False, "Stable User fences must not run on a replica alias"
        except ValueError:
            pass
